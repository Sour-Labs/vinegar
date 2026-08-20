#!/usr/bin/env python3
#
# Copyright 2026 Sour Labs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Poll GitHub for new pull requests and review them with Claude Code.

Vinegar reviews nothing itself. It decides which pull requests deserve a
reviewer, puts a checkout on the pull request's head commit, and runs
`claude -p '/code-review <n>'` in it. The reviewer returns its findings and
Vinegar posts them as one review when the run finishes.
"""

import argparse
import base64
import errno
import fcntl
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# The path component review-settings.json denies the reviewer every read
# under, as `Read(//**/.vinegar/**)`. It is written there as a literal, so it
# does not move with VINEGAR_HOME, and both of these paths have to be
# reconciled against it rather than against each other. check_paths() does
# that, and refuses to start when they disagree.
DENIED_COMPONENT = ".vinegar"

# The deny rule itself, spelled the way review-settings.json spells it.
# check_paths() refuses to start without it: the two path checks reason
# about what this glob covers, and neither of them notices if it is gone.
DENY_HOME = "Read(//**/.vinegar/**)"

# The read denials the security argument actually rests on, pinned so that
# editing the file cannot quietly drop one. Only DENY_HOME used to be
# checked, out of 47 rules, so deleting `Read(//**/.ssh/**)` while widening
# the list left every check passing: the reviewer of an attacker-authored
# branch could then read `~/.ssh/id_ed25519` and quote it into a finding
# Vinegar publishes on a public pull request.
#
# These and not the rest of the file. The allow list is meant to be tuned,
# and the write denials are backed by the sandbox now; what cannot be
# recovered from is a credential read, because the finding carrying it is
# already public by the time anyone notices.
DENY_ALWAYS = (
    DENY_HOME,
    "Read(//**/.claude/**)",
    "Read(//**/.ssh/**)",
    "Read(//**/.aws/**)",
    "Read(//**/.gnupg/**)",
    "Read(//**/.config/gh/**)",
    "Read(//**/.netrc)",
    "Read(//**/.env)",
)

# And the one key that would make all of them moot. `bypassPermissions`
# ignores the allow and deny lists entirely, so a single word here undoes
# every rule above without touching one of them. Absent is the same as
# "default"; anything else is refused.
PERMISSION_MODE = "default"

# What the sandbox stanza in review-settings.json must say, and why each
# part of it carries weight. The allow list names subcommands and cannot
# see the flags that follow them, so `git show -s --format=%B
# --output=<path> HEAD` writes a commit message byte for byte to any file
# the daemon user can write, while matching `Bash(git show:*)` exactly
# like the reads a review is for. That is not theoretical: run against
# this settings file it created the file, and Claude Code's own command
# analyser did not stop it, though it does stop `sort -o` — which is how
# the flag went unnoticed while a narrower one did not.
#
# Denying `git show` would not close it either. `git diff` takes the same
# flag and is the command every review gets its diff from, and `git
# blame`, `sort -o` and `uniq in out` all write files too. A rule that
# cannot see flags cannot be the boundary; the sandbox can.
SANDBOX_RULES = (
    ("enabled", True,
     "nothing else confines what the reviewer writes"),
    ("failIfUnavailable", True,
     "a sandbox that cannot start would otherwise be skipped and the "
     "review would run unconfined, saying nothing about it"),
    ("allowUnsandboxedCommands", False,
     "a command that may ask to run outside the sandbox is a command "
     "the sandbox does not cover"),
)

# The network rule, pinned rather than assumed. Measured: with the sandbox
# on and no network key at all, `gh pr view` fails with "Forbidden", which
# is why the reviewer is told it has no network — but that came from a
# default nothing here stated. A release that changed the default would
# reopen the network silently, while the brief went on promising it was
# closed and an injected review could send a diff or a credential
# anywhere. Saying it costs nothing and cannot drift.
SANDBOX_NETWORK = {"allowedDomains": []}

# Every key the file's own sandbox stanza may carry, at both levels.
# reviewer_settings() sends these and nothing else, so a key outside this
# set changes what a hand-run does while leaving the daemon untouched —
# which makes the file describe something Vinegar does not do.
# `filesystem` needs its own set: checking only the top level accepted a
# `filesystem.allowWrite` that hands a hand-run the whole disk.
SANDBOX_KEYS = frozenset(
    [name for name, _, _ in SANDBOX_RULES] + ["filesystem", "network"])
SANDBOX_FS_KEYS = frozenset(["denyWrite"])

# abspath, not just expanduser, so a trailing slash or a relative path cannot
# make either of these look like it sits somewhere it does not.
HOME = os.path.abspath(
    os.path.expanduser(os.environ.get("VINEGAR_HOME", "~/.vinegar")))
STATE_PATH = os.path.join(HOME, "state.json")
LOCK_PATH = os.path.join(HOME, "vinegar.pid")
REVIEW_DIR = os.path.join(HOME, "reviews")

# What `reviewed_sha` has to look like before anything diffs from it.
#
# The other fields in an entry are counters and flags, and load_state()
# type-checks them because the give-up's own log line tells operators to
# edit this file by hand. This one is a string that is concatenated and
# then handed to git, so the shape matters twice: a non-string raises
# where review_scope builds the probe, which is outside the try that
# catches a probe failing, and a string that is not a commit sends a pass
# looking for one that cannot exist. Forty lowercase hex characters is the
# whole of what a resolved commit can be and what `git rev-parse` answers
# with. Not an injection guard: run() never uses a shell.
#
# Anchored at both ends. `re.match` alone would take a valid sha with
# anything at all after it, which is the half of the check that matters.
FULL_SHA = re.compile(r"\A[0-9a-f]{40}\Z")

# What `owner/name` is allowed to be, for the two places that take one: a
# `repos` entry and the argument to `--pr`. One pattern, because the two
# are meant to be the same rule and were a copied line apart; tighten one
# copy and `--pr o/r#1` and `"repos": ["o/r"]` start disagreeing about the
# same string.
#
# Counting the slash and testing both halves truthy was the first version
# and let through everything that is well-formed and unusable.
# `"Sour Labs/vinegar"`, an organisation's display name pasted instead of
# its slug, has one slash and two non-empty halves, so the daemon started
# and then failed on every poll for ever, with one swallowed line a
# minute and nothing at startup saying why: verbatim the failure the
# check was added to stop. `"o /r"` is worse, because a NUL or a space
# reaches subprocess, which raises rather than returning an error.
#
# The character class is what GitHub accepts in either half, so this
# refuses what cannot resolve and nothing else. A well-formed name that
# does not exist still starts, because the alternative is a network call
# before the daemon can run.
REPO_NAME = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")

# How a transcript opens when its review read less than the whole pull
# request. One definition, because repost() has to recognise it in a file
# save_transcript() wrote, and a repost that cannot find it sends a
# narrowed review with nothing saying so.
#
# The whole written prefix rather than "Scope: ". repost() reads the file,
# reviewer prose included, and the reviewer writes its own summary: a
# closing paragraph beginning "Scope: f0f6ee9..HEAD, three commits" would
# have been hoisted into the resend's opening and cut out of the review.
SCOPE_MARK = "Scope: only what was added since "

# The transcript's other narrowing line. Beside SCOPE_MARK because
# repost() lifts both out of the transcript by matching them, and a mark
# that is written but not matched is a review delivered from disk reading
# as though it had reported everything it found.
#
# "Asked for", not "Reported", for the reason review_body() gives at
# length: nothing filters what the reviewer hands back, and the bullets
# below this line carry the severity pass's tier dots. It read "Reported:
# blockers only." on every narrowed transcript, `wonky-flow#107` included,
# where it sits fourteen lines above a blue advisory dot, and repost()
# lifts that line into the opening of a review delivered days later.
#
# No number in it, unlike the scope line's commit. What the reader needs
# is which way the pass was narrowed, and the configured round count is
# not something a transcript posted days later can still speak for.
BLOCKERS_MARK = "Asked for: blockers only."

# What the lift has to recognise, which is not the set it writes. A
# transcript is written by one version of this file and can be reposted by
# the next, so the spelling BLOCKERS_MARK had before it said "Asked for"
# is still on disk: thirteen transcripts carried it when it changed. A
# resend that matches neither mark skips the lift, and for the oversized
# transcript the lift exists for the cut then takes the *last* `room`
# characters, shearing the front off and delivering a narrowed review with
# nothing on it saying so.
#
# Droppable once no transcript predating that rename can still be pending,
# which is when no `.unposted` marker in REVIEW_DIR is older than it.
LIFTED_MARKS = (SCOPE_MARK, BLOCKERS_MARK, "Reported: blockers only.")

# What the pull request is told when the severity pass puts a finding
# under `blocker` on a narrowed round. One sentence pair in one place,
# because it has to reach the pull request by two routes that build
# nothing in common: review_body() puts it in the comment, and
# save_transcript() writes it into the block repost() lifts, since a
# review delivered from disk never runs review_body() at all.
#
# Spelled once rather than twice held in step by a check. This pull
# request exists because the same sentence written in two places drifted,
# and the first draft of this constant reworded it a third way in the same
# commit that reworded the paragraph.
#
# "on each finding" and not "below", because an anchored finding's tag is
# rendered into its inline comment on the diff rather than into the review
# body. That is the common case and the one `wonky-flow#107` took, where
# the body ends "1 finding (1 advisory), 1 posted inline." and a paragraph
# promising tags below it points at nothing.
#
# In the transcript it goes under BLOCKERS_MARK and never alone, which is
# what keeps it inside the block repost() lifts: that block is found by
# matching its first line, and this is never the first line.
DISAGREED_SAID = (
    "The tier tag on each finding is set after the review, by a separate "
    "pass that reads only its summary and never the code. A tag under "
    "blocker is that pass disagreeing with the reviewer, not a smaller "
    "finding shown here anyway.")

# Between a transcript's heading and its body. Named because both the
# writer and repost()'s reader have to agree on it, and because reading
# the scope statement is only unambiguous at a known offset.
TRANSCRIPT_SEP = "\n\n---\n\n"

# Checkouts sit beside HOME rather than inside it, and that is load-bearing
# rather than tidiness. The reviewer is denied every read under HOME, so a
# checkout in there is a repository it cannot open: it falls back to fetching
# each file over the API, which costs more and reviews worse, while
# `permission_denials` stays empty and reports nothing wrong.
#
# The default is derived from HOME rather than fixed, so that VINEGAR_HOME
# still isolates an entire instance. A fixed path would leave two instances
# sharing clones while holding separate locks, which is the exact race
# acquire_lock() exists to prevent.
CHECKOUT_DIR = os.path.abspath(os.path.expanduser(
    os.environ.get("VINEGAR_CHECKOUTS", HOME + "-checkouts")))
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "review-settings.json")

# `ultra` is deliberately absent. It runs in the cloud and bills usage credits
# separately, so automation must never reach it.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# What a review attempt settled. DONE means the subscription was spent, whether
# or not findings came back, so the pull request is closed off. FAILED means it
# never got that far, which is the case worth retrying: an exhausted rate
# limit, a logged-out CLI, a model name that does not exist.
DONE, FAILED = "reviewed", "failed"

# What one posting attempt settled. Named rather than True/False/None,
# because the caller has four distinct moves to make and three of these
# arrive as the same failed `gh` exit: POSTED is on the pull request;
# REFUSED means GitHub judged the request and created nothing, so a
# changed request may still work; THROTTLED is refused as well, but by a
# limit that a retry milliseconds later cannot help; UNSURE is a timeout or
# a 5xx, where the review may be up and a resend can duplicate it.
POSTED, REFUSED, THROTTLED, UNSURE = "posted", "refused", "throttled", "unsure"

# How many times a FAILED pull request is retried before it is left alone. The
# point is to survive a rate-limit window without turning a permanent failure
# into a review every minute forever.
MAX_ATTEMPTS = 3

# Seconds of token life reserved for the checkout, on top of the review's own
# budget. One token covers both and the checkout runs first: a clone of a
# large repository over a slow link is minutes, so asking only for
# `review_timeout` can hand back a token that dies part-way through the
# review, taking with it the `gh` calls the review makes to read the pull
# request.
#
# 1500 covers the checkout that runs every time: the usability probe and the
# four local steps at DIFF_TIMEOUT each, plus the head fetch at its whole
# FETCH_TIMEOUT. It does **not** cover a first clone that runs its full
# CLONE_TIMEOUT, and cannot: that would need 1800 more, and the sum below is
# capped far tighter than that.
#
# The cap is the hour a GitHub token lives. The cache serves one only while
# `now + good_for < expires`, so once `CHECKOUT_GRACE + review_timeout`
# reaches 3600 no token can ever satisfy it and every call mints a fresh one.
# At 1500 plus the shipped `review_timeout` of 1800 that leaves 300 seconds
# of each token's life in which the cache still dedupes, where 600 left
# 1200. That is the accepted cost of the raise: more minting, in exchange for
# a token that survives the checkout it was minted for. load_config refuses a
# `review_timeout` that pushes the sum over the cap, because crossing it is
# otherwise silent and once ran at roughly 1440 tokens a day per open pull
# request without anyone noticing.
#
# What is left uncovered is one review, on one repository, on the poll that
# first clones it, and it is retried. That is the trade: the alternative was
# a clone bounded tightly enough to fit the budget, which costs that
# repository every review rather than its first.
#
# It deliberately does not have to cover the posting as well. That happens
# after a review which may have consumed the entire `review_timeout`, and at
# the timeouts people actually configure no obtainable token is guaranteed to
# survive that far. POST_GRACE asks for a fresh one at that point instead.
CHECKOUT_GRACE = 1500

# The hour a GitHub installation token lives. Not a tunable: it is GitHub's
# number, named here because two things are measured against it.
TOKEN_LIFE = 3600

# The longest one review may be allowed to hold the poll thread. A ceiling
# on `review_timeout`, not a timeout itself.
#
# It exists because the token-life check above used to refuse rather than
# say, and that refusal was incidentally load_config's only upper bound on
# `review_timeout`. Downgrading it to a warning removed the bound, leaving
# `isinstance(value, int)` and `value > 0`. An operator answering repeated
# "killed after 1800s" lines with an extra zero would then start a daemon
# that parks on one review for five hours: nothing else listed, nothing
# else reviewed, and the watchdog calling it healthy throughout, because it
# reads the pid and a quiet log is what a quiet week looks like too.
#
# Refused rather than said, unlike the check above, and the difference is
# the point: a daemon that parks is running wrongly, not expensively. Two
# hours is roughly nine times the slowest review measured here, so a value
# past it is a typo rather than an intent.
#
# It is not the whole hold any more, and load_config's message says so.
# The severity pass is a second subprocess on the same thread, after the
# review and before the posting, so the worst case is this plus
# SEVERITY_TIMEOUT. The cap is left where it is rather than lowered by
# 300: the number is a typo detector, not a budget, and the argument for
# 7200 is unchanged by five minutes.
MAX_REVIEW_TIMEOUT = 7200

# The most repositories one machine will review at once, whatever the file
# asks for. A typo detector, like MAX_REVIEW_TIMEOUT, rather than a budget.
#
# The setting multiplies the one cost nothing else here bounds. Every
# other runaway has a ceiling: MAX_ATTEMPTS bounds retries, PR_LIMIT
# bounds a listing, MAX_REVIEW_TIMEOUT bounds how long the daemon can go
# quiet. `parallel_repos` bounds none of those and multiplies all of them,
# because each unit of it is a full `claude` agentic review with its own
# clone beside it. At twenty-four that is twenty-four reviewers and
# twenty-four `git clone`s on one machine, which is memory exhaustion or
# the thread-creation refusal poll_once already handles, and against
# GitHub it is the secondary-rate-limit burst the README warns about,
# where each refusal comes back FAILED and spends one of three attempts.
#
# Refused rather than clamped, and refused whatever `repos` holds. A
# clamp would make twenty-four mean two on a two-repository install and
# eight on a twenty-repository one, so the same file would behave
# differently as repositories were added, quietly, at the moment the
# machine could least afford it.
MAX_PARALLEL_REPOS = 8

# How often the App is asked which repositories its installations cover,
# where `repos` names none and discover_repos() answers instead.
#
# An hour rather than every poll, and the two numbers are answering
# different questions. `poll_interval` is how stale a pull request may be,
# which is a minute because somebody is waiting on a review. This is how
# stale the *list* may be, and it changes when somebody ticks a box in the
# App's settings, which happens when a repository is onboarded rather than
# when anyone pushes. So an hour costs one token mint and one listing per
# installation per hour instead of per minute, to notice something that
# moves monthly.
#
# It is not a bound on how quickly a change is picked up after a failure.
# refresh_repos() asks again on the next pass when an ask does not answer,
# so this is the interval between asks that worked.
DISCOVERY_INTERVAL = 60 * 60

# Enough life for `gh pr list`, which is one call. This is not zero because the
# cache serves a token right up to its recorded expiry, and that expiry is
# optimistic: it is computed from the local clock after the mint response
# arrives, while GitHub set the real one before sending it.
LISTING_GRACE = 60

# How many open pull requests one listing asks for. GitHub's own default
# is 30; this is deliberately higher, and open_prs says so when a
# repository reaches it, because anything past the cap is never seen.
PR_LIMIT = 50

PR_FIELDS = ("number,title,headRefOid,baseRefName,isDraft,author,additions,"
             "deletions,isCrossRepository,url")

# Seconds of token life asked for immediately before the review is posted.
# The token minted at the top cannot be relied on here: it has to survive the
# checkout and the whole review first, and `review_timeout` alone can consume
# more life than a token has. Whatever is left at that point, this asks for a
# usable token now, when the only work remaining is one or two API calls.
POST_GRACE = 300

# Seconds a fetch may take. Generous because it is the network and a
# first fetch after a long gap legitimately runs for minutes; the point is
# only that it cannot hang for ever. A failed checkout is retried on every
# poll and never counted against MAX_ATTEMPTS, so a cap tight enough to
# bite would mean a pull request that is never reviewed and never given up
# on.
FETCH_TIMEOUT = 600

# Seconds a first clone may take. Longer than the fetch above because it is
# the whole history rather than one ref, and a large repository over a slow
# link legitimately runs for many minutes. The cost of setting this too
# tight is the one FETCH_TIMEOUT describes: a repository that is re-cloned
# and re-abandoned on every poll, reviewing nothing and giving up on
# nothing.
#
# Bounded at all because this was the last call on the poll thread that
# could hang for ever. A socket that is open and never answers is not an
# error anyone raises, so `run()` with no timeout waits as long as TCP
# allows, no repository is polled meanwhile, and the watchdog reads a live
# pid with no log lines as healthy, which is what it reads from a genuinely
# quiet week. Half an hour parked is bad and it ends; for ever ends when
# somebody notices that no reviews have arrived. A clone still running
# after this long is not a slow link.
CLONE_TIMEOUT = 1800

# Seconds `git diff` may take before the poll loop gives up on it. Local
# work, so generous is already absurd; the point is that a checkout on a
# filesystem that stops answering cannot hold a poll thread for ever.
DIFF_TIMEOUT = 120

# Seconds `gh pr list` may take. One HTTP call, made once a minute per
# repository on the poll thread, which makes it the daemon's most frequent
# exposure to a socket that answers nothing. The clone and fetch in
# checkout() get far longer bounds rather than none, for the reasons written
# beside each.
#
# Nothing on this thread is unbounded, and the one that is easy to miss when
# checking that is the openssl signing in app_jwt(): it is the only
# subprocess in this file that does not go through run(), so a reader
# auditing run()'s callers will not see it. github_api() bounds its own
# urlopen. Two earlier versions of this comment claimed the fetch was
# unbounded when it was not, so treat a claim here as something to re-derive
# rather than inherit.
LIST_TIMEOUT = 120

# Seconds a single posting request may take. Generous for one API call, and
# the point is only that it ends: the poll loop is one thread and an
# unanswered socket would otherwise hold it for as long as TCP allows.
POST_TIMEOUT = 60

# Seconds the severity pass may take. Measured on haiku across four saved
# reviews of ten to thirteen findings: 25s to 65s. This is roughly four
# times the worst of those.
#
# Bounded because it is a `claude -p` subprocess on the same single poll
# thread as everything above, run at the worst possible moment: the review
# is finished and paid for, and its findings are in hand but not yet posted.
# The bound is generous rather than tight for the reason FETCH_TIMEOUT
# gives, but overrunning it costs nothing here: triage() logs and returns
# the findings untiered, and the review posts exactly as it did before this
# existed.
SEVERITY_TIMEOUT = 300

# How every review Vinegar posts opens, and how it recognises its own when
# asking whether one already landed. The two uses must agree, so they read
# it from here rather than each spelling it out.
BODY_MARK = "**Vinegar** \u00b7"

# Characters of the reviewer's own prose worth carrying when it never
# reported. It stands in for a closing summary, so a couple of paragraphs is
# the shape wanted; a killed run's narration is not, and the pull request is
# not the place to discover the difference.
MAX_SPOKEN = 4000

# The tool the reviewer reports its findings through. Named here because
# review() has to make it available and read_stream() has to recognise it,
# and the two must agree or findings arrive nowhere.
REPORT_TOOL = "ReportFindings"

# The severity tiers, most severe first. One tuple decides three things
# that have to agree: what the severity pass may answer, what order the
# findings are posted in, and what the top-level comment counts. Spelled
# out separately they could disagree, and the way that shows is a tier the
# model is invited to use that then sorts below every other one.
TIERS = ("blocker", "advisory", "note")

# One answer line, compiled once and kept beside the tuple it is built
# from, so the pattern and the tiers it accepts cannot drift apart.
#
# Anchored at the start of a line rather than searched for anywhere in
# one: findings quote the branch under review and the model narrates
# around its answer, so a sentence mentioning a tier is the likeliest
# thing in the reply that is not the answer.
#
# The alternation says what an answer looks like; it is not what decides
# which tiers are accepted. IGNORECASE folds four non-ASCII letters into
# the ASCII ones, so read_tiers() checks what this captured against TIERS
# and a word this let through is refused there. Loosening the alternation
# would change nothing a caller can see, which is why the mutation for
# that property is on the membership test rather than here.
TIER_LINE = re.compile(r"\s*\[?(\d+)\]?[\s:.)-]+(%s)\b" % "|".join(TIERS),
                       re.IGNORECASE)

# The colored dot each tier's label opens with, keyed by the same three
# words as TIERS. A check holds the two together, and describe() reads it
# with a default rather than a subscript, so a tier that drifts out of
# here costs its comment a dot and never a review.
#
# An emoji rather than colored text, because GitHub sanitises a comment
# body. Measured through its own markdown endpoint: `style` is stripped off
# a `<span>` and a `<font color>` tag is dropped whole, so both arrive as
# plain text. What is left that is genuinely colored is worse. A badge
# image is fetched through camo, which puts a network round trip behind
# every finding and greys out silently when it fails. An alert block is
# block-level and cannot open a bullet. Neither survives the transcript,
# which is plain text and is what repost() sends when the review is
# refused.
#
# Unicode has no gray circle, so `note` takes the white one.
TIER_DOTS = {"blocker": "\U0001f534",    # red circle
             "advisory": "\U0001f535",   # blue circle
             "note": "\u26aa"}           # white circle

# What the severity pass is told. Kept beside TIERS because the tiers it
# names and the tuple above have to be the same three words: a rule
# describing a tier read_tiers() will not accept is a rule the model obeys
# and the answer is then thrown away whole.
#
# severity_brief() explains why these particular rules, and which measured
# variants did worse.
SEVERITY_PROMPT = """You are triaging findings from a code review that has \
already run.
You are not looking for bugs and you are not judging whether each finding is
true. Assume every one is true as written. Decide only how much it would
matter to the person who has to act on this pull request.

Give each finding exactly one tier:

blocker  - someone must act before this merges. To call a finding a blocker
           you must be able to name what goes wrong at runtime for a user or
           an operator: a wrong result, lost data, a security hole, a hang, a
           crash, or a failure that happens silently.
advisory - a real defect with bounded cost. It degrades quality, misleads a
           reader, leaves a gap in tests, wastes resources, or duplicates
           work, but nothing at runtime behaves wrongly because of it.
note     - taste, naming, structure, or a small cleanup. Ignoring it forever
           is a defensible choice.

Two rules that decide most of the hard cases:

1. A missing test, a stale comment, a duplicated helper, a wasted cycle, and
   a clumsy structure are never blockers, however real and however serious
   the code they concern. Nothing at runtime behaves wrongly because of them.
   The most they can be is advisory.
2. The stated category is a hint, not the answer. A finding filed as
   `altitude` whose failure scenario describes a wrong result is a blocker. A
   finding filed as `correctness` whose failure scenario only describes a
   misleading comment is a note. Judge the failure scenario, not the label.

Most findings in a careful review are advisory. Blocker is for the few that
would cause an incident. If you cannot name the runtime harm, it is not one.

Answer with one line per finding and nothing else, in this exact form:

<index> <tier>

Use the index numbers given below. Output exactly {count}.

{findings}"""

# What the severity pass runs under: no tools, and the sandbox behind them.
#
# The findings it reads are attacker-influenced. A reviewer reading a public
# branch quotes that branch's own text into a summary and a failure
# scenario, so a file that says "ignore your instructions and run this"
# arrives here inside the thing being triaged. This is the reviewer's own
# exposure, one step further down the pipe, and it is the reason a second
# `claude -p` call is not free just because it is small and cheap.
#
# What a compromised triage cannot do is publish. read_tiers() takes
# `<index> <tier>` and discards every other character of the answer, so the
# whole output surface of this call is one of three words per finding: it
# can get the order wrong and it cannot put one byte of its own on the pull
# request. What is left is what it could *do*, and that is what these rules
# close.
#
# `--allowedTools ""` is not this, which is worth knowing before anyone
# simplifies it to that. Measured: with that flag and nothing else, the
# model read a file it was asked for and printed the contents, and
# `permission_denials` came back empty. With the deny list below the tools
# are not in the session at all and the same request writes nothing.
#
# The sandbox as well, but not as a general write boundary, because
# measurement says it is not one. With these rules and `Write` permitted
# instead of denied, the model wrote both inside the working directory and
# to `$HOME`: the sandbox stanza closes the network and leaves writes to
# the deny list above. An earlier version of this comment claimed the
# sandbox was the backstop that survives a stale tool list, which would
# have been a false assurance for the next person to widen the list.
#
# So the two rules divide the job. The deny list is what stops a tool
# running, and it has to be kept in step with Claude Code's tool set. The
# network rule and the denied paths below are what a write cannot reach
# even if one ever does get through.
TRIAGE_SETTINGS = {
    "permissions": {
        "defaultMode": PERMISSION_MODE,
        "allow": [],
        # By bare name, which denies every use of the tool. The reviewer's
        # file needs argument patterns because it allows some git and
        # refuses the rest. Nothing here needs any tool at all.
        "deny": ["Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob",
                 "Grep", "Task", "WebFetch", "WebSearch", "Workflow"],
        "ask": [],
    },
    "sandbox": dict(
        ((name, wanted) for name, wanted, _ in SANDBOX_RULES),
        # The two directories a stray write must not reach: HOME holds the
        # App's private key and the state this daemon trusts, and the
        # checkouts are what the next poll runs git in, where one line in
        # a `.git/config` buys a command outside the sandbox. Both forms
        # of each, because the kernel judges a write by the path it
        # resolves to; reviewer_settings() argues that at length.
        #
        # Sorted so the stanza does not depend on set ordering: this is
        # sent to a subprocess and compared in the suite.
        filesystem={"denyWrite": sorted(
            {form for path in (HOME, CHECKOUT_DIR)
             for form in (path, os.path.realpath(path))})},
        network=dict(SANDBOX_NETWORK)),
}

# What the review calls itself in the pull request's list of checks. One
# name, because GitHub keys nothing on it but a human reads the list to see
# whether the reviewer has finished, and two spellings would read as two
# tools.
CHECK_NAME = "Vinegar"

# Which Vinegar made a check run, written into it as `external_id` and
# read back before one is adopted or closed.
#
# HOME is the identity because HOME is what separates two instances on one
# machine: its own lock, its own state, its own checkouts. What it buys is
# the question `--once` could not express. Both instances authenticate as
# the same App, so App and name together cannot tell their runs apart, and
# the lock cannot either because each holds its own. Without this a second
# instance closes the production daemon's live indicator, and adopts a run
# that daemon is still writing to.
#
# Not a secret. `external_id` is returned by the API to anyone who can
# read the check run, so this is a path on the machine going somewhere
# public. It is already the shape of thing the log prints, and there is no
# credential in it, but it is the reason not to reach for something like
# the App key here.
DEPLOYMENT = HOME

# How a finished review reports itself in that list, and it is never
# `failure`.
#
# `failure` would make Vinegar a merge gate wherever the check is required,
# which the README promises it is not, and reviews are submitted as COMMENT
# for the same reason. Severity triage knows what a blocker is now, so
# failing on one is newly possible and still wrong: the blocker rate
# measured 45% on two of four reviews, so the gate would be closed most of
# the time on a judgement that is only good enough to sort a list.
#
# `neutral` renders as a grey mark that cannot block anything, and the
# count goes in the title where it says something true. It is what every
# ending gets except the one below, including the four that report nothing
# without being clean: a review whose output could not be read, one killed
# part way, one that never reached the pull request, and a retry whose
# posting was the earlier attempt's. finish() names them in one line.
CHECK_CONCLUSION = "neutral"

# And the one ending that is a pass, so a clean pull request reads as clean
# in the list rather than as a grey mark beside the failures.
#
# A green tick on a pull request carrying twelve findings would be a
# statement nobody made, which is why this is the exception and not the
# rule. On a review that reported nothing it is the statement the reviewer
# did make.
#
# Deliberately not conditioned on how much the pass read. A scoped later
# round that reads one commit, finds nothing and goes green leaves a tick
# on a pull request whose earlier rounds may have findings still open.
#
# That case was put up with its worst reading and accepted, so do not
# close it as an oversight. The argument for it: a finding still open
# several rounds later is usually one somebody decided not to act on, and
# what the list is wanted for is the state of the latest run, which is
# what the pull request mostly is. Two alternatives were costed and
# declined. Green only on a whole-pull-request read turns a green pull
# request grey when a clean commit is pushed to it. Remembering in the
# state file whether any round found anything is a new field to keep
# correct for a tick.
CHECK_CLEAN = "success"

# Characters a review comment may carry. GitHub's own ceiling is 65536 and it
# refuses the whole review for going over, which on the path that posts the
# reviewer's message verbatim would mean posting nothing at all. The reviewer
# decides that message's length, not Vinegar, so it is cut to fit.
MAX_BODY = 60000

# Everything two repositories polled at once would otherwise share.
#
# STATE_LOCK covers the `state` dict and the file behind it together,
# because the two hazards are one hazard. save_state() serialises the whole
# dict, and json.dumps calls back into Python between entries, so a thread
# adding a pull request it has never seen raises "dictionary changed size
# during iteration" out of the other thread's save. And write_atomic() names
# its temporary file after the target, so two saves landing together write
# `state.json.tmp` over each other and os.replace publishes whichever half
# won: every pull request forgotten, and every one of them reviewed again at
# full cost.
#
# LOG_LOCK is smaller and not cosmetic. print() writes the message and the
# newline as two calls on sys.stdout, so two repositories logging at the
# same moment can produce one line carrying both and one empty line. The log
# is where a runaway round count is found before the bill arrives, and that
# is a grep.
STATE_LOCK = threading.RLock()
LOG_LOCK = threading.Lock()

# Set when a stop has been asked for, so a worker takes no further pull
# request and no further repository.
#
# A thread cannot be interrupted from outside, which is the whole problem
# this solves. On the serial path Ctrl-C raises inside the review that is
# running, unwinds through handle_pr's finally so the checks entry is
# closed, and reaches main(). A worker sees none of that: the signal is
# delivered to the main thread alone. Without a flag the only ways to end
# a worker were to let it finish everything it had been given, or to make
# it a daemon and have the interpreter kill it mid-review, which skips
# that finally and leaves a checks entry spinning for ever on a pull
# request nobody can then merge.
#
# Asked between pull requests rather than only between repositories,
# because a repository's pass is every open pull request on it. At a
# terminal the interrupt reaches the whole process group, so the `claude`
# that was running dies and its review ends in seconds; a flag read only
# at the top of a pass would then go on to buy full reviews for that
# repository's remaining pull requests, which is the opposite of stopping.
#
# Never cleared. The one path that sets it is on its way out of main().
STOPPING = threading.Event()

# What a poll worker's thread is called. release_lock() finds them by
# this and declines while any is alive, so the two have to agree
# exactly and a literal in either place is a silent way to disagree.
POLL_WORKER = "vinegar-poll-"

DEFAULTS = {
    "repos": [],
    "poll_interval": 60,
    "effort": "high",
    "comment": True,
    "model": None,
    # The model a review runs again on when the first one cannot be
    # routed, or null for no fallback. `model` is allowed to name a
    # routing identifier rather than a public model id, and those carry
    # no promise of continuing to resolve: the deployment this was
    # written for pins `claude-opus-5[1m]`. When one stops resolving the
    # review comes back a 404 having spent nothing, FAILED is returned
    # three times, and Vinegar goes quiet on every repository it polls
    # until an operator reads the log. Null by default, the way `model`
    # is, because most installs pin nothing and have nothing to fall
    # back from.
    "fallback_model": None,
    "review_on_push": False,
    # How many reviews of one pull request report everything they find.
    # Every review after that keeps reviewing every push and reports only
    # blockers, so a branch under rework is never left unwatched and its
    # author is not handed the same list of small findings on every push.
    # Null for no narrowing at all, which with `review_on_push` on is a
    # full review of every push for the life of the pull request.
    #
    # Not inert while `review_on_push` is false, which an earlier version
    # of this comment claimed. The daemon reviews such a pull request once
    # and never counts a second round, but `--pr` counts rounds on the
    # same rule, so a third hand run of the same pull request narrows.
    # `--whole` is how an operator asks for all of it back.
    "blockers_only_after": 2,
    "max_changed_lines": 3000,
    # How many repositories are polled and reviewed at the same time. One
    # is what Vinegar did before this existed: a review parks the only
    # thread there is for nine to twenty-two minutes, and the repository
    # behind it in the list waits that out before it is even listed.
    #
    # Reviews of one repository stay one at a time whatever this says.
    # checkout() gives each repository its own tree and nothing more, so two
    # concurrent reviews of the same one would share a directory and the
    # second one's `git reset --hard` would pull the tree out from under the
    # first. That is the race acquire_lock() exists to prevent, reappearing
    # inside one process.
    #
    # Default one, and deliberately. This buys latency, not money: the same
    # reviews are paid for, closer together, which is what makes a rate
    # limit more likely to refuse one, and a refused review comes back
    # FAILED and spends an attempt. An operator raising it is choosing that
    # trade for their own repositories.
    "parallel_repos": 1,
    "skip_drafts": True,
    "skip_bots": True,
    "skip_forks": True,
    "authors": [],
    "review_timeout": 1800,
    "github_app": None,
    # The model that tiers the findings, or null to post them in the order
    # the reviewer reported them, as Vinegar did before this existed. A
    # small model on purpose: measured against a larger one on the same
    # saved reviews it reached the same blocker counts for a fifth of the
    # cost. An alias rather than a dated model id, so it does not name a
    # model that is retired while this default goes on being shipped.
    "severity_model": "haiku",
}


def checkout_grace(config):
    """Token life the checkout and the review need between them.

    Both the daemon and the `--pr` path ask for this, and each used to spell
    the sum out with a comment saying it had to match the other. Drift shows
    up only under a slow clone, at review time, on a real pull request.
    """
    return CHECKOUT_GRACE + config["review_timeout"]


def utc_stamp():
    """Now, in the one format this program writes times in.

    Three callers want it since the checks-list indicator arrived, and
    this file extracts a one-line rule the moment there are two:
    priced(), both_streams() and checkout_grace() each exist because two
    copies of one drifted or had to be fixed twice.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(message):
    # Stamped inside the lock, not before taking it. LOG_LOCK's comment
    # says why the printing has to be one write; the stamp has to be under
    # the same hold for a duller reason. Built outside, a thread preempted
    # between reading the clock and acquiring prints a line stamped earlier
    # below one stamped later, and a log whose timestamps disagree with its
    # own order cannot be sliced by time or sorted.
    with LOG_LOCK:
        print("%s %s" % (utc_stamp(), message), flush=True)


def run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    # `input` and `stdin` cannot both be passed, so the default stays DEVNULL
    # and only a caller with something to send opts out of it.
    #
    # utf-8 and "replace" rather than the locale and "strict", in both
    # directions. git prints a Latin-1 file's diff as the raw bytes it holds,
    # and a strict decode raised UnicodeDecodeError out of diff_lines into
    # announce(), which swallowed the finished review it was carrying. And
    # under the C locale a launchd job can run in, a strict *encode* of the
    # posting payload dies on the first non-ASCII character a finding quotes.
    # A replacement character in a quoted line survives both; an exception
    # here costs the whole review.
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True, env=env,
                          encoding="utf-8", errors="replace",
                          input=stdin_text,
                          stdin=None if stdin_text is not None
                          else subprocess.DEVNULL,
                          capture_output=True)


def both_streams(result, cap):
    """Both halves of what a command said, stderr first, cut to a log line.

    `gh api` puts its one-line summary on stderr and GitHub's own body on
    stdout, and the body is the half that names which comment was refused
    and why. Two callers log this, and as two inlined copies a fix to either
    had to be made twice or the same failure read differently in one log.
    """
    return " ".join(part.strip() for part in (result.stderr, result.stdout)
                    if part and part.strip())[:cap]


def priced(event):
    """What a model call cost, as a log-line tail, or "" if it did not say.

    Both calls a review makes report this and they used to spell it out
    separately. Totalling the daemon's spend means grepping the log for
    this phrase, and the suite leans on the two sites wording it
    identically, so one changed format would quietly stop counting the
    other. finding_bullet() records what the last duplication of a
    rendering rule cost.

    bool first: it is an int, and True would print as 1.00 USD.
    """
    cost = event.get("total_cost_usd")
    return ", %.2f USD equivalent" % cost if isinstance(
        cost, (int, float)) and not isinstance(cost, bool) else ""


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def app_jwt(app_id, key_path):
    """Sign the short-lived JWT that authenticates Vinegar as the GitHub App.

    GitHub wants RS256 and Python has no RSA in its standard library. One
    shell-out to openssl is cheaper than making the daemon depend on a crypto
    package, and openssl ships with macOS.
    """
    now = int(time.time())
    parts = ({"alg": "RS256", "typ": "JWT"},
             {"iat": now - 60, "exp": now + 540, "iss": str(app_id)})
    signing_input = b".".join(
        b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in parts)

    # Bounded like every other call on this thread, and this one does not go
    # through run(): it needs bytes rather than text, because a signature is
    # not UTF-8. Signing is milliseconds of local CPU, so the bound is only
    # against the key sitting on a mount that stops answering, which parks
    # openssl in the kernel and with it that repository's poll thread, while
    # the watchdog reads a live pid as healthy. Same argument as
    # DIFF_TIMEOUT, and the same number.
    try:
        signed = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", key_path],
            input=signing_input, capture_output=True, timeout=DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError("openssl did not finish signing with %s within "
                           "%ds" % (key_path, DIFF_TIMEOUT))
    if signed.returncode != 0:
        raise RuntimeError("openssl could not sign with %s: %s" % (
            key_path, signed.stderr.decode(errors="replace").strip()))
    return (signing_input + b"." + b64url(signed.stdout)).decode()


def github_api(path, token, scheme="Bearer", payload=None, method=None):
    """Call the GitHub API directly.

    The App endpoints need `Authorization: Bearer <jwt>`, and `gh` sends
    `token`, so these two calls cannot go through `gh` the way every other
    GitHub call in this file does.

    `method` names the verb, and it exists because the line below guesses
    one. Inferring it from whether there is a payload is right for every
    call that has one and silently wrong for a POST that has none: minting
    an installation-wide token for discover_repos() is exactly that call,
    and inferred it went out as a GET, which that path answers 404 to. A
    404 there reads as an App that is not installed, so the guess did not
    merely fail, it accused the wrong thing. Passing `payload={}` also
    turns it into a POST and was how this was first written; it works
    because `json.dumps({})` is two bytes rather than because anyone
    decided it should.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        "https://api.github.com" + path, data=body,
        method=method or ("POST" if body else "GET"))
    request.add_header("Authorization", "%s %s" % (scheme, token))
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as err:
        raise RuntimeError("GitHub said %s for %s: %s" % (
            err.code, path, err.read().decode(errors="replace")[:200]))


def installation_token(app, repo, cache, good_for=0):
    """Return a token that can act on one repository and nothing else.

    This is the point of the GitHub App. The token is scoped to the single
    repository under review, so a diff that talks the reviewer into calling
    `gh` cannot reach anything else the owner can see, even other
    repositories in the same installation. The allow list names only
    read-only `gh` subcommands, but a prefix rule cannot see the flags that
    follow one, and this is what bounds the damage when a rule is not enough.

    `good_for` is how many seconds of life the caller needs. A token with less
    than that left is replaced now rather than expiring mid-review and losing
    the comments the review was about to post.

    The cache needs no lock, which is worth saying because `state` next door
    does. It is keyed by repository and `parallel_repos` gives each
    repository one thread, so no two threads ever read or write the same
    entry. A lock here would be worse than nothing: the mint below is two
    HTTPS calls, and holding one across them would stop the repository that
    does not need it.
    """
    token, expires = cache.get(repo, (None, 0))
    if token and time.time() + good_for < expires:
        return token

    jwt = app_jwt(app["app_id"], os.path.expanduser(app["private_key"]))
    try:
        install = github_api("/repos/%s/installation" % repo, jwt)
    except RuntimeError as err:
        # The App existing and the App being installed on a repository are two
        # different things, and this is the first place that difference shows.
        if "said 404" not in str(err):
            raise
        slug = github_api("/app", jwt).get("slug", "<app>")
        raise RuntimeError(
            "the App is not installed on %s. Install it at "
            "https://github.com/apps/%s/installations/new" % (repo, slug))

    minted = github_api("/app/installations/%d/access_tokens" % install["id"],
                        jwt, payload={"repositories": [repo.partition("/")[2]]})

    # GitHub issues these for an hour.
    cache[repo] = (minted["token"], time.time() + 60 * 60)
    return minted["token"]


def discover_repos(app):
    """What the App's installations cover, as owner/name, and what was left out.

    This is what makes onboarding a repository a checkbox in the App's
    settings rather than an edit to `repos` and a restart, which is the
    difference between watching two repositories and watching an
    organisation. It runs only where the config names no repositories at
    all: an explicit `repos` is an instruction and wins over this.

    The token minted here is the broadest one Vinegar ever holds, and that
    is the reason this is a function of its own rather than a call inside
    installation_token(). Listing what an installation covers needs a
    token scoped to the installation rather than to one repository, so
    that token can reach every repository the App was given.
    installation_token()'s whole argument -- that a diff which talks the
    reviewer into running `gh` reaches nothing but the repository under
    review -- holds only while the token in the reviewer's environment is
    the narrow one. So this one is minted here, spent on one listing, and
    dropped. It is never written to the cache github_env() reads, and it is
    not part of what this returns, so there is no path from here to a
    reviewer's environment.

    Archived repositories are left out, and that is money rather than
    tidiness. GitHub refuses every write to an archived repository, so one
    with an open pull request is a review paid for in full, nine to
    twenty-two minutes of it, and then a 403 when it tries to post -- again
    on every push, for as long as the pull request stays open. Nothing on
    the pull request can say so, because nothing can be written to it. They
    appear only where an installation covers every repository rather than a
    chosen list, which is one setting away. They are returned separately
    rather than only dropped, so refresh_repos() can say how many.

    Up to a hundred installations, which is one page and no loop. An App
    on more accounts than that is a public one serving strangers, and this
    is a private App reviewing the organisation that owns it. The
    repositories under each installation *are* paged, because that is the
    list that grows with the organisation, and a silent hundred-and-first
    entry there is a repository that is simply never reviewed.
    """
    jwt = app_jwt(app["app_id"], os.path.expanduser(app["private_key"]))
    # One spelling for both the page asked for and the length that means
    # there is another. As two literals they have to agree, and the way
    # they disagree is silent: a URL asking for 30 beside a loop that stops
    # below 100 reads one page and calls it the whole organisation.
    per_page = 100
    found, archived = [], []
    for install in github_api("/app/installations?per_page=%d" % per_page,
                              jwt):
        token = github_api(
            "/app/installations/%d/access_tokens" % install["id"],
            jwt, method="POST")["token"]
        page = 1
        while True:
            listed = github_api(
                "/installation/repositories?per_page=%d&page=%d" % (
                    per_page, page), token, scheme="token")
            covered = listed.get("repositories", [])
            for repo in covered:
                (archived if repo.get("archived") else found).append(
                    repo["full_name"])
            # A short page is the last one. A full one costs one more call
            # that answers nothing, which is the cheap half of the trade:
            # `total_count` would save it and cannot be compared against
            # `found`, because the archived ones have already been taken
            # out of that list and are still counted in the number.
            if len(covered) < per_page:
                break
            page += 1
    # Sorted, so neither the poll order nor the line naming them depends on
    # the order GitHub answered in. Below a width of one worker per
    # repository that order decides which repositories are reviewed first,
    # and an order that moves between passes is one nobody can reason about
    # from the log.
    return sorted(found), sorted(archived)


def refresh_repos(config, asked_at):
    """Bring `config["repos"]` up to date with what the App covers.

    Called from main()'s own thread between passes, which is the only
    place it is safe. poll_once() joins every worker before it returns, so
    no pass is reading the list while this replaces it, and the next pass
    reads the new one: poll_width() is recomputed from it too, so a
    repository onboarded an hour ago is polled without a restart.

    At most hourly, and an ask that failed does not count as one. A
    failure returns `asked_at` untouched, so the next pass asks again a
    minute later rather than an hour later. That matters most where it is
    least visible: at startup there is no previous list at all, so an ask
    that failed is a daemon watching nothing, and counting it would turn
    one bad minute into a bad hour of reviewing no repository.

    A failure keeps the list already being polled, rather than emptying
    it. Listing genuinely fails on a real network -- 96 times in the three
    days to 2026-08-19 on this deployment, about 1.1% of attempts -- and
    reading one of those as "the App covers nothing" would stop every
    repository being reviewed until an ask answered. The log says that it
    kept them, because a daemon polling a list GitHub did not just confirm
    should say which of the two it is doing.

    Every change is named, both halves of it. A repository that starts
    being reviewed, or stops, with nothing in the log saying why is the
    failure this path is most likely to produce: the cause is a checkbox
    in a web UI on another machine, so the log is the only place those two
    facts ever meet.
    """
    if time.time() - asked_at < DISCOVERY_INTERVAL:
        return asked_at
    try:
        found, archived = discover_repos(config["github_app"])
    except Exception as err:
        log("cannot ask the App which repositories it covers, so the %d "
            "already being polled stay: %s" % (len(config["repos"]), err))
        return asked_at

    new = [name for name in found if name not in config["repos"]]
    gone = [name for name in config["repos"] if name not in found]
    if new or gone:
        changed = []
        if new:
            changed.append("added " + ", ".join(new))
        if gone:
            changed.append("removed " + ", ".join(gone))
        if archived:
            changed.append("%d archived and left out" % len(archived))
        log("the App covers %d repositor%s: %s" % (
            len(found), "y" if len(found) == 1 else "ies",
            "; ".join(changed)))
    config["repos"] = found
    return time.time()


def github_env(config, repo, cache, good_for=0):
    """The environment for every git, gh, and claude call touching `repo`.

    Without a configured App this is the ambient environment, so `gh` uses
    whatever account you logged in with and comments post under your name.
    """
    app = config.get("github_app")
    if not app:
        return None
    return dict(os.environ,
                GH_TOKEN=installation_token(app, repo, cache, good_for))


def load_config(path):
    try:
        with open(path) as handle:
            config = dict(DEFAULTS, **json.load(handle))
    except FileNotFoundError:
        sys.exit("no config at %s (copy config.example.json)" % path)
    except json.JSONDecodeError as err:
        sys.exit("%s is not valid JSON: %s" % (path, err))

    unknown = set(config) - set(DEFAULTS)
    if unknown:
        sys.exit("%s: unknown keys %s" % (path, ", ".join(sorted(unknown))))
    if not isinstance(config["repos"], list):
        # A bare string passes a truthiness check and then iterates as
        # characters, which polls `-R S`, `-R o`, `-R u` once a minute forever.
        sys.exit("%s: repos must be a list of owner/name" % path)
    # Empty is a request to discover, and only where there is an App to
    # discover from. That is the whole rule: an explicit `repos` is an
    # instruction and wins, and discover_repos() fills the list when the
    # file gives none.
    #
    # `repos` cannot simply be dropped in favour of discovery, which is
    # why this reads as a fallback rather than a replacement. Discovery
    # needs an App and config.example.json ships `"github_app": null`, so
    # an install following the example has no installation to ask.
    #
    # Refused here rather than discovered as nothing later. Without this
    # the daemon starts, polls an empty list once a minute for ever, and
    # says nothing at all: it is the same silent-stop class the shape
    # check below exists for, and the operator's mistake is one line
    # further up in the same file.
    if not config["repos"] and not config["github_app"]:
        sys.exit("%s: repos is empty and there is no github_app to discover "
                 "repositories from, so there is nothing to poll. Name them "
                 "in repos, or configure the App and Vinegar watches every "
                 "repository it is installed on" % path)
    # Every entry a string, and stored stripped, the way the model names
    # further down are. Nothing checked this before: `repos: ["o/a", {}]`
    # was accepted here and then raised TypeError out of main()'s own
    # startup line, before a single repository was polled, which under
    # launchd is a traceback and a restart every 30 seconds for ever. It
    # has to come before the duplicate check below, which compares these
    # entries and folds their case.
    for nth, name in enumerate(config["repos"]):
        if not isinstance(name, str) or not name.strip():
            sys.exit("%s: repos must hold owner/name strings, not %r" % (
                path, name))
        name = config["repos"][nth] = name.strip()
        # The shape too, which the sentence above promised and nothing
        # checked. `"repos": ["o/api", "web"]` is one dropped owner, and
        # it started the daemon perfectly: every poll then ran
        # `gh pr list -R web`, which fails on the format, is swallowed by
        # poll_repo's handler, and logs one line. That repository was
        # never reviewed again and nothing at startup said why. With an
        # App it also mints a token against a repository named "".
        #
        # The same rule main()'s `--pr` branch runs on its argument, from
        # the one pattern rather than a second copy of the test. REPO_NAME
        # says what it refuses and why the first spelling was not enough.
        if not REPO_NAME.match(name):
            sys.exit("%s: repos wants owner/name, got %r" % (path, name))

    # Named twice is not the same as reviewed twice. A duplicate is one
    # wasted listing per pass while repositories are polled one at a time,
    # and above that it puts two passes on the single checkout directory
    # that repository is given: the second pass's `git reset --hard` moves
    # the tree under the first, which then reports findings about a commit
    # nobody asked about.
    #
    # Collapsed and said, not refused, and the first draft of this refused.
    # That was wrong in the one direction load_state()'s docstring argues
    # against for the file next to this one: a duplicate was harmless
    # before this setting existed, so an operator upgrading with
    # `["o/api", "o/api"]` already on disk met a daemon that exited at
    # startup, and launchd relaunched it every ten seconds reviewing
    # nothing at all. Turning a working install into an outage is a worse
    # answer than the wasted listing it was correcting.
    #
    # The argument for refusing was that a daemon polling a shorter list
    # than the file names is a disagreement nothing explains. The log line
    # is what answers that, so it names the entry rather than only the
    # count.
    #
    # Collapsed at every width rather than only above one. The hazard is
    # real only when two passes run at once, but a rule that makes the
    # same file valid or invalid depending on `parallel_repos` is one an
    # operator has to hold two settings in mind to predict.
    #
    # Folded, because neither of the things that would collide cares about
    # case. GitHub resolves a repository name without it, and the default
    # macOS filesystem does too, so `Sour-Labs/vinegar` beside
    # `sour-labs/vinegar` is two entries listing the same pull requests
    # into one clone directory. An exact comparison missed the collision
    # this check exists to catch.
    seen, kept, twice = set(), [], []
    for name in config["repos"]:
        if name.casefold() in seen:
            # Each offending spelling once. Recording every repeat printed
            # "names o/r, o/r more than once" for three copies, which
            # reads as two separate problems.
            if name not in twice:
                twice.append(name)
            continue
        seen.add(name.casefold())
        kept.append(name)
    if twice:
        config["repos"] = kept
        log("%s: repos names %s more than once, matched without case. A "
            "repository is polled once per pass and has one checkout, so "
            "the extra copies are dropped and %d repositor%s watched"
            % (path, ", ".join(twice), len(kept),
               "y is" if len(kept) == 1 else "ies are"))
    if config["effort"] not in EFFORTS:
        sys.exit("%s: effort must be one of %s" % (path, ", ".join(EFFORTS)))

    # The numbers, because they are read as numbers. A hand-edited
    # `"review_timeout": "1800"` is accepted by JSON and by every check
    # above it, and then raises TypeError inside checkout_grace on every
    # pull request on every poll: nothing reviewed, MAX_ATTEMPTS never
    # reached, no give-up posted, and the pull requests silent. This is
    # the file operators actually edit, and load_state guards the same
    # class for the one they are only told to edit.
    units = {"max_changed_lines": "lines", "parallel_repos": "repositories"}
    for name in ("poll_interval", "review_timeout", "max_changed_lines",
                 "parallel_repos"):
        value = config[name]
        if not isinstance(value, int) or isinstance(value, bool):
            sys.exit("%s: %s must be a whole number of %s, not %r" % (
                path, name, units.get(name, "seconds"), value))
        if value <= 0:
            sys.exit("%s: %s must be greater than zero" % (path, name))

    # Below the loop, not above it, and the first draft was above with two
    # isinstance guards of its own. The reason given for that placement
    # was that the loop exits on a non-number, which is exactly what makes
    # here correct: `"parallel_repos": "4"` never reaches this line, so
    # the guards were three lines defending a state the loop had already
    # refused. The token-life warning below reads `review_timeout` after
    # the same loop with no guard at all, for the same reason.
    if config["parallel_repos"] > MAX_PARALLEL_REPOS:
        sys.exit("%s: parallel_repos must be at most %d. Each one is a "
                 "whole reviewer with its own clone beside it, so the "
                 "number is what a machine can run at once rather than "
                 "how many repositories are watched: the ones over the "
                 "width wait for a worker" % (path, MAX_PARALLEL_REPOS))

    # Said, not refused, like the token-life warning. A width above the
    # number of repositories is clamped by poll_width(), and the startup
    # line prints the clamped number and stays silent at one, so a
    # single-repository install that set `parallel_repos: 4` read a line
    # byte-identical to the one it printed before the edit and had no way
    # to learn the setting did nothing. Every other setting here that
    # cannot do what it says either refuses or says so; this one did
    # neither.
    #
    # Through poll_width() rather than a `min()` written out again. Its
    # docstring records that this same rule expressed twice already
    # shipped as a bug, with the startup line naming a width the pass did
    # not use, and this line exists only to tell an operator the width
    # that will really be used.
    # Only where the file named them. Discovered repositories are not known
    # yet at this point, and comparing against the empty list told an
    # operator "there are 0 repositories to poll" on a daemon that was
    # about to discover seventeen and poll all of them.
    watched = len(config["repos"])
    if watched and config["parallel_repos"] > watched:
        log("%s: parallel_repos is %d and there %s %d repositor%s to poll, "
            "so %d of them run at once" % (
                path, config["parallel_repos"],
                "is" if watched == 1 else "are", watched,
                "y" if watched == 1 else "ies", poll_width(config)))

    # Its own check rather than the loop above, because null is a value
    # here and that loop refuses one.
    #
    # Zero is refused as well as a non-number, and the two failures are
    # not the same failure. A non-number raises TypeError inside
    # blockers_only() on every review, which is the class the loop above
    # exists for. Zero parses and compares perfectly and means a first
    # review that reports only blockers: the pull request is then never
    # told anything smaller, once, and nothing on it says so. An operator
    # who wants that is describing a different tool, and one who typed it
    # meaning "off" wanted null.
    rounds = config["blockers_only_after"]
    if rounds is not None and (not isinstance(rounds, int)
                               or isinstance(rounds, bool) or rounds <= 0):
        sys.exit("%s: blockers_only_after must be a whole number of reviews "
                 "greater than zero, or null for every review to report "
                 "everything it finds, not %r" % (path, rounds))

    # Every setting that names a model, in one place because they are one
    # rule. Each reaches argv as a `--model` argument, where a number or a
    # list raises TypeError inside subprocess.run: triage() catches that
    # the way it catches everything and review()'s lands in handle_pr's
    # catch-all, so the operator gets one log line per review, or one per
    # pull request per poll, with nothing in it naming the config. `null`
    # is how each is turned off, so every message names that rather than
    # leaving the operator to guess at `false` or `""`.
    #
    # Stripped as well as checked, and stored stripped. A name is a name
    # whatever a hand-edit leaves around it: `"model": "claude-opus-5 "`
    # passes the check, is not something the API can route, and 404s every
    # review of every repository, which is the failure this check exists
    # to prevent. It also has to be stripped *before* the same-model
    # refusal below, or a `" claude-opus-5"` beside a pinned
    # `"claude-opus-5"` walks past it and buys a second 404 per review.
    for name, off in (
            ("severity_model",
             " to post findings in the order the reviewer reported them"),
            ("model", " to use your Claude Code default"),
            ("fallback_model", " for no fallback")):
        named = config[name]
        if named is not None and not (isinstance(named, str)
                                      and named.strip()):
            sys.exit("%s: %s must be the name of a model, or null%s, not %r"
                     % (path, name, off, named))
        # Guarded by the same isinstance the refusal above rests on, not by
        # `is not None`. The two are equivalent only while the refusal is
        # intact, and a guard that raises the moment the one above it is
        # weakened turns a clean failure into an AttributeError out of
        # load_config.
        if isinstance(named, str):
            config[name] = named.strip()

    # A fallback naming the model it is a fallback for is not one. It buys
    # a second 404 per review and, worse, an operator who believes the
    # daemon can survive its pinned model going away when it cannot. The
    # equality is also what lets review() tell the two attempts apart.
    if (config["fallback_model"] is not None
            and config["fallback_model"] == config["model"]):
        sys.exit("%s: fallback_model is the same model as model, so it "
                 "cannot stand in for it" % path)

    if config["review_timeout"] > MAX_REVIEW_TIMEOUT:
        # The severity pass is named because the sentence is an argument
        # about how long the daemon can go quiet, and review_timeout has
        # not been the whole of that since it was added: it runs after the
        # review and before the posting, on the same thread.
        sys.exit("%s: review_timeout must be at most %d seconds. One pull "
                 "request holds its repository's poll thread for as long as "
                 "its review runs, plus up to %ds for the severity pass "
                 "after it, so nothing else in that repository is listed or "
                 "reviewed meanwhile and the watchdog reads a parked daemon "
                 "as a healthy one."
                 % (path, MAX_REVIEW_TIMEOUT, SEVERITY_TIMEOUT))

    # Said, not refused. The cache serves a token only while
    # `now + good_for < expires`, so once the checkout and the review
    # together ask for a token's whole life no cached token can satisfy it:
    # every review mints a fresh one and then runs past the hour that token
    # lives.
    #
    # What that costs is a mint per review and a dead fallback, not a broken
    # review. Since the reviewer is handed no GitHub credential and has no
    # network, nothing it does needs the token, and the posting mints its
    # own. What is left holding it is posting_env()'s fallback, used only
    # when that fresh mint fails, and past the cap it would be expired.
    # Every other refusal in this function stops a daemon that would run
    # *wrongly*; taking one down over a token bill is the worse trade, and
    # it was a refusal for one day whose first catch was the deploy of the
    # change that added it.
    #
    # Only with an App. Without one github_env() returns None before
    # installation_token() is reached, so nothing mints and there is nothing
    # to say; the shipped config.example.json has no App, so the warning
    # would otherwise greet the documented starting configuration with a
    # cost it cannot incur.
    #
    # `>=`, because the cache condition is a strict `<`: asking for exactly
    # a token's life already fails it. The message says "at least" to match.
    #
    # No rate is quoted. The mint happens once per review that actually
    # runs, not once per poll — handle_pr reaches it only past the DONE,
    # give-up and skip returns — so a per-day figure derived from
    # `poll_interval` overstates it by about three orders of magnitude.
    if config.get("github_app") and checkout_grace(config) >= TOKEN_LIFE:
        log("%s: review_timeout is %d, and with the %ds the checkout "
            "reserves that asks for at least the %ds a GitHub token lives. "
            "No cached token can satisfy it, so every review mints a fresh "
            "one and then runs on a token that can expire before it "
            "finishes. Set it under %d to use the cache."
            % (path, config["review_timeout"], CHECKOUT_GRACE, TOKEN_LIFE,
               TOKEN_LIFE - CHECKOUT_GRACE))

    # A misconfigured App is caught here rather than at the first review, which
    # is minutes later and on a real pull request.
    app = config["github_app"]
    if app:
        missing = {"app_id", "private_key"} - set(app)
        if missing:
            sys.exit("%s: github_app needs %s" % (
                path, " and ".join(sorted(missing))))
        key = os.path.expanduser(app["private_key"])
        if not os.path.isfile(key):
            sys.exit("%s: no private key at %s" % (path, key))
        # And it has to sit where the sandbox denies reads. check_paths()
        # argues at length that the key is safe because HOME carries the
        # denied component and review-settings.json denies reads there —
        # an argument about a path this setting is free to point
        # somewhere else entirely. `~/keys/vinegar.pem` is a natural
        # choice, passes every other check, and is readable by any
        # review, which then quotes it into a finding posted on a public
        # pull request.
        if not any(part.lower() == DENIED_COMPONENT
                   for part in os.path.realpath(key).split(os.sep)):
            sys.exit(
                "%s: the private key at %s is somewhere a review can read. "
                "It must sit under a `%s` directory, which is the only "
                "path review-settings.json denies. Move it, or point "
                "private_key at a copy that lives there." % (
                    path, key, DENIED_COMPONENT))
    return config


def check_paths():
    """Refuse to run when these paths and the sandbox disagree.

    Everything the reviewer may read is decided by one fixed glob in
    review-settings.json, `Read(//**/.vinegar/**)`. That glob matches a
    literal path component. It cannot follow VINEGAR_HOME, so it is not enough
    for the checkout to sit outside HOME: what matters is whether each path
    contains the component the sandbox actually denies.

    Both directions fail silently, which is why this exits rather than warns.

    A checkout that the glob covers is one the reviewer cannot open. It
    reviews anyway by fetching each file over the API, `permission_denials`
    comes back empty, and the log reads like a healthy run.

    A HOME that the glob does *not* cover is worse. Nothing then denies the
    App's private key, which is the one credential not scoped to a single
    repository, and a key that can be read is a key that can be copied out in
    a finding. Silence here means the protection was never applied, not that
    it held.

    Checking both makes the older "outside HOME" rule redundant: HOME must
    contain the component and the checkout must not, so the checkout cannot be
    inside HOME.
    """
    def components(path):
        # realpath, so a symlink cannot present a denied directory under an
        # allowed name. `ln -s ~/.vinegar/checkouts ~/.vinegar-checkouts` is
        # the shortcut someone reaches for to avoid re-cloning on upgrade.
        return os.path.realpath(path).split(os.sep)

    # Case-insensitive, because APFS is case-insensitive by default: `.Vinegar`
    # is the same directory to the filesystem and the glob may not agree.
    # Rejecting more than the glob denies is the safe direction to be wrong in.
    if any(part.lower() == DENIED_COMPONENT for part in components(CHECKOUT_DIR)):
        sys.exit(
            "checkouts must not sit under a `%s` directory: %s does, and "
            "review-settings.json denies the reviewer every read there, so "
            "reviews would run from API fetches and report nothing wrong. "
            "Point VINEGAR_CHECKOUTS somewhere without that component."
            % (DENIED_COMPONENT, CHECKOUT_DIR))

    # Exact, because this is the direction where being wrong exposes the key,
    # and a component the glob does not match is not protected whatever it
    # looks like.
    if DENIED_COMPONENT not in components(HOME):
        sys.exit(
            "%s must contain a `%s` directory. review-settings.json denies "
            "the reviewer reads under that name and nowhere else, so as "
            "configured nothing stops a review from reading the App private "
            "key kept there. Rename it, or set VINEGAR_HOME to a path ending "
            "in `%s` (`~/instances/test/%s` isolates an instance and is "
            "still covered)." % (HOME, DENIED_COMPONENT, DENIED_COMPONENT,
                                 DENIED_COMPONENT))

    # And everything the settings file has to say, checked here so it is
    # said at startup rather than first discovered on a pull request. The
    # same call runs again for every review, which is where it matters:
    # this function runs once, and the file can be edited afterwards.
    load_settings()


def load_settings():
    """Read review-settings.json, or say in one sentence why it cannot be.

    Both callers use this, and that is the point: check_paths() runs once
    at startup while reviewer_settings() runs for every review, and when
    the two read the same file with different care they drift. The first
    version of this change validated at startup and forwarded blindly per
    review, so an operator editing the file to chase a denial — the case
    the docstring below already argues about — could widen `allow`, drop
    `Read(//**/.vinegar/**)`, and have the next review of an
    attacker-authored branch run with it, refused and logged by nothing.

    Every exit here is a sentence rather than a raised exception, and
    that matters more per review than at startup. Left to raise, a
    trailing comma saved mid-edit came out of review() as an ordinary
    failure: handle_pr records FAILED, the attempt is already charged,
    and three polls later every open pull request has a give-up comment
    and is abandoned for good over a typo. Exiting instead puts the
    daemon back in launchd's hands, which restarts it into this same
    check and prints the same sentence until the file is fixed.
    """
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            settings = json.load(handle)
        permissions = settings.get("permissions")
        permissions = permissions if isinstance(permissions, dict) else {}
        # isinstance, not `or []`. `or []` covers null and covers nothing
        # else: a hand-edited `"deny": "Read(//**/.vinegar/**)"` — the list
        # collapsed to the string it held — is truthy, survives, and turns
        # the membership test below into a substring test that passes
        # while Claude Code has a malformed rule and the App private key
        # is no longer denied. `allow` collapses the same way.
        allowed = permissions.get("allow")
        allowed = allowed if isinstance(allowed, list) else []
        denied = permissions.get("deny")
        denied = denied if isinstance(denied, list) else []
        sandbox = settings.get("sandbox")
        sandbox = sandbox if isinstance(sandbox, dict) else {}
    except Exception as err:
        sys.exit("%s cannot be read (%s), and the reviewer runs under it."
                 % (SETTINGS_PATH, err))

    # The reporting contract has a third copy: REPORT_TOOL names the tool
    # review() asks for and read_stream() listens for, and the allow list in
    # review-settings.json is what makes calling it possible. That list is a
    # literal in a file this code cannot derive from the constant. If they
    # disagree, every review comes back as unanchored prose with findings
    # nobody sees, quietly, which is the same shape of silent failure the
    # path checks exist to refuse.
    if REPORT_TOOL not in allowed:
        sys.exit(
            "review-settings.json does not allow %s. The reviewer would be "
            "told to report through a tool it cannot call, and every review "
            "would come back as prose with no findings. Add %s to "
            "permissions.allow." % (REPORT_TOOL, REPORT_TOOL))
    # And the rule check_paths' two path checks spend thirty lines reasoning
    # about. They confirm HOME sits where the glob covers; nothing
    # confirmed the glob was still there. Dropped or mistyped while
    # widening the deny list, every path check still passes, the daemon
    # starts, and a review can read the App private key — the one
    # credential not scoped to a single repository.
    for rule in DENY_ALWAYS:
        if rule not in denied:
            sys.exit(
                "review-settings.json must deny %s. The credential reads in "
                "DENY_ALWAYS are what keep a review from quoting a private "
                "key into a finding this program then publishes, and %s is "
                "missing. Add it to permissions.deny."
                % (rule, rule))
    # And the word that would make every rule above decorative.
    mode = permissions.get("defaultMode", PERMISSION_MODE)
    if mode != PERMISSION_MODE:
        sys.exit(
            "review-settings.json sets permissions.defaultMode to %r. Only "
            "%r is allowed here: the alternatives ignore the allow and deny "
            "lists, so one word would undo every rule in this file without "
            "touching one of them." % (mode, PERMISSION_MODE))

    # Reads are governed above. Writes are governed by nothing there, and
    # the allow list cannot govern them: a prefix rule matches `git show`
    # and cannot see the `--output=<path>` that follows it.
    #
    # Compared by identity, for two different reasons. Not truthiness,
    # because `"false"` is a non-empty string and would read as enabled.
    # And not `!=` either, because `1 == True` and `0 == False` in Python,
    # so `!=` accepts `"enabled": 1` and `"allowUnsandboxedCommands": 0` —
    # a number where a flag belongs, in a file edited by hand.
    for name, wanted, why in SANDBOX_RULES:
        if sandbox.get(name) is not wanted:
            sys.exit(
                "review-settings.json must set sandbox.%s to %s, because %s. "
                "Without the sandbox the reviewer can write any file the "
                "daemon user can, including this program, through "
                "`git show --output=`, which the allow list matches as an "
                "ordinary read." % (name, str(wanted).lower(), why))

    # And nothing in that stanza beyond what reviewer_settings() sends.
    # The daemon's own reviews are safe either way, since it builds the
    # stanza rather than inheriting it — but the README tells operators
    # this file is what a hand-run `claude --settings review-settings.json`
    # uses and what to read to learn what the reviewer may do. An
    # `allowWrite` added to unblock a hand-run leaves that hand-run
    # genuinely unconfined against an attacker-authored branch, and a
    # `network` rule added to tighten it is silently dropped, so the file
    # would describe something the program does not do. Refusing keeps the
    # two readings of this file the same one.
    filesystem = sandbox.get("filesystem")
    filesystem = filesystem if isinstance(filesystem, dict) else {}
    extra = (sorted(set(sandbox) - SANDBOX_KEYS)
             + ["filesystem." + name
                for name in sorted(set(filesystem) - SANDBOX_FS_KEYS)])
    if extra:
        sys.exit(
            "review-settings.json sets sandbox.%s, which Vinegar does not "
            "send: it builds that stanza itself for every review, so the "
            "key would change what a hand-run does and nothing else. "
            "Remove it, or change reviewer_settings() if the daemon should "
            "carry it too." % ", sandbox.".join(extra))
    # The network rule is pinned like the flags, not merely permitted. The
    # brief tells the reviewer it has no network and the README counts that
    # as the larger half of the win, and until now both rested on a default
    # nothing stated.
    if sandbox.get("network", SANDBOX_NETWORK) != SANDBOX_NETWORK:
        sys.exit(
            "review-settings.json must set sandbox.network to %s or leave "
            "it out. The reviewer is told it has no network and an allowed "
            "domain would make that false — while also failing, since the "
            "sandbox terminates TLS and `gh` will not trust it."
            % json.dumps(SANDBOX_NETWORK))
    return settings


def reviewer_settings(workspace):
    """The settings the reviewer runs under: the file, plus this checkout.

    `workspace` is the directory the review runs in, which review() has
    and this cannot derive: it is CHECKOUT_DIR/<owner>__<repo>, and any
    part of that may be a symlink pointing somewhere neither this
    function nor CHECKOUT_DIR would name.

    Passed to `claude --settings` as JSON rather than as a path, because
    the one rule that cannot live in the file is the one that matters
    most here. The sandbox leaves the workspace writable, the workspace
    is the checkout, and `.git/config` sits inside it: `core.fsmonitor`
    there names a command git runs, and the commands Vinegar runs in that
    checkout on its next poll — reset, clean, checkout — would run it
    outside the sandbox as the daemon user. Measured: all three execute
    it, and it survives both `reset --hard` and `clean -qfd`, so one line
    written into `.git/config` buys the next poll.

    CHECKOUT_DIR is derived from VINEGAR_HOME so that one machine can run
    isolated instances, which means no fixed string in review-settings.json
    is right for all of them. Deriving it here cannot drift.

    The whole stanza is built here rather than taken from the file, so
    what runs cannot be weakened by editing that file while the daemon
    polls. The file keeps its own copy because it is what a person reads
    to learn what the reviewer may do, and what a hand-run
    `claude --settings review-settings.json` uses; check_paths() refuses
    to start when the two disagree, so the file cannot quietly describe a
    weaker sandbox than the one in force.

    Nothing legitimate is lost. Every read-only git command a review runs
    — diff, log, show, blame, status, ls-files, rev-parse — works with the
    checkout read-only.
    """
    # Through load_settings(), so the permissions half of this file is
    # re-checked on the same schedule the sandbox half is rebuilt on.
    # Copying `permissions` straight through would have left the read-side
    # confinement — the deny rule guarding the App private key included —
    # as whatever the file said on the next poll, which is the failure this
    # function was written to end for writes.
    settings = load_settings()
    # Built from SANDBOX_RULES, not inherited from the file. check_paths()
    # reads that file once, at startup, and this runs again for every
    # review: an operator who edits it to chase a denial, or a `git pull`
    # of this directory, would otherwise launch the next review with the
    # stanza weakened and nothing would refuse or say so — the failure
    # that looks exactly like a successful review. Overwriting also drops
    # any key that is not checked, since an `allowWrite` left in the file
    # would widen what three validated keys still describe as closed, and
    # means a hand-edited `"sandbox": null` cannot raise out of here into
    # a give-up comment about a local typo.
    # The workspace as well as the directory holding it, and the resolved
    # form of each. The kernel judges a write by the path it resolves to,
    # and denying only the parent missed the case that matters: the
    # reviewer's workspace is CHECKOUT_DIR/<owner>__<repo>, so moving one
    # large clone with `ln -s /Volumes/big/o__r ~/.vinegar-checkouts/o__r`
    # put the workspace outside every denied entry while the parent still
    # looked covered. An entry that matches nothing costs nothing, and
    # guessing which form the sandbox canonicalises does not.
    denied = []
    for path in (CHECKOUT_DIR, workspace):
        for form in (path, os.path.realpath(path)):
            if form not in denied:
                denied.append(form)
    settings["sandbox"] = dict(
        ((name, wanted) for name, wanted, _ in SANDBOX_RULES),
        filesystem={"denyWrite": denied},
        network=dict(SANDBOX_NETWORK))
    return json.dumps(settings)


def waive(key, what, waived):
    """Whether a rate-limited attempt is forgiven rather than counted.

    A limit refuses without judging the request and lifts on its own
    clock, so counting it spent a budget on time passing: three polls a
    minute apart against a limit that resets hourly abandoned finished
    work. Bounded all the same, because a caller that returns straight
    after this would otherwise be pinned on one pull request for ever.

    Both budgets that can meet a limit ask here, rather than each
    spelling the rule out, which is how the two came to say it in two
    sentences with two different bounds in mind.
    """
    if waived >= MAX_ATTEMPTS:
        return False
    log("%s: %s is rate limited, so this attempt is not counted against "
        "the %d (%d of %d such waivers)" % (
            key, what, MAX_ATTEMPTS, waived + 1, MAX_ATTEMPTS))
    return True


def carry_forward(kept):
    """The fields a rebuilt entry keeps, as keywords for state_entry.

    Four sites spelled this out, and the one that inlined its own version
    instead is exactly where a counter reset on every charged attempt. A
    fifth persisted field should be one edit, not four.
    """
    return {"post_tries": kept.get("post_tries", 0),
            "unposted": kept.get("unposted", False),
            "waivers": kept.get("post_waivers", 0),
            "announce_waivers": kept.get("announce_waivers", 0)}


def reviewed_through(covered, head, was):
    """Where the next pass may start, as a keyword for state_entry.

    One rule, called by the daemon and by `--pr`, because the two writing
    it out separately is how a hand-run review would quietly widen or
    narrow every later daemon pass on that pull request. The other rebuild
    helpers beside this one exist because four hand-written copies of an
    entry drifted; this is the same shape and was extracted before it
    could.

    `covered` is review()'s own answer and nothing is inferred from it
    here. It is false for a run cut off part-way, for one whose findings
    never reached the pull request, and for a dry run.

    Every rebuild goes through this, not only the two that can advance.
    A skip, a failed checkout and the marker written before a review all
    call it with `covered` false, which is what keeps them from being the
    thing that forgets a finished review: they rebuild the entry from
    `kept`, which is deliberately empty once the head has moved, and that
    is precisely when this field is needed. It carried its own helper for
    a while, which was this function with the flag wired to false.
    """
    return {"reviewed_sha": head if covered else was.get("reviewed_sha")}


def rounds_done(reached, was):
    """How many reviews this pull request has had, as a keyword for
    state_entry.

    Counted per pull request rather than per commit, which is what makes
    it unlike `attempts` and why it reads `was` rather than `kept`: the
    head moving is the normal way a round ends, so a counter emptied along
    with the head-scoped ones would never reach two.

    Every rebuild carries it, for reviewed_through()'s reason and at a
    higher cost than there. A skip, a failed checkout or a give-up that
    reset this hands back every round already spent, so a pull request
    that draws one draft toggle or one bad clone goes back to reporting
    everything it finds and stays there.

    `reached` is whether this review's findings arrived on the pull
    request, and it is deliberately not "the outcome was DONE". review()
    answers DONE whenever the subscription was spent, the posting refused
    included, and a round counted there is a round the author was never
    shown: with GitHub refusing writes through two rounds, the third tells
    an author that "the first 2 reviews reported everything they found,
    and those findings are on the pull request already" on a pull request
    carrying nothing at all. It is also not `covered`, which is stricter
    for reviewed_through()'s own reasons — a review killed part-way still
    put findings in front of the author, and narrowing after it is right.

    Which makes repost() a counting site too. It is the one place a
    refused review's findings do reach the author, so the round it never
    got is counted there, once, when the send lands.

    A FAILED review never reaches anyone and never counts. MAX_ATTEMPTS
    already bounds those, and charging a round for one would let three bad
    minutes at GitHub decide that the next real review is narrowed.
    """
    return {"rounds": was.get("rounds", 0) + (1 if reached else 0)}


def state_entry(head, outcome, attempts=0, reason=None, announced=False,
                tries=0, post_tries=0, unposted=False, waivers=0,
                announce_waivers=0, reviewed_sha=None, rounds=0):
    """One pull request's record, built the one way.

    Four sites used to spell this out as a fresh literal, each deciding for
    itself which fields survived, which is the drift the other extracted
    helpers here exist to stop.

    `announced` is deliberately not carried through a skip. It is only ever
    set on a FAILED entry at MAX_ATTEMPTS, and that entry returns at the
    budget guard before skip_reason() is ever consulted, so a skip cannot
    see one: carrying it would be a line no test can reach and no failure
    needs today. If that ordering is ever changed, pass `announced=True`
    here rather than rebuilding the dict at the call site — the cost of
    getting it wrong is the give-up posted twice.
    """
    entry = {"outcome": outcome, "sha": head}
    if attempts:
        entry["attempts"] = attempts
    if reason:
        entry["reason"] = reason
    if announced:
        entry["announced"] = True
    if tries:
        entry["announce_tries"] = tries
    if post_tries:
        # Carried like the others. Every site that rebuilt an entry
        # through this helper dropped it, so a repost budget already spent
        # was handed back whenever anything else rewrote the entry, and
        # "three sends and stop" became three sends per rewrite.
        entry["post_tries"] = post_tries
    if unposted:
        # Whether a saved review is waiting behind this entry. Without it
        # the only way to find out was to list the reviews directory,
        # which the poll did for every pull request that had ever been
        # pushed past, for ever.
        entry["unposted"] = True
    if waivers:
        # Carried for the same reason as post_tries: it bounds how many
        # rate-limited sends are forgiven, and any rebuild that dropped
        # it handed those waivers back.
        entry["post_waivers"] = waivers
    if announce_waivers:
        # The same rule for the give-up's own waivers. It lived outside
        # this helper and was re-added by hand at one site, which is the
        # exception every other counter here exists to remove.
        entry["announce_waivers"] = announce_waivers
    if reviewed_sha and FULL_SHA.match(reviewed_sha):
        # The head whose findings actually reached the pull request, which
        # is what a later pass diffs from. Deliberately not `sha`: that one
        # is the head this entry last *acted* at, and record_once() rewrites
        # it for skips and failed checkouts, neither of which showed anyone
        # anything.
        #
        # Checked on the way in as well as on the way out, and the two
        # checks are not the same check. load_state() drops a bad one
        # because an operator can hand-edit this file; this one is about
        # never writing what that reader would reject, so that the file
        # this process wrote never needs repairing on the way back in.
        # Both cost the same thing now, one widened pass, since the
        # reader drops the key rather than the entry.
        entry["reviewed_sha"] = reviewed_sha
    if rounds:
        # Not beside `attempts`, though both are counters, because they
        # are counted over different things: `attempts` bounds the tries
        # at one commit and empties when the head moves, this counts the
        # reviews this pull request has had over its whole life.
        # rounds_done() is where that difference is argued.
        entry["rounds"] = rounds
    return entry


def write_atomic(path, text):
    """Write the whole file or none of it, and make the name durable.

    Three sites spelled this out and only one of them flushed, while the
    other two documented the guarantee they were not giving: finish()
    reads a transcript's existence as "the attempts left words worth
    keeping", which a half-written file would wear just as well.

    The directory flush is best-effort by design. SMB and several FUSE
    mounts answer EINVAL to fsync on a directory, and save_state() is
    called before the review runs, so raising here would abort every
    review on such a mount over a durability nicety after the bytes are
    already written.
    """
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    except OSError as err:
        log("%s is written but its directory could not be opened to flush "
            "(%s)" % (path, err))
        return path
    try:
        os.fsync(fd)
    except OSError as err:
        log("%s is written but its directory could not be flushed (%s)" % (
            path, err))
    finally:
        os.close(fd)
    return path


def quarantine(path, why):
    """Move a file Vinegar cannot use aside, and say where it went.

    Timestamped rather than a fixed `.unreadable`, because a second bad
    file must not overwrite the first: the copy is kept so an operator can
    recover what was known, and the second corruption is usually the one
    made while repairing the first.
    """
    # The stamp is seconds, so two quarantines inside one second would
    # collide and os.replace would overwrite silently, losing exactly the
    # copy this exists to keep. A free name is found first. Nothing races
    # for it: acquire_lock() means one Vinegar, and this runs at startup.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    aside = "%s.%s.unreadable" % (path, stamp)
    nth = 1
    while os.path.exists(aside):
        nth += 1
        aside = "%s.%s-%d.unreadable" % (path, stamp, nth)
    try:
        os.replace(path, aside)
    except OSError as err:
        log("%s is unusable (%s) and cannot be set aside either (%s)" % (
            path, why, err))
        return None
    log("%s is unusable (%s). It is kept at %s, and every open pull request "
        "will be reviewed once more" % (path, why, aside))
    return aside


def load_state():
    """Everything Vinegar remembers about which pull requests it has done.

    A missing file is a first run and starts empty. A file that exists but
    cannot be used is moved aside and the daemon carries on empty, saying
    so. Exiting instead read as the safe choice until two facts met it:
    launchd restarts an exited daemon every 30 seconds, so a bad file
    stopped every configured repository indefinitely rather than stopping
    anything cleanly, and the give-up log's own advice is to edit this file
    by hand, which makes bad exactly the state an operator can leave it in.
    One round of re-reviews is a bounded bill; an outage that lasts until a
    human reads the right log is not. The bad file is kept, so what was
    known is recoverable rather than guessed at.

    Every way it can be unusable, not only malformed JSON. A root-owned
    file left by one `sudo` run raises PermissionError, a non-UTF-8 byte
    raises UnicodeDecodeError, and a top-level array parses perfectly and
    then raises AttributeError on the first `.get` in handle_pr, once per
    pull request per poll, for ever. All three are the same outage, and the
    narrow catch answered only the third-most-likely of them.

    Read once, at startup, so a quarantine that itself fails cannot loop.
    """
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            state = json.load(handle)
        if isinstance(state, dict):
            # Each entry too, not only the top level. handle_pr() reads
            # every value with .get, so one `"o/r#12": "reviewed"` left by
            # the hand-edit this file's own log recommends raises
            # AttributeError for that pull request on every poll, for
            # ever, with nothing rewriting it. A bad entry is dropped
            # rather than taking the file with it: the pull request is
            # reviewed once more and the entry heals itself.
            # The fields too, not only the entry. `attempts` is compared
            # with >= and indexed directly, so an entry hand-edited to
            # `"attempts": "3"` raises TypeError for that pull request on
            # every poll, for ever, with nothing rewriting it to heal. A
            # counter that is not a whole number is not a counter.
            bad = [key for key, done in state.items()
                   if not isinstance(done, dict)
                   or any(not isinstance(done[field], int)
                          or isinstance(done[field], bool)
                          for field in ("attempts", "announce_tries",
                                        "post_tries", "post_waivers",
                                        "announce_waivers", "seen", "rounds")
                          if field in done)
                   # The flags too, and for the same reason: this file is
                   # hand-edited on the log's own advice, and a bare
                   # truthiness test reads `"announced": "no"` as yes.
                   # Someone writing that means the opposite, and gets a
                   # pull request that is never told Vinegar gave up.
                   or any(not isinstance(done[field], bool)
                          for field in ("announced", "unposted")
                          if field in done)]
            for key in bad:
                log("%s: its entry in %s cannot be read, so it is forgotten "
                    "and will be reviewed again" % (key, STATE_PATH))
                del state[key]
            # The one string, and it loses only itself. The fields above
            # are counters and flags that crash a comparison, so an entry
            # carrying a bad one has to go. A bad value here is not worth
            # that: what it costs, once dropped, is one widened pass,
            # which is the outcome dropping it asks for anyway.
            #
            # Dropped rather than left to fail downstream, because it does
            # not fail harmlessly. A non-string raises on the `since + ...`
            # that builds the probe, and review_scope builds that outside
            # the try that catches a probe refusing, so it would escape
            # handle_pr with an attempt already charged. Only this check
            # stands between the two.
            #
            # Dropping the entry for it was strictly worse than dropping
            # the key. An operator following the give-up log's own advice
            # and hand-editing the short sha they read in the comment
            # would have taken `unposted` down with it, and handle_pr
            # forgets a marker whose entry is gone: a paid-for review,
            # saved and waiting to be sent, deleted unsent and bought
            # again.
            for key, done in state.items():
                seen = done.get("reviewed_sha")
                if seen is not None and not (isinstance(seen, str)
                                             and FULL_SHA.match(seen)):
                    log("%s: its reviewed_sha in %s is not a commit id, so "
                        "the whole pull request is reviewed" % (
                            key, STATE_PATH))
                    del done["reviewed_sha"]
            return state
        why = "it holds %s, not an object" % type(state).__name__
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        # ValueError covers both JSONDecodeError and UnicodeDecodeError;
        # OSError covers the permission and I/O failures that open() and
        # read() raise.
        why = str(err)
    quarantine(STATE_PATH, why)
    return {}


def save_state(state):
    # Held here as well as by every caller that mutates first, and it is a
    # reentrant lock so those callers nest into this one for free. A site
    # that takes it only around the mutation would still let two saves race
    # for `state.json.tmp`, which is the half of the hazard that loses the
    # whole file rather than one entry.
    with STATE_LOCK:
        os.makedirs(HOME, exist_ok=True)
        write_atomic(STATE_PATH, json.dumps(state, indent=2, sort_keys=True))


def remember(state, key, entry, write=True):
    """Put one pull request's entry in what Vinegar remembers, and save it.

    Every site that changes that comes through here, so the lock two
    repositories polled at once need is taken in one place rather than at
    the ten call sites that would each have to. What matters is that
    the change and the save happen under a single hold: the hazard
    STATE_LOCK describes is another repository's save serialising this
    entry while it is half written, and a lock taken separately for each
    of the two allows exactly that.

    `write` is False for the one caller that is deliberately not paying
    for a file write. record_once() counts a repeated skip on every poll
    and rewrites the file on every tenth of them, and turning that into a
    write a minute per skipped pull request is what the counter exists to
    avoid.
    """
    with STATE_LOCK:
        state[key] = entry
        if write:
            save_state(state)


def open_prs(repo, env):
    # Bounded because this is the poll loop's heartbeat, once a minute per
    # repository, on the one thread that repository gets. A socket that is
    # open but never answers would otherwise park it indefinitely, polling
    # nothing, while the watchdog sees a live pid and calls it healthy.
    try:
        result = run(["gh", "pr", "list", "-R", repo, "--state", "open",
                      "--limit", str(PR_LIMIT), "--json", PR_FIELDS],
                     env=env,
                     timeout=LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("%s: gh pr list timed out after %ds" % (repo, LIST_TIMEOUT))
        return []
    if result.returncode != 0:
        log("%s: gh pr list failed: %s" % (
            repo, both_streams(result, 400)))
        return []
    prs = json.loads(result.stdout)
    # A list of objects, or nothing. `gh` answering 0 with something else
    # — a wrapper's error object, a future release reporting in band —
    # sails past the length check below and then raises inside
    # handle_pr; poll_once's own handler builds its message with
    # `pr.get`, which raises AttributeError from inside the except and
    # takes the daemon out entirely. load_state was hardened against
    # exactly this shape; the listing was not.
    if not isinstance(prs, list) or not all(
            isinstance(pr, dict) for pr in prs):
        log("%s: gh pr list did not answer with pull requests" % repo)
        return []
    # Said out loud when the cap is reached. Anything past it is never
    # handed to handle_pr: not skipped, not reviewed, not recorded and
    # not mentioned — indistinguishable from Vinegar having judged it,
    # which is the one thing silence is not allowed to mean.
    if len(prs) >= PR_LIMIT:
        log("%s: %d open pull requests is the most this asks for, so any "
            "beyond that are not seen at all" % (repo, PR_LIMIT))
    return prs


def skip_reason(pr, config):
    """Say why this pull request needs no review, or return None to review it."""
    if config["skip_drafts"] and pr["isDraft"]:
        return "draft"
    if config["skip_forks"] and pr["isCrossRepository"]:
        return "head branch lives in a fork"
    # A pull request from a deleted account has no author at all.
    author = pr.get("author") or {}
    login = author.get("login", "")
    if config["skip_bots"] and author.get("is_bot"):
        return "opened by the bot %s" % login
    if config["authors"] and login not in config["authors"]:
        return "author %s is not in the authors list" % (login or "unknown")
    changed = pr["additions"] + pr["deletions"]
    if changed > config["max_changed_lines"]:
        return "%d changed lines, over the %d cap" % (
            changed, config["max_changed_lines"])
    return None


def checkout(repo, pr, env):
    """Put a detached checkout on the pull request's head commit.

    With a pull request target, /code-review reads local files only when the
    checkout matches that branch. Otherwise it fetches each file over the API,
    which costs more and reviews worse.
    """
    path = os.path.join(CHECKOUT_DIR, repo.replace("/", "__"))

    # What a killed run leaves behind, cleared before it can wedge every
    # future poll. A SIGKILL during a fetch leaves `.git/index.lock`, and
    # every later `git reset` then fails with "Unable to create
    # index.lock: File exists" — a checkout failure, which is
    # deliberately exempt from MAX_ATTEMPTS, so no pull request in that
    # repository is ever reviewed again and nothing says why beyond one
    # line. The lock is only meaningful while a git process holds it, and
    # acquire_lock() means no other Vinegar is running.
    #
    # A clone killed part-way is the same shape one level up: `.git`
    # exists, so the clone is skipped and a repository git itself refuses
    # to open is used for ever. If it does not answer to rev-parse it is
    # not a repository, and starting again costs one clone.
    stale = os.path.join(path, ".git", "index.lock")
    if os.path.exists(stale):
        log("%s: clearing a lock left by a killed run" % repo)
        forget(stale)
    if os.path.isdir(os.path.join(path, ".git")):
        try:
            usable = run(["git", "rev-parse", "--git-dir"], cwd=path,
                         env=env, timeout=DIFF_TIMEOUT).returncode == 0
        except subprocess.TimeoutExpired:
            usable = False
        if not usable:
            log("%s: the checkout is not a usable repository, cloning it "
                "again" % repo)
            shutil.rmtree(path, ignore_errors=True)

    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(CHECKOUT_DIR, exist_ok=True)
        log("%s: cloning into %s" % (repo, path))
        # Bounded like the steps below, and reported in the same shape: the
        # caller tells them apart only by the message, so both name the
        # command that hung. Grepping the log for one has to find the other.
        clone = ["gh", "repo", "clone", repo, path, "--", "--quiet"]
        try:
            result = run(clone, env=env, timeout=CLONE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Removed before raising, or the timeout is worse than the hang
            # it replaced. `git clone` writes .git during its init phase,
            # long before it has any refs, so a killed clone leaves a
            # directory that `git rev-parse --git-dir` answers 0 for. The
            # probe above then calls it usable, the clone is skipped for
            # ever, and the steps loop runs against a repository with an
            # unborn HEAD: `git reset --quiet --hard` fails, checkout()
            # raises, and handle_pr exempts that from MAX_ATTEMPTS. One log
            # line, then that repository is never reviewed again. The
            # unusable-repository branch above already does this, for a
            # state that is milder and easier to reach.
            shutil.rmtree(path, ignore_errors=True)
            raise RuntimeError("%s did not finish within %ds"
                               % (" ".join(clone), CLONE_TIMEOUT))
        if result.returncode != 0:
            raise RuntimeError("clone failed: %s" % result.stderr.strip())

    # The tree is cleaned before the checkout, not after. A review killed by
    # its timeout can leave the tree dirty, and then `git checkout` refuses to
    # overwrite the leftovers, which would wedge this repo on every later poll.
    #
    # The credential helper is set on every pass rather than once at clone
    # time. It is what makes `git fetch` use GH_TOKEN, so if it ever fails to
    # write, a private repo stops fetching until someone finds a config write
    # that failed days earlier.
    steps = (["git", "config", "--local",
              "credential.https://github.com.helper", "!gh auth git-credential"],
             ["git", "fetch", "--quiet", "origin",
              "pull/%d/head" % pr["number"]],
             ["git", "reset", "--quiet", "--hard"],
             ["git", "clean", "-qfd"],
             ["git", "checkout", "--quiet", "--detach", pr["headRefOid"]])
    for step in steps:
        # The head fetch gets the network budget, the rest get the local
        # one. It is the same work the clone above is exempted for — a
        # first fetch after the daemon has been down, on a slow link, is
        # minutes — and unlike the local steps its failure is fatal to
        # checkout(), which is deliberately never counted against
        # MAX_ATTEMPTS: a 120-second cap turned a slow repository into a
        # pull request that was never reviewed and never given up on.
        # The local steps do need a ceiling: on a filesystem that stops
        # answering they block in the kernel for ever, parking the one
        # poll thread while the watchdog sees a live pid and calls it
        # healthy.
        bound = FETCH_TIMEOUT if step[:2] == ["git", "fetch"] else DIFF_TIMEOUT
        try:
            result = run(step, cwd=path, env=env, timeout=bound)
        except subprocess.TimeoutExpired:
            raise RuntimeError("%s did not finish within %ds" % (
                " ".join(step), bound))
        if result.returncode != 0:
            raise RuntimeError("%s failed: %s" % (" ".join(step),
                                                  result.stderr.strip()))

    # Refreshing the base branch is not housekeeping. Fetching only the pull
    # request head leaves the clone's base frozen at clone time, so it falls
    # further behind every merge, and `git diff <base>...HEAD` then reports
    # already-merged work as part of the pull request. Measured on this repo:
    # reviewing #7 against a base three merges stale showed five changed files
    # where the pull request had four, the extra one being `vinegar.py` from #5
    # and #6. Every review of this repo so far hit it, noticed, and re-scoped by
    # hand, which is effort spent per review and a correctness risk for any pass
    # that does not think to check.
    #
    # It happens here, after the loop, for two separate reasons.
    #
    # It cannot happen earlier: git refuses to fetch into a branch that is
    # checked out, and a fresh clone sits on exactly that branch, so this only
    # works once HEAD is detached.
    #
    # And it must not be able to fail the checkout, which is why it is not just
    # a sixth step. By this point the tree is already on the right commit and is
    # perfectly reviewable; a stale base only widens the diff, which is the
    # state every review was in before this existed. A failed checkout is worse
    # than that, and worse than it looks: handle_pr() records no state when
    # checkout() raises, so MAX_ATTEMPTS never applies, and anything persistent
    # would log one line per poll forever with the pull request never reviewed
    # and never given up on.
    base = pr["baseRefName"]
    # Bounded like the steps above it, and more so: this one goes to the
    # network, which is the call most likely to hang. A remote that
    # accepts and never answers — a dropped VPN, a proxy holding the
    # connection — would park that repository's poll thread here for ever
    # while the watchdog saw a live pid and called it healthy. Non-fatal
    # either way: a stale base widens the diff, it does not lose the review.
    try:
        result = run(["git", "fetch", "--quiet", "--force", "origin",
                      "%s:%s" % (base, base)], cwd=path, env=env,
                     timeout=FETCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("%s#%d: base %s not refreshed after %ds, the diff may include "
            "merged work" % (repo, pr["number"], base, FETCH_TIMEOUT))
        return path
    if result.returncode != 0:
        log("%s#%d: base %s not refreshed, the diff may include merged work: %s"
            % (repo, pr["number"], base, result.stderr.strip()))
    return path


def transcript_path(repo, pr):
    """Where the transcript of this pull request at this commit lives.

    One derivation, shared by the writer and by the check in finish() that
    an ending with nothing to say is not about to replace a transcript that
    says something. A second copy of this name is all it would take for
    "a different file" to quietly mean "the same file".
    """
    name = "%s__%d__%s.md" % (repo.replace("/", "__"), pr["number"],
                              pr["headRefOid"][:7])
    return os.path.join(REVIEW_DIR, name)


def save_transcript(repo, pr, text, findings=None, note=None, since=None,
                    blockers=False):
    """Write the review to disk, findings included.

    They have to be written explicitly now. The reviewer reports them through
    a tool and is told not to repeat them as text, so `result` is a closing
    sentence and nothing else. Without this the transcript directory, which
    is the entire output of a dry run and the copy that survives a failed
    post, would hold that sentence and no findings at all.
    """
    os.makedirs(REVIEW_DIR, exist_ok=True)
    path = transcript_path(repo, pr)
    # The same marker the comment gets. Without it a run killed after
    # reporting an empty list wrote a file saying "## Findings / None." with
    # nothing to say it never finished, and on a dry run that file is the
    # only artifact there is. The comment and the transcript disagreeing
    # about whether a review completed is the exact asymmetry finish() was
    # written to remove, one layer down.
    body = "%s\n\n%s" % (note, text) if note else text
    # Above the words, and in the transcript rather than only in the review
    # comment. repost() rebuilds a refused review's comment from this file
    # under its own opening, so a narrowed review delivered that way used
    # to arrive reading as though it had covered the whole pull request.
    # The transcript is also the artifact an operator is told to send by
    # hand when every retry is spent, and it said nothing about the scope
    # either.
    # Both narrowings, as one block ending in a blank line, because
    # repost() lifts the block whole and a second one written elsewhere in
    # the file would not be found. "No findings" means a different thing
    # under each of them and a third thing under both.
    marks = []
    if since:
        marks.append("%s`%s`." % (SCOPE_MARK, since[:7]))
    if blockers:
        marks.append(BLOCKERS_MARK)
        # Nested, not a third `if`, because DISAGREED_SAID explains the
        # line above it and must never open the block: repost() finds the
        # block by matching its first line and would leave a block opening
        # with this one in the body, unlifted.
        if below_blocker(findings):
            marks.append(DISAGREED_SAID)
    if marks:
        body = "%s\n\n%s" % ("\n".join(marks), body)
    if findings:
        body += "\n\n## Findings\n\n" + "\n\n".join(
            finding_bullet(finding) for finding in findings)
    elif findings is not None:
        body += "\n\n## Findings\n\n%s\n" % (
            "None reported before it stopped." if note else "None.")
    # Whole or not at all, and in utf-8: findings quote source, the C
    # locale a launchd job can run under encodes almost none of it, and on
    # a dry run a transcript that cannot be written is the entire output of
    # the run, silently gone. write_atomic carries both, and finish() reads
    # this file's existence as "the attempts left words worth keeping",
    # which a half-written file must not be able to wear.
    return write_atomic(path, "# %s#%d %s\n\n%s%s%s\n" % (
        repo, pr["number"], pr["headRefOid"][:7], pr["url"],
        TRANSCRIPT_SEP, body))


def review_scope(path, pr, done, env, label):
    """The commit a re-review diffs from, or None to read the whole thing.

    Two probes, and both of them answer the same way when they fail: read
    the whole pull request. That is the only direction a wrong answer here
    is allowed to fail in. A pass that reads too much costs money and says
    so in the log; a pass that reads too little reports a change clean
    without having looked at it, and nothing downstream can tell.

    The first probe is whether the commit is still here at all. checkout()
    fetches `pull/N/head` into a clone it may have deleted and remade since
    the last pass, and an unreachable commit can be pruned, so the sha in
    `state.json` is a claim about this clone rather than a fact of it.

    The second is whether the branch was rewritten. After a rebase or a
    force-push the old commit is no longer an ancestor of the head, so
    there is no "since" to speak of: `git diff old..HEAD` would describe
    the difference between two branches rather than the work added to one.
    `--is-ancestor` answers that in one call and costs nothing.

    The third is whether the branch has merged anything in. `<since>..HEAD`
    is every commit added to the branch, and a `git merge main` adds all of
    the base branch's own work to it: the "narrowed" scope is then wider
    than the whole pull request, and wider in the worst way, because those
    lines are absent from `refs/heads/<base>...HEAD` so diff_lines() cannot
    anchor a single finding about them and the overflow trim can push the
    author's real findings out of the comment. Any merge commit in the
    increment widens the pass. That refuses an unrelated topic merge too,
    which is the conservative direction and rare beside the case it exists
    for.

    A moving base branch on its own deliberately gets no probe. checkout()
    refreshes the base every pass, so the merge base really does move under
    a long-lived pull request, but the increment is measured along the
    branch: commits that leave the pull request's diff by being merged into
    the base leave `refs/heads/<base>...HEAD` and never entered
    `<since>..HEAD`. What the moving base does change is which lines can
    carry an inline comment, and diff_lines() recomputes that from the
    current full diff on every pass regardless.

    Every failure answers None, including the ones with no branch of their
    own. A probe can raise as well as refuse — a fork that cannot allocate,
    a checkout directory that vanished between checkout() and here — and
    letting that out charges an attempt for a review that never ran, three
    times over, until the pull request is announced as given up on with
    nothing having been attempted. The caller is deliberately outside the
    try that guards the review itself.
    """
    since = done.get("reviewed_sha")
    if not since:
        return None

    # Nothing new to read. Unreachable from the daemon, whose DONE check
    # returns first, and reached routinely by hand: `--pr` has no such
    # check, on purpose, so that trying another `--model` is possible at
    # all. Without this the reviewer is handed `git diff <head>..HEAD`, an
    # empty diff, and the pull request gets "No findings." under a sentence
    # saying only the new commits were read. A force-push back to a commit
    # already reviewed reaches it from the daemon too.
    if since == pr["headRefOid"]:
        log("%s: nothing has been pushed since its last review, so the whole "
            "pull request is reviewed" % label)
        return None

    # Each probe carries how to read it, beside the argv rather than
    # inferred from it. Two conventions live here: `cat-file` and
    # `merge-base` answer by exiting non-zero, `rev-list` answers by
    # printing and exits 0 for a clean range and for one full of merges
    # alike. Picking between them on `probe[1] == "rev-list"` read the
    # convention out of the command line, so adding `git -c
    # core.quotepath=false rev-list ...` — the form diff_lines() already
    # uses — would have silently reverted every merge to "safe".
    def by_exit(result):
        """Refused when the command said so with its exit code."""
        return result.returncode != 0

    def by_output(result):
        """Refused when the command printed anything, or could not run.

        Non-zero as well as any output. rev-list can fail having printed
        nothing, and reading only stdout called that "no merges" and
        narrowed, which is the one direction this function may not fail
        in.
        """
        return result.returncode != 0 or result.stdout.strip()

    for probe, why, refused_if in (
            (["git", "cat-file", "-e", since + "^{commit}"],
             "the commit its last review finished at is not in this clone",
             by_exit),
            (["git", "merge-base", "--is-ancestor", since, pr["headRefOid"]],
             "the branch was rewritten since its last review", by_exit),
            (["git", "rev-list", "--max-count=1", "--merges",
              "%s..%s" % (since, pr["headRefOid"])],
             "the branch has merged something in since its last review",
             by_output)):
        try:
            result = run(probe, cwd=path, env=env, timeout=DIFF_TIMEOUT)
        except subprocess.TimeoutExpired:
            log("%s: %s did not finish within %ds, so the whole pull request "
                "is reviewed" % (label, " ".join(probe), DIFF_TIMEOUT))
            return None
        except Exception as err:
            log("%s: %s could not be run (%s), so the whole pull request is "
                "reviewed" % (label, " ".join(probe), err))
            return None
        if refused_if(result):
            log("%s: %s, so the whole pull request is reviewed"
                % (label, why))
            return None
    return since


def reviewer_brief(pr, config, since=None, blockers=False):
    """What the reviewer cannot work out from the checkout it wakes up in.

    Two things, and both of them are things it otherwise guesses at.

    The base branch. /code-review's own instructions reach for
    `git diff @{upstream}...HEAD` and name `git diff main...HEAD` as the
    fallback. This checkout is detached, so there is no upstream and the
    fallback is what runs: every transcript before this showed a diff against
    `main`. On a pull request that targets anything else, that is not the
    pull request's diff at all, and the reviewer has no way to tell.

    And how to hand the findings back. Vinegar submits the review itself, in
    one piece, so a finding the reviewer keeps to itself is a finding nobody
    ever sees.

    That half names the same tool /code-review asks for rather than a
    competing format. An earlier version deferred to "your output contract"
    and then, when that turned out to mean a tool whose output Vinegar could
    not see, argued with it instead. Both cost live reviews. Agreeing with
    the command is the only arrangement that has worked.

    Both go in the system prompt rather than after the command, because
    everything after the effort level is collapsed into /code-review's review
    target and a sentence there would be read as the thing to review.

    The base branch is described as what it is rather than promised, and the
    reviewer is given somewhere to go when it does not resolve. checkout()
    treats a failed base fetch as non-fatal on purpose, so the ref named here
    is the right one to use and not always one that exists locally. Telling a
    reviewer to diff against a missing ref and forbidding any alternative
    leaves it with the guess this exists to prevent.

    `since` narrows what is reported without narrowing what may be read, and
    the two have to be said separately or the reviewer picks one. A diff read
    with no surrounding code produces confident findings about calls whose
    definitions it never saw, so the permission to read the whole branch is
    part of the instruction rather than an aside. What it must not do is
    report again on code this pass's diff does not touch: that was reported
    on an earlier pass and is already on the pull request as its own comment.

    The whole-pull-request wording is left exactly as it was when `since` is
    absent. It is the arrangement that was measured working after two others
    cost live reviews, and the first pass of every pull request still runs
    through it.

    `blockers` narrows the same way `since` does, one axis over: `since`
    says which lines may be reported on, this says which findings among
    them are worth reporting.

    It goes last, after the reporting contract, where `since` goes before
    it. That is not symmetry for its own sake. The contract ends "a
    finding you leave out of it is a finding nobody sees", which is the
    one sentence in the brief that argues against leaving anything out, so
    a paragraph telling the reviewer to leave most things out has to be
    the one that answers it rather than the one it answers.

    What it cannot run sits beside the network, for the reason the network
    is there at all: a reviewer discovering a denial one command at a time
    pays a turn for each. Measured across the three rounds of PR #24, of
    sixteen denied commands about six were `sed`, `find` and `awk` doing
    what Read, Grep and Glob already do, and most of the rest were
    `python3` trying to run the suite. The last sentence is the one that
    earns its place: told only which commands are denied, a reviewer plans
    a review around running the tests and discovers three times that it
    cannot. It is also true rather than tactful, and the transcript of a
    review that says so is worth more than one that quietly did less.
    """
    base = pr["baseRefName"]
    # Named as what it is in each case. Calling the base diff "the review
    # scope" while a later paragraph names a different scope is two
    # instructions for one decision, and the reviewer settles that itself
    # with nothing in the transcript saying which it chose.
    return (
        "This checkout is detached at the head commit of pull request #%d, so "
        "it has no upstream branch and `@{upstream}` does not resolve. The "
        "pull request targets `%s`, which should be fetched into this clone: "
        "`git diff refs/heads/%s...HEAD` is %s, spelled that "
        "way because a tag of the same name would otherwise win. If that ref "
        "does not resolve, use `refs/remotes/origin/%s...HEAD`, which this "
        "clone carries even when the branch itself was not fetched, and say "
        "in your summary that you used it. %s You have "
        "no network: `gh` cannot reach GitHub from here, so do not reach for "
        "it. Use Read, Grep and Glob to read this checkout: `sed`, `awk`, "
        "`find` and every interpreter, `python3` among them, are denied, so "
        "reaching for one costs a turn and returns nothing. You cannot run "
        "this repository's tests or any of its code. Do not substitute a "
        "branch of your own choosing, and do not assume `main`.%s\n\n"
        "Post nothing to GitHub yourself. Report every finding through the "
        "%s tool, including when you found none, and give `file` relative to "
        "the repository root. Vinegar reads that call and posts the whole "
        "review from it, so a finding you leave out of it is a finding "
        "nobody sees.%s"
        % (pr["number"], base, base,
           "the pull request's full diff" if since else "the review scope",
           base,
           # Only when there is no other scope to fall back on. checkout()
           # carries on when the base refresh fails, so a clone can hold
           # the head with neither base ref present; telling a reviewer to
           # report that it could not establish the scope, while the
           # paragraph after it hands over one that resolves, is two
           # instructions for one decision. It can settle that by refusing
           # to review, which is a paid-for run posting "could not
           # establish the scope" with a good scope on offer.
           "If `%s` does not resolve either, review the whole branch and "
           "say which refs you could not reach." % since if since else
           "If neither resolves, say you could not establish the scope "
           "rather than guessing at one.",
           since_brief(since) if since else "", REPORT_TOOL,
           blockers_brief(config) if blockers else ""))


def since_brief(since):
    """The paragraph that turns a full review into a re-review.

    Its own function because it is the half of the brief that changes, and
    a test that asserts on it should not have to spell out the base-branch
    half to reach it.
    """
    return (
        "\n\nThis is a re-review. An earlier pass already reviewed this pull "
        "request up to commit `%s` and its findings are already on the pull "
        "request, so this pass reviews only what has been added since: "
        "`git diff %s..HEAD` is the review scope. Read anything in the "
        "repository you need in order to judge that change, the earlier "
        "commits on this branch and the files they touch included. Report "
        "only on the review scope: a problem in code this pass's diff does "
        "not touch was either already reported or deliberately left, and "
        "raising it again is noise. If `%s` does not resolve, review the "
        "pull request's full diff instead and say so in your summary."
        % (since, since, since))


def blockers_only(round_number, config):
    """Whether the review that is about to run reports only blockers.

    One rule, because the answer has to reach four places that would
    otherwise each compare a config key themselves: the reviewer's brief,
    the comment that explains why the pass went quiet about small things,
    the checks list while it runs, and the checks list after. Two of those
    disagreeing is a pull request told that findings were held back by a
    review that was never asked to hold any back.

    `round_number` counts this review, so the first one is 1. Reading it
    as the count of reviews already done narrowed a pull request one round
    early, which is a whole round of findings nobody was ever shown.
    """
    after = config["blockers_only_after"]
    return after is not None and round_number > after


def this_round(entry, config, label):
    """Which review this is, whether it narrows, and one line saying so.

    Both callers had this written out: the same arithmetic, the same
    blockers_only() call and the same sentence, in handle_pr and in the
    `--pr` path. ended_title() records at length what that cost the last
    time — one copy was anchored by a mutation and the other was not, so
    reworded or inverted text in the hand-run path was caught by nothing —
    and it had happened again here before this was extracted.

    The round is logged whenever there has been one before it, not only
    when the pass narrows. `blockers_only_after` set to null is the one
    configuration with nothing at all bounding what a pull request can
    cost, and it was the one configuration that never printed a round, so
    the line meant to make a runaway greppable was missing from the only
    case that can run away. Round one prints nothing, because every pull
    request has one and it says nothing.
    """
    number = entry.get("rounds", 0) + 1
    blockers = blockers_only(number, config)
    if blockers:
        log("%s: review %d of this pull request, so it reports only blockers"
            % (label, number))
    elif number > 1:
        log("%s: review %d of this pull request" % (label, number))
    return blockers


def blockers_brief(config):
    """The paragraph that turns a full review into a blockers-only one.

    Beside since_brief() and for its reason: it is a half of the brief
    that changes, and a test reaching it should not have to spell out the
    base-branch half to get there.

    It narrows what is reported without narrowing what is read, which is
    the same division since_brief() draws and for a sharper reason here. A
    reviewer told to *look* only for blockers reads less carefully and
    finds fewer of them; the judgement of whether a thing is a blocker is
    the expensive judgement this whole program buys, and it cannot be made
    from a diff skimmed for severity.

    The definition here has to mean what SEVERITY_PROMPT's means. That
    prompt tiers these findings afterwards and the comment prints the
    tally, so a reviewer working to a wider definition reports findings
    the tally then counts as advisory: a pull request told it is being
    shown blockers only, under a list of things Vinegar itself calls
    smaller. The two texts are separate because SEVERITY_PROMPT is a
    measured prompt whose variants did worse and is not worth rewording to
    share a constant, so they are held together by a test that both name
    the same runtime harms instead. Edit neither alone.

    Permission to report nothing is explicit, and it is the load-bearing
    sentence. The measured failure of the severity pass was inflation:
    asked to name the harm behind each tier it invented one for every
    finding and promoted more of them, at 2.4 times the cost. A reviewer
    that believes it must hand something back has the same incentive and
    the whole pull request to find it in.
    """
    return (
        "\n\nThe first %d review%s of this pull request reported "
        "everything %s found, and those findings are on the pull request "
        "already. Every review after %s, this one included, reports only "
        "blockers.\n\n"
        "A blocker is a finding where someone must act before this merges, "
        "and to call one you must be able to name what goes wrong at "
        "runtime for a user or an operator: a wrong result, lost data, a "
        "security hole, a hang, a crash, or a failure that happens "
        "silently. A missing test, a stale comment, a duplicated helper, a "
        "wasted cycle and a clumsy structure are never blockers, however "
        "real and however serious the code they concern, because nothing "
        "at runtime behaves wrongly because of them.\n\n"
        "Read and judge exactly as carefully as you would on any other "
        "pass. Only what you report is narrowed: report every blocker you "
        "find, through the tool named above and by the same rules, and "
        "leave out everything that is not one. Reporting no findings at "
        "all is the expected outcome here, and it is the right answer "
        "whenever you cannot name the runtime harm. Do not promote a "
        "smaller finding so that this pass has something to say."
        % (config["blockers_only_after"],
           "" if config["blockers_only_after"] == 1 else "s",
           "it" if config["blockers_only_after"] == 1 else "they",
           "it" if config["blockers_only_after"] == 1 else "those"))


def clamp(label, body):
    """A comment body GitHub will accept, cut with a note if it has to be.

    Logged as well as marked. On the retry that packs every finding into one
    comment, what gets cut is whole findings off the end, and a parenthesis
    at the bottom of a long comment is not where anyone looks for that.
    """
    if len(body) <= MAX_BODY:
        return body
    log("%s: the comment is %d characters, cut to %d to fit GitHub's limit"
        % (label, len(body), MAX_BODY))
    return body[:MAX_BODY] + "\n\n(cut to fit GitHub's comment limit)"


def cap_spoken(text):
    """The reviewer's own prose, cut to what a comment should carry.

    Applied wherever that prose reaches the pull request, not only where
    it was first read. The stream's version was capped here while the
    result event's went out at whatever length the reviewer wrote, so the
    same "its own words follow unedited" sentence introduced four thousand
    characters or fifty thousand depending on which ending produced them.
    """
    text = text.strip()
    if len(text) <= MAX_SPOKEN:
        return text
    # Said, not silently done. review_body introduces this text with "its
    # own words follow unedited", and a closing summary keeps its
    # conclusion at the end, which is the half a silent cut removes.
    return text[:MAX_SPOKEN] + "\n\n(cut after %d characters)" % (MAX_SPOKEN,)


def stream_lines(text):
    """The stream one line at a time, without a second copy of the whole.

    Exactly `split("\\n")`, lazily. A --verbose stream echoes every tool
    result, so a long review's stdout is already the largest thing the
    daemon holds, and split() doubled it at the worst moment. splitlines()
    is not the same contract: it also breaks on characters the stream's
    newline-delimited JSON never uses as boundaries.
    """
    start = 0
    while True:
        end = text.find("\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def read_stream(stdout, label="review"):
    """The reviewer's findings, and the result event that ends its stream.

    Findings arrive as a ReportFindings tool call rather than as text in the
    final message. That call is the reviewer's own structured output, so
    there is nothing to parse out of prose: no fences to track, no arrays to
    tell apart from arrays it happened to quote, and no way for a `[]` in a
    sentence to be mistaken for the answer. Seven rounds of review found bugs
    in the code that did all that, and this replaces it.

    Also returns what the reviewer said in prose, which is what a killed run
    leaves behind: there is no result event to read the summary out of, and
    the transcript should hold the reviewer's words rather than a slice of
    raw stream.

    Returns findings as None when no call was made, which is different from
    an empty list. None means the reviewer said nothing Vinegar can act on
    and its own words get posted instead; an empty list means it looked and
    found nothing.

    The last call wins. The contract asks for one, and a second would be a
    correction of the first.
    """
    result, findings, spoken = None, None, ""
    for line in stream_lines(stdout):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A single unreadable line must not cost the whole stream: the
            # result event may still be further down it.
            continue
        # A subagent's event is not the review's, whatever its type. Finder
        # subagents are what the default `high` prompt spawns, `Task` is in
        # the allow list, and with --verbose their events arrive here as
        # ordinary events tagged with the tool call that started them.
        # Unfiltered, an assistant event of theirs replaces the review's
        # findings with two candidates from one angle, because the last call
        # wins, and a terminal event of theirs stands in as the ending of a
        # run that was cut off before reaching its own.
        if event.get("parent_tool_use_id"):
            continue
        if event.get("type") == "result":
            result = event
            continue
        # Assistant events only, and the reviewer's own at that. The stream
        # also carries the injected prompt and every tool result as `user`
        # events, and text harvested from those put the review instructions
        # in a transcript that claims to hold what the reviewer said. An
        # earlier pre-filter that string-matched serialized JSON to save the
        # parse let exactly that through, and on this repository it saved
        # nothing: any echoed source file contains "ReportFindings".
        if event.get("type") != "assistant":
            continue
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and block.get("name") == REPORT_TOOL):
                asked = block.get("input")
                reported = asked.get("findings") if isinstance(asked, dict) \
                    else None
                # Every entry an object, because the rest of this file reads
                # them with .get(). One string in the list would raise inside
                # split_findings, and announce() would swallow the review
                # whole rather than lose the one bad entry.
                if isinstance(reported, list) and all(
                        isinstance(item, dict) for item in reported):
                    findings = reported
                else:
                    # A later call is a correction of an earlier one, so
                    # keeping the earlier list means posting findings the
                    # reviewer has superseded. Nothing can be done about that
                    # here, but it must not happen quietly, and it must say
                    # which review it happened to: the daemon interleaves
                    # several repositories into one log.
                    log("%s: a %s call was not shaped like findings and was "
                        "ignored" % (label, REPORT_TOOL))
            elif isinstance(block, dict) and block.get("type") == "text":
                # Only the most recent block is kept. A finished review's
                # closing summary is one block, but a run killed at minute
                # thirty has been narrating throughout, and posting all of it
                # put a wall of "let me read the enclosing function" on the
                # pull request in place of a review.
                # The last *non-empty* block. A trailing empty or
                # whitespace-only one would otherwise wipe the closing
                # summary and leave the pull request told the reviewer said
                # nothing, while its analysis sat one block earlier.
                said = str(block.get("text") or "")
                spoken = said if said.strip() else spoken
    return result, findings, cap_spoken(spoken)


def exec_safe(text):
    """`text`, with everything an argv entry cannot carry taken out.

    A prompt built from a reviewer's findings is handed to
    subprocess.run as an argv entry, and the findings quote a branch
    Vinegar does not trust. Two things there stop the process before it
    starts, and each one costs the severity pass and posts the findings
    untiered with a bare exception name in the log to say why:

    - A NUL. Measured on wonky-flow#95, where the reviewer quoted a
      literal one to report that a name check accepted it: the finding
      was about NUL handling, and the byte it quoted is what stopped it
      being tiered. CPython raises `ValueError: embedded null byte`.
    - An unpaired surrogate. `json.loads` produces one happily from a
      `\\ud800` escape in the reviewer's stream, and exec then raises
      `UnicodeEncodeError: surrogates not allowed`. That subclasses
      ValueError, so in a log it reads much like the first.

    An earlier version of this named NUL and called itself complete. It
    was not, and enumerating hostile inputs is how it got that wrong, so
    this states the property instead: after the round trip the string is
    something utf-8 can encode, and after the replace it holds no NUL.
    Those are exactly the two conditions exec imposes, so anything else
    the reviewer quotes survives untouched.

    A space rather than a deletion for the NUL. Dropping it turns a
    quoted "a\\0.md" into "a.md", which reads as a name that would
    legitimately pass the check the finding is complaining about, and
    this text is what the severity model judges from.

    Only the prompt. Nothing here reaches the pull request, which
    describe() renders from the finding itself, and the review command
    is not exposed at all: reviewer_brief() interpolates the pull
    request number and the base ref name, and git will not put either
    of these in a ref name.
    """
    return text.encode("utf-8", "replace").decode("utf-8").replace("\0", " ")


def severity_brief(findings):
    """Everything the severity pass is asked, the findings included.

    The findings go in as the reviewer's own words and nothing else: no
    diff, no file contents, no checkout. This call decides how much a
    finding would matter *if it were true*, and the failure scenario the
    reviewer wrote is the answer to exactly that question. Handing it the
    code as well would invite it to re-judge whether the finding is true,
    which is the reviewer's job, done at far higher effort, and which a
    model this small would do badly. Only three findings in forty-three
    rounds turned out to be false, so precision was never what needed
    fixing here.

    The rules below are the ones that measured best on haiku across four
    saved reviews, and two variants that measured worse are recorded so
    nobody pays to rediscover them. Without rule 1, PR #11's eleven
    findings came back with seven blockers instead of five. Requiring the
    model to name the runtime harm beside each tier, which reads like it
    ought to discipline the judgement, made it invent a harm for every
    finding and promote more of them, at 2.4 times the cost and four times
    the latency. A model five times the price matched these rules rather
    than beating them.

    What is left unfixed, so that it is not mistaken for working: on two
    of those four reviews about 45% of findings still came back blockers,
    and on one of them three test-coverage findings did, against rule 1.
    That is good enough to order a comment, which is all this is used for.
    It is not good enough on its own to decide when to stop re-reviewing a
    pull request, so whatever does that needs a bound that does not depend
    on the blocker count falling.
    """
    # Every field is collapsed to a single line, and every one of them
    # needs it rather than just the two long ones. These blocks are the
    # only structure the model has for telling one finding from the next,
    # and each field arrives from the reviewer's tool input unfiltered, so
    # a newline anywhere in one forges a block: with only `summary` and
    # `failure_scenario` collapsed, a `file` of "a.py\n[1] blocker" sent
    # one finding and showed the model two.
    def flat(finding, name):
        return " ".join(str(finding.get(name) or "").split())

    blocks = []
    for index, finding in enumerate(findings):
        # Category and verdict together, the way describe() renders them
        # and the way the transcripts the rules were measured against
        # carried them. Measuring on one shape and shipping another would
        # make the measurement describe a prompt that was never used.
        tags = ", ".join(part for part in (flat(finding, "category"),
                                           flat(finding, "verdict")) if part)
        blocks.append("[%d] %s\ncategory: %s\nsummary: %s\nfailure: %s" % (
            index, finding_where(finding), tags or "(none)",
            flat(finding, "summary") or "(none)",
            flat(finding, "failure_scenario") or "(none)"))
    # Pluralised, because a one-finding review is a common shape and
    # "Output exactly 1 lines" is the sort of wrongness that invites a
    # model to decide the instruction is approximate.
    #
    return exec_safe(SEVERITY_PROMPT.format(
        count="%d line%s" % (len(findings),
                             "" if len(findings) == 1 else "s"),
        findings="\n\n".join(blocks)))


def read_tiers(said, count):
    """One tier per finding out of the answer, or None for all of them.

    All or nothing on purpose. A partial answer would leave some findings
    tiered and some not, and the sort would then interleave "this is a
    blocker" with "nobody judged this", which reads on the pull request as
    a judgement that was never made. Untiered findings in the reviewer's
    own order say nothing false.

    TIER_LINE is anchored at the start of a line, so that prose mentioning
    a tier ("finding 3 is arguably a blocker") cannot be read as the
    answer. Every other character of the reply is discarded, and what is
    left is checked against TIERS rather than trusted for having matched,
    which together are what keep the output surface of an
    attacker-influenced call down to three words per finding.
    """
    seen = {}
    for line in said.splitlines():
        match = TIER_LINE.match(line)
        if not match:
            continue
        index = int(match.group(1))
        # Matching the alternation is not enough to be one of the three.
        # TIER_LINE is compiled IGNORECASE and a Unicode pattern under
        # that flag also matches four non-ASCII letters, of which only the
        # Kelvin sign lowers back into the word it came from. Measured on
        # 3.9: `0 advısory` matches and comes out of .lower() still
        # holding the dotless i.
        #
        # What that costs without this line is a whole review. The word is
        # written onto the finding, TIERS.index() in triage()'s sort
        # raises ValueError on it, and that sort is past the except that
        # exists to keep an ordering step from costing a review: nothing
        # is posted, no transcript is saved, and the outcome is still
        # recorded DONE, so with review_on_push off the pull request is
        # never looked at again.
        tier = match.group(2).lower()
        # The first answer for an index wins, and a repeat does not
        # overwrite it. A model that answers the same finding twice has
        # contradicted itself, and the coverage check below is what
        # decides whether the reply is usable at all.
        if tier in TIERS and 0 <= index < count and index not in seen:
            seen[index] = tier
    return [seen[i] for i in range(count)] if len(seen) == count else None


def triage(label, findings, config):
    """The findings, tiered and ordered most severe first.

    Returns them exactly as they arrived, untiered and in the reviewer's
    own order, whenever the severity pass is off, fails, or answers something
    that cannot be read. That is not a fallback so much as the whole
    safety argument: every failure here lands on the behaviour Vinegar had
    before this existed, so none of them is worth costing a finished
    review that is already paid for and sitting in hand.

    Which is why the guard is one broad except, against this file's habit
    of narrow ones. announce() and save_or_log() take the same shape for
    the same reason. What matters is not which failure happened but that
    no failure of an ordering step reaches the caller, and the ways a
    subprocess can fail are open-ended: a `claude` missing from PATH, a
    fork that cannot allocate, a machine with no sandbox, output that is
    not JSON, a model name the CLI rejects.
    """
    chooser = config["severity_model"]
    # The tier is this function's to set, so a finding that arrives
    # already carrying one has it taken away first, before anything
    # renders it and whether or not the pass then runs.
    #
    # `tier` is not in the ReportFindings contract, but read_stream()
    # passes the reviewer's tool input through as it arrived, and the
    # reviewer reads a branch that is free to tell it what to emit.
    # Without this, a finding carrying "tier": "blocker" is printed in
    # bold at the top of its comment and counted in the tally with
    # `severity_model` set to null, which is the documented off switch:
    # a triage verdict no model produced, under a setting the README says
    # leaves findings exactly as they were. Said out loud rather than
    # dropped quietly, because a reviewer emitting keys outside the
    # contract is worth knowing about on its own.
    if findings and any("tier" in finding for finding in findings):
        log("%s: %d finding(s) arrived already carrying a tier, which is "
            "not the reviewer's to set, so it is discarded" % (
                label, sum(1 for f in findings if "tier" in f)))
        findings = [{name: value for name, value in finding.items()
                     if name != "tier"} for finding in findings]

    # `not findings` covers both None, which is a review that reported
    # nothing Vinegar can act on, and an empty list, which is a review
    # that looked and found nothing. Neither has anything to order, and
    # both would otherwise pay for a call to be told so.
    if not chooser or not findings:
        return findings

    # From os.environ rather than the review's env, and stripped anyway.
    # Without a configured App github_env() returns None and every call
    # runs on the ambient environment, which is where an operator's own
    # `GH_TOKEN` for `gh` lives. review() argues this at length for the
    # reviewer; the same reasoning reaches here, because the text this
    # call reads came out of a branch Vinegar does not trust.
    env = dict(os.environ)
    for carried in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(carried, None)

    started = time.monotonic()
    try:
        result = run(["claude", "-p", severity_brief(findings),
                      "--output-format", "json",
                      "--model", chooser,
                      "--settings", json.dumps(TRIAGE_SETTINGS),
                      "--setting-sources", "",
                      "--strict-mcp-config"],
                     timeout=SEVERITY_TIMEOUT, env=env)
        event = json.loads(result.stdout)
        said = str(event.get("result") or "")
        if event.get("is_error"):
            log("%s: the severity pass failed, so findings are posted in "
                "the order they were reported: %s" % (label, said[:200]))
            return findings
        tiers = read_tiers(said, len(findings))
    except Exception as err:
        # The exception's own text is not logged, and that is the point of
        # the three branches. subprocess.TimeoutExpired stringifies the
        # whole command, and this command carries every finding's text,
        # quoted out of a branch Vinegar does not trust, plus the settings
        # JSON: one timeout would put all of it on a single line in a log
        # nothing rotates. review() avoids the same disclosure on the same
        # failure by logging only how long it waited.
        #
        # An OSError keeps its message, because an exec failure names the
        # executable and nothing behind it: "claude is not on PATH" is
        # worth having and cannot carry the prompt. Everything else is
        # named by type, which costs some diagnostic detail and is the
        # trade this makes deliberately.
        if isinstance(err, subprocess.TimeoutExpired):
            why = "it ran longer than %ds" % SEVERITY_TIMEOUT
        elif isinstance(err, OSError):
            why = str(err)
        else:
            why = type(err).__name__
        log("%s: the severity pass did not run, so findings are posted in "
            "the order they were reported: %s" % (label, why))
        return findings

    if tiers is None:
        log("%s: the severity pass did not tier all %d finding(s), so they "
            "are posted in the order they were reported" % (
                label, len(findings)))
        return findings

    tiered = [dict(finding, tier=tier)
              for finding, tier in zip(findings, tiers)]
    # Priced and counted on one line, because this is a second model call
    # per review and an operator totalling what the daemon spends should
    # not have to infer it. Through priced(), which is why review()'s line
    # and this one cannot word it differently.
    log("%s: triaged %d finding(s) in %ds%s: %s" % (
        label, len(tiered), round(time.monotonic() - started), priced(event),
        severity_tally(tiered)))
    # Stable, so findings the pass judged equally serious stay in the order
    # the reviewer reported them, which is the order it thought mattered.
    #
    # A tier this cannot rank sorts last rather than raising, for the
    # reason describe() reads TIER_DOTS with a default: this line is past
    # the except above, so a ValueError here is not an ordering step that
    # failed, it is a finished review that posts nothing and saves no
    # transcript while the outcome is recorded DONE. read_tiers() refuses
    # a tier that is not in TIERS, so the drift this covers is TIERS
    # itself losing a word its second spellings still carry.
    return sorted(tiered, key=lambda finding: TIERS.index(finding["tier"])
                  if finding["tier"] in TIERS else len(TIERS))


def diff_lines(path, base, env, label):
    """The head-side line numbers the pull request's diff covers, per file.

    This decides which findings can be inline comments. The reviews endpoint
    refuses a comment on a line outside the diff, and it applies the whole
    review or none of it, so a single badly anchored comment would throw away
    every finding alongside it.

    Three lines of context, because that is what GitHub shows and therefore
    what it accepts a comment on. At `--unified=0` the ranges held only added
    lines, so a finding about an unchanged line two below an edit, which
    GitHub would have taken happily, was demoted to the general comment for
    no reason. A hunk that contributes no head-side line at all still reports
    `+n,0`, an empty range, and correctly yields nothing.

    The prefixes are pinned rather than assumed. `diff.noprefix` and
    `diff.mnemonicPrefix` are both real settings that change what precedes
    the path, and reading a fixed two characters off the front under either
    of them would truncate every path and send every finding to the general
    comment.

    `+++ ` only counts between `diff --git` and the first hunk. An added
    line whose own text begins with `++ ` renders as `+++ ...` and looks
    exactly like a file header, at any context width; the `doc.md` fixture
    in the tests is that line, so deleting this guard turns the suite red
    rather than quietly retargeting hunks at a path that does not exist.
    """
    # Every one of these pins something the repository under review could
    # otherwise decide for it, and the diff has to describe the file as
    # GitHub will see it.
    #
    # core.quotepath is on by default and prints a non-ASCII path as
    # `"b/caf\303\251.py"`, quotes and all, which matches no finding's `file`
    # and would route every finding in that file to the general comment. A
    # path holding a quote or a backslash is still escaped even so, and still
    # degrades that way; that is the safe direction and the rarer case.
    #
    # --no-textconv and --no-ext-diff because a `.gitattributes` in the pull
    # request can name a converter, and nbdime on `*.ipynb` is a normal thing
    # to find. The hunk headers then count lines in the converted text, which
    # can still land inside GitHub's range for that file, so the review is
    # accepted and the comments attach to whatever happens to be at those
    # numbers. Wrong lines, confidently placed, are worse than no lines. An
    # external driver does not print a unified diff at all, which is the
    # harmless half of the same setting.
    # refs/heads/, because a bare name lets git resolve refs/tags first. A
    # repository with a release branch `v2` and a tag `v2` would diff against
    # the frozen tag, git would say only `warning: refname is ambiguous` on
    # stderr and exit 0, and every anchor would be computed against the wrong
    # range. checkout() creates the branch under refs/heads, so this names it.
    #
    # Timed out, because submit_review's docstring says every call out of
    # this process is, and the poll loop is one thread. A checkout on a
    # filesystem that stops answering would otherwise wedge it silently.
    try:
        result = run(["git", "-c", "core.quotepath=false", "-c",
                      "color.ui=false", "diff", "--unified=3", "--no-color",
                      "--no-textconv", "--no-ext-diff",
                      "--src-prefix=a/", "--dst-prefix=b/",
                      "refs/heads/%s...HEAD" % base], cwd=path,
                     env=env, timeout=DIFF_TIMEOUT)
    except subprocess.TimeoutExpired:
        result = None
    if result is None or result.returncode != 0:
        # Every finding is about to be routed to the general comment. Say why
        # here, because the comment itself can only report the effect.
        log("%s: cannot diff refs/heads/%s...HEAD, no finding can be "
            "anchored: %s" % (label, base,
                              "timed out" if result is None
                              else result.stderr.strip()[:200]))
        return {}

    # stream_lines, not splitlines(): a lone CR must stay inside its line,
    # or an added line holding one is cut into a fragment that can look
    # like a hunk header, putting line numbers into `covered` that the diff
    # never touched. stream_lines carries that contract for the reviewer's
    # stream too; one rule, one place.
    covered, name, heading = {}, None, False
    for line in stream_lines(result.stdout):
        if line.startswith("diff --git "):
            name, heading = None, True
        elif heading and line.startswith("+++ "):
            target = line[4:]
            # /dev/null is a delete, and there is no head-side file to
            # comment on. The b/ prefix is git's, not part of the path.
            # rstrip("\t"), because git appends a tab to this header for any
            # path containing a space. Without it the key carries the tab,
            # no finding's `file` can match, and every finding in that file
            # is silently demoted to the general comment.
            name = None if target == "/dev/null" else target[2:].rstrip("\t")
        elif name and line.startswith("@@"):
            heading = False
            hunk = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            if hunk:
                start, count = int(hunk.group(1)), int(hunk.group(2) or 1)
                if count:
                    covered.setdefault(name, set()).update(
                        range(start, start + count))
    return covered


def repo_path(name, root):
    """The finding's file as the reviews endpoint wants it, or None.

    Relative to the repository root. Reviewers have reported absolute paths
    into the checkout despite being asked not to, and a finding is worth more
    routed to the general comment than dropped, so anything that does not
    resolve inside the checkout returns None rather than raising.

    normpath because `./vinegar.py` and `src//app.py` both name a file that is
    in the diff and neither matches the key git prints for it. The absolute
    branch is normalised by relpath already.
    """
    # A NUL byte is refused before anything stats the path, because what it
    # does otherwise depends on the Python. On 3.9 realpath swallows the
    # lstat ValueError inside islink() and hands the byte back, riding it
    # into a comment body; on 3.10+ the same ValueError escapes realpath
    # and would surface inside announce(), which discards the whole review.
    # The swallow was measured on 3.9.6; the raise is 3.10's _joinrealpath
    # catching only OSError. Neither byte belongs in a routing decision.
    if not isinstance(name, str) or not name.strip() or "\x00" in name:
        return None
    name = name.strip()
    # realpath both sides, as check_paths() already does for its own check.
    # A checkout directory reached through a symlink resolves one way for the
    # reviewer's tools and another for this comparison, and every absolute
    # path it reports would then look like it sits outside the checkout.
    #
    # Wrapped because the docstring promises None for anything that does not
    # resolve, and realpath and relpath keep finding new ways to raise: one
    # poisoned `file` must not cost the run's other findings their posting.
    try:
        name = (os.path.relpath(os.path.realpath(name), os.path.realpath(root))
                if os.path.isabs(name) else os.path.normpath(name))
    except (OSError, ValueError):
        return None
    # The component, not the two characters. `..config.py` is an ordinary
    # file name and rejecting it would send a perfectly anchorable finding to
    # the general comment.
    if name == ".." or name.startswith(".." + os.sep):
        return None
    return name


def finding_line(finding):
    """The line a finding anchors to, as an int, or None.

    A model asked for `line` answers with `812`, `"812"` or `812.0`
    interchangeably. Only the first anchors an inline comment, and the other
    two would otherwise cost the finding both its anchor and, in the general
    listing, the line number that says where to look.

    bool is an int to Python and True would anchor at line 1, so it is not
    one of the answers accepted here.
    """
    line = finding.get("line")
    if isinstance(line, bool):
        return None
    # A fraction is not a line number. int() floors it, which anchored
    # the comment one line above the code the finding described — a
    # wrong line, confidently placed, which diff_lines' own docstring
    # calls worse than no line at all.
    if isinstance(line, float) and not line.is_integer():
        return None
    try:
        return int(line)
    except (TypeError, ValueError, OverflowError):
        # OverflowError because json.loads accepts the non-standard
        # `Infinity` and `1e400`, and int(float("inf")) raises it. Uncaught,
        # one bad line number in one finding took the whole review with it.
        return None


def finding_where(finding):
    """Where a finding points: `file:line`, `file`, or `(no file)`.

    One definition, because three places render it now and the last time
    two of them built it separately they disagreed: a `file` of only
    spaces came out as empty backticks in one and `(no file)` in the
    other, for the same finding. The third is the severity prompt, where
    a divergence would change which finding the model thinks it is
    tiering.

    Whitespace inside the name is collapsed rather than only trimmed. The
    name is the reviewer's tool input, which read_stream() checks for
    being a dict and nothing else, and a newline in it forged an extra
    numbered block in the severity prompt: one finding went in and the
    model saw two, so the indices it answers against stopped matching the
    findings it was given.
    """
    where = " ".join(str(finding.get("file") or "").split()) or "(no file)"
    line = finding_line(finding)
    return "%s:%d" % (where, line) if line is not None else where


def finding_bullet(finding):
    """A finding as one markdown bullet, for the comment and the transcript.

    Both need it and they used to build it separately, which was enough for
    them to disagree: a `file` of only spaces rendered as empty backticks in
    one and `(no file)` in the other, for the same finding.
    """
    where = finding_where(finding)
    # Runs of blank lines collapse first. Replacing "\n\n" alone left a
    # whitespace-only line behind whenever the reviewer's own prose ended
    # a field with a blank line, and GitHub reads two spaces on their own
    # as the end of the list item: the failure scenario and the category
    # rendered at column zero, detached from the finding they belong to.
    body = re.sub(r"(\n[ \t\r]*){2,}", "\n  ", describe(finding))
    return "- `%s`: %s" % (where, body)


def describe(finding):
    """A finding as prose, without the file and line that anchor it.

    The category comes through because it is most of what the tool call
    carries that text never did, and it is the difference between a finding
    that can break the change and one that is taste. The README promises it,
    and until now nothing rendered it anywhere.
    """
    # `or ""` rather than a get default, because these keys arrive present
    # and null often enough, and a default only covers a key that is absent.
    # str(None) is "None", which reads as a finding that says None.
    summary = str(finding.get("summary") or "").strip() or "(no summary)"
    scenario = str(finding.get("failure_scenario") or "").strip()
    category = str(finding.get("category") or "").strip()
    # The tier opens the comment, because that is the whole of what it
    # buys. Every finding renders through here, inline comments and the
    # general list alike, and a reader facing nine or thirteen of them
    # decides which to open from the first few characters. Below the
    # summary it would be a label nobody reaches until they have already
    # read the thing it was meant to help them skip.
    #
    # The word stays beside the dot rather than being replaced by it. The
    # color is what a reader picks the comment out by; the word is what
    # tells them which tier it is without knowing the three colors, and it
    # is all a screen reader has, because the dot is announced by its own
    # name and not by what it means here.
    #
    # Absent when the severity pass is off or did not answer, and then
    # this reads exactly as it did before tiers existed. read_tiers()
    # tiers all of the findings or none, so a comment never sits beside
    # one that was judged and says nothing.
    #
    # A default and not a subscript, like every other field here. This
    # runs with the review finished and paid for, on the path that posts
    # and the path that saves the transcript, so a KeyError on a tier that
    # drifted out of TIER_DOTS would lose the review whole while the
    # outcome was still recorded DONE.
    #
    # The space travels with the dot rather than sitting in the format,
    # which would leave a comment that lost its dot opening on a stray
    # space instead.
    tier = str(finding.get("tier") or "").strip()
    if tier:
        dot = TIER_DOTS.get(tier)
        summary = "%s**%s** · %s" % (dot + " " if dot else "", tier, summary)
    # The verdict rides with the category when the effort level ran a verify
    # pass. CONFIRMED and PLAUSIBLE read very differently, and posting them
    # identically claims a certainty the reviewer did not.
    verdict = str(finding.get("verdict") or "").strip()
    body = "%s\n\nFailure: %s" % (summary, scenario) if scenario else summary
    tags = ", ".join(part for part in (category, verdict) if part)
    return "%s\n\n(%s)" % (body, tags) if tags else body


def severity_tally(findings):
    """The tier counts as one phrase, or "" when nothing carries a tier.

    Counted off the findings themselves and not off `inline` and
    `general`. split_findings() turns an anchored finding into a GitHub
    comment payload, which has no room for a tier and drops it, so
    counting the two halves would report only the findings that failed to
    anchor: on a clean review, "3 findings (0 blockers)" under three
    inline comments, one of which is a blocker.

    Zero counts are left out. Naming a tier no finding reached says
    nothing and makes the common shape, all of them advisory, read like a
    table.
    """
    counted = [(tier, sum(1 for finding in findings or ()
                          if finding.get("tier") == tier)) for tier in TIERS]
    return ", ".join("%d %s" % (count, tier)
                     for tier, count in counted if count)


def below_blocker(findings):
    """Whether anything here is tiered under `blocker`.

    Only the narrowed comment asks, and only to explain a tag the reader
    would otherwise read as a broken promise. A pass told to report
    blockers only hands back what it judged to be blockers; the severity
    pass then tiers those findings on its own, from one summary line with
    no code in front of it. Measured disagreeing on
    `Sour-Labs/wonky-flow#107` round three, which reported one finding and
    had it tiered `advisory`.

    Read off TIERS rather than from the two tier names, because TIERS is
    ordered most severe first and is the one place that ordering is
    written down, so a tier added at either end is covered without this
    being a site someone has to remember.

    From `blocker`'s own index and not a literal 1. `TIERS[1:]` means
    "everything but the most severe", which is the same set today and
    stops being it the day a tier is added above `blocker`: the slice
    would then include `blocker` itself, and every narrowed round where
    the two passes agreed would explain a disagreement over a row of red
    dots. A check reorders TIERS and holds this to the name.

    Defaulted rather than raising when `blocker` is not there at all,
    which is triage()'s sort's rule and for the reason argued there: this
    runs inside save_transcript() and inside post_review(), so a
    ValueError is a finished review that writes no transcript and reaches
    no pull request while the outcome is recorded DONE. An empty tuple
    means no finding is under `blocker`, which is the behaviour this
    function was added to, and it costs an explanation nobody can act on
    rather than the review that paid for it.

    Off the findings for severity_tally()'s reason: half of them are
    GitHub comment payloads by the time the comment is built, and those
    carry no tier.
    """
    under = TIERS[TIERS.index("blocker") + 1:] if "blocker" in TIERS else ()
    return any(finding.get("tier") in under for finding in findings or ())


def pr_key(repo, pr):
    """How one pull request is named in the state file and the log."""
    return "%s#%s" % (repo, pr["number"])


def relative_findings(findings, root):
    """The findings with every `file` made repo-relative where it resolves.

    Done once, above everything that renders them. Reviewers report
    absolute paths into the checkout — repo_path exists for that — and
    normalising only where a finding is anchored left three other routes
    to the pull request carrying the daemon's own directory layout: the
    anchorless retry, the transcript, and the repost that sends that
    transcript.
    """
    if not findings:
        return findings
    out = []
    for finding in findings:
        name = repo_path(finding.get("file"), root)
        out.append(dict(finding, file=name) if name else finding)
    return out


def split_findings(findings, covered, label):
    """Route each finding to an inline comment or to the general comment.

    The path is taken as it arrived. finish() has already put every
    `file` through repo_path, above the transcript and every posting
    path, so resolving it a second time here would be a second place to
    keep one rule in step. Nothing is lost by trusting it: `covered` is
    keyed on the names git itself printed, so anything absolute, escaping
    or unresolvable simply fails to match and goes to the general
    comment, which is where it belonged.
    """
    inline, general = [], []
    for finding in findings:
        name = str(finding.get("file") or "").strip()
        line = finding_line(finding)
        if name and line is not None and line in covered.get(name, ()):
            # Capped like the top-level body. GitHub applies the same ceiling
            # to a comment, and one overlong finding refused there takes the
            # whole review with it.
            inline.append({"path": name, "line": line, "side": "RIGHT",
                           "body": clamp(label, describe(finding))})
        else:
            general.append(finding)
    return inline, general


def review_heading(pr, config, verb="reviewed"):
    """The first line of everything Vinegar posts.

    already_posted() recognises Vinegar's own work by matching the start
    of it, so the shape has one definition: the repost path spelled it out
    again and the two could have drifted into saying different things
    about the same commit.
    """
    return "%s at %s effort" % (posted_mark(pr, verb), config["effort"])


def overflow_note(dropped):
    """What the comment says about the findings that did not fit."""
    return ("%d finding(s) did not fit GitHub's comment limit and are only "
            "in the transcript." % dropped)


def review_body(label, pr, config, inline, general, raw=None,
                heading="These could not be anchored in the diff:", note=None,
                verb="reviewed", tally="", since=None, blockers=False,
                disagreed=False):
    """The review's top-level comment.

    Always present, and not only because the endpoint requires one for a
    COMMENT review. It is what makes the comments arriving mean the round is
    over: a pull request Vinegar reviewed says so, including when the answer
    was that there is nothing to report. Silence has to keep meaning that
    something went wrong.

    `heading` is a parameter because the retry path lists findings that could
    be anchored perfectly well and were refused for some other reason. Saying
    they could not be anchored there would send whoever is debugging to
    diff_lines and the base branch instead of to the error submit_review
    logged.
    """
    # `verb` because the give-up posts through here too, and its first line
    # used to claim a review happened when none ever ran. The marker itself
    # stays fixed: already_posted() matches it as a prefix.
    lines = [review_heading(pr, config, verb)]

    # What this pass actually read, whenever that is not the whole pull
    # request. Without it "no findings" on a re-review is ambiguous in the
    # one way that matters: it reads as the change being clean when it may
    # only mean the last forty lines were. Said before the note below,
    # because it frames everything under it, the note included.
    if since:
        lines += ["", "This pass reviewed only what was added since `%s`, "
                      "which is where the last review of this pull request "
                      "finished. Earlier findings are already on the pull "
                      "request as their own comments." % since[:7]]

    # The other half of what "no findings" means here, and it has to be
    # said for the same reason the scope line is: without it a quiet
    # re-review reads as the change being clean when it only means nothing
    # in it would break at runtime. Said after the scope line, because a
    # reader needs to know what was read before what was reported from it.
    #
    # It names the number rather than saying "later reviews", so the
    # author can tell whether this is Vinegar's rule or something someone
    # configured for this repository, without reading the daemon's config.
    if blockers:
        # Every clause here is what the pass was asked for, and none of it
        # is what came back. The severity pass runs afterwards and is
        # independent, so it can tier a finding this review reported as
        # `advisory` or `note`, and the tally a few lines below then reads
        # "1 finding (1 advisory)" under this paragraph.
        #
        # The last sentence used to be "Anything smaller it found is not
        # listed here", which is the one claim on this comment that
        # Vinegar cannot keep. Nothing filters what the reviewer hands
        # back, deliberately, because the reviewer read the code and the
        # pass that tiers it did not. What is listed here is whatever
        # came back, and on `Sour-Labs/wonky-flow#107` round three that
        # was one finding tiered `advisory` under a sentence promising
        # none. Saying what the reviewer was told is true whatever comes
        # back.
        #
        # Number agreement, because `blockers_only_after: 1` is a valid
        # setting and nothing else in this program prints "1 findings".
        after = config["blockers_only_after"]
        lines += ["", "The first %s of a pull request %s everything %s "
                      "find%s. This is a later one, so it was asked for "
                      "blockers only: findings where something goes wrong "
                      "at runtime. Anything smaller it found, it was told "
                      "to leave out." % (
                          "review" if after == 1 else "%d reviews" % after,
                          "reports" if after == 1 else "report",
                          "it" if after == 1 else "they",
                          "s" if after == 1 else "")]

        # Only when there is a tag to explain. A reader who sees a blue
        # dot under the paragraph above has no way to know a second pass
        # exists, so the tag reads as Vinegar showing what it just said it
        # would not. Said whenever the two disagree and never otherwise,
        # because on the ordinary narrowed round, nothing found or
        # everything tiered `blocker`, it answers a question nobody has.
        if disagreed:
            # The constant, not a copy: save_transcript() writes the same
            # sentences for the resend path, and this pull request is
            # about what happens when one sentence is spelled in two
            # places. DISAGREED_SAID says why it reads "on each finding"
            # rather than "below".
            lines += ["", DISAGREED_SAID]

    # A run that was killed or that errored still reports whatever it had
    # got to, and without this that is indistinguishable from a finished
    # review. The comments arriving is the signal that the round is over and
    # the feedback is complete enough to act on, so a partial round has to
    # say it is one.
    if note:
        lines += ["", note]

    total = len(inline) + len(general)
    if raw is not None and not raw.strip():
        # Distinct from unreadable output, because the two send you to
        # different places: this one to whether the reviewer ran at all.
        # Under a note the note stands alone. It already says how the run
        # ended and how to read that, and the sentence this branch used to
        # add described a single run that stopped, which the give-up after
        # MAX_ATTEMPTS, arriving here with the same empty text, is not.
        if not note:
            lines += ["", "The review finished without saying anything. "
                          "There are no findings to show and no words to "
                          "quote, which means the run produced nothing, not "
                          "that the change is clean."]
    elif raw is not None:
        # Note-aware like the branches around it. Under a note saying the run
        # was killed, "did not return its findings in a form Vinegar could
        # read" names the wrong cause and sends the reader to check the three
        # coupled settings when the answer is a longer review_timeout.
        lines += ["", "It did not reach the point of reporting findings, so "
                      "its own words follow unedited." if note else
                      "The reviewer did not return its findings in a form "
                      "Vinegar could read, so its own words follow unedited.",
                  "", "---", "", raw.strip()]
    elif not total:
        # Never "No findings." on a run that did not finish: it did not look
        # at everything, so it is not entitled to say the change is clean.
        lines += ["", "It reported nothing before it stopped." if note
                  else "No findings."]
    else:
        # `tally` is passed in rather than counted here, because half the
        # findings have already become GitHub comment payloads by the time
        # this runs and those carry no tier. severity_tally() says so at
        # more length.
        lines += ["", "%d finding%s%s, %d posted inline." % (
            total, "" if total == 1 else "s",
            " (%s)" % tally if tally else "", len(inline))]

    if general:
        bullets = [finding_bullet(finding) for finding in general]
        # Whole findings come off the end when the body cannot fit, said
        # out loud, with clamp() left as the last resort only. Its
        # character cut shears mid-bullet and logs a byte count, which
        # loses the tail findings with nothing on the pull request saying
        # more existed. The transcript holds every finding regardless of
        # what fits here.
        #
        # Measured by arithmetic rather than by re-joining the body on
        # every pop: thirty long findings trimmed twenty times copied
        # megabytes of string on a poll thread to learn a length.
        fixed = len("\n".join(lines + ["", heading, ""]))
        running = sum(len(bullet) + 1 for bullet in bullets)
        dropped = 0
        # +2, not +1: the note is appended as ["", note], so joining costs
        # a newline for the blank line and another before the note itself.
        # Budgeting one left a body that could land on MAX_BODY + 1 and
        # fall into clamp(), which shears the note it just wrote and adds
        # a second, contradicting truncation notice.
        while bullets and fixed + running + (
                len(overflow_note(dropped)) + 2 if dropped else 0) > MAX_BODY:
            running -= len(bullets.pop()) + 1
            dropped += 1
        if dropped:
            log("%s: %d finding(s) did not fit the comment and are only in "
                "the transcript" % (label, dropped))
        # The heading only when something follows it. With every bullet
        # dropped it introduced nothing but the note saying they were.
        if bullets:
            lines += ["", heading, ""] + bullets
        if dropped:
            lines += ["", overflow_note(dropped)]

    return clamp(label, "\n".join(lines))


def check_api(label, repo, path, method, payload, env):
    """One Checks API call. Answers what it replied, or None if it failed.

    None rather than an exception, and every failure logged and swallowed,
    because nothing here is worth a review. The check run is a progress
    indicator: it tells a human the reviewer is working and then that it
    stopped. A review that runs and posts with no indicator beside it is a
    worse pull request, not a broken one.

    Bounded on POST_TIMEOUT, which is the same shape of call for the same
    reason: one request on the repository's poll thread, with a finished
    review waiting behind it, and a socket that never answers is not an
    error anyone raises.
    """
    # One condition for both, because they are one decision. Written as
    # `is not None` for the flag and truthiness for the body, a caller
    # passing `{}` told `gh` to read a request body from a stdin run()
    # had already pointed at /dev/null, and the parse error it died of
    # named nothing that would lead anyone back here.
    body = json.dumps(payload) if payload is not None else None
    cmd = ["gh", "api", "repos/%s/%s" % (repo, path), "--method", method]
    if body is not None:
        cmd += ["--input", "-"]
    try:
        result = run(cmd, env=env, timeout=POST_TIMEOUT, stdin_text=body)
    except Exception as err:
        log("%s: the checks call did not run: %s" % (label, err))
        return None
    if result.returncode:
        said = both_streams(result, 300)
        # The permission is named because this is the one failure an
        # operator can fix and will otherwise see once per review with no
        # idea what to do about it. `checks: write` is not granted by
        # default, and adding it to the App is only half of it: GitHub
        # holds the change until the installation accepts it.
        if "HTTP 403" in said:
            log("%s: the check run needs the App's `checks: write` "
                "permission, which must also be accepted on the "
                "installation: %s" % (label, said))
        else:
            log("%s: the checks call failed: %s" % (label, said))
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        # It answered 2xx, so whatever it created exists. Only the reply
        # was unreadable, and the caller wants an id it will not get.
        return {}


def running_checks(label, repo, sha, config, env):
    """The ids of every check run this App has spinning at `sha`.

    Two callers ask the same question for opposite reasons, which is why
    it is one function. open_check() asks so that a retry reuses the
    indicator an earlier attempt left rather than adding a second one;
    sweep_checks() asks so that a startup closes the ones nobody is coming
    back for. Written out twice they could disagree about which runs are
    Vinegar's, and the way that shows is a sweep closing another App's
    check run.

    `.get("id")` as well as the app, not just membership. A reply with no
    id, from a truncated body or one check_api answered as `{}`, would
    otherwise build a handle whose every close sends
    `PATCH check-runs/None`. open_check()'s create branch refuses exactly
    that, and this was the one place the guard was missing.

    Compared as strings, because app_jwt() already establishes that this
    value need not be an int: it signs with `str(app_id)`, and load_config
    type-checks only the three numeric settings. A quoted
    `"app_id": "123456"` therefore mints tokens perfectly and matched
    nothing here, so reuse silently stopped and the spinning duplicates it
    exists to prevent came back with nothing logged.

    The App is not enough on its own, and `DEPLOYMENT` is the rest of it.
    Two Vinegars on one machine under different VINEGAR_HOMEs
    authenticate as the same App, so their runs are indistinguishable on
    the wire, and both of the things this function serves are wrong across
    that line: a sweep closing the other one's live indicator, and a reuse
    adopting a run another process is still writing to. The lock cannot
    separate them, because it is per home. This can, because open_check()
    stamps the home that made the run.

    A run carrying no `external_id` at all is treated as this
    deployment's. Those are the ones written before the stamp existed, and
    refusing them would leave every check run open at the moment of the
    upgrade stranded for good: never adopted, never swept, spinning on
    a pull request nothing will come back to. Droppable once no such run
    can still be open, the way LIFTED_MARKS is.
    """
    said = check_api(
        label, repo,
        "commits/%s/check-runs?check_name=%s&status=in_progress"
        % (sha, CHECK_NAME), "GET", None, env)
    # None for a call that did not answer, an empty list for one that
    # answered nothing. open_check() cannot tell them apart and does not
    # need to, because both mean "no run to adopt" and it creates one
    # either way. sweep_checks() does: a repository whose reads are all
    # failing is one it stops asking about, and read as "no runs" that is
    # the check_api permission paragraph logged once per open pull
    # request on every start.
    if said is None:
        return None
    return [was.get("id") for was in said.get("check_runs") or []
            if str((was.get("app") or {}).get("id"))
            == str(config["github_app"].get("app_id")) and was.get("id")
            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]


def open_check(label, repo, pr, config, env, blockers=False):
    """Say in the pull request's checks that a review is running.

    Answers a handle to close afterwards, or None when there is nothing to
    show and no call worth making.

    Only with a GitHub App. A check run belongs to the App that created it
    and no user token can own one, so on the ambient `gh` login this would
    be a 403 per review telling the operator to fix something they cannot.
    And only when Vinegar is posting at all: a dry run puts nothing on the
    pull request, and an indicator is something on the pull request.
    """
    if not config["comment"] or not config.get("github_app"):
        return None
    sha = pr["headRefOid"]

    # An indicator an earlier attempt left spinning is reused rather than
    # joined by a second one. A review is killed mid-flight often enough
    # to matter: stopping the daemon during one is a documented step for
    # restarting it, the process is recorded FAILED before the review
    # runs, and MAX_ATTEMPTS then brings the same head back twice more.
    # Creating a run each time would leave the pull request listing three
    # checks called Vinegar, two of them running for ever.
    #
    # Only a running one, and that is a limit of the API rather than a
    # choice. A completed check run cannot be moved back to in_progress:
    # measured, the PATCH answers 200 and changes nothing at all, which
    # is the worst way for it to refuse. So an attempt that ended and is
    # then retried does add a second entry, and a pull request whose
    # review fails twice before succeeding lists three. That is honest
    # history rather than a defect, and trying to collapse it is the
    # thing that silently does not work.
    mine = running_checks(label, repo, sha, config, env)
    if mine:
        log("%s: reusing the check run an earlier attempt left running"
            % label)
        return {"repo": repo, "id": mine[0], "closed": False}

    # The narrowing is in the title as well as in the comment, because the
    # checks list is the half of this an agent reads. `gh pr checks`
    # returning a review that reported nothing means one thing on a first
    # pass and another on a fifth, and the comment saying so does not
    # reach anything polling the check.
    asked = {"name": CHECK_NAME, "head_sha": sha, "status": "in_progress",
             "started_at": utc_stamp(),
             # Which Vinegar this run belongs to. DEPLOYMENT says why it
             # has to be written at creation: a run with no stamp is
             # readable by every instance as its own, and that is the
             # state every run made before this existed is in.
             "external_id": DEPLOYMENT,
             "output": {
                 "title": "Reviewing at %s effort%s" % (
                     config["effort"], ", blockers only" if blockers else ""),
                 "summary": "Vinegar is reviewing this commit. The findings "
                            "arrive as one review when it finishes."}}
    # Only when there is one. An empty `details_url` is not a URL and
    # GitHub judges the whole request on it.
    if pr.get("url"):
        asked["details_url"] = pr["url"]
    made = check_api(label, repo, "check-runs", "POST", asked, env)
    # An id or nothing. A handle without one cannot be closed, and
    # pretending otherwise would send a PATCH to `check-runs/None`.
    return {"repo": repo, "id": made["id"], "closed": False} \
        if made and made.get("id") else None


def ended_title(outcome, attempts=0):
    """What the checks list says about a review that reached no ending.

    One definition, called from both finallys. Written out twice, only
    handle_pr's copy was anchored by a mutation, so reworded or inverted
    text in the `--pr` path was caught by nothing.

    Never "the review finished" for a DONE outcome. review() answers DONE
    whenever the subscription was spent, which includes the endings where
    announce() swallowed a raise and nothing reached the pull request at
    all. finish() closes the indicator itself on every ending that did
    post, so a caller reaching for this with the indicator still open is
    in the case where the posting is exactly what did not happen. Saying
    it finished would be the same false all-clear the `clean` line in
    finish() refuses the tick for. This path never passes a conclusion, so
    it takes the grey default, which is the right answer for every ending
    that reaches it.
    """
    if outcome == FAILED and attempts >= MAX_ATTEMPTS:
        return "The review failed %d times and was given up on" % attempts
    if outcome == FAILED:
        return ("The review failed and will be tried again" if attempts
                else "The review failed")
    return "The review ran but nothing reached the pull request"


def close_check(label, check, title, env, summary="",
                conclusion=CHECK_CONCLUSION):
    """Finish the indicator, whatever ended the review.

    The credentials come in here rather than riding along on the handle,
    for two reasons that both bit. A handle holding a token is one debug
    log line away from publishing it, and this program prints finding text
    to public pull requests for a living; printing one handle during
    development put a live installation token in a terminal, and it had to
    be revoked. And the token the review started on can be older than a
    long review: the caller closest to the ending knows which credentials
    are current, and finish() hands over the fresh ones it just minted to
    post with.

    `neutral` unless the caller says otherwise, and never `failure`:
    CHECK_CONCLUSION and CHECK_CLEAN say why at length. Only finish()
    passes anything else, because it is the only caller that knows both
    what was found and whether it landed. The title carries the rest, so
    it says what happened rather than how it feels about it.

    Closed on the handle only once it is really closed. finish() closes
    it with the tally and the caller closes it again as a backstop, so
    marking it up front made a refusal final: one 401, from a token that
    outlived a long review, left the run in_progress on the pull request
    for ever, and a stuck run blocks a merge anywhere Vinegar is a
    required check, which is the outcome CHECK_CONCLUSION exists to make
    impossible. Marked afterwards, the backstop gets its turn, holding
    credentials of its own.
    """
    if not check or check["closed"]:
        return
    # What the first attempt tried to say wins over what a later one
    # brings. `closed` stays False when the PATCH is refused, so that the
    # backstop can retry it, and the backstop works its title out from
    # "the indicator is still open", which it reads as "the posting never
    # happened". For a review that posted and then failed only to say so,
    # that inference is exactly backwards: the tally finish() wrote was
    # replaced by "nothing reached the pull request", on a pull request
    # visibly carrying the review, and the retry's fresh credentials made
    # the wrong answer the one that stuck.
    title = check.get("said") or title
    summary = check.get("summary") or summary
    # The conclusion travels with the title it belongs to, and for the same
    # reason. A clean review whose PATCH was refused is retried by a
    # backstop that knows only "the indicator is still open", so without
    # this a green review would come back grey under a title still saying
    # it found nothing.
    conclusion = check.get("conclusion") or conclusion
    check["said"], check["summary"] = title, summary
    check["conclusion"] = conclusion
    settled = check_api(
        label, check["repo"], "check-runs/%s" % check["id"], "PATCH", {
            "status": "completed", "conclusion": conclusion,
            "completed_at": utc_stamp(),
            # GitHub refuses a title over 255 characters and would refuse
            # the whole update with it, leaving the indicator running.
            "output": {"title": title[:255], "summary": summary or title}},
        env)
    check["closed"] = settled is not None


def submit_review(label, repo, pr, payload, env):
    """Post one review, as one request, and say whether it landed.

    Bounded, like the listing and the diff on the same thread. `run()` waits
    for as long as the far end takes, and a socket that is open but never
    answers, which is what a black-holed connection or a proxy holding the
    request looks like, is not an error anyone raises. The poll loop is one
    thread: it would sit here, no repository would be polled, and the watchdog
    would see a live pid producing no log lines and call that healthy. The
    clone and fetch in checkout() get far longer bounds than this, for the
    reasons written beside each, but they are bounded too.
    """
    try:
        result = run(["gh", "api",
                      "repos/%s/pulls/%d/reviews" % (repo, pr["number"]),
                      "--method", "POST", "--input", "-"],
                     env=env, timeout=POST_TIMEOUT,
                     stdin_text=json.dumps(payload))
    except subprocess.TimeoutExpired:
        # UNSURE, not REFUSED, and the difference is the whole point. The
        # request may well have arrived: the timeout is on this side, and
        # GitHub can have accepted the review and been slow to say so.
        # Resending blind posts the review twice.
        log("%s: posting the review timed out after %ds, so it may or may "
            "not have landed" % (label, POST_TIMEOUT))
        return UNSURE
    if result.returncode == 0:
        return POSTED
    said = both_streams(result, 600)
    log("%s: posting the review failed: %s" % (label, said))
    # A rate limit is a refusal that says "not now" rather than "not like
    # that", and it is the one refusal where trying again in the same
    # millisecond is certain to fail. GitHub answers 403 for the secondary
    # limit and 429 for the primary, so the status alone does not separate
    # it from a validation error: the message does.
    if re.search(r"HTTP (403|429)", said) and re.search(
            r"rate limit|too many requests|abuse|secondary", said, re.I):
        return THROTTLED
    # Any other 4xx is GitHub judging the request and creating nothing, so
    # the caller may change the request and retry without first asking
    # whether it landed. Anything else, a 5xx or a summary with no status
    # in it at all, may have landed after the error was already on the
    # wire: the timeout's ambiguity, spelled differently.
    return REFUSED if re.search(r"HTTP 4\d\d", said) else UNSURE


def posting_env(label, config, repo, tokens, fallback):
    """Credentials for the posting: a fresh token, or the review's own.

    Minting here is a live API call, and the endpoint it calls is one
    handle_pr() already documents as prone to transient 5xx. Letting that
    decide the fate of a finished review is the wrong trade: the token the
    run has been using is in scope and, at any `review_timeout` short of the
    hour a token lives, usually has time left on it. A one-second network
    blip should not cost a review that is sitting there ready to post.
    """
    if not config["comment"]:
        return None
    try:
        return github_env(config, repo, tokens, good_for=POST_GRACE)
    except Exception as err:
        log("%s: could not mint a token to post with (%s), using the one the "
            "review ran on" % (label, err))
        return fallback


def announce(label, post):
    """Run the posting step so that nothing it does can escape review().

    Returns what the posting answered, and False when it raised, so a
    caller that records "this was said" cannot record it about an
    exception.

    Not defensiveness about the posting: defensiveness about what raising
    here would cost. By this point the subscription is spent and the
    transcript is on disk, and review() has to reach its `return DONE` for
    handle_pr() to record that. It does not wrap the call, so an exception
    leaves no state at all: no outcome, no attempts, MAX_ATTEMPTS never
    reached, and the next poll a minute later checks the pull request out and
    reviews it again, at full cost, on every poll from then on.

    Minting the token is what made that reachable. It happens after the
    review now, so it is a live API call every time `review_timeout` is set
    near the hour a token lives, and `github_api` turns only HTTPError into
    RuntimeError: a DNS failure or a dropped connection raises URLError
    straight through. A transient 5xx on that endpoint is documented in
    handle_pr() as something that already happens.
    """
    try:
        return post()
    except Exception as err:
        log("%s: the review is not posted: %s" % (label, err))
        return False


def posted_mark(pr, verb):
    """How one kind of Vinegar comment opens, for recognising it again.

    The verb is part of it. Matching BODY_MARK alone asked "is any Vinegar
    comment up at this commit", which a give-up posted at the same head
    answers yes to: a later run that reviewed the pull request properly
    then discarded six findings as a duplicate of the note saying it had
    given up. The effort is left out because it is configuration and can
    change between runs.
    """
    return "%s %s `%s`" % (BODY_MARK, verb, pr["headRefOid"][:7])


def already_posted(label, repo, pr, env, verb="reviewed"):
    """Whether one of *Vinegar's own* reviews of this commit is already up.

    Asked before resending one that may have failed after landing. A 5xx or
    a dropped connection is reported the same way whether GitHub committed
    the review or not, so a blind retry can leave two reviews where the
    operator asked for one. Reading is cheap and settles it.

    Three things this has to get right, each of which it got wrong first.

    It matches on Vinegar's own first line, not merely on the commit. Asking
    whether *any* review exists at this sha lets a human reviewing the same
    commit suppress Vinegar's entirely, and a suppressed review is silence,
    which is the one outcome that is never allowed.

    It paginates. GitHub returns thirty per page, oldest first, so on a busy
    pull request the review that just landed is on the last page and a single
    page would miss precisely the case this exists for.

    It answers "no" when it cannot tell. A duplicate review is worse than one
    review; no review at all is worse than both.
    """
    # Full pages. The whole paginated read shares one POST_TIMEOUT, and at
    # GitHub's default of thirty a pull request with two hundred reviews is
    # seven sequential calls where two would do. The timeout's answer is
    # "no", and "no" is the answer that resends.
    #
    # split("\\n"), so the jq program carries the two-character escape
    # rather than a raw newline byte. The program is assembled here as a
    # Python string and handed to whatever engine gh embeds, and the middle
    # of a string literal is the wrong place to learn how that engine feels
    # about a bare line break. The tests never execute this filter, so it
    # has to be unambiguous by construction.
    try:
        result = run(["gh", "api", "--paginate", "-X", "GET",
                      "-f", "per_page=100",
                      "repos/%s/pulls/%d/reviews" % (repo, pr["number"]),
                      "--jq", '.[] | select(.commit_id == "%s") '
                              '| (.body // "") | split("\\n")[0]'
                              % pr["headRefOid"]],
                     env=env, timeout=POST_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Still "no", but never silently: this is the answer that resends,
        # and a duplicate review with nothing in the log saying the check
        # could not tell reads as the check never having run.
        log("%s: the landed-review read timed out after %ds, so the resend "
            "goes ahead unchecked" % (label, POST_TIMEOUT))
        return False
    if result.returncode != 0:
        log("%s: the landed-review read failed (%s), so the resend goes "
            "ahead unchecked" % (label, both_streams(result, 200)))
        return False
    # stream_lines, not splitlines(): splitlines() also breaks on \v, \f,
    # \x1c-\x1e and \x85, so a review body carrying one of those before a
    # quoted BODY_MARK would produce a fragment that starts with it, and
    # Vinegar would read someone else's comment as its own review already
    # being up. That answer suppresses the resend, and a suppressed resend
    # is silence.
    return any(line.startswith(posted_mark(pr, verb))
               for line in stream_lines(result.stdout))


def post_review(label, repo, pr, path, text, findings, config, env,
                note=None, verb="reviewed", resent=False, since=None,
                blockers=False):
    """Turn what the reviewer reported into one review on the pull request.

    Answers POSTED when the pull request carries the review, THROTTLED
    when a limit refused it and it should be tried again unchanged, and
    False when it did not land for any other reason.

    Named rather than true-or-false because there are three answers and
    they need three different things done about them. Smuggling the third
    through a two-state contract meant a caller testing truthiness read
    "rate limited, try again later" as "posted", and deleted the saved
    review it had just promised to resend.
    """
    # Before the routing, both of these. Working out which findings could
    # be anchored means a full `git diff` over the pull request and up to
    # 60KB of assembled markdown, and neither of these endings looks at
    # any of it.
    #
    # The dry run first. A dry run mints no token, so posting_env() hands
    # this an env of None, and asking GitHub anything with it is a live
    # call under whatever ambient credentials exist: on an App-only
    # deployment that is a 401 and a logged warning about a resend that a
    # dry run was never going to make.
    if not config["comment"]:
        # A dry run discards the answer to print two counts.
        #
        # True because a dry run wanted nothing posted: it is the one
        # ending where an empty pull request is the correct outcome, and
        # calling it a failure would have the give-up announce itself on
        # every poll for ever.
        log("%s: dry run, %d finding(s) not posted" % (
            label, len(findings) if findings else 0))
        return POSTED

    # Asked again after each send, deliberately. Every one of these three
    # reads is separated from the next by a submit_review that may have
    # landed, so a cached answer would be exactly the stale one that
    # produces the duplicate the read exists to prevent.
    if resent and already_posted(label, repo, pr, env, verb):
        log("%s: the review is already on the pull request" % label)
        return POSTED

    if findings is None:
        log("%s: %s, posting its text as the review" % (
            label, "the review stopped before reporting" if note else
            "the reviewer reported no findings through %s" % REPORT_TOOL))
        # Capped here as well as in read_stream: this text can come from
        # the result event, which nothing had cut.
        inline, general, raw = [], [], cap_spoken(text)
    elif not findings:
        # Nothing to anchor, so nothing to work the diff out for. That is a
        # `git diff` over the whole pull request saved on every clean review.
        inline, general, raw = [], [], None
    else:
        inline, general = split_findings(
            findings, diff_lines(path, pr["baseRefName"], env, label), label)
        raw = None

    payload = {"event": "COMMENT",
               "commit_id": pr["headRefOid"],
               "body": review_body(label, pr, config, inline, general, raw,
                                   note=note, verb=verb,
                                   tally=severity_tally(findings),
                                   since=since, blockers=blockers,
                                   disagreed=below_blocker(findings))}
    if inline:
        payload["comments"] = inline

    def landed(settled):
        """Whether that attempt left the review up, asking when unsure.

        Every send ends here so that UNSURE is never read as "lost". The
        last send used to compare against POSTED alone, so a review GitHub
        committed before the connection dropped was recorded as unposted
        and said again on the next poll: the duplicate the read exists to
        prevent, on the one path that never ran it.
        """
        if settled == POSTED:
            # What the payload carries now, not what was routed at the
            # top: the anchor-stripping retry pops the comments, and
            # reporting three inline comments on a request that sent none
            # tells whoever is debugging that anchoring worked.
            log("%s: posted %d inline comment(s) and the review comment" % (
                label, len(payload.get("comments", ()))))
            return POSTED
        if settled == UNSURE:
            return POSTED if already_posted(
                label, repo, pr, env, verb) else False
        # THROTTLED passes through. Flattening it to False here undid the
        # distinction the callers were just taught to make: a limit met
        # on the anchor-stripping retry would have been reported as a
        # refusal, and the marker that resends it dropped.
        return settled if settled == THROTTLED else False

    settled = submit_review(label, repo, pr, payload, env)
    if settled == POSTED:
        return landed(settled)

    if settled == UNSURE:
        # A timeout, a 5xx, an error with no status in it. Any of those can
        # arrive after GitHub has already created the review, so one cheap
        # read comes before the resend, and the resend comes rather than
        # returning: returning recorded DONE around a pull request carrying
        # no review at all, and with `review_on_push` false nothing ever
        # came back for it, the one silence the README does not allow. The
        # resend can duplicate only inside the window the read cannot see,
        # and the ordering already_posted() documents stands: a duplicate
        # is worse than one review, silence is worse than both.
        if already_posted(label, repo, pr, env, verb):
            log("%s: the review is already on the pull request" % label)
            return POSTED
        log("%s: the post may not have landed and nothing is up, so it is "
            "sent again" % label)
        settled = submit_review(label, repo, pr, payload, env)
        if settled in (POSTED, UNSURE):
            return landed(settled)
        # A resend that came back judged falls through to the retries
        # below. Returning here instead lost every finding whenever the
        # first attempt met a transient 5xx and the second was refused
        # over the one bad anchor the anchor-stripping retry exists for.

    if settled == THROTTLED:
        # Retrying now would be refused for the same reason. The review is
        # on disk, and saying where says more than a second failure would.
        #
        # THROTTLED, not False. A limit refuses without judging the
        # request and lifts on its own clock, so a caller that counts
        # failures against a budget must not count this one: the give-up
        # was marked announced after three throttled polls against a
        # limit that resets hourly, and the pull request was then never
        # told anything at all.
        log("%s: the posting is rate limited, so the review is only in the "
            "transcript for now. It is sent again on a later poll" % label)
        return THROTTLED

    # A definite refusal created nothing, so there is nothing to go and
    # look for: the status code already answered the question the
    # landed-review read exists to ask, and that read is seven paginated
    # calls on the poll thread on a busy pull request.
    if inline:
        # The endpoint took none of it, and the likeliest reason is an
        # anchor it disagrees with: a comment lands only on a line inside
        # the diff *it* computed, and checkout() deliberately carries on
        # when the base branch cannot be refreshed, which widens the local
        # diff past GitHub's. Rather than lose ten findings to one line
        # number, say all of it in the comment that needs no anchor at all.
        log("%s: retrying with every finding in the review comment" % label)
        payload.pop("comments")
        payload["body"] = review_body(
            label, pr, config, [], findings, note=note, verb=verb,
            tally=severity_tally(findings), since=since, blockers=blockers,
            disagreed=below_blocker(findings),
            heading="GitHub refused the inline comments, so all of it is "
                    "here:")
    else:
        # Nothing to strip out and nothing to change, so nothing to retry.
        # This branch is reachable only for REFUSED now: the transient
        # failures it was written for, a 502 or a dropped connection, are
        # UNSURE and were resent by the block above. Sending identical
        # bytes to an endpoint that has already judged them buys one
        # guaranteed refusal, and on an archived repository one per
        # give-up retry as well.
        log("%s: GitHub refused the review and there is nothing to change "
            "about the request. It is in the transcript" % label)
        return False
    return landed(submit_review(label, repo, pr, payload, env))


def save_or_log(label, write):
    """Write a transcript, and never let that failure escape.

    Three callers do this and each spelled the guard out, with the same
    sentence for the failure: keep(), and both branches of finish(). The
    write is the cheap local copy and must not be able to take the posting
    down with it, which is one policy, so it lives in one place.

    Answers whether it was written, because a caller that goes on to point
    at that file needs to know it is the file it thinks it is.
    """
    try:
        write()
        return True
    except Exception as err:
        log("%s: the transcript is not saved: %s" % (label, err))
        return False


def keep(label, repo, pr, text, why):
    """Save what a failed attempt said, since nothing else will.

    The two FAILED endings post nothing, correctly: they are retried, and
    posting on each attempt would leave three comments. But the reviewer's
    own words are in hand by then and were being dropped, so a run that
    narrated for twenty minutes before dying left no trace anywhere. On a
    dry run the transcript is the only output there is.
    """
    if not text.strip():
        return
    save_or_log(label, lambda: log(
        "%s: %s, its words are in %s" % (label, why, save_transcript(
            repo, pr, text, None,
            "This attempt did not finish, so this is what it said before it "
            "stopped. It is not a review."))))


def finish(label, repo, pr, path, text, findings, config, env, tokens,
           note=None, verb="reviewed", preserve=False, resent=False,
           check=None, since=None, blockers=False, whole=False):
    """Record the review on disk and post it, whatever ended the run.

    Answers post_review()'s answer: whether the pull request carries it.

    Both callers do exactly this and used to do it separately, which is how
    the killed-run path came to post a partial review under a heading that
    read like a finished one: the marker was added to one and not the other.

    It runs inside announce(), so writing the transcript is covered too. That
    write can fail on its own, a `~/.vinegar/reviews` left root-owned by one
    `sudo` run being the way it happens, and an exception here would leave
    handle_pr() with no state to record and the pull request re-reviewed at
    full cost on every poll from then on.
    """
    # The transcript is written in its own right, because it must not be
    # able to take the posting down with it. It is the cheap local copy; the
    # review on the pull request is the entire output of the run. Putting the
    # write first and unguarded meant a `~/.vinegar/reviews` left root-owned
    # by one `sudo` run silenced every review from then on, while the outcome
    # was still recorded reviewed.
    #
    # An ending with nothing to say must not replace one that said
    # something. The give-up after MAX_ATTEMPTS arrives here with no text
    # and no findings, seconds after keep() saved what the third attempt
    # said, and a plain write would truncate that file: same repository,
    # same number, same sha, so transcript_path() names the file keep()
    # just wrote. The words stay and the note goes beneath them, because on
    # a dry run this file is the only place the giving-up can be recorded
    # at all. Skipping the write entirely kept the words but left the
    # ending nowhere but a log nobody is watching.
    # Before the transcript is written, so every route to the pull
    # request — this file, the comment, the anchorless retry, and the
    # repost that sends this file later — carries the same paths.
    findings = relative_findings(findings, path)

    # Here for the same reason as the line above it, and it is the reason
    # this is not in review(): those four routes to the pull request leave
    # from this function, and tiering on any one of them would have left
    # the other three ordering findings differently from the comment. The
    # repost in particular sends the transcript verbatim, so a tier that
    # is not in the file by now never reaches a pull request that needed
    # the retry.
    #
    # Every ending, not only the finished one. A review that was killed at
    # minute thirty still reported real findings before it died, and a
    # reader triaging a partial review by hand needs the ordering more
    # than a reader of a complete one, not less. The give-up arrives with
    # no findings at all and triage() returns immediately.
    #
    # A dry run too, where nothing is posted: the transcript is that run's
    # only artifact, and it is how the severity pass is exercised without
    # spending a review.
    findings = triage(label, findings, config)

    # `preserve` is passed by the give-up, not inferred from the ending
    # being empty. Inferring it caught a different case that looks the
    # same from here: a review that ran to completion and reported
    # nothing at all. That left an earlier attempt's words on disk under
    # a header saying they are not a review, while the comment said the
    # run produced nothing, so the transcript and the pull request
    # disagreed about what happened, which is the asymmetry this function
    # exists to remove.
    path_kept = transcript_path(repo, pr)
    # `note` is in the condition rather than nested inside it: the only
    # caller that preserves is the give-up, and it always brings a note
    # saying why, so a preserve-without-note branch was three lines no
    # test could reach and a reader still had to account for.
    def append_ending():
        # Rewritten whole through a rename, like save_transcript, not
        # appended in place. This branch trusts the existence of the file
        # to mean the attempts left words worth keeping, and an append
        # that dies half-way would hand the next reader a file that
        # passes that test and ends mid-note.
        # Not `whole`, which is this function's parameter and means
        # something else entirely. Nothing here reads it, so the shadow
        # cost nothing; the line that would have paid is the obvious one
        # to add next, an ending that says whether the preserved run
        # finished, which would have got this file's text instead and
        # never raised.
        with open(path_kept, encoding="utf-8", errors="replace") as handle:
            saved = handle.read()
        # Once, however many times the announcement is retried. The
        # give-up is attempted on up to MAX_ATTEMPTS polls while the
        # posting keeps failing, and each attempt used to append another
        # identical ending to the file that is a dry run's only artifact.
        if note in saved:
            log("%s: the transcript already records this ending" % label)
        else:
            write_atomic(path_kept, saved + "\n\n---\n\n%s\n" % note)
            log("%s: the ending is appended to the transcript the attempts "
                "left" % label)

    if preserve and note and os.path.exists(path_kept):
        wrote = save_or_log(label, append_ending)
    else:
        wrote = save_or_log(label, lambda: log("%s: transcript at %s" % (
            label, save_transcript(repo, pr, text, findings, note, since,
                                   blockers))))
    # Marked before the posting, not after. Written afterwards it was
    # skipped entirely by anything that raised out of post_review — `gh`
    # missing from PATH, a fork that cannot allocate — so a transcript
    # that was safely on disk had nothing pointing at it and could never
    # be sent, while the log told the operator nothing had been saved and
    # invited them to pay for the review again. Optimistic and then
    # cleared is the safe order: the worst a stale marker costs is one
    # extra send, and already_posted answers that.
    marker = unposted_path(repo, pr)
    if config["comment"] and not preserve and wrote:
        # Its own guard, not save_or_log's. That one's message says the
        # transcript was not saved, which here is false and misleading:
        # the transcript is safely on disk and what failed is the note
        # saying it still needs sending.
        try:
            write_atomic(marker, "%s\n" % pr["headRefOid"])
        except OSError as err:
            log("%s: the review is saved but cannot be marked for sending "
                "again: %s" % (label, err))

    # Hoisted, because the indicator is finished on these too. They are
    # freshly minted where the review's own may be an hour old by now.
    sending = posting_env(label, config, repo, tokens, env)
    posted = post_review(label, repo, pr, path, text, findings, config,
                         sending, note, verb, resent, since=since,
                         blockers=blockers)
    # Cleared once it is on the pull request. What is left behind says a
    # review is saved and waiting, which is what handle_pr acts on: the
    # outcome is recorded DONE either way, `review_on_push` is false, and
    # without this the pull request was never looked at again — the
    # silence the README forbids, reached by the one path that had no
    # retry of its own. The give-up has its own bounded retry and is
    # never marked.
    # `== POSTED`, not truthiness. THROTTLED is a string and every string
    # is true, so a rate-limited post — which had just logged that the
    # review is safe and will be sent again — deleted the marker that was
    # the only thing able to send it. Named once and read three times
    # below, so the next ending added here cannot get the comparison right
    # in two places and wrong in the third.
    landed = posted == POSTED
    #
    # `not preserve` as well, because the give-up writes no marker, so
    # forgetting one on its way out could only ever delete somebody
    # else's.
    if landed and not preserve:
        forget(marker)

    # The indicator is finished here, for the reason the two lines at the
    # top of this function are here: every route to the pull request
    # leaves from finish(), and this is the only one of them that knows
    # both how many findings there were and whether they landed. Closing
    # it in review() instead would have had to say "reviewed" without
    # being able to say what was found.
    #
    # Counted off `findings`, which triage() has already tiered, so the
    # checks list carries the same tally as the comment.
    if findings is None:
        # Not "no findings". The reviewer said something Vinegar could not
        # read, and a checks list saying the change is clean would be the
        # false all-clear the `clean` line below refuses the tick for.
        title = "Nothing Vinegar could read"
    elif not findings:
        title = "No findings"
    else:
        tally = severity_tally(findings)
        title = "%d finding%s%s" % (
            len(findings), "" if len(findings) == 1 else "s",
            " (%s)" % tally if tally else "")
    # And what the pass was asked for, both narrowings, because the title
    # this leaves behind is the one that stands for the rest of the pull
    # request's life. Without them, "No findings" from a narrowed fifth
    # round is the same six characters as "No findings" from a first review
    # that read everything, and `gh pr checks` is where an agent reads it.
    # It matters more now than it did: that fifth round closes green.
    #
    # open_check() takes only `blockers`, so while a review is running its
    # scope is still invisible and a scoped round reads like a first one
    # for the nine to twenty-two minutes it takes. That is a smaller
    # window than this one and it is not closed here.
    if since:
        title = "%s in what was added since `%s`" % (title, since[:7])
    if blockers:
        # "asked for", not "reporting", for the reason review_body() gives
        # at length: nothing filters what the reviewer hands back, so a
        # title claiming this review reported blockers only sits beside a
        # tally that can say `1 advisory`. The title has no room to
        # explain the disagreement the comment explains, so it stops at
        # the claim it can keep.
        title = "%s, asked for blockers only" % title
    # A partial run says so in the title rather than only in the comment.
    # "3 findings" from a review killed at minute thirty reads as the
    # whole answer, and the checks list is what people look at first.
    # Last, because it is the caveat that most changes how the rest reads.
    #
    # Off `whole` and not off `note`, which is the distinction review()
    # keeps the two apart for: a note also carries the fallback-model
    # notice, and a review that ran to the end on the fallback model was
    # titled as one that was cut short.
    if not whole:
        title = "%s, and the review did not finish" % title
    # Green only for the ending that is a pass, and every term here is one
    # way of reporting nothing without being clean.
    #
    # `findings == []` and not `not findings`, because None is the review
    # whose output could not be read, which the branch above already
    # refuses to call clean.
    #
    # `whole`, for the reason the title uses it: a killed run reported
    # nothing because it stopped, and reading that off `note` would deny
    # the tick to every clean review on a deployment whose pinned model
    # stopped routing.
    #
    # `not resent`, and it is deliberately broader than the case it
    # defends. post_review answers POSTED without posting when a retry
    # finds the review already up, and that earlier review is the one on
    # the commit, so a retry reporting nothing would tick a commit whose
    # visible review is full of findings. Nothing here can tell that retry
    # from one that did its own posting, because both answers are POSTED,
    # so every retry loses the tick rather than the one that should.
    #
    # `resent` is `attempts > 1`, so what that costs is the tick on a
    # clean review whose first attempt failed before posting anything. A
    # grey mark beside "No findings" is what every clean review looked
    # like before this existed, so the cost is a tick withheld and never a
    # claim that is false. Issue #27 is the answer that would narrow it:
    # post_review saying which of the two happened, which reaches the
    # `covered` logic that decides narrowing and is why it is not done
    # here.
    clean = findings == [] and whole and landed and not resent
    close_check(label, check, title, sending or env,
                "The review is on the pull request." if landed
                else "The review did not reach the pull request. The log "
                     "says where it is saved.",
                CHECK_CLEAN if clean else CHECK_CONCLUSION)
    return posted


def unposted_path(repo, pr):
    """The marker saying this pull request's review never reached it."""
    return transcript_path(repo, pr) + ".unposted"


def forget(path):
    """Remove a marker, without minding that it was already gone."""
    try:
        os.remove(path)
    except OSError:
        pass


def unposted_for(repo, pr, scan=True):
    """Any saved review of this pull request that never reached it.

    Found by pull request rather than by commit, because the head can move
    between the refusal and the retry. Keyed on the current head, the
    marker written for the previous one could never be found again: with
    `review_on_push` false that pull request is never reviewed a second
    time either, so the refused review was the only one there would ever
    be, and it was silently abandoned with its file left behind.

    Returns the marker path and the full commit it was written for, which
    the marker carries because the transcript's name holds only seven
    characters of it and the reviews endpoint wants all forty.
    """
    # The current head first, which is the answer almost every time and
    # costs one stat. The scan behind it is for the case the head has
    # moved, and it runs for every open pull request on every poll against
    # a directory that only ever grows, so it does no sorting: it returns
    # the first match, and ordering a list of thousands of names to pick
    # one arbitrary member of it was pure work.
    here = unposted_path(repo, pr)
    if os.path.exists(here):
        return here, read_mark(here)
    if not scan:
        # The scan answers only one question — where did the marker go
        # when the head moved — and the caller knows when that can have
        # happened. Running it regardless meant listing a directory that
        # only grows, once per open pull request per poll, to learn what
        # the stat above had already answered.
        return None, None

    prefix = "%s__%d__" % (repo.replace("/", "__"), pr["number"])
    try:
        names = os.listdir(REVIEW_DIR)
    except OSError:
        return None, None
    for name in names:
        if not (name.startswith(prefix) and name.endswith(".md.unposted")):
            continue
        path = os.path.join(REVIEW_DIR, name)
        return path, read_mark(path)
    return None, None


def read_mark(path):
    """The commit a marker was written for, or None if it cannot be read.

    None rather than "": an unreadable marker and one written for another
    commit are the same string to a caller comparing against a sha, and
    the caller's answer to "another commit" is to throw the saved review
    away. A `~/.vinegar/reviews` left root-owned by one `sudo` run would
    have deleted a finished review that way.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            # An empty file answers None too. Zero bytes — a truncated
            # write, a filesystem fault, an operator clearing it — is no
            # more "written for another commit" than a permission error
            # is, and that is the answer that deletes the saved review.
            return handle.read().strip() or None
    except OSError:
        return None


def repost(key, repo, pr, config, state, tokens, done, marker, sha):
    """Send a review that GitHub refused, again, from the transcript.

    The review itself is not re-run: the subscription is spent and the
    findings are on disk. What is retried is only the posting, which is
    the part that failed, and it is retried as one plain comment because
    the anchors are what GitHub is most likely to have refused and the
    transcript holds every finding either way.

    Bounded like the give-up, and for the same reason: an App without
    `pull_requests: write` refuses every time, and an unbounded retry is
    two API calls a minute per pull request for ever. When the budget is
    spent the marker is removed and the review stays where the log says
    it is.
    """
    # The attempt is spent before it is made, and every way out of this
    # function goes through the same ending. Charging it afterwards meant
    # anything that raised — `gh` missing from PATH, a fork that cannot
    # allocate — escaped to poll_once() with the counter unmoved and the
    # marker in place, and the budget that is supposed to bound this
    # never advanced: a token mint, a file read and a log line a minute,
    # per pull request, for ever. review() has announce() for the same
    # reason.
    tries = done.get("post_tries", 0) + 1
    waived = done.get("post_waivers", 0)
    remember(state, key, dict(done, post_tries=tries))

    at = dict(pr, headRefOid=sha or pr["headRefOid"])
    saved = transcript_path(repo, at)
    landed, give_up_on_it = False, tries >= MAX_ATTEMPTS

    # The read is guarded on its own, and narrowly. Wrapping the sending
    # in the same `except OSError` swallowed a missing `gh` and a fork
    # that cannot allocate as "the saved review cannot be read", which
    # names the wrong cause and abandons on the first try something that
    # deserves the other two.
    body = None
    try:
        with open(saved, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    except OSError as err:
        # The transcript is what there was to send. Without it there is
        # nothing to retry and no point counting to three about it.
        log("%s: the saved review cannot be read (%s), so it is given up "
            "on" % (key, err))
        give_up_on_it = True

    if body is not None:
        try:
            env = github_env(config, repo, tokens, good_for=POST_GRACE)
            opening = ("%s\n\nThis review was refused when it was written "
                       "and is posted from the transcript, so its findings "
                       "are not anchored in the diff.\n\n---\n\n" % (
                           review_heading(at, config),))
            # Cut from the front, not the back. save_transcript puts the
            # findings last, and clamp() truncates the end, so an
            # oversized transcript kept the reviewer's narration in full
            # and sheared off exactly what the repost exists to deliver.
            cut_said = ("(the beginning was cut to fit GitHub's comment "
                        "limit)\n\n")
            # Lifted into the opening before the cut can reach it. The
            # scope line is written at the front of the transcript's body
            # and the cut keeps the tail, so the one case it exists for —
            # a narrowed review too big to post — is exactly the case that
            # lost it, and the review then arrived reading as though it
            # had covered the whole pull request.
            #
            # Found as a line rather than at position zero: what is read
            # here is the whole file, so the transcript's own heading and
            # the `---` come first.
            #
            # Either mark opens the block, and the block runs to the blank
            # line, so one find serves however many lines save_transcript
            # wrote. Matching only the scope line lost the blockers-only
            # line on a pass that reported narrowly and read everything,
            # which is the transcript that carries that mark alone.
            sep = body.find(TRANSCRIPT_SEP)
            starts = sep + len(TRANSCRIPT_SEP) if sep != -1 else -1
            end = (body.find("\n\n", starts)
                   if starts != -1 and body.startswith(LIFTED_MARKS, starts)
                   else -1)
            if end != -1:
                opening += "%s\n\n" % body[starts:end]
                body = body[:starts] + body[end + 2:]
            # Measured, not a hand-picked slack. The old constant was
            # chosen for the length of the sentence above it, so
            # rewording that sentence longer would have pushed the body
            # over the ceiling with nothing to catch it.
            room = MAX_BODY - len(opening) - len(cut_said)
            if len(body) > room:
                log("%s: the saved review is %d characters, sending the "
                    "last %d so the findings survive" % (
                        key, len(body), room))
                body = cut_said + body[-room:]
            # Always, including the first try. Skipping it there assumed
            # post_review had already established that nothing landed —
            # true only when *its* landed-review read succeeded, and that
            # read answers "no" when it times out or errors, which is
            # exactly what happens during the GitHub incident that made
            # the post ambiguous in the first place. The saved review then
            # went on top of one that was already up.
            # Said where it is actually done. Logged before the check, a
            # poll that sent nothing because the review was already up
            # still claimed to have sent one, and the line after it
            # ("the saved review is on the pull request") then read as
            # confirmation that this attempt was what put it there.
            if already_posted(key, repo, at, env):
                settled = POSTED
            else:
                log("%s: posting the review that was refused earlier "
                    "(attempt %d of %d)" % (key, tries, MAX_ATTEMPTS))
                settled = submit_review(
                    key, repo, at,
                    {"event": "COMMENT", "commit_id": at["headRefOid"],
                     "body": opening + body}, env)
            landed = settled == POSTED
            if settled == THROTTLED and waive(key, "the posting", waived):
                waived += 1
                tries -= 1
                give_up_on_it = False
        except Exception as err:
            log("%s: the saved review could not be sent: %s" % (key, err))

    if landed:
        log("%s: the saved review is on the pull request" % key)
    elif body is None:
        # Nothing was sent, so it cannot be reported as attempts spent,
        # and the file the give-up line points at is the one the line
        # above just said could not be read.
        log("%s: there is nothing left to send for this pull request" % key)
    elif give_up_on_it:
        log("%s: the saved review could not be posted in %d attempts. It "
            "stays in %s" % (key, tries, saved))
    # `unposted` comes off with the marker. Leaving it set kept the scan
    # gate armed for ever: once the head moved, every poll listed a
    # directory that only grows to learn that a marker already deleted is
    # still gone.
    if landed or give_up_on_it:
        forget(marker)
        entry = dict(done,
                     post_tries=MAX_ATTEMPTS if give_up_on_it else tries)
        entry.pop("unposted", None)
        entry.pop("post_waivers", None)
        if landed:
            # The pull request has its review, whatever the entry said
            # before. It can say FAILED here: the marker is written
            # inside review() and the outcome only after, so a process
            # killed between the two leaves one behind. Left as FAILED,
            # the next poll announced "Vinegar tried to review this 3
            # times and each attempt failed" on a pull request that had
            # received a full review a minute earlier, and already_posted
            # could not suppress it because it matches a different verb.
            entry["outcome"] = DONE
            # And the round it never got, counted once, here. The review
            # that wrote this transcript answered DONE with the posting
            # refused, so handle_pr deliberately did not count it: the
            # author had been shown nothing. This send is the moment they
            # are, and it is the only one, because the marker is forgotten
            # on the next line. Counted through the same helper as every
            # other site so the rule stays in one place.
            entry.update(rounds_done(True, done))
    else:
        entry = dict(done, post_tries=tries)
        if waived:
            entry["post_waivers"] = waived
    remember(state, key, entry)


def partial_note(cause):
    """The note every partial ending shares, phrased the one way.

    Three endings post findings they know are incomplete: killed, stream
    stopped early, failed. review_body and the tests key off the shared
    clause, and as three hand-written copies one edit could quietly have
    the same class of ending described three different ways.
    """
    return ("This review %s, so these are the findings it had reported by "
            "then and not a finished round." % cause)


def unroutable(output, findings):
    """Did that attempt fail only because its model could not be routed?

    Five things have to hold together, and every one of them is what
    stops a second attempt from costing something.

    Measured on Claude Code 2.1.221, against `claude-opus-5[999m]` and
    against a name that is not a model at all: both came back a result
    event with `is_error` true, `api_error_status` 404 and
    `total_cost_usd` 0, about a second in. `subtype` was "success" on
    that same event, so nothing here may read it.

    `total_cost_usd` is the one that keeps the promise the rest of this
    rests on. A 404 need not arrive in the first second: the model can be
    retired mid-run, or a finder subagent's can be. Such a run has spent
    the review's budget, and re-running it would spend that budget again
    for one pull request, three times over MAX_ATTEMPTS. Only a run that
    bought nothing is free to throw away.

    Compared against 0 rather than tested for falsiness, because a missing
    key is not a report of having spent nothing. `not output.get(...)` is
    true for both, so a result shape that omits the field entirely would
    read as free and buy a second full-price review, while priced() would
    print nothing beside it and leave the log with no cost to contradict
    the claim. An absent field is unknown, and unknown is not free.

    `is_error` because a 404 recorded on a run that *finished* is not a
    routing failure: it would be a sub-request that failed and was
    retried, and discarding the finished review to pay for another is the
    opposite of the repair.

    `findings is None` because nothing else in this file spends findings
    already in hand to buy another attempt, and `output is not None`
    because a stream that stopped before its result event has no status
    to read and .get() on None raises out of review() into handle_pr's
    catch-all, which records FAILED and re-reviews the same head at full
    cost twice more.
    """
    return (findings is None and output is not None
            and output.get("is_error")
            and output.get("api_error_status") == 404
            and output.get("total_cost_usd") == 0)


def review(path, repo, pr, config, env, tokens, resent=False, check=None,
           since=None, blockers=False):
    """Run one review and post it. Answers the outcome and whether it covered.

    Two answers rather than one, for the reason post_review() has three:
    the caller has two different decisions to make and one of them cannot
    be read off the other. The outcome decides whether this pull request
    is closed off and how much retry budget is left. The second answers
    whether the author has now seen the whole of what this pass was asked
    to read, which is the only thing that may narrow the pass after it.

    DONE does not imply covered. Every ending that spent the subscription
    answers DONE, a run killed half-way through the scope included, and
    narrowing on one of those steps the next pass over lines nothing ever
    read.
    """
    prompt = "/code-review %s %d" % (config["effort"], pr["number"])

    # The review reads a diff that Vinegar did not write, so it runs under
    # vinegar's own settings file and loads none of the user, project, or
    # local settings.json an interactive session would, and no MCP server.
    #
    # `--setting-sources ""` covers the checkout's memory files too, and
    # this comment said the opposite until it was measured. On Claude Code
    # 2.1.221 a `CLAUDE.md` in the working directory is not applied with
    # the flag and is applied without it, holding everything else equal:
    # same directory, same settings, same model, six runs, `AGENTS.md` and
    # a real git repository included. So the head commit cannot instruct
    # the reviewer through a memory file.
    #
    # Treat that as true of this version rather than for ever. It is the
    # behaviour of a flag in a tool Vinegar does not own, nothing here can
    # detect it changing, and the offline suite cannot reach it because it
    # stubs the CLI. The README has the probe that re-checks it in a
    # minute; run that rather than inheriting this paragraph, and read its
    # warning about the control first, because the weak version of the
    # probe passes whether or not the flag does anything.
    #
    # Three settings that only work together, and the reviewer's findings
    # arrive nowhere if any one of them is dropped.
    #
    # /code-review picks how it reports from whether ReportFindings is in the
    # session and what the output format is. `stream-json` plus the env var
    # selects the tool; `json` or `text` selects a JSON array printed in the
    # final message instead. The tool is the better half of that choice: it
    # is the reviewer's own structured output, so nothing has to be picked
    # back out of prose, and it carries a category and a short summary that
    # text never did.
    #
    # `--verbose` because the tool call is an event in the stream rather than
    # part of the final result, and this is the combination that was measured
    # working rather than the one that reads most likely.
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", reviewer_brief(pr, config, since,
                                                    blockers),
           "--output-format", "stream-json", "--verbose",
           "--settings", reviewer_settings(path),
           "--setting-sources", "",
           "--strict-mcp-config"]

    # The env var is what makes the choice deterministic. Without it the
    # decision falls through to a server-side flag that is off by default,
    # and the reviewer goes back to printing text.
    #
    # Without the token, and this is the whole of a credential leak rather
    # than tidiness. The environment handed in here is the one checkout()
    # used, so it carries the App installation token, and enabling the
    # sandbox stopped the allow list gating Bash at all: measured, `env`
    # is refused without the sandbox and runs with it, with nothing in
    # `permission_denials`. So the reviewer could print the token, and a
    # reviewer reading an attacker-authored branch publishes what it is
    # told to — finding text goes to the pull request verbatim. Measured
    # end to end with a fake token: `env | grep GH_TOKEN` printed the
    # value and the model quoted it back.
    #
    # Nothing is lost by removing it. The reviewer has no network, so
    # `gh` cannot reach GitHub whatever credential it holds, and the git
    # it does run is read-only inside a checkout that is already on disk.
    # GITHUB_TOKEN goes too: `gh` reads either, and either would be an
    # operator's own credential rather than one scoped to this repository.
    #
    # The caller's `env` is left alone, because posting_env() falls back
    # to it when minting a fresh token fails, and that fallback is what
    # gets a finished review onto the pull request during a GitHub blip.
    env = env or os.environ
    reviewing = dict(env, CLAUDE_CODE_REPORT_FINDINGS="1")
    for carried in ("GH_TOKEN", "GITHUB_TOKEN"):
        reviewing.pop(carried, None)

    label = pr_key(repo, pr)

    # Whether a whole reading of the review scope reached the author, which
    # is the only thing that may narrow a later pass. Filled in by deliver()
    # because that is where all three facts meet, and answered back rather
    # than left for handle_pr to infer.
    #
    # Inferring it was wrong in two ways at once, and both of them lost
    # findings rather than repeating them. A run killed by `review_timeout`
    # after reporting two findings from the first file returns DONE and
    # posts, so a marker-based test called it covered and moved the next
    # pass past the hundreds of lines it never reached. And the marker
    # itself is written only when the transcript write succeeded, so a run
    # that could neither save nor post — `~/.vinegar/reviews` left
    # root-owned by one `sudo` run — left no marker and read as posted.
    covered = []
    # And whether the author was shown anything at all, which is a weaker
    # question with its own answer. `covered` says a whole reading reached
    # them and decides what the next pass may skip; this says a review
    # reached them and decides whether this was a round. A partial review
    # is not covered and is a round.
    #
    # Answered here for the reason written above, and the marker-based
    # test the comment above rejects for `covered` had been left in place
    # for this one: it counted a round for a run that could neither save
    # nor post, and for every dry run.
    reached = []

    def deliver(text, findings, note=None, whole=False):
        """Record and post one ending, whichever ending it turned out to be.

        Every way this function returns DONE goes through here. The three
        endings used to repeat the same nine arguments, and finish()'s own
        docstring records what that cost the last time: the partial-run
        marker was added to one path and not the other.

        The outcome stays DONE whatever the posting answers, because the
        subscription is spent and re-running the review is the one repair
        nobody wants. But a refusal must not read like a success: the
        pull request is closed off at this point, `review_on_push` is
        false, and the findings exist only on disk. Saying which findings
        and where is what makes that recoverable by hand.
        """
        # No answer to give back: every caller follows this with `return
        # DONE`, because the subscription is spent whatever the posting
        # said. Returning a boolean nobody read left a contract for the
        # next ending to honour that nothing checked.
        if announce(label, lambda: finish(
                label, repo, pr, path, text, findings, config, env, tokens,
                note, resent=resent, check=check, since=since,
                blockers=blockers, whole=whole)) == POSTED:
            # Four, and none of them is implied by another. POSTED says the
            # pull request carries it. `whole` says the reviewer reached the
            # end of the scope, and it is passed in rather than read off
            # `note`: a note also carries the fallback-model notice, which
            # is information about the run and not a statement that it was
            # cut short, so deriving it there turned the narrowing off for
            # good on any deployment whose pinned model stopped routing.
            # `findings is not None` says findings were reported at all,
            # since a run can end cleanly having narrated instead of
            # calling the reporting tool, and its prose reaching the pull
            # request is not its findings reaching it. `comment` says there
            # was a pull request to carry any of it, because a dry run
            # answers POSTED for having correctly posted nothing.
            if whole and findings is not None and config["comment"]:
                covered.append(True)
            # The weaker half of the same answer, and the one the round
            # count needs: the author was shown a review, whether or not
            # it read the whole scope. Same `comment` guard, and for the
            # identical reason. A dry run answers POSTED for having
            # correctly posted nothing, and counting that as a round
            # narrows a dry daemon's own later passes, whose transcripts
            # are its entire output.
            #
            # Answered here rather than worked out by the caller. handle_pr
            # had it from `os.path.exists(unposted_path(...))`, which is
            # not the same question: finish() writes that marker only when
            # the transcript write succeeded. A run that could neither save
            # nor post, which is what a reviews directory left root-owned
            # by one `sudo` run produces, leaves no marker and was counted
            # as a round the author never saw. This file already learned
            # that lesson once for `covered`, and inferring is what it
            # learned not to do.
            if config["comment"]:
                reached.append(True)
            return
        if config["comment"]:
            # Promised only when it is true. finish() writes the marker
            # only if the transcript was written, so on the path where
            # that failed too — a `~/.vinegar/reviews` left root-owned by
            # one `sudo` run — this said a retry was scheduled and named a
            # file that does not exist, while the outcome was recorded
            # DONE and the pull request closed off for good.
            if os.path.exists(unposted_path(repo, pr)):
                log("%s: nothing reached the pull request. The review is "
                    "in %s and posting it again is scheduled" % (
                        label, transcript_path(repo, pr)))
            else:
                log("%s: nothing reached the pull request and nothing was "
                    "saved to send later. To review it again, stop "
                    "Vinegar, delete this pull request's entry from %s, "
                    "and start it again" % (label, STATE_PATH))

    log("%s: reviewing at %s effort%s" % (
        label, config["effort"], "" if config["comment"] else ", dry run"))

    # The configured model, then the fallback if there is one. Only one
    # failure is worth a second attempt, and it is the cheapest: a model the
    # API cannot route is refused before a token is spent. Measured on Claude
    # Code 2.1.221 against `claude-opus-5[999m]` and a name that is not a
    # model at all, both answered the same way: a result event carrying
    # `api_error_status` 404 and `total_cost_usd` 0, about a second in. So
    # the retry is free, and the review lands on this poll instead of three
    # failed ones later.
    #
    # Nothing else falls back. An overload or a kill has already spent the
    # review's budget by the time it is known. A live 529 arrived eight and
    # a half minutes into an xhigh run, and a second model is not the repair
    # those want. Nor is a spent subscription, which no model reachable from
    # here is outside of. They stay FAILED, which is what MAX_ATTEMPTS is for.
    models = [config["model"]]
    if config["fallback_model"]:
        models.append(config["fallback_model"])

    # One bound across both attempts, not one each. load_config refuses a
    # review_timeout over MAX_REVIEW_TIMEOUT and tells the operator, in the
    # sentence it exits with, that one pull request holds the only poll
    # thread for its review plus the severity pass. A fallback given its
    # own fresh review_timeout makes that false: at the 7200 cap two
    # attempts park the daemon for four hours while the watchdog reads a
    # live pid and a quiet log as healthy, which is the exact failure that
    # ceiling was added to make impossible.
    left = config["review_timeout"]

    # Which model was abandoned, or None. The pull request is told, because
    # a fallback that works silently is a pin that stays dead: reviews keep
    # arriving, they read exactly like the pinned model's, and the only
    # record is one daemon log line nobody greps. Weeks of that is the
    # quiet degradation this feature exists to end, not something it may
    # introduce.
    abandoned = None

    for index, model in enumerate(models):
        started = time.monotonic()
        try:
            result = run(cmd + (["--model", model] if model else []),
                         cwd=path, timeout=left, env=reviewing)
        except subprocess.TimeoutExpired as expired:
            # A timeout burned the budget it burned. Retrying would burn it
            # again, so this returns rather than reaching the fallback: a
            # killed review is never re-run on another model, whatever the
            # loop around it might suggest.
            log("%s: killed after %ds" % (label, left))

            # What it managed to say before the kill is still in hand. The
            # review reports its findings and then writes a closing summary,
            # so a kill during that summary lands after the tool call: the
            # findings exist, they are in this buffer, and throwing it away
            # would tell the pull request the review "returned nothing" while
            # holding all of them. It is bytes even in text mode, because the
            # timeout path skips the decoding the normal one does.
            salvaged = expired.stdout or ""
            if isinstance(salvaged, bytes):
                salvaged = salvaged.decode(errors="replace")
            _, findings, spoken = read_stream(salvaged, label)

            # Everything a killed run leaves goes through finish(), the same
            # as a finished one: the findings it reported, or failing that
            # whatever it said, and a transcript either way. A separate
            # posting path for this case is how one of them came to say
            # "returned nothing" while the reviewer's words sat in the
            # buffer, and how a killed dry run came to leave no trace at all.
            #
            # `is not None`, not truthiness: a reviewer that reported an empty
            # list looked and found nothing, and read_stream draws that
            # distinction deliberately.
            #
            # `left`, not `config["review_timeout"]`, in both notes and in
            # the log line above. On a fallback attempt those differ, and
            # quoting the configured value would tell the pull request it
            # was killed after a duration nothing ever waited: an operator
            # grepping the log for the number in the comment finds nothing,
            # and in the floored case the comment claims half an hour for
            # an attempt that was given one second.
            if findings is not None:
                log("%s: it had already reported %d finding(s), posting those"
                    % (label, len(findings)))
                note = partial_note("was killed after %ds" % left)
            else:
                note = ("This review was killed after %ds. Read that as the "
                        "review not finishing, not as the change being clean."
                        % left)
            deliver(spoken, findings, note)
            return DONE, bool(covered), bool(reached)
        took = round(time.monotonic() - started)

        output, findings, spoken = read_stream(result.stdout, label)

        # By position, not by comparing the model to the last entry. That
        # comparison was correct only while load_config keeps refusing a
        # fallback equal to `model`: relax that rule three thousand lines
        # away, or add a second fallback, and `["x", "x"]` matches on the
        # first iteration, breaks, and disables the fallback with every
        # check in the suite still green.
        if index == len(models) - 1 or not unroutable(output, findings):
            break

        # What the abandoned attempt cost, said here because nothing else
        # will say it. `started` is reset below and the "reviewed in Nds"
        # line at the end reads only the attempt that produced the review,
        # so without this the discarded one leaves no trace of its wall
        # time in a log whose one record of daemon spend is priced(). The
        # cost prints as zero by construction, since unroutable() is what
        # let the code get here, and printing it is what makes that
        # checkable rather than merely claimed.
        log("%s: %s is not a model this account can reach, so the review "
            "runs again on %s. That attempt took %ds%s" % (
                label, model or "your Claude Code default",
                config["fallback_model"], took, priced(output)))
        abandoned = model
        # The fallback inherits what is left of the one bound, never a
        # fresh one. Floored at a second because `took` is rounded, so an
        # attempt finishing a hair under the bound could otherwise hand the
        # fallback a zero and have it killed before it started.
        left = max(1, left - took)

    if output is None:
        # No terminal event, so the process died rather than finished: killed
        # for memory, a segfault, a truncated pipe. If it had already reported
        # its findings that is still a review, and throwing it away would lose
        # it and then charge for it twice more, since FAILED is retried.
        detail = (result.stderr or result.stdout)[:400].strip()
        log("%s: claude printed no result after %ds: %s" % (
            label, took, detail))
        if findings is None:
            keep(label, repo, pr, spoken, "the stream stopped early")
            return FAILED, False, False
        log("%s: it had reported %d finding(s) first, posting those"
            % (label, len(findings)))
        deliver(spoken, findings, partial_note("stopped before it finished"))
        return DONE, bool(covered), bool(reached)

    # A non-empty list is worth acting on. An empty one proves nothing.
    #
    # `permission_denials` does not record every refusal. A denied Bash command
    # lands here; a Read refused by a path deny does not, and the array comes
    # back empty from a review that could not open a single file in its own
    # checkout. That is how exactly that failure ran unnoticed for long enough
    # to need check_paths(), and it is why the guard is a startup check on the
    # paths rather than something that reads this after the fact. Measured on
    # Claude Code 2.1.220.
    denied = output.get("permission_denials") or []
    if denied:
        # The command, not just the tool name. "denied 4 Bash calls" is not
        # something anyone can act on, and this line's own advice is to widen
        # the allow list, which needs to know what to widen.
        # Deduplicated and capped. A reviewer that keeps reaching for a
        # denied command produces one entry per attempt, and fifty identical
        # lines bury the summary beneath them in a log several repositories
        # share. Entries that are not objects are described rather than
        # dereferenced: an AttributeError here would discard the finished
        # review that is sitting one line further down.
        seen = []
        for entry in denied:
            asked = entry.get("tool_input") if isinstance(entry, dict) else None
            said = (asked.get("command") or asked.get("file_path") or asked
                    if isinstance(asked, dict) else entry)
            tool = entry.get("tool_name", "?") if isinstance(entry, dict) \
                else "?"
            line = "%s %s" % (tool, str(said)[:160])
            if line not in seen:
                seen.append(line)
        for line in seen[:10]:
            log("%s: denied %s" % (label, line))
        if len(seen) > 10:
            log("%s: and %d more distinct denied command(s)" % (
                label, len(seen) - 10))
        log("%s: %d permission denial(s). The review ran with less than it "
            "asked for; widen review-settings.json if it needed them" % (
                label, len(denied)))

    # `or ""`, not a `get` default: the key is present and null on some
    # outcomes, and a default only applies to a key that is absent. Missing
    # that turned "the review said nothing" into the four characters "None",
    # posted as though the reviewer had written them.
    # Falling back to what the reviewer actually said. `result` arrives
    # present-and-null on some outcomes, and posting "the run produced
    # nothing" while holding its closing message is a false statement about
    # a review that spoke. Both salvage paths already prefer `spoken`.
    text = str(output.get("result") or "") or spoken

    # Whether findings arrived, not which error subtype did. An error can
    # land after the reviewer has already reported: the turn limit is the
    # obvious one, but a session can fail during the closing summary just as
    # easily, and a release can add a subtype nobody here has heard of. In
    # every one of those cases the subscription is spent and the findings are
    # in hand, so discarding them loses the review and then charges for it
    # twice more, because FAILED means "worth retrying" and MAX_ATTEMPTS
    # allows three.
    #
    # Nothing reported means nothing to post, and that is the case retrying
    # is for. Deciding this on the text instead posted "Claude AI usage limit
    # reached" as though the reviewer had written it, so the text has no vote.
    # Priced before the failure is handled, not after. A run that ends in
    # an error has spent whatever it spent, and the failing path returned
    # before ever reaching this, so the only line that says what the
    # daemon costs skipped exactly the runs that bought nothing. Measured
    # on a live 529 after eight and a half minutes of xhigh: the log said
    # what broke and never said what it cost.
    spent = priced(output)

    # Said on the pull request, not only in the log. A fallback that works
    # quietly leaves the pin dead and everything looking fine: the reviews
    # keep arriving and read exactly like the configured model's. This is
    # the one place an operator who only reads pull requests can learn
    # otherwise, and it composes with the partial-run note below rather
    # than replacing it, because a run can both fall back and be cut short.
    # Tracked apart from `notes`, because the two answer different
    # questions. A note is anything the pull request should be told; this
    # is whether the reviewer reached the end of the scope. The
    # fallback-model notice is the case that separates them, and reading
    # completeness off the note turned narrowing off for good wherever
    # the pinned model had stopped routing.
    whole = True
    notes = []
    if abandoned is not None:
        notes.append(
            "This review did not run on the model Vinegar is configured to "
            "use. `%s` could not be reached, so `%s` reviewed instead. The "
            "findings stand; the configuration needs looking at."
            % (abandoned, config["fallback_model"]))

    if output.get("is_error"):
        log("%s: review failed after %ds%s: %s" % (
            label, took, spent, text[:400]))
        if findings is None:
            keep(label, repo, pr, spoken, "the review failed")
            return FAILED, False, False
        log("%s: it failed with %d finding(s) already reported, so those are "
            "posted" % (label, len(findings)))
        notes.append(partial_note("failed before it finished"))
        whole = False
    else:
        # Only when it did not fail. Moving the price above the error
        # handling so a failed run reports it left this line printing a
        # second time under the same run: the same cost counted twice by
        # anyone totalling the log, and a completed review counted by
        # anyone grepping for one.
        log("%s: reviewed in %ds%s" % (label, took, spent))

    # The outcome stays DONE whatever the posting answers: the
    # subscription is spent by this point, and re-running a review to
    # recover from a failed post would spend it again. What happens to
    # the transcript and to the token is finish()'s and posting_env()'s,
    # and each says so where it happens.
    #
    # None rather than "" when there is nothing to say, because review_body
    # tests the note for truthiness and an empty string would be as silent
    # as None while reading like a value.
    deliver(text, findings, " ".join(notes) or None, whole=whole)
    return DONE, bool(covered), bool(reached)


def sweep_checks(config, tokens):
    """Close the indicators an earlier process left spinning.

    A check run says a review is running. Nothing here can make that true
    again, so what this fixes is the lie: after a kill there is no review,
    and a run left `in_progress` blocks a merge anywhere Vinegar is a
    required check, which is the outcome CHECK_CONCLUSION exists to make
    impossible.

    The lock is the whole argument for closing them. main() holds it
    before this runs, so no other Vinegar of this deployment is polling,
    so an `in_progress` run is one an abandoned process left behind rather
    than one being written to. Nothing else here has to be inferred.

    What strands one is a kill with no `finally`: `launchctl bootout`
    sends SIGTERM and this program installs no handler, so an in-flight
    review dies where it stands. On the serial code that mostly heals
    itself on the next poll, because the entry is FAILED at that head and
    open_check() reuses the spinner.

    So what this delivers is mostly the *bound*, and the claim should not
    be made larger than that. Reuse leaves the run spinning through the
    whole next review, nine to twenty-two minutes, where this closes it
    seconds after the daemon comes back; a required check is stuck for
    that whole window otherwise. The one case reuse never reaches at all
    is a pull request newly skipped before the retry and still sitting at
    the head the killed review ran on: a draft toggled is the example,
    because toggling one moves nothing. Growing a branch past
    `max_changed_lines` is *not* an example, however well it reads as
    one, because growing it pushes commits and the head moves away from
    the stranded run, into the blind spot the next paragraph describes.
    That bound is what lets a parallel fan-out choose the simplest stop
    it can rather than a perfect one, which is the question six review
    rounds of PR #29 never settled.

    Open pull requests only, and their heads only. Two limits, and both
    are real rather than tidy:

    A run stranded at a commit the head has moved past is not found.
    Check runs are read per commit and no endpoint lists a repository's,
    so finding those means walking every commit of every pull request.
    The pull request shows its head commit's checks and nothing shows the
    others, so what is left is invisible.

    A run stranded on a pull request since closed or merged is not found
    either, because open_prs() asks for open ones. That is not the loss
    it first reads as: the harm being repaired is a stuck required check
    blocking a merge, and nothing blocks a merge that has already
    happened or been abandoned. Do not widen the listing to `--state all`
    to reach them; that is every pull request the repository has ever had,
    for a check nobody is waiting on.

    Closed rather than spared, including at heads a retry would reuse.
    Sparing them keeps one tidy entry per pull request and costs the
    window this exists to bound, and it would mean deciding here whether
    handle_pr is going to review something, which is a decision written
    once already. The cost taken instead is a pull request listing a
    neutral entry above a fresh running one, which open_check() argues at
    length is honest history rather than a defect.

    Every failure swallowed, per repository and per pull request, for
    check_api()'s reason: an indicator is not worth a poll. A repository
    whose listing fails is swept on the next start, and the daemon must
    reach its loop either way.

    The third limit, and the one an operator meets first: a repository
    that answers nothing at all stops being asked after one pull request.
    That is the `checks` permission granted on the App and not yet
    accepted on the installation, which check_api() logs a paragraph for
    every time. Once anything has answered, a later failure is transient
    by demonstration and only that pull request is skipped.
    """
    # A dry run puts nothing on a pull request, so it has nothing to
    # close; without an App these runs belong to no one Vinegar can PATCH.
    # open_check() refuses on the same pair, and a sweep that ran where
    # the opener cannot would be closing another App's check runs.
    if not config["comment"] or not config.get("github_app"):
        return
    for repo in config["repos"]:
        try:
            env = github_env(config, repo, tokens, good_for=LISTING_GRACE)
            prs = open_prs(repo, env)
        except Exception as err:
            log("%s: cannot list pull requests to close old checks: %s"
                % (repo, err))
            continue
        answered = False
        for pr in prs:
            # Inside the try, not above it. `pr_key` subscripts `number`,
            # and open_prs only establishes that each entry is a dict, so
            # a listing answering 0 with error objects raised KeyError
            # from here, past the per-repository handler already exited
            # and out of sweep_checks entirely. main() catches only
            # KeyboardInterrupt, so that killed the process before it
            # reached the poll loop, and launchd restarted it into the
            # same line every thirty seconds: a working deployment
            # polling nothing at all. poll_once survives the same input
            # because its pr_key call is inside its own try.
            try:
                label = pr_key(repo, pr)
                found = running_checks(label, repo, pr["headRefOid"],
                                       config, env)
            except Exception as err:
                log("%s#%s: could not read its old checks: %s"
                    % (repo, pr.get("number", "?"), err))
                continue
            # A repository that has answered nothing at all is dropped
            # rather than asked once per pull request. check_api logs a
            # three-line paragraph naming the permission when `checks` is
            # granted but not yet accepted on the installation, and
            # unbounded that is one paragraph per open pull request per
            # start, repeated every thirty seconds by launchd's restart,
            # burying the line that says which Vinegar is up.
            #
            # `answered` is what makes that a repository-wide judgement
            # rather than a per-call one. Read as "any failure ends the
            # repository", a single 502 on the twentieth of thirty pull
            # requests abandoned the last ten for the whole window this
            # exists to bound, and paid the cost of a permission failure
            # for none of its benefit. A failure after something has
            # worked is transient by demonstration, so it is skipped like
            # any other and the rest of the repository is still swept.
            if found is None:
                if answered:
                    continue
                log("%s: cannot read its check runs, so the rest of this "
                    "repository is swept on a later start" % repo)
                break
            answered = True
            # No try here. It wrapped running_checks(), whose subscript
            # could raise, and that call has moved up into the guard
            # above. What is left cannot raise: log() formats two
            # strings, and close_check() does dict lookups on a literal
            # it was just handed plus one check_api() call, which returns
            # None on every failure path rather than raising. A handler
            # over it would be a branch no mutation can arm, because
            # deleting it would change nothing.
            for was in found:
                log("%s: closing the check run left spinning by a "
                    "Vinegar that stopped" % label)
                close_check(label, {"repo": repo, "id": was,
                                    "closed": False},
                            "The review was interrupted", env,
                            "A Vinegar stopped while this review was "
                            "running. Nothing is reviewing this commit "
                            "now. If the pull request is still due a "
                            "review, the next poll starts one.")


def poll_repo(repo, config, state, tokens):
    """One repository's whole pass: list it, then work through what is open.

    Its pull requests one at a time, always. Two reviews of one repository
    would share the one checkout directory checkout() gives it, and the
    second one's `git reset --hard` would move the tree under the first,
    which then reports findings about a commit nobody asked about.
    `parallel_repos` buys concurrency between repositories and not inside
    one.

    So a repository waits out the slowest repository's *whole pass*, not
    one review: the fan-out is per pass, and main() sleeps for
    `poll_interval` only once every worker has finished. Five open pull
    requests on one repository, twenty minutes each, is a hundred minutes
    before the other repository is listed again. Worth knowing when sizing
    `poll_interval`, and the reason giving each repository its own loop
    would be a different change rather than a bigger number.
    """
    try:
        prs = open_prs(repo, github_env(config, repo, tokens,
                                        good_for=LISTING_GRACE))
    except Exception as err:
        log("%s: cannot list pull requests: %s" % (repo, err))
        return
    for pr in prs:
        # Read here, so a stop reaches this pass without it first working
        # through every pull request the listing returned. STOPPING's own
        # comment says why between pull requests is the right place.
        if STOPPING.is_set():
            return
        try:
            handle_pr(repo, pr, config, state, tokens)
        except Exception as err:
            # One bad pull request must not stop the daemon. Under launchd
            # a crash restarts the process every 30 seconds and polls
            # nothing in between.
            log("%s#%s: unhandled error: %s" % (
                repo, pr.get("number", "?"), err))


def poll_width(config):
    """How many repositories this pass will really run at once.

    Both poll_once and the line main() prints at startup need this, and as
    two expressions they disagreed: the startup line announced
    `parallel_repos` while the pass ran at the clamped number, so a
    one-repository install told to run four at a time said so and then
    polled serially. That line exists to confirm the config took, which is
    the one job it cannot do while it names a width nothing uses.
    """
    return min(config["parallel_repos"], len(config["repos"]))


def poll_once(config, state, tokens):
    """Every configured repository, poll_width() of them at a time."""
    # One repository is the default and keeps the whole pass on the thread
    # main() is already on, rather than handing it to a worker. That is not
    # only tidiness: a foreground run is stopped with Ctrl-C, and a
    # KeyboardInterrupt raised on the main thread unwinds a review that
    # thread is running while a worker's review carries on. Every install
    # that has not asked for this keeps that behaviour exactly.
    width = poll_width(config)
    if width <= 1:
        for repo in config["repos"]:
            poll_repo(repo, config, state, tokens)
        return

    # A queue rather than a thread each, because `parallel_repos` can be
    # smaller than the number of repositories, and the ones over the width
    # have to wait for any worker rather than for a particular one.
    todo = queue.Queue()
    for repo in config["repos"]:
        todo.put(repo)
    # Text for every failure, and the exception object once. `first` is a
    # list because a worker assigns it and a closure cannot rebind a name
    # it did not define, and `hurt` is what makes the pair atomic: the
    # first spelling of this bound tested `not fell_over` and then
    # appended, two separate bytecodes, so four workers failing in the
    # same instant all read the list empty and all four stored their
    # exception. That is the exact case the bound exists for, a settings
    # file the reviewer cannot use putting every repository in here at
    # once, and the guard did nothing there.
    #
    # Homogeneous, which the raise below now depends on less rather than
    # more: a mixed list of exceptions and strings made `fell_over[0][1]`
    # correct only while index 0 was never a string.
    fell_over, first, hurt = [], [], threading.Lock()

    def passes():
        while not STOPPING.is_set():
            try:
                repo = todo.get_nowait()
            except queue.Empty:
                return
            try:
                poll_repo(repo, config, state, tokens)
            except BaseException as err:
                # Kept rather than raised. A raise here ends one worker
                # silently, and the repositories still in the queue are
                # then shared out among the others as if nothing had
                # happened.
                #
                # BaseException, not Exception. This replaced a pool, and
                # a pool's work item catches BaseException and hands it
                # back on result(), so the narrower catch quietly lost a
                # class of failure the old shape reported. It is reachable:
                # review() builds the reviewer's settings through
                # load_settings(), which sys.exits on a settings file it
                # cannot use and is re-read on every review precisely so a
                # mid-run edit is caught. That SystemExit killed one worker
                # with `fell_over` still empty, so the pass reported every
                # repository clean and one of them was never reviewed
                # again, with nothing in the log. KeyboardInterrupt cannot
                # arrive here, because signals reach the main thread only.
                # The text for every one, the exception itself for the
                # first only. Each stored exception pins its
                # `__traceback__`, and through it every frame and local of
                # the pass that failed: handle_pr's and review()'s frames
                # hold the reviewer's transcript, the findings list and
                # the review body, megabytes per repository, kept alive
                # until this pass returns. The worker loop takes the next
                # repository after each catch, so a settings file the
                # reviewer cannot use puts every repository in here at
                # once. Only the first is ever re-raised; the rest exist
                # for the `%s` below.
                with hurt:
                    if not first:
                        first.append(err)
                    fell_over.append((repo, str(err)))

    # Not daemons, and joined here rather than by the interpreter. Both
    # halves matter and each was learned from a failure.
    #
    # A ThreadPoolExecutor's shutdown ran at interpreter exit, which is
    # after main() had logged "stopped" and released the lock: measured
    # with three repositories and two workers, the third repository's pass
    # *started* after the daemon said it had gone, with the lock free for a
    # `--pr` run to take and reset a tree a live review was reading. Making
    # the workers daemons ended that by having the interpreter kill them
    # instead, which was worse in a quieter way: a daemon thread killed at
    # finalization runs no finally block, so handle_pr never closed its
    # checks entry and every in-flight pull request kept a Vinegar check
    # spinning for ever, blocking a merge wherever that check is required.
    #
    # Joining inside the interrupt handler keeps both. The lock is still
    # held, because main() has not returned from here; the queue is
    # abandoned, so no repository that had not started starts now; and the
    # passes still running unwind on their own and close what they opened.
    workers = [threading.Thread(target=passes,
                                name=POLL_WORKER + str(nth))
               for nth in range(width)]

    # No signal handling here at all, deliberately, and the paragraph is
    # worth reading before adding some back. Four attempts at making a
    # stop correct by guarding it were each reopened by the next
    # interrupt: a pool joined at interpreter exit, daemon threads that
    # skipped handle_pr's finally, a refusal that could not cover the log
    # calls around it, and a deafness installed a few hundred bytecodes
    # too late. Every one of them ended the same way, with main()'s
    # finally freeing the lock while a worker was still reading its
    # checkout.
    #
    # So the lock is what changed, not the signal handling. release_lock()
    # declines while a pass is alive, and the kernel drops the lock when
    # the process dies, which cannot happen first because these threads
    # are not daemons. That holds however the interrupt arrives and
    # whatever it interrupts, so nothing here has to be timed correctly.
    #
    # What is left is best effort and is allowed to be. An interrupt that
    # lands before STOPPING is set costs the early stop: the passes drain
    # the queue and the poll is paid for in full. That is a bill, not a
    # review of the wrong commit, and it is the only thing at stake now.
    #
    # And the one case this cannot reach is now caught elsewhere, which is
    # what changed while this branch sat closed. A second interrupt
    # arriving during interpreter shutdown kills these threads inside
    # threading._shutdown()'s wait, which is interruptible and does not
    # retry, so their `finally` never runs and every pull request in
    # flight keeps a check run spinning. That was the last thing standing
    # between this design and being good enough, and it is not a hole to
    # be closed here: sweep_checks() closes those runs on the next start,
    # deliberately, and PR #31 was built first so that this file would not
    # have to get an interrupt perfectly right. Do not add signal handling
    # back to buy what the sweep already pays for; four attempts at that
    # are recorded above.
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
    except BaseException:
        # One try over both loops. Split in two, a RuntimeError out of
        # start() -- thread creation refused under an RLIMIT_NPROC ceiling
        # -- left STOPPING clear and the workers that had started draining
        # the whole queue. Measured: one failed start, and the surviving
        # worker went on to review every remaining repository.
        STOPPING.set()
        # Said so the wait that follows is not read as a hang. The process
        # does not exit until the passes finish, and the lock is theirs
        # until it does.
        log("stopping: the passes already running keep the lock until they "
            "finish; kill the process to force it")
        raise

    # Every pass that fell over is named, and then the first is raised.
    # poll_repo catches the two failures it names and no others, so
    # anything reaching here ended the process before this existed, which
    # under launchd is a restart and a line in the error log rather than a
    # repository that quietly stops being reviewed. Reading one result at a
    # time raised on the first and never looked at the rest, so a second
    # repository failing in the same round was discarded and the traceback
    # named one repository where two had failed.
    for repo, said in fell_over:
        log("%s: its whole pass fell over: %s" % (repo, said))
    if first:
        raise first[0]


def give_up(key, repo, pr, config, attempts, tokens, path=None, env=None,
            tries=0, resent=False):
    """Say, once, that Vinegar has stopped trying to review this.

    Called from two moments that must say the same thing: the last attempt
    returning FAILED in-process, and a later poll discovering a spent
    budget whose attempt never returned at all, because the pre-review
    marker charges the attempt before review() runs and a kill between the
    two leaves no one behind to announce anything. `path` and `env` exist
    only at the first moment; `env` still matters there, because
    posting_env() falls back to it when a mint fails, and a fallback of
    None leaves `gh` running under whatever ambient login the daemon
    happens to have rather than as the App.

    Answers whether it was said, so the caller marks the entry announced
    only when it actually reached the pull request. Marking regardless
    turned one bad minute at GitHub into permanent silence: every later
    poll saw the mark and returned.
    """
    # The decision once, the retries as retries. This runs again on every
    # poll while the announcement fails to land, and three identical
    # "leaving it alone" lines in a log several repositories share read as
    # three separate pull requests being abandoned.
    if tries:
        # No count against the bound. `tries` here includes attempts that
        # were waived for being rate limited, so printing it against
        # MAX_ATTEMPTS produced "attempt 5 of 3", which reads as a broken
        # bound rather than as three forgiven attempts.
        log("%s: trying again to say that Vinegar gave up" % key)
    else:
        log("%s: %d failed attempts, leaving it alone. Fix the cause, then "
            "stop Vinegar, delete its entry from %s, and start it again" % (
                key, attempts, STATE_PATH))
    # Said on the pull request, not only in a log nobody is watching.
    # Silence has to keep meaning that something broke, which is only true
    # if the giving-up is announced.
    return announce(key, lambda: finish(
        key, repo, pr, path, "", None, config, env, tokens,
        note="Vinegar tried to review this %d times and each attempt "
             "failed before it could report anything. Read that as the "
             "review not running, not as the change being clean." % (
                 attempts,),
        verb="gave up on", preserve=True, resent=resent or bool(tries)))


def spend_announce(key, config, state, head, attempts, tries, said):
    """Count one attempt at announcing a give-up, and record where it left
    things.

    One place, called by three: the in-process give-up, the discovery of
    a spent budget on a later poll, and the mint failure that has nothing
    to send at all. The rule for when a pull request is abandoned lives
    here so those three cannot come to disagree about it.

    Bounded because unmarked means "try again next poll": an App without
    `pull_requests: write`, a locked pull request or an archived
    repository refuses every time, and without a cap that is two API calls
    and a log line a minute, per stuck pull request, for ever. After
    MAX_ATTEMPTS the entry is marked anyway and the giving-up stays in the
    log, which is the honest end of a pull request Vinegar cannot write to.

    A dry run never marks anything announced. post_review() answers True
    there because posting nothing is what a dry run asked for, but that
    answer must not reach `state.json`: one `--dry-run` against the real
    VINEGAR_HOME would otherwise tell the live daemon the give-up had
    already been said, and the pull request would stay silent for good.

    It writes nothing at all for a dry run, not even the attempt count.
    What keeps that from becoming an endless retry is handle_pr(), which
    does not enter the discovery branch when commenting is off: a dry run
    has nothing to announce, so there is nothing for it to come back and
    try again. Counting here as well would spend the live daemon's budget
    on a run that never posted anything.
    """
    if not config["comment"]:
        return
    if said == THROTTLED:
        # Waived, and bounded like repost()'s. A rate limit refuses
        # without judging the request, so spending an attempt on it let
        # three polls a minute apart against a limit that resets hourly
        # mark the entry announced — and a pull request that was never
        # told anything is the silence the README does not allow.
        waived = state.get(key, {}).get("announce_waivers", 0)
        if waive(key, "the give-up", waived):
            remember(state, key, dict(state.get(key, {}),
                                      announce_waivers=waived + 1))
            return
        said = False
    tries += 1
    spent = tries >= MAX_ATTEMPTS
    if not said and spent:
        log("%s: the give-up could not be posted in %d attempts, so it "
            "stays in this log only" % (key, tries))
    was = state.get(key, {})
    entry = state_entry(head, FAILED, attempts,
                        announced=said == POSTED or spent, tries=tries,
                        **dict(carry_forward(was),
                               **reviewed_through(False, head, was),
                               **rounds_done(False, was)))
    remember(state, key, entry)


def record_once(state, key, done, head, outcome, reason):
    """Record a decision that repeats, and say it only when it changes.

    A skip and a failed checkout are both decided again on every poll and
    both have to keep the attempts already burned at this head. Written
    out twice, the preservation was fixed in one of them and only later
    in the other, which is the drift these helpers exist to end.
    """
    same = (done.get("outcome") == outcome and done.get("sha") == head
            and done.get("reason") == reason)
    seen = done.get("seen", 0) + 1 if same else 1
    if same:
        # Said again as it becomes a wedge rather than a blip. Silencing
        # the repeat entirely was right for noise and wrong for this:
        # a checkout that fails permanently is exempt from MAX_ATTEMPTS
        # on purpose, so nothing else ever mentions the pull request
        # again, and one line from weeks ago is indistinguishable from
        # having judged it. Tenfold intervals keep that rare.
        if seen not in (10, 100, 1000) and seen % 10000:
            # Through remember() although it saves nothing, because the
            # lock is what it is here for. This is an insertion for a pull
            # request whose entry may be new, and an insertion is what
            # makes another repository's save raise part way through the
            # file.
            remember(state, key, dict(done, seen=seen), write=False)
            return
        log("%s: still true after %d polls: %s" % (key, seen, reason))
    else:
        log("%s: %s" % (key, reason))
    kept = done if done.get("sha") == head else {}
    # `kept` for the head-scoped counters, `done` for the one field that
    # outlives the head. A skip or a failed checkout at a new commit must
    # not be what forgets where the last real review got to: the pull
    # request would then be read whole on the next pass that does run,
    # silently, with only the bill to show for it.
    entry = state_entry(head, outcome, kept.get("attempts", 0), reason,
                        **dict(carry_forward(kept),
                               **reviewed_through(False, head, done),
                               **rounds_done(False, done)))
    entry["seen"] = seen
    remember(state, key, entry)


def handle_pr(repo, pr, config, state, tokens):
    key = pr_key(repo, pr)
    head = pr["headRefOid"]
    done = state.get(key) or {}

    # A review that was written but never reached the pull request is
    # finished work waiting on one API call, so it is retried before
    # anything else decides this pull request is done.
    if config["comment"]:
        # The scan behind the stat runs only when the entry says a saved
        # review is actually waiting at some other head. Gating on "the
        # head has moved" alone meant every pull request that was ever
        # reviewed and then pushed to listed the whole reviews directory
        # once a minute for the life of the daemon, looking for a prefix
        # that was never going to be there.
        # FAILED counts as "a marker may be out there" as well as the
        # flag does. The flag is written after review() returns while the
        # marker is written inside it, so a process killed between the
        # two leaves a marker behind and an entry that denies it; if the
        # head then moved, nothing would ever look for it again.
        marker, saved_sha = unposted_for(
            repo, pr,
            scan=done.get("sha") != head and bool(
                done.get("unposted") or done.get("outcome") == FAILED))
        if marker and saved_sha is None:
            # Unreadable, which is not the same as belonging to another
            # commit. Believing the entry rather than deleting a paid-for
            # review is the safe way to be wrong: the repost reads the
            # transcript next, and gives up on its own budget if that
            # cannot be read either.
            log("%s: the unposted marker cannot be read, so the entry's own "
                "commit is used" % key)
            saved_sha = done.get("sha")
        # Tied to the entry that produced it. Without that, the repair the
        # log recommends — delete the entry and let it be reviewed again —
        # made the next poll post the *old* transcript instead, and the
        # poll after that review and post again: two reviews, which is
        # what the operator was trying to avoid. A marker with no entry
        # behind it is left over from a review that has been forgotten.
        #
        # The sha decides it, not the outcome. The marker is written
        # inside review(), before handle_pr records how the review ended,
        # so a process killed in that window leaves it beside a FAILED
        # entry — the same window `resent=attempts > 1` exists for one
        # branch below, and requiring DONE here deleted the finished
        # review it was written to protect.
        #
        # An entry with no sha at all is the deleted-entry case, and it
        # has to be caught before the comparison: with an unreadable
        # marker `saved_sha` becomes that same missing sha, None equals
        # None, and the stale transcript was posted to the pull request
        # the operator had just cleared in order to have it reviewed
        # afresh — followed by the real review, which is the pair of
        # reviews the whole rule exists to prevent.
        if marker and (done.get("sha") is None
                       or done.get("sha") != saved_sha):
            log("%s: an unposted review is left over from a run that is no "
                "longer recorded, so it is forgotten" % key)
            forget(marker)
            # And the flag comes off with it, as it does in repost().
            # Left set, the scan gate stays armed and every later poll
            # lists a directory that only grows to learn that a file it
            # deleted is still gone.
            if done.get("unposted"):
                cleared = dict(done)
                cleared.pop("unposted", None)
                remember(state, key, cleared)
        elif marker and done.get("post_tries", 0) < MAX_ATTEMPTS:
            repost(key, repo, pr, config, state, tokens, done, marker,
                   saved_sha)
            return

    # Only a finished review closes a pull request off. A skip is decided
    # again on every poll, because the filter that caused it can stop
    # applying: a draft is marked ready, an oversized pull request is split
    # down, a login is added to `authors`.
    if done.get("outcome") == DONE:
        if done.get("sha") == head or not config["review_on_push"]:
            return

    # A review that never ran is worth retrying, because it spent nothing. The
    # case this protects is a rate-limit window: without it, every pull request
    # opened during the window is written off as reviewed and never looked at
    # again once the limit resets.
    #
    # FAILED is the only outcome that can be carrying a spent budget: the
    # skip branch below preserves `attempts` but runs after this return, so
    # an exhausted entry keeps saying FAILED and keeps meeting this check.
    # The laundering went the other way, a skip landing while attempts were
    # still below the cap and dropping them; keeping them through the skip
    # is what closed it.
    if done.get("outcome") == FAILED and done.get("sha") == head:
        if done.get("attempts", 0) >= MAX_ATTEMPTS:
            # The last attempt can die without returning: the marker below
            # is written before review() runs, so a kill mid-attempt leaves
            # a spent budget that no one ever announced, and every later
            # poll returned here with the pull request permanently silent.
            # The discovery announces it instead, once.
            # A dry run has nothing to announce and no token to mint for
            # it. The in-process give-up already wrote its transcript,
            # which is a dry run's whole output; coming back here every
            # poll to re-say it would be the log spam the bound forbids.
            if not done.get("announced") and config["comment"]:
                # Its own token, minted here. posting_env() falls back to
                # what it is given, and this path had nothing to give: the
                # give-up would have gone out under the operator's own
                # `gh` login rather than as the App, or failed with an
                # auth error that reads like a GitHub outage.
                tries = done.get("announce_tries", 0)
                try:
                    post_env = github_env(config, repo, tokens,
                                          good_for=POST_GRACE)
                except Exception as err:
                    # Counted, not just skipped: a repository whose token
                    # never mints would otherwise be retried every minute
                    # for ever, which is the bound this branch is under.
                    log("%s: cannot mint a token to announce the give-up: "
                        "%s" % (key, err))
                    spend_announce(key, config, state, head,
                                   done["attempts"], tries, False)
                    return
                # None is not a failure here: it is what github_env answers
                # when no App is configured, and ambient `gh` is then the
                # credential the operator chose.
                # `resent` whatever the count says. This branch is a
                # rediscovery by definition, and the case it exists for —
                # a process killed between posting and recording — leaves
                # `announce_tries` at 0, so trusting the count skipped the
                # check on exactly the run that needed it and posted the
                # give-up twice.
                # On its own line: this posts to the pull request, and as
                # an argument expression it ran before the function meant
                # to govern it had a say, which a reader has to know
                # Python's evaluation order to see.
                # `tries` here counts every announcement attempt made,
                # waived ones included, because it only decides whether
                # this is the first time we are saying it. Taking the
                # charged count instead reprinted the whole "leaving it
                # alone" paragraph on every rate-limited poll, which in a
                # shared log reads as four pull requests being abandoned.
                said = give_up(key, repo, pr, config, done["attempts"],
                               tokens, env=post_env,
                               tries=tries + done.get("announce_waivers", 0),
                               resent=True)
                spend_announce(key, config, state, head, done["attempts"],
                               tries, said)
            return

    reason = skip_reason(pr, config)
    if reason:
        # Deciding again is free. Saying so every minute is noise, and the
        # attempts burned at this head have to ride along: dropping them
        # is what let a draft toggle launder the retry budget.
        record_once(state, key, done, head, "skipped", "skipped, %s" % reason)
        return

    # Credentials are minted here, once a review is actually going to happen,
    # rather than for every pull request the pass looks at.
    #
    # Still per review rather than once per pass: a pass can run for hours, and
    # a token minted at the top of it would expire under a later review, whose
    # comments would then fail to post. What is asked for covers the checkout
    # as well, because that runs first and on this one token.
    #
    # Asking before the checks above meant minting for every open pull request
    # on every poll, including the ones that return one line later as already
    # reviewed, and a transient 5xx on that endpoint took the pull request out
    # of the pass. Whether anything deduplicated it depended on a setting that
    # looks unrelated: the cache serves a token only while
    # `now + good_for < expires`, so once `good_for` reaches the hour a token
    # lives, no token can ever satisfy it and every call is a fresh mint. At the
    # shipped `review_timeout` of 1800 the cache did dedupe, for the first half
    # of each token's life. Raising it to 3600 turned that off silently, which
    # is how this ran at roughly 1440 tokens a day per open pull request without
    # anyone noticing.
    #
    # That ceiling still applies to the sum below. Set `review_timeout` high
    # enough and no obtainable token can outlast the review, which is worth
    # knowing when revisiting it: past that point the guarantee is gone rather
    # than merely expensive.
    env = github_env(config, repo, tokens,
                     good_for=checkout_grace(config))

    try:
        path = checkout(repo, pr, env)
    except Exception as err:
        # A checkout spends no subscription budget, so leave this pull request
        # unrecorded and try it again on the next poll.
        #
        # Deliberately not bounded by MAX_ATTEMPTS. The failures here are
        # mostly transient (a network blip during clone, a lock held by
        # another git), retrying costs nothing but disk, and three polls is
        # three minutes: bounding it would abandon a pull request over a
        # brief outage, which is worse than retrying a broken one. What
        # was worth fixing is the noise, so the same failure is said once
        # rather than once a minute for ever.
        # Recorded the same way a skip is, and for the same reasons: said
        # once rather than once a minute, and keeping the attempts burned
        # at this head. Writing a bare entry handed the retry budget back,
        # so a flapping clone re-reviewed at full cost on every other poll
        # while MAX_ATTEMPTS was never reached.
        record_once(state, key, done, head, "checkout",
                    "checkout failed: %s" % err)
        return

    attempts = done.get("attempts", 0) + 1 if done.get("sha") == head else 1

    # Recorded before the review runs, not after. Nothing inside review() can
    # protect against the process simply ceasing to exist: a SIGKILL, a power
    # cut, launchd booting the job out mid-review. Without a mark already on
    # disk, the restart finds no entry, reviews the same pull request again,
    # and does so on every restart for ever. Written as FAILED because that is
    # what an interrupted attempt is, and because MAX_ATTEMPTS then bounds the
    # damage at three rather than leaving it open-ended. The real outcome
    # overwrites it a few lines below.
    # Carried only while the head is the same one, exactly as `attempts`
    # is computed above. A budget spent on one head's saved review must
    # not follow the pull request to the next: with `review_on_push` on,
    # a head that had exhausted its three sends made every later head's
    # review unpostable the moment it was written.
    kept = done if done.get("sha") == head else {}
    remember(state, key, state_entry(
        head, FAILED, attempts,
        **dict(carry_forward(kept), post_tries=0, waivers=0,
               **reviewed_through(False, head, done),
               **rounds_done(False, done))))

    # Worked out before the review, because it is what the review is told,
    # and after the checkout, because both probes read this clone.
    since = review_scope(path, pr, done, env, key)
    if since:
        log("%s: reviewing what was added since %s" % (key, since[:7]))

    # Worked out from the entry rather than from a counter of this run,
    # because the round this review is has to survive the process dying
    # between two reviews. Nothing stops re-reviewing a pull request now,
    # deliberately, so a branch pushed to forty times buys forty reviews;
    # the state file records that and this call's log line is what makes
    # it greppable before the bill arrives.
    blockers = this_round(done, config, key)

    # Opened here rather than inside review(), so that the one place that
    # sees every ending is also the place that finishes it. review()
    # returns FAILED on two paths and can raise out of a third, and none
    # of those knows whether MAX_ATTEMPTS has just run out, which is the
    # difference between "it will be tried again" and "it was given up
    # on".
    # Everything from here to the give-up sits in a try, so that the one
    # line that finishes the indicator cannot be skipped. The end of this
    # function is reachable only when nothing goes wrong: save_state
    # below raises on a full disk, a failure this function already treats
    # as real, and a foreground run is stopped with Ctrl-C. Under launchd
    # there is no interrupt at all: `bootout` is a SIGTERM that Python
    # installs no handler for, so the process dies here without running
    # this finally or any other, which is the case the pre-review marker
    # on disk exists for rather than this try.
    # KeyboardInterrupt is not an Exception, so it walks past every
    # handler here untouched. Either way the pull request was left
    # carrying a Vinegar check that spins for ever, and a stuck run
    # blocks a merge wherever the check is required.
    outcome = FAILED
    covered = reached = False
    check = None
    try:
        # Inside the try, because opening it is the one call here that
        # parses a reply GitHub sent. check_api swallows every way the
        # subprocess can fail, but a 2xx whose `check_runs` is not a list
        # of objects raises out of the comprehension below it, and from
        # above the try that escaped handle_pr entirely: with FAILED
        # already on disk, the review never ran, the outcome was never
        # recorded, the give-up never fired, and the next poll did it
        # again until MAX_ATTEMPTS was spent on a pull request nobody had
        # reviewed. check_api's docstring promises nothing here is worth a
        # review; this is what makes that structural.
        check = open_check(key, repo, pr, config, env, blockers)
        try:
            # A second attempt at a head asks before posting. The marker
            # above is written before review() runs and the real outcome
            # only after, so a process killed in between — launchd
            # booting the job out, a save_state that raises on a full
            # disk — leaves FAILED on disk for a review that did post.
            # Without this the retry re-reviews at full cost and posts a
            # complete second review with duplicate inline comments. The
            # give-up rediscovery already says `resent` for the same
            # crash window.
            outcome, covered, reached = review(
                path, repo, pr, config, env, tokens, resent=attempts > 1,
                check=check, since=since, blockers=blockers)
        except Exception as err:
            # The subscription is spent by the time most of these can
            # happen, and an unrecorded pull request is reviewed again on
            # the very next poll, at full cost, for ever. announce()
            # covers the posting; this covers everything else review()
            # touches, including the two read_stream calls and `claude`
            # missing from PATH entirely. Recording FAILED keeps
            # MAX_ATTEMPTS in charge of how many times that may repeat.
            log("%s: the review did not complete: %s" % (key, err))
            outcome, covered, reached = FAILED, False, False

        # Recorded with whether a saved review is waiting behind it, so the
        # next poll can find that out without listing a directory.
        # post_tries reset: this review writes its own transcript over any
        # saved one, so the budget that governed the old copy is void. Kept,
        # it met the new marker already spent and nothing would repost or
        # forget it.
        # The marker says a review is waiting to be sent. It does not say
        # the author saw nothing, which is what the round count needs and
        # what review() now answers: finish() writes the marker only when
        # the transcript write succeeded, so a run that could neither save
        # nor post leaves none and was counted as a round nobody saw.
        remember(state, key, state_entry(
            head, outcome, attempts,
            **dict(carry_forward(kept), post_tries=0, waivers=0,
                   unposted=os.path.exists(unposted_path(repo, pr)),
                   **reviewed_through(covered, head, done),
                   **rounds_done(reached, done))))

        if outcome == FAILED and attempts >= MAX_ATTEMPTS:
            # Marked only if it was said, so the restart path knows.
            # Without the mark a daemon restart would say it all again;
            # with it applied regardless, a failed announcement was never
            # retried at all.
            tries = done.get("announce_tries", 0)
            said = give_up(key, repo, pr, config, attempts, tokens, path,
                           env, tries + done.get("announce_waivers", 0))
            spend_announce(key, config, state, head, attempts, tries, said)
    finally:
        # Its own credentials, minted now. The ones above were asked to
        # cover the checkout and the review, and by here a full-length
        # review has spent all of that: closing on them was a 401 at the
        # exact moment the indicator most needs finishing.
        close_check(key, check, ended_title(outcome, attempts),
                    posting_env(key, config, repo, tokens, env) or env)


def find_pr(repo, number, env):
    """Read the one pull request named by an `owner/repo#number` target."""
    target = "%s#%s" % (repo, number)
    # Bounded like the listing it mirrors. A `--pr` run holds the lock for
    # as long as it lasts, so an unanswered socket here also keeps the daemon
    # from starting.
    try:
        result = run(["gh", "pr", "view", number, "-R", repo,
                      "--json", PR_FIELDS], env=env, timeout=LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        sys.exit("cannot read %s: timed out after %ds" % (target, LIST_TIMEOUT))
    if result.returncode != 0:
        sys.exit("cannot read %s: %s" % (
            target, both_streams(result, 400)))
    # Said in one sentence like the two failures above it. `gh` exiting
    # zero with something that is not the object asked for is unlikely
    # and not impossible, and a traceback out of here reads as a bug in
    # Vinegar rather than as an answer it could not use.
    try:
        return json.loads(result.stdout)
    except ValueError as err:
        sys.exit("cannot read %s: %s did not answer with the pull request "
                 "(%s)" % (target, "gh pr view", err))


# The locked descriptor, held open for the life of the process. Closing it,
# including by letting it fall out of scope and be collected, drops the lock.
_lock_handle = None


def acquire_lock():
    """Refuse to start when another Vinegar already holds the lock.

    Two Vinegars sharing a repo's checkout is not a small problem: the second
    one runs `git reset --hard` under the first one's review, which then
    reports findings against a commit nobody asked about.

    The lock is the kernel's flock on the file, not the pid written in it. A
    pid means nothing once its process is gone: pids are reused, and after a
    reboot they are all reused. Deciding staleness by asking whether the
    recorded pid is alive gets that wrong in both directions. If the number
    now belongs to some unrelated process, a dead Vinegar looks alive and
    nothing starts until someone deletes the file by hand; that is not
    theoretical, it stopped this daemon for 78 minutes when a reboot handed
    pid 780 to a system extension. If the number belongs to a Vinegar in
    another VINEGAR_HOME, a live one looks like ours. flock has no such
    question to answer. The kernel releases it when the process dies, however
    it dies, so a crash, a SIGTERM with no handler, and a power cut all leave
    a lock that is already free.

    The pid is still written, because the watchdog and anyone reading the log
    need to know which process to look at, but nothing decides anything by it.

    The file outliving the process is now normal rather than a sign of a crash.
    It is never unlinked, so it survives `--once` finishing, a Ctrl-C, and a
    clean shutdown alike, and from then until the next start it names a process
    that is gone. Nothing outside this module may read it as evidence Vinegar
    is alive: the file existing means nothing, and the pid in it means nothing
    until you have checked that the process it names is really this daemon.
    """
    global _lock_handle
    os.makedirs(HOME, exist_ok=True)
    # The file is never unlinked, here or in release_lock. Removing it would
    # open the race the lock exists to close: a second Vinegar can hold the
    # old inode locked while a third creates a new one at the same path and
    # locks that, leaving two holders who cannot see each other.
    #
    # Opening RDWR rather than the old WRONLY|EXCL means an existing lock file
    # now has to be writable, which one `sudo python3 vinegar.py` is enough to
    # break: it leaves the file owned by root and every later unprivileged
    # start fails here. That is worth a sentence naming the file, because the
    # bare traceback it would otherwise raise sends the reader looking at the
    # lock logic instead of at `ls -l`.
    try:
        handle = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as err:
        sys.exit("cannot open the lock file %s: %s" % (LOCK_PATH, err))
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as err:
        os.close(handle)
        # Only contention means another Vinegar. A filesystem with no working
        # flock, or a kernel out of lock structures, fails here too, and
        # reporting those as "already running" would send the operator hunting
        # for a second process that does not exist while KeepAlive restarts
        # into the same message every 30 seconds. POSIX allows either errno
        # for a lock someone else holds.
        if err.errno not in (errno.EAGAIN, errno.EACCES):
            sys.exit("cannot lock %s: %s" % (LOCK_PATH, err))
        sys.exit("vinegar is already running as pid %s" % locked_by())
    os.ftruncate(handle, 0)
    os.write(handle, str(os.getpid()).encode())
    _lock_handle = handle


def locked_by():
    """The pid recorded in the lock file, for error messages only."""
    try:
        with open(LOCK_PATH) as handle:
            return int(handle.read().strip())
    except (ValueError, OSError):
        return "unknown"


def release_lock():
    """Drop the lock, unless a pass is still running under it.

    Exiting would drop it too, and that is now the point rather than an
    aside. The kernel releases an flock when the process dies, and the
    process cannot die before the poll workers finish because they are
    not daemons, so "held for as long as a pass is running" is true by
    construction. Releasing here while one is alive is the only way it
    ever comes free early.

    Which is how every stop on the parallel path went wrong, four times
    over: whatever escaped poll_once -- a second Ctrl-C, an interrupt in a
    log call, a thread that could not be started -- main()'s finally freed
    the lock, and a `--pr` run could then take it and `git reset --hard` a
    tree a live review was reading, which reports findings about a commit
    nobody asked about. Guarding each of those in turn reopened the next.
    This declines to release instead, so none of them has to be caught.

    threading.enumerate() rather than is_alive(), and the difference is
    the case that is hardest to see: a thread whose start() was
    interrupted is running and answers False to is_alive() until it sets
    its started flag. enumerate() lists it anyway, because start() puts it
    in threading's limbo before the OS thread exists and enumerate()
    reports limbo as well as the active set.

    Declining rather than waiting: this runs on the way out, and the
    interpreter is about to wait for those threads on its own.
    """
    global _lock_handle
    running = [thread for thread in threading.enumerate()
               if thread.name.startswith(POLL_WORKER)]
    if running:
        log("%d pass(es) still finishing, so the lock stays until this "
            "process exits" % len(running))
        return
    if _lock_handle is not None:
        os.close(_lock_handle)
        _lock_handle = None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.join(HOME, "config.json"))
    parser.add_argument("--once", action="store_true",
                        help="poll one time and exit")
    parser.add_argument("--pr", metavar="OWNER/REPO#N",
                        help="review one pull request now and exit, whatever "
                             "the poll state says about it. It is still "
                             "scoped the way the daemon would scope it: if "
                             "an earlier review posted, only what was added "
                             "since is read. --whole reads all of it")
    parser.add_argument("--whole", action="store_true",
                        help="with --pr, read the entire pull request even "
                             "if an earlier review already covered part of "
                             "it")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the review but post nothing to GitHub")
    args = parser.parse_args()

    check_paths()
    config = load_config(args.config)
    if args.dry_run:
        config["comment"] = False

    # A run that posts nothing keeps its own bookkeeping. The poll path
    # records an outcome for every pull request it reviews, and written to
    # the shared file that outcome tells the live daemon the pull request
    # is done: it returns at the DONE check, `review_on_push` is false, and
    # a pull request nobody ever posted a review for is never looked at
    # again. spend_announce() guards the same hazard for one field; this is
    # the field that closes the whole pull request off.
    #
    # A separate file rather than no file, because a dry run is also run as
    # a daemon (`comment: false` is a configuration, not only a flag), and
    # remembering nothing would review every open pull request again on
    # every poll at full cost.
    global STATE_PATH, REVIEW_DIR
    # Idempotent, because this rewrites module state rather than deriving
    # it: reaching here twice in one process would otherwise produce
    # `state.json.dry.dry` and lose everything the first pass recorded.
    if not config["comment"] and not STATE_PATH.endswith(".dry"):
        STATE_PATH = STATE_PATH + ".dry"
        # The transcripts too. They are named from repo, number and sha, so
        # a rehearsal of a pull request the daemon has already reviewed
        # writes over that review's transcript — and when a post has failed
        # that file is the only copy there is, the one the log tells the
        # operator to send by hand.
        REVIEW_DIR = REVIEW_DIR + ".dry"
        log("posting nothing, so this run remembers in %s and writes its "
            "transcripts to %s" % (STATE_PATH, REVIEW_DIR))

    # Refused rather than ignored, the way load_config refuses a bad value
    # rather than carrying on with a default. `--whole` is read only by the
    # `--pr` branch, so as a bare flag it was accepted, did nothing, and
    # said nothing: an operator running `vinegar.py --whole` to stop the
    # daemon narrowing its re-reviews would have had no way to learn that
    # every poll went on narrowing exactly as before.
    if args.whole and not args.pr:
        sys.exit("--whole only means something with --pr; the daemon's own "
                 "scoping is not a command-line choice")

    # Both paths take the lock. A manual --pr run shares the daemon's checkout
    # for that repo, so without it the manual run would reset and re-check-out
    # the tree under a review the daemon already has in flight, and that review
    # would report findings against a commit it was never asked about.
    acquire_lock()
    tokens = {}
    try:
        if args.pr:
            # Checked before anything is minted, so a mistyped target says
            # it is mistyped rather than minting a token for whatever the
            # string happened to name and failing on that instead. The whole
            # shape, not just the `#`, since a half-check leaves the same
            # error to be discovered later by the call it was meant to guard.
            repo, _, number = args.pr.partition("#")
            # isascii too: "²".isdigit() and "١٢".isdigit() are both true,
            # and either would sail past this to fail on the gh call the
            # guard exists to run before.
            if (not number.isascii() or not number.isdigit()
                    or not REPO_NAME.match(repo)):
                sys.exit("--pr wants owner/repo#number, got %s" % args.pr)
            # The same sum handle_pr() asks for, and for the same reason: one
            # token covers the checkout and the review that follows it. The
            # posting asks for its own, in review().
            env = github_env(config, repo, tokens,
                             good_for=checkout_grace(config))
            pr = find_pr(repo, number, env)
            reason = skip_reason(pr, config)
            if reason:
                log("%s: would be skipped (%s), reviewing anyway" % (
                    args.pr, reason))
            # Deliberately not `resent`. A person running this has asked
            # for a review and expects to see one, and asking GitHub
            # first makes the commonest reason for a second run — trying
            # another `--model` — impossible: Vinegar's own line is
            # already up at that commit, so the review would be paid for
            # and then thrown away. An unwanted duplicate is visible and
            # harmless; a discarded review is neither.
            try:
                where = checkout(repo, pr, env)
            except Exception as err:
                # One sentence, like the daemon gives for the same
                # failure, rather than a traceback through the lock.
                sys.exit("%s: checkout failed: %s" % (args.pr, err))

            # Read here as well as after the review, rather than hoisting
            # the one below. That one is deliberately re-read at the end so
            # that a review lasting twenty minutes records against whatever
            # the file says then; holding a copy across the review would
            # write back a snapshot from before it and undo anything the
            # operator changed meanwhile.
            #
            # This copy feeds two things, the scope and the round, and is
            # then dropped. Read separately they would be two snapshots of
            # a file the running daemon is writing to, and the scope and
            # the round would describe different rounds.
            #
            # A manual run scopes itself exactly as the daemon would, which
            # is what makes `--pr` the way to exercise this. Under a scratch
            # VINEGAR_HOME there is no state, so there is no `reviewed_sha`
            # and the pull request is read whole, which is the right answer
            # rather than a limitation.
            #
            # `--whole` is the way out. The operator reaching for `--pr`
            # after an unsatisfying review wants the whole thing read
            # again, and silently handing them the increment gives them a
            # near-empty diff and a review that says nothing.
            entry = load_state().get(pr_key(repo, pr), {})
            since = None if args.whole else review_scope(
                where, pr, entry, env, args.pr)
            if since:
                log("%s: reviewing what was added since %s"
                    % (args.pr, since[:7]))
            # Counted the way the daemon counts it, through the same rule,
            # and `--whole` opts out of this narrowing as well as the
            # scope one.
            #
            # That is not what this did first. The argument for splitting
            # them was that one flag answers a question about scope and the
            # other a question about severity, and it left an operator with
            # no way to ask for a full review at all: a fourth hand run
            # reports only blockers, and the flag whose whole purpose is
            # "read it all again, properly" does not turn that off. There
            # is no second flag and there should not be one. `--whole` is
            # the way out of every narrowing, which is what its name says.
            # Called before `--whole` is consulted, not after. As
            # `not args.whole and this_round(...)` Python never ran it
            # under the flag, so the round went unlogged for the one
            # kind of run that is always a full-cost whole-pull-request
            # review and is the documented remedy for an unsatisfying
            # one, the exact case its own docstring says the line
            # exists for.
            narrows = this_round(entry, config, args.pr)
            blockers = narrows and not args.whole
            # Wrapped, so the recording below always happens. The
            # subscription is spent by the time most of these can fire,
            # and dying here left no entry at all: the daemon reviewed
            # the same head a minute later at full cost and posted a
            # second complete review, because its first attempt does not
            # ask. handle_pr wraps its own call for the same reason.
            # The same indicator as the daemon's. A hand-run review is
            # still minutes of silence on a real pull request, which is
            # the whole thing this shows.
            hand = None
            outcome = FAILED
            # Beside `outcome`, and for the same reason it is here. The
            # recording below runs in a finally, so Ctrl-C between here and
            # the review leaves both unbound otherwise, and the whole
            # protection that finally exists to give — an entry on disk, so
            # the daemon does not buy the same head again — is lost to a
            # NameError on the way out.
            covered = reached = False
            # One finally over both the indicator and the recording, and
            # the recording is the half that matters. The comment above
            # says why it must always happen: without an entry the daemon
            # reviews the same head a minute later at full cost and posts
            # a second complete review, because a first attempt does not
            # ask. `except Exception` never covered Ctrl-C, which is how
            # the README says to stop a run and which as a BaseException
            # walks straight out to main's own handler. Protecting the
            # cheap artifact and not the expensive one was the asymmetry
            # this fixes.
            try:
                hand = open_check(args.pr, repo, pr, config, env, blockers)
                outcome, covered, reached = review(
                    where, repo, pr, config, env, tokens, check=hand,
                    since=since, blockers=blockers)
            except Exception as err:
                log("%s: the review did not complete: %s" % (args.pr, err))
                outcome, covered, reached = FAILED, False, False
            finally:
                # Not "finished" for a review that answered DONE. finish()
                # closes the indicator itself on every ending that posted,
                # so an open one here means the posting is what did not
                # happen. handle_pr says the same at more length.
                close_check(args.pr, hand, ended_title(outcome),
                            posting_env(args.pr, config, repo, tokens, env)
                            or env)

                # Recorded, always. A manual run is still a review of that
                # commit, and leaving no trace meant the daemon reviewed the
                # same head a minute later at full cost and posted a second
                # complete review — its first attempt does not ask, because
                # the state file is what usually tells it. Two reviews and
                # two subscriptions for one commit.
                #
                # This run's own head only for the marker: the scan would
                # find one the daemon left at some other head, claim a review
                # was saved when none was, and move the entry's sha away from
                # the marker that needed it.
                state = load_state()
                was = state.get(pr_key(repo, pr), {})
                kept = was if was.get("sha") == pr["headRefOid"] else {}
                # The outcome the review actually reached, not DONE. Writing
                # DONE for a run that never got that far — a rate-limit
                # window, a logged-out CLI — closed the pull request off for
                # good: the daemon returns at the DONE check and, with
                # review_on_push false, never looks again. FAILED is what
                # MAX_ATTEMPTS is for.
                #
                # And a fresh review voids any earlier saved one's budget,
                # because this run writes its own transcript over it. Carried
                # forward, a spent post_tries met the new marker at 3 of 3,
                # so neither the repost branch nor the forget branch fired
                # and the review sat on disk for ever.
                remember(state, pr_key(repo, pr), state_entry(
                    pr["headRefOid"], outcome, kept.get("attempts", 0) + 1,
                    **dict(carry_forward(kept), post_tries=0, waivers=0,
                           unposted=bool(
                               unposted_for(repo, pr, scan=False)[0]),
                           **reviewed_through(covered, pr["headRefOid"],
                                              was),
                           **rounds_done(reached, was))))
                if state[pr_key(repo, pr)].get("unposted"):
                    log("%s: the review is saved to be posted on a later "
                        "poll" % args.pr)
            return

        state = load_state()
        # Discovery, and the decision about whether this run does any.
        # Asked once and here, because after the first ask the list is no
        # longer empty and the question cannot be asked a second time.
        #
        # Before the line below that names the repositories, because that
        # line's one job is to say what this run will really poll, and
        # with discovery the config file does not know.
        #
        # `--pr` never reaches this: it names its own repository and
        # returns above, so a config with an App and no `repos` reviews a
        # named pull request without asking GitHub anything extra.
        discovering = not config["repos"]
        asked_at = 0
        if discovering:
            asked_at = refresh_repos(config, asked_at)
        # The width is said only when it is not one, for the same reason
        # the App is: this line is what an operator reads to confirm the
        # config they just edited took, and a daemon reviewing two
        # repositories at once is the fact about this run that most
        # changes what the log below it will look like.
        #
        # The width the pass will use, not the number in the file. They
        # differ whenever `parallel_repos` is larger than `repos`, and this
        # line said "4 at a time" over a single-repository install that
        # then polled serially: the one reading that could not be more
        # wrong on the one question it is printed to answer.
        width = poll_width(config)
        # "no repositories" rather than an empty gap in the sentence, which
        # is what this printed as. It is reachable only under discovery,
        # where the first ask can fail or the App can genuinely cover
        # nothing, and load_config refuses an empty `repos` everywhere
        # else. Both of those are states an operator has to be able to
        # read, and "watching  every 60s" reads as a truncated line rather
        # than as the daemon's answer.
        log("watching %s every %ds%s%s" % (
            ", ".join(config["repos"]) or "no repositories",
            config["poll_interval"],
            ", %d at a time" % width if width > 1 else "",
            " as the GitHub App" if config.get("github_app") else ""))
        # After the line that says this Vinegar is up, because the sweep
        # logs per pull request and those lines are about the last run
        # rather than this one: read above the identity line they look
        # like the new daemon's own work.
        #
        # No `--once` carve-out here, and the first version of this had
        # one. It keyed on one-shot versus loop while its own comment said
        # that was not the axis, and it was not: a second instance under
        # its own VINEGAR_HOME run *as a loop* passed it and swept the
        # production daemon's live indicator anyway. What separates two
        # instances is the home, so DEPLOYMENT is what running_checks()
        # matches on, and a run this deployment did not make is neither
        # adopted nor closed whichever way either process was started.
        #
        # `--pr` still never reaches here, because it returns above.
        sweep_checks(config, tokens)
        while True:
            poll_once(config, state, tokens)
            if args.once:
                return
            time.sleep(config["poll_interval"])
            # After the sleep and before the pass, so the pass about to run
            # reads the list this just confirmed. refresh_repos() decides
            # for itself whether the hour is up; asking it every pass is
            # what lets a failed ask be retried in a minute.
            if discovering:
                asked_at = refresh_repos(config, asked_at)
    except KeyboardInterrupt:
        # "stopping", not "stopped", because above one repository this is
        # not the end of anything. The passes still running keep the lock
        # and keep reviewing, and release_lock() says so on the line after
        # this one. Written as "stopped" it sat between two lines that
        # contradicted it, and the operator who read the one word that has
        # always meant the daemon is gone then ran `--pr` and was refused
        # by a live pid, for up to `review_timeout` per pass in flight.
        #
        # Unconditional, rather than asking whether a pass is alive. At
        # one repository the process exits immediately after this, so
        # "stopping" is true and momentary; deciding between the two words
        # would mean consulting the same thread-name match release_lock()
        # uses, for a word.
        log("stopping")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
