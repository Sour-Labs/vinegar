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
import re
import subprocess
import sys
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

# abspath, not just expanduser, so a trailing slash or a relative path cannot
# make either of these look like it sits somewhere it does not.
HOME = os.path.abspath(
    os.path.expanduser(os.environ.get("VINEGAR_HOME", "~/.vinegar")))
STATE_PATH = os.path.join(HOME, "state.json")
LOCK_PATH = os.path.join(HOME, "vinegar.pid")
REVIEW_DIR = os.path.join(HOME, "reviews")

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
# It deliberately does not have to cover the posting as well. That happens
# after a review which may have consumed the entire `review_timeout`, and at
# the timeouts people actually configure no obtainable token is guaranteed to
# survive that far. POST_GRACE asks for a fresh one at that point instead.
CHECKOUT_GRACE = 600

# Enough life for `gh pr list`, which is one call. This is not zero because the
# cache serves a token right up to its recorded expiry, and that expiry is
# optimistic: it is computed from the local clock after the mint response
# arrives, while GitHub set the real one before sending it.
LISTING_GRACE = 60

PR_FIELDS = ("number,title,headRefOid,baseRefName,isDraft,author,additions,"
             "deletions,isCrossRepository,url")

# Seconds of token life asked for immediately before the review is posted.
# The token minted at the top cannot be relied on here: it has to survive the
# checkout and the whole review first, and `review_timeout` alone can consume
# more life than a token has. Whatever is left at that point, this asks for a
# usable token now, when the only work remaining is one or two API calls.
POST_GRACE = 300

# Seconds `git diff` may take before the poll loop gives up on it. Local
# work, so generous is already absurd; the point is that a checkout on a
# filesystem that stops answering cannot hold the one poll thread for ever.
DIFF_TIMEOUT = 120

# Seconds `gh pr list` may take. One HTTP call, made once a minute per
# repository on the poll thread, which makes it the daemon's biggest
# exposure to a socket that answers nothing. The clone and fetch in
# checkout() stay unbounded on purpose: a first clone of a large repository
# over a slow link legitimately runs for minutes, and a cap tight enough to
# matter would break exactly that case.
LIST_TIMEOUT = 120

# Seconds a single posting request may take. Generous for one API call, and
# the point is only that it ends: the poll loop is one thread and an
# unanswered socket would otherwise hold it for as long as TCP allows.
POST_TIMEOUT = 60

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

# Characters a review comment may carry. GitHub's own ceiling is 65536 and it
# refuses the whole review for going over, which on the path that posts the
# reviewer's message verbatim would mean posting nothing at all. The reviewer
# decides that message's length, not Vinegar, so it is cut to fit.
MAX_BODY = 60000

DEFAULTS = {
    "repos": [],
    "poll_interval": 60,
    "effort": "high",
    "comment": True,
    "model": None,
    "review_on_push": False,
    "max_changed_lines": 3000,
    "skip_drafts": True,
    "skip_bots": True,
    "skip_forks": True,
    "authors": [],
    "review_timeout": 1800,
    "github_app": None,
}


def checkout_grace(config):
    """Token life the checkout and the review need between them.

    Both the daemon and the `--pr` path ask for this, and each used to spell
    the sum out with a comment saying it had to match the other. Drift shows
    up only under a slow clone, at review time, on a real pull request.
    """
    return CHECKOUT_GRACE + config["review_timeout"]


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("%s %s" % (stamp, message), flush=True)


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

    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path],
                            input=signing_input, capture_output=True)
    if signed.returncode != 0:
        raise RuntimeError("openssl could not sign with %s: %s" % (
            key_path, signed.stderr.decode(errors="replace").strip()))
    return (signing_input + b"." + b64url(signed.stdout)).decode()


def github_api(path, token, scheme="Bearer", payload=None):
    """Call the GitHub API directly.

    The App endpoints need `Authorization: Bearer <jwt>`, and `gh` sends
    `token`, so these two calls cannot go through `gh` the way every other
    GitHub call in this file does.
    """
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request("https://api.github.com" + path,
                                     data=body,
                                     method="POST" if body else "GET")
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
    if not isinstance(config["repos"], list) or not config["repos"]:
        # A bare string passes a truthiness check and then iterates as
        # characters, which polls `-R S`, `-R o`, `-R u` once a minute forever.
        sys.exit("%s: repos must be a non-empty list of owner/name" % path)
    if config["effort"] not in EFFORTS:
        sys.exit("%s: effort must be one of %s" % (path, ", ".join(EFFORTS)))

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

    # The reporting contract has a third copy: REPORT_TOOL names the tool
    # review() asks for and read_stream() listens for, and the allow list in
    # review-settings.json is what makes calling it possible. That list is a
    # literal in a file this code cannot derive from the constant. If they
    # disagree, every review comes back as unanchored prose with findings
    # nobody sees, quietly, which is the same shape of silent failure the
    # path checks above exist to refuse at startup.
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as handle:
            allowed = json.load(handle).get("permissions", {}).get("allow", [])
    except Exception as err:
        sys.exit("%s cannot be read (%s), and the reviewer runs under it."
                 % (SETTINGS_PATH, err))
    if REPORT_TOOL not in allowed:
        sys.exit(
            "review-settings.json does not allow %s. The reviewer would be "
            "told to report through a tool it cannot call, and every review "
            "would come back as prose with no findings. Add %s to "
            "permissions.allow." % (REPORT_TOOL, REPORT_TOOL))


def state_entry(head, outcome, attempts=0, reason=None, announced=False):
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
    return entry


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
    os.makedirs(HOME, exist_ok=True)
    temp = STATE_PATH + ".tmp"
    with open(temp, "w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.flush()
        # fsync before the rename. os.replace promises the name flips
        # atomically, not that the bytes behind it reached the disk first,
        # and a power cut in that gap leaves a zero-length state file under
        # the final name.
        os.fsync(handle.fileno())
    os.replace(temp, STATE_PATH)
    # And the directory, because the rename is a change to it: fsyncing the
    # file makes its bytes durable, not the name that reaches them. A power
    # cut in that window leaves no state.json at all, and every open pull
    # request is reviewed once more at full cost.
    fd = os.open(HOME, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_prs(repo, env):
    # Bounded because this is the poll loop's heartbeat, once a minute per
    # repository, on the only thread there is. A socket that is open but
    # never answers would otherwise park the daemon indefinitely, polling
    # nothing, while the watchdog sees a live pid and calls it healthy.
    try:
        result = run(["gh", "pr", "list", "-R", repo, "--state", "open",
                      "--limit", "50", "--json", PR_FIELDS], env=env,
                     timeout=LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        log("%s: gh pr list timed out after %ds" % (repo, LIST_TIMEOUT))
        return []
    if result.returncode != 0:
        log("%s: gh pr list failed: %s" % (repo, result.stderr.strip()))
        return []
    return json.loads(result.stdout)


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
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(CHECKOUT_DIR, exist_ok=True)
        log("%s: cloning into %s" % (repo, path))
        result = run(["gh", "repo", "clone", repo, path, "--", "--quiet"],
                     env=env)
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
        result = run(step, cwd=path, env=env)
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
    result = run(["git", "fetch", "--quiet", "--force", "origin",
                  "%s:%s" % (base, base)], cwd=path, env=env)
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


def save_transcript(repo, pr, text, findings=None, note=None):
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
    if findings:
        body += "\n\n## Findings\n\n" + "\n\n".join(
            finding_bullet(finding) for finding in findings)
    elif findings is not None:
        body += "\n\n## Findings\n\n%s\n" % (
            "None reported before it stopped." if note else "None.")
    # Whole or not at all, the way save_state already writes. finish() reads
    # this file's existence as "the attempts left words worth keeping", and
    # a crash mid-write must not leave a truncated file wearing that
    # meaning.
    #
    # utf-8 pinned for the same reason run() pins it: findings quote source,
    # the C locale a launchd job can run under encodes almost none of it,
    # and on a dry run a transcript that cannot be written is the entire
    # output of the run, silently gone.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", errors="replace") as handle:
        handle.write("# %s#%d %s\n\n%s\n\n---\n\n%s\n" % (
            repo, pr["number"], pr["headRefOid"][:7], pr["url"], body))
    os.replace(tmp, path)
    return path


def reviewer_brief(pr):
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
    """
    base = pr["baseRefName"]
    return (
        "This checkout is detached at the head commit of pull request #%d, so "
        "it has no upstream branch and `@{upstream}` does not resolve. The "
        "pull request targets `%s`, which should be fetched into this clone: "
        "`git diff refs/heads/%s...HEAD` is the review scope, spelled that "
        "way because a tag of the same name would otherwise win. If that ref "
        "does not "
        "resolve, fall back to `gh pr diff %d` and say in your summary that "
        "you did. Do not substitute a branch of your own choosing, and do not "
        "assume `main`.\n\n"
        "Post nothing to GitHub yourself. Report every finding through the "
        "%s tool, including when you found none, and give `file` relative to "
        "the repository root. Vinegar reads that call and posts the whole "
        "review from it, so a finding you leave out of it is a finding "
        "nobody sees."
        % (pr["number"], base, base, pr["number"], REPORT_TOOL))


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
    spoken = spoken.strip()
    if len(spoken) > MAX_SPOKEN:
        # Said, not silently done. review_body introduces this text with
        # "its own words follow unedited", and a closing summary keeps its
        # conclusion at the end, which is the half a silent cut removes.
        spoken = spoken[:MAX_SPOKEN] + "\n\n(cut after %d characters)" % (
            MAX_SPOKEN,)
    return result, findings, spoken


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
    try:
        return int(line)
    except (TypeError, ValueError, OverflowError):
        # OverflowError because json.loads accepts the non-standard
        # `Infinity` and `1e400`, and int(float("inf")) raises it. Uncaught,
        # one bad line number in one finding took the whole review with it.
        return None


def finding_bullet(finding):
    """A finding as one markdown bullet, for the comment and the transcript.

    Both need it and they used to build it separately, which was enough for
    them to disagree: a `file` of only spaces rendered as empty backticks in
    one and `(no file)` in the other, for the same finding.
    """
    where = str(finding.get("file") or "").strip() or "(no file)"
    line = finding_line(finding)
    if line is not None:
        where += ":%d" % line
    return "- `%s`: %s" % (where, describe(finding).replace("\n\n", "\n  "))


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
    # The verdict rides with the category when the effort level ran a verify
    # pass. CONFIRMED and PLAUSIBLE read very differently, and posting them
    # identically claims a certainty the reviewer did not.
    verdict = str(finding.get("verdict") or "").strip()
    body = "%s\n\nFailure: %s" % (summary, scenario) if scenario else summary
    tags = ", ".join(part for part in (category, verdict) if part)
    return "%s\n\n(%s)" % (body, tags) if tags else body


def split_findings(findings, covered, root, label):
    """Route each finding to an inline comment or to the general comment."""
    inline, general = [], []
    for finding in findings:
        name = repo_path(finding.get("file"), root)
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


def review_body(label, pr, config, inline, general, raw=None,
                heading="These could not be anchored in the diff:", note=None,
                verb="reviewed"):
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
    lines = ["%s %s `%s` at %s effort" % (
        BODY_MARK, verb, pr["headRefOid"][:7], config["effort"])]

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
        lines += ["", "%d finding%s, %d posted inline." % (
            total, "" if total == 1 else "s", len(inline))]

    if general:
        bullets = [finding_bullet(finding) for finding in general]
        # Whole findings come off the end when the body cannot fit, said
        # out loud, with clamp() left as the last resort only. Its
        # character cut shears mid-bullet and logs a byte count, which
        # loses the tail findings with nothing on the pull request saying
        # more existed. The transcript holds every finding regardless of
        # what fits here.
        dropped, extra = 0, []
        while bullets and len("\n".join(
                lines + ["", heading, ""] + bullets + extra)) > MAX_BODY:
            bullets.pop()
            dropped += 1
            extra = ["", "%d finding(s) did not fit GitHub's comment limit "
                         "and are only in the transcript." % dropped]
        if dropped:
            log("%s: %d finding(s) did not fit the comment and are only in "
                "the transcript" % (label, dropped))
        lines += ["", heading, ""] + bullets + extra

    return clamp(label, "\n".join(lines))


def submit_review(label, repo, pr, payload, env):
    """Post one review, as one request, and say whether it landed.

    Bounded, like the listing and the diff on the same thread. `run()` waits
    for as long as the far end takes, and a socket that is open but never
    answers, which is what a black-holed connection or a proxy holding the
    request looks like, is not an error anyone raises. The poll loop is one
    thread: it would sit here, no repository would be polled, and the watchdog
    would see a live pid producing no log lines and call that healthy. The
    clone and fetch in checkout() are the deliberate exception; the constant
    beside LIST_TIMEOUT says why.
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


def already_posted(label, repo, pr, env):
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
    return any(line.startswith(BODY_MARK)
               for line in stream_lines(result.stdout))


def post_review(label, repo, pr, path, text, findings, config, env,
                note=None, verb="reviewed"):
    """Turn what the reviewer reported into one review on the pull request.

    Answers whether the pull request carries the review now. The give-up
    marks itself announced on that answer, so "posted nothing and said so"
    must not read the same as "posted".
    """
    if not config["comment"]:
        # Before the routing, not after. Working out which findings could be
        # anchored means a full `git diff` over the pull request, and a dry
        # run discards the answer to print two counts.
        #
        # True because a dry run wanted nothing posted: it is the one
        # ending where an empty pull request is the correct outcome, and
        # calling it a failure would have the give-up announce itself on
        # every poll for ever.
        log("%s: dry run, %d finding(s) not posted" % (
            label, len(findings) if findings else 0))
        return True

    if findings is None:
        log("%s: %s, posting its text as the review" % (
            label, "the review stopped before reporting" if note else
            "the reviewer reported no findings through %s" % REPORT_TOOL))
        inline, general, raw = [], [], text
    elif not findings:
        # Nothing to anchor, so nothing to work the diff out for. That is a
        # `git diff` over the whole pull request saved on every clean review.
        inline, general, raw = [], [], None
    else:
        inline, general = split_findings(
            findings, diff_lines(path, pr["baseRefName"], env, label),
            path, label)
        raw = None

    payload = {"event": "COMMENT",
               "commit_id": pr["headRefOid"],
               "body": review_body(label, pr, config, inline, general, raw,
                                   note=note, verb=verb)}
    if inline:
        payload["comments"] = inline

    settled = submit_review(label, repo, pr, payload, env)
    if settled == POSTED:
        log("%s: posted %d inline comment(s) and the review comment" % (
            label, len(inline)))
        return True

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
        if already_posted(label, repo, pr, env):
            log("%s: the review is already on the pull request" % label)
            return True
        log("%s: the post may not have landed and nothing is up, so it is "
            "sent again" % label)
        settled = submit_review(label, repo, pr, payload, env)
        if settled == POSTED:
            log("%s: posted %d inline comment(s) and the review comment" % (
                label, len(inline)))
            return True
        if settled == UNSURE:
            return False
        # A resend that came back judged falls through to the retries
        # below. Returning here instead lost every finding whenever the
        # first attempt met a transient 5xx and the second was refused
        # over the one bad anchor the anchor-stripping retry exists for.

    if settled == THROTTLED:
        # Retrying now would be refused for the same reason. The review is
        # on disk, and saying where says more than a second failure would.
        log("%s: the posting is rate limited, so the review is only in the "
            "transcript. Post it by hand, or delete the entry from %s to "
            "review it again" % (label, STATE_PATH))
        return False

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
            heading="GitHub refused the inline comments, so all of it is "
                    "here:")
    else:
        # Nothing to strip out, so the same request again. A review with no
        # inline comments cannot have been refused over an anchor, which
        # leaves the transient failures a second attempt is exactly right
        # for. Without this a clean review, or the reviewer's own words, met
        # one 502 and the pull request received nothing at all, for good.
        log("%s: retrying the review comment" % label)
    return submit_review(label, repo, pr, payload, env) == POSTED


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
    try:
        log("%s: %s, its words are in %s" % (label, why, save_transcript(
            repo, pr, text, None,
            "This attempt did not finish, so this is what it said before it "
            "stopped. It is not a review.")))
    except Exception as err:
        log("%s: the transcript is not saved: %s" % (label, err))


def finish(label, repo, pr, path, text, findings, config, env, tokens,
           note=None, verb="reviewed"):
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
    path_kept = transcript_path(repo, pr)
    if not text.strip() and findings is None and os.path.exists(path_kept):
        if note:
            try:
                # Rewritten whole through a rename, like save_transcript,
                # not appended in place. This branch trusts the existence
                # of the file to mean the attempts left words worth
                # keeping, and an append that dies half-way would hand the
                # next reader a file that passes that test and ends
                # mid-note.
                with open(path_kept, encoding="utf-8",
                          errors="replace") as handle:
                    whole = handle.read()
                tmp = path_kept + ".tmp"
                with open(tmp, "w", encoding="utf-8",
                          errors="replace") as handle:
                    handle.write(whole + "\n\n---\n\n%s\n" % note)
                os.replace(tmp, path_kept)
                log("%s: the ending is appended to the transcript the "
                    "attempts left" % label)
            except Exception as err:
                log("%s: the transcript is not saved: %s" % (label, err))
        else:
            log("%s: the transcript already holds what the attempts said, "
                "leaving it" % label)
    else:
        try:
            log("%s: transcript at %s" % (
                label, save_transcript(repo, pr, text, findings, note)))
        except Exception as err:
            log("%s: the transcript is not saved: %s" % (label, err))
    return post_review(label, repo, pr, path, text, findings, config,
                       posting_env(label, config, repo, tokens, env), note,
                       verb)


def partial_note(cause):
    """The note every partial ending shares, phrased the one way.

    Three endings post findings they know are incomplete: killed, stream
    stopped early, failed. review_body and the tests key off the shared
    clause, and as three hand-written copies one edit could quietly have
    the same class of ending described three different ways.
    """
    return ("This review %s, so these are the findings it had reported by "
            "then and not a finished round." % cause)


def review(path, repo, pr, config, env, tokens):
    prompt = "/code-review %s %d" % (config["effort"], pr["number"])

    # The review reads a diff that Vinegar did not write, so it runs under
    # vinegar's own settings file and loads none of the user, project, or
    # local settings.json an interactive session would, and no MCP server.
    # This does not cover a CLAUDE.md in the checkout, which is still read as
    # project instructions. See "What the reviewer is allowed to do".
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
           "--append-system-prompt", reviewer_brief(pr),
           "--output-format", "stream-json", "--verbose",
           "--settings", SETTINGS_PATH,
           "--setting-sources", "",
           "--strict-mcp-config"]
    if config["model"]:
        cmd += ["--model", config["model"]]

    # The env var is what makes the choice deterministic. Without it the
    # decision falls through to a server-side flag that is off by default,
    # and the reviewer goes back to printing text.
    env = dict(env or os.environ, CLAUDE_CODE_REPORT_FINDINGS="1")

    label = "%s#%d" % (repo, pr["number"])

    def deliver(text, findings, note=None):
        """Record and post one ending, whichever ending it turned out to be.

        Every way this function returns DONE goes through here. The three
        endings used to repeat the same nine arguments, and finish()'s own
        docstring records what that cost the last time: the partial-run
        marker was added to one path and not the other.
        """
        announce(label, lambda: finish(
            label, repo, pr, path, text, findings, config, env, tokens, note))

    log("%s: reviewing at %s effort%s" % (
        label, config["effort"], "" if config["comment"] else ", dry run"))

    started = time.monotonic()
    try:
        result = run(cmd, cwd=path, timeout=config["review_timeout"], env=env)
    except subprocess.TimeoutExpired as expired:
        # A timeout burned the budget it burned. Retrying would burn it again.
        log("%s: killed after %ds" % (label, config["review_timeout"]))

        # What it managed to say before the kill is still in hand. The review
        # reports its findings and then writes a closing summary, so a kill
        # during that summary lands after the tool call: the findings exist,
        # they are in this buffer, and throwing it away would tell the pull
        # request the review "returned nothing" while holding all of them.
        # It is bytes even in text mode, because the timeout path skips the
        # decoding the normal one does.
        salvaged = expired.stdout or ""
        if isinstance(salvaged, bytes):
            salvaged = salvaged.decode(errors="replace")
        _, findings, spoken = read_stream(salvaged, label)

        # Everything a killed run leaves goes through finish(), the same as
        # a finished one: the findings it reported, or failing that whatever
        # it said, and a transcript either way. A separate posting path for
        # this case is how one of them came to say "returned nothing" while
        # the reviewer's words sat in the buffer, and how a killed dry run
        # came to leave no trace at all.
        #
        # `is not None`, not truthiness: a reviewer that reported an empty
        # list looked and found nothing, and read_stream draws that
        # distinction deliberately.
        if findings is not None:
            log("%s: it had already reported %d finding(s), posting those"
                % (label, len(findings)))
            note = partial_note(
                "was killed after %ds" % config["review_timeout"])
        else:
            note = ("This review was killed after %ds. Read that as the "
                    "review not finishing, not as the change being clean."
                    % config["review_timeout"])
        deliver(spoken, findings, note)
        return DONE
    took = round(time.monotonic() - started)

    output, findings, spoken = read_stream(result.stdout, label)
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
            return FAILED
        log("%s: it had reported %d finding(s) first, posting those"
            % (label, len(findings)))
        deliver(spoken, findings, partial_note("stopped before it finished"))
        return DONE

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
    note = None
    if output.get("is_error"):
        log("%s: review failed after %ds: %s" % (label, took, text[:400]))
        if findings is None:
            keep(label, repo, pr, spoken, "the review failed")
            return FAILED
        log("%s: it failed with %d finding(s) already reported, so those are "
            "posted" % (label, len(findings)))
        note = partial_note("failed before it finished")

    cost = output.get("total_cost_usd")
    # bool first: it is an int, and True would print as 1.00 USD.
    priced = ", %.2f USD equivalent" % cost if isinstance(
        cost, (int, float)) and not isinstance(cost, bool) else ""
    log("%s: reviewed in %ds%s" % (label, took, priced))

    # After the transcript is on disk, so a review whose findings cannot be
    # posted is still a review someone can read. The outcome stays DONE
    # either way: the subscription is spent by this point, and re-running a
    # review to recover from a failed post would spend it again.
    # A fresh token, rather than the one minted before the checkout. That one
    # had to outlast the clone and the whole review, and at a `review_timeout`
    # near the hour a token lives it cannot be guaranteed to. Posting is the
    # entire output of the run, and this is the last moment it can be made
    # safe cheaply. A dry run mints nothing, having nothing to post.
    deliver(text, findings, note)
    return DONE


def poll_once(config, state, tokens):
    for repo in config["repos"]:
        try:
            prs = open_prs(repo, github_env(config, repo, tokens,
                                            good_for=LISTING_GRACE))
        except Exception as err:
            log("%s: cannot list pull requests: %s" % (repo, err))
            continue
        for pr in prs:
            try:
                handle_pr(repo, pr, config, state, tokens)
            except Exception as err:
                # One bad pull request must not stop the daemon. Under launchd
                # a crash restarts the process every 30 seconds and polls
                # nothing in between.
                log("%s#%s: unhandled error: %s" % (
                    repo, pr.get("number", "?"), err))


def give_up(key, repo, pr, config, attempts, tokens, path=None, env=None):
    """Say, once, that Vinegar has stopped trying to review this.

    Called from two moments that must say the same thing: the last attempt
    returning FAILED in-process, and a later poll discovering a spent
    budget whose attempt never returned at all, because the pre-review
    marker charges the attempt before review() runs and a kill between the
    two leaves no one behind to announce anything. `path` and `env` exist
    only at the first moment; the posting needs neither, since a give-up
    has no findings to anchor and posting_env() mints its own token.

    Answers whether it was said, so the caller marks the entry announced
    only when it actually reached the pull request. Marking regardless
    turned one bad minute at GitHub into permanent silence: every later
    poll saw the mark and returned.
    """
    log("%s: %d failed attempts, leaving it alone. Fix the cause, then "
        "delete its entry from %s to try again" % (key, attempts, STATE_PATH))
    # Said on the pull request, not only in a log nobody is watching.
    # Silence has to keep meaning that something broke, which is only true
    # if the giving-up is announced.
    return announce(key, lambda: finish(
        key, repo, pr, path, "", None, config, env, tokens,
        note="Vinegar tried to review this %d times and each attempt "
             "failed before it could report anything. Read that as the "
             "review not running, not as the change being clean." % (
                 attempts,),
        verb="gave up on"))


def handle_pr(repo, pr, config, state, tokens):
    key = "%s#%d" % (repo, pr["number"])
    head = pr["headRefOid"]
    done = state.get(key) or {}

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
            if not done.get("announced"):
                said = give_up(key, repo, pr, config, done["attempts"],
                               tokens)
                state[key] = state_entry(head, FAILED, done["attempts"],
                                         announced=said)
                save_state(state)
            return

    reason = skip_reason(pr, config)
    if reason:
        # Deciding again is free. Saying so every minute is noise, so this
        # logs only when the decision is new or its reason changed.
        if (done.get("outcome") != "skipped" or done.get("sha") != head
                or done.get("reason") != reason):
            log("%s: skipped, %s" % (key, reason))
            # The attempts burned at this head ride along. Dropping them
            # is what let a draft toggle launder the retry budget.
            kept = done if done.get("sha") == head else {}
            state[key] = state_entry(head, "skipped",
                                     kept.get("attempts", 0), reason)
            save_state(state)
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
        log("%s: checkout failed: %s" % (key, err))
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
    state[key] = state_entry(head, FAILED, attempts)
    save_state(state)

    try:
        outcome = review(path, repo, pr, config, env, tokens)
    except Exception as err:
        # The subscription is spent by the time most of these can happen, and
        # an unrecorded pull request is reviewed again on the very next poll,
        # at full cost, for ever. announce() covers the posting; this covers
        # everything else review() touches, including the two read_stream
        # calls and `claude` missing from PATH entirely. Recording FAILED
        # keeps MAX_ATTEMPTS in charge of how many times that may repeat.
        log("%s: the review did not complete: %s" % (key, err))
        outcome = FAILED

    state[key] = state_entry(head, outcome, attempts)
    save_state(state)

    if outcome == FAILED and attempts >= MAX_ATTEMPTS:
        # Marked only if it was said, so the restart path knows. Without
        # the mark a daemon restart would say it all again; with it applied
        # regardless, a failed announcement was never retried at all.
        said = give_up(key, repo, pr, config, attempts, tokens, path, env)
        state[key] = state_entry(head, outcome, attempts, announced=said)
        save_state(state)


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
        sys.exit("cannot read %s: %s" % (target, result.stderr.strip()))
    return json.loads(result.stdout)


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
    """Drop the lock. Exiting would do it too; this just makes it explicit."""
    global _lock_handle
    if _lock_handle is not None:
        os.close(_lock_handle)
        _lock_handle = None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=os.path.join(HOME, "config.json"))
    parser.add_argument("--once", action="store_true",
                        help="poll one time and exit")
    parser.add_argument("--pr", metavar="OWNER/REPO#N",
                        help="review one pull request now and exit, ignoring "
                             "the poll state")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the review but post nothing to GitHub")
    args = parser.parse_args()

    check_paths()
    config = load_config(args.config)
    if args.dry_run:
        config["comment"] = False

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
                    or repo.count("/") != 1 or not all(repo.split("/"))):
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
            review(checkout(repo, pr, env), repo, pr, config, env,
                   tokens)
            return

        state = load_state()
        log("watching %s every %ds%s" % (
            ", ".join(config["repos"]), config["poll_interval"],
            " as the GitHub App" if config.get("github_app") else ""))
        while True:
            poll_once(config, state, tokens)
            if args.once:
                return
            time.sleep(config["poll_interval"])
    except KeyboardInterrupt:
        log("stopped")
    finally:
        release_lock()


if __name__ == "__main__":
    main()
