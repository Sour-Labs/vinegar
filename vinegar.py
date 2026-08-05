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

# Seconds a single posting request may take. Generous for one API call, and
# the point is only that it ends: the poll loop is one thread and an
# unanswered socket would otherwise hold it for as long as TCP allows.
POST_TIMEOUT = 60

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


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("%s %s" % (stamp, message), flush=True)


def run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    # `input` and `stdin` cannot both be passed, so the default stays DEVNULL
    # and only a caller with something to send opts out of it.
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True, env=env,
                          input=stdin_text,
                          stdin=None if stdin_text is not None
                          else subprocess.DEVNULL,
                          capture_output=True)


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


def load_state():
    try:
        with open(STATE_PATH) as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(HOME, exist_ok=True)
    temp = STATE_PATH + ".tmp"
    with open(temp, "w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temp, STATE_PATH)


def open_prs(repo, env):
    result = run(["gh", "pr", "list", "-R", repo, "--state", "open",
                  "--limit", "50", "--json", PR_FIELDS], env=env)
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


def save_transcript(repo, pr, text):
    os.makedirs(REVIEW_DIR, exist_ok=True)
    name = "%s__%d__%s.md" % (repo.replace("/", "__"), pr["number"],
                              pr["headRefOid"][:7])
    path = os.path.join(REVIEW_DIR, name)
    with open(path, "w") as handle:
        handle.write("# %s#%d %s\n\n%s\n\n---\n\n%s\n" % (
            repo, pr["number"], pr["headRefOid"][:7], pr["url"], text))
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

    That half spells the contract out rather than deferring to the one
    /code-review sets, which an earlier version did, to its cost. See the
    --disallowedTools note in review() for why the two used to disagree.

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
        "`git diff %s...HEAD` is the review scope. If that ref does not "
        "resolve, fall back to `gh pr diff %d` and say in your summary that "
        "you did. Do not substitute a branch of your own choosing, and do not "
        "assume `main`.\n\n"
        "Post nothing to GitHub yourself. End your final message with a "
        "fenced ```json block holding an array of every finding, each an "
        "object with `file`, `line`, `summary` and `failure_scenario`. Use an "
        "empty array when you found nothing, and give `file` relative to the "
        "repository root. That block is the only part of your answer Vinegar "
        "can read, and it posts the whole review from it, so a finding "
        "missing from it is a finding nobody sees."
        % (pr["number"], base, base, pr["number"]))


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


def is_findings(value):
    """Whether a decoded JSON value is a non-empty array of findings.

    "Array of objects" is a shape plenty of other things have, this file's own
    `comments` payload among them, and a review explaining itself quotes one.
    So at least one entry has to carry a field a finding carries.
    """
    return (isinstance(value, list) and value
            and all(isinstance(item, dict) for item in value)
            and any(key in item for item in value
                    for key in ("file", "summary", "failure_scenario")))


def parse_findings(text):
    """The reviewer's findings array, or None if it did not return one.

    None and `[]` are different answers and the caller treats them that way:
    an empty array is the reviewer saying it found nothing, while None is the
    reviewer having said something Vinegar cannot read.

    An array whose entries are not all objects is not findings, and reading it
    as an empty review would put a confident "No findings." on a pull request
    that had some. It counts as unreadable instead.

    Fenced blocks are found by tracking the fences down the message rather
    than by pairing them with a regex. A regex pairs them left to right, so
    one earlier code block in the summary shifts every pair after it and the
    real block at the end is never seen.

    An array with no fence around it is tried after that. `raw_decode` reads
    one value and ignores what follows, so a closing sentence, or a stray
    bracket in the prose, cannot swallow or truncate it.

    An array has to hold something shaped like a finding, because "objects"
    alone is a shape plenty of other things have. A review that quotes a JSON
    payload while explaining itself, this file's own `comments` array being
    the obvious one, hands over a nested array of dicts that would otherwise
    be accepted the moment the real block failed to parse. The result would be
    a confident review of one finding with no file and no summary, posted in
    place of the reviewer's actual words.
    """
    # split("\n"), not splitlines(). splitlines() also breaks on \x0b, \x0c,
    # \x1c to \x1e, \x85 and U+2028/U+2029, every one of which is legal inside
    # a JSON string. Rejoining those pieces with \n rewrites the block into
    # something that no longer parses, and a review is lost to a form feed
    # someone quoted.
    fenced, block, open_fence = [], None, False
    for line in text.split("\n"):
        marker = line.strip().startswith("```")
        if marker and not open_fence:
            block, open_fence = [], True
        elif marker and open_fence:
            fenced.append("\n".join(block))
            block, open_fence = None, False
        elif open_fence:
            block.append(line)

    # The last fenced block is the answer, and only that one. Reading past it
    # to an earlier block was how an empty array quoted while explaining the
    # format came to answer for a review whose real block was truncated, and
    # "No findings." is the one thing this must never say by accident.
    #
    # A last block that is not an array leaves the question open rather than
    # settling it, so a review that ends with some other snippet falls to the
    # scan below. That scan never returns an empty array, so nothing down
    # there can invent a clean review either.
    if fenced:
        try:
            answer = json.loads(fenced[-1])
        except json.JSONDecodeError:
            answer = None
        if isinstance(answer, list):
            return answer if is_findings(answer) or not answer else None

    # Only now, and only because nothing fenced parsed as an array at all. It
    # walks the message once per `[`, so running it when a fenced block
    # already answered would cost the message's length squared for nothing.
    decoder = json.JSONDecoder()
    for start in reversed([hit.start() for hit in re.finditer(r"\[", text)]):
        try:
            findings, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if is_findings(findings):
            return findings
    return None


def diff_lines(path, base, env, label):
    """The head-side line numbers the pull request's diff covers, per file.

    This decides which findings can be inline comments. The reviews endpoint
    refuses a comment on a line outside the diff, and it applies the whole
    review or none of it, so a single badly anchored comment would throw away
    every finding alongside it.

    `--unified=0` so the ranges are the changed lines themselves rather than
    the context around them. A deletion-only hunk reports `+n,0`, which is an
    empty range and correctly contributes no line: nothing was added there for
    a comment to sit on.

    The prefixes are pinned rather than assumed. `diff.noprefix` and
    `diff.mnemonicPrefix` are both real settings that change what precedes
    the path, and reading a fixed two characters off the front under either
    of them would truncate every path and send every finding to the general
    comment.

    `+++ ` only counts between `diff --git` and the first hunk, because at
    `--unified=0` an added line whose own text begins with `++ ` produces one
    that looks exactly like a file header.
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
    result = run(["git", "-c", "core.quotepath=false", "-c", "color.ui=false",
                  "diff", "--unified=0", "--no-color",
                  "--no-textconv", "--no-ext-diff",
                  "--src-prefix=a/", "--dst-prefix=b/",
                  "%s...HEAD" % base], cwd=path, env=env)
    if result.returncode != 0:
        # Every finding is about to be routed to the general comment. Say why
        # here, because the comment itself can only report the effect.
        log("%s: cannot diff %s...HEAD, no finding can be anchored: %s"
            % (label, base, result.stderr.strip()[:200]))
        return {}

    # split("\n") for the reason parse_findings() uses it, and here a forged
    # line is worse than a lost one: splitlines() breaks on a lone CR, so an
    # added line whose content holds one would be cut into a fragment that can
    # look like a hunk header and put line numbers into `covered` that the
    # diff never touched.
    covered, name, heading = {}, None, False
    for line in result.stdout.split("\n"):
        if line.startswith("diff --git "):
            name, heading = None, True
        elif heading and line.startswith("+++ "):
            target = line[4:]
            # /dev/null is a delete, and there is no head-side file to
            # comment on. The b/ prefix is git's, not part of the path.
            name = None if target == "/dev/null" else target[2:]
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
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    name = (os.path.relpath(name, root) if os.path.isabs(name)
            else os.path.normpath(name))
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
    except (TypeError, ValueError):
        return None


def describe(finding):
    """A finding as prose, without the file and line that anchor it."""
    # `or ""` rather than a get default, because these keys arrive present
    # and null often enough, and a default only covers a key that is absent.
    # str(None) is "None", which reads as a finding that says None.
    summary = str(finding.get("summary") or "").strip() or "(no summary)"
    scenario = str(finding.get("failure_scenario") or "").strip()
    return "%s\n\nFailure: %s" % (summary, scenario) if scenario else summary


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
                heading="These could not be anchored in the diff:"):
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
    lines = ["**Vinegar** · reviewed `%s` at %s effort" % (
        pr["headRefOid"][:7], config["effort"])]

    total = len(inline) + len(general)
    if raw is not None and not raw.strip():
        # Distinct from unreadable output, because the two send you to
        # different places: this one to whether the reviewer ran at all.
        lines += ["", "The review finished without saying anything. There are "
                      "no findings to show and no words to quote, which means "
                      "the run produced nothing, not that the change is clean."]
    elif raw is not None:
        lines += ["", "The reviewer did not return its findings in a form "
                      "Vinegar could read, so its own words follow unedited.",
                  "", "---", "", raw.strip()]
    elif not total:
        lines += ["", "No findings."]
    else:
        lines += ["", "%d finding%s, %d posted inline." % (
            total, "" if total == 1 else "s", len(inline))]

    if general:
        lines += ["", heading, ""]
        for finding in general:
            where = str(finding.get("file") or "").strip() or "(no file)"
            line = finding_line(finding)
            if line is not None:
                where += ":%d" % line
            lines.append("- `%s`: %s" % (
                where, describe(finding).replace("\n\n", "\n  ")))

    return clamp(label, "\n".join(lines))


def submit_review(repo, pr, payload, env):
    """Post one review, as one request, and say whether it landed.

    Bounded, because every other call out of this process is. `run()` waits
    for as long as the far end takes, and a socket that is open but never
    answers, which is what a black-holed connection or a proxy holding the
    request looks like, is not an error anyone raises. The poll loop is one
    thread: it would sit here, no repository would be polled, and the watchdog
    would see a live pid producing no log lines and call that healthy.
    """
    try:
        result = run(["gh", "api",
                      "repos/%s/pulls/%d/reviews" % (repo, pr["number"]),
                      "--method", "POST", "--input", "-"],
                     env=env, timeout=POST_TIMEOUT,
                     stdin_text=json.dumps(payload))
    except subprocess.TimeoutExpired:
        # None, not False, and the difference is the whole point. The request
        # may well have arrived: the timeout is on this side, and GitHub can
        # have accepted the review and been slow to say so. Retrying that
        # posts the review twice. Callers retry a definite refusal and leave
        # this alone.
        log("%s#%d: posting the review timed out after %ds, so it may or may "
            "not have landed" % (repo, pr["number"], POST_TIMEOUT))
        return None
    if result.returncode == 0:
        return True
    # Both streams. `gh api` puts a one-line summary on stderr and GitHub's
    # own body on stdout, and the body is the half that names which comment
    # was refused and why. Logging stderr alone left the retry's comment
    # telling the reader to consult an error that does not say.
    log("%s#%d: posting the review failed: %s" % (
        repo, pr["number"],
        " ".join(part.strip() for part in (result.stderr, result.stdout)
                 if part and part.strip())[:600]))
    return False


def announce(label, post):
    """Run the posting step so that nothing it does can escape review().

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
        post()
    except Exception as err:
        log("%s: the review is not posted: %s" % (label, err))


def post_timeout(repo, pr, config, seconds, env):
    """Say on the pull request that the review was killed.

    A timeout returns no text at all, so there is nothing for post_review() to
    read and the pull request would otherwise get nothing. It is recorded as
    reviewed and never tried again, so nothing later fills the gap either.
    That was survivable only while the reviewer posted its findings as it
    went, which left whatever it had found before the kill.
    """
    if not config["comment"]:
        return
    payload = {
        "event": "COMMENT",
        "commit_id": pr["headRefOid"],
        "body": "**Vinegar** · review of `%s` killed after %ds\n\n"
                "It ran past `review_timeout` and returned nothing, so there "
                "are no findings to show. Read that as the review not "
                "finishing, not as the change being clean."
                % (pr["headRefOid"][:7], seconds)}
    # Twice, for the reason post_review() retries: this comment carries no
    # inline anchors, so a refusal is a transient one, and losing it leaves a
    # pull request silent about a review that really did run.
    if submit_review(repo, pr, payload, env) is False:
        submit_review(repo, pr, payload, env)


def post_review(repo, pr, path, text, config, env):
    """Turn what the reviewer returned into one review on the pull request."""
    label = "%s#%d" % (repo, pr["number"])
    findings = parse_findings(text)
    if findings is None:
        log("%s: the reviewer returned no findings array, posting its text "
            "as the review" % label)
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

    if not config["comment"]:
        log("%s: dry run, %d inline and %d general finding(s) not posted" % (
            label, len(inline), len(general)))
        return

    payload = {"event": "COMMENT",
               "commit_id": pr["headRefOid"],
               "body": review_body(label, pr, config, inline, general, raw)}
    if inline:
        payload["comments"] = inline

    landed = submit_review(repo, pr, payload, env)
    if landed:
        log("%s: posted %d inline comment(s) and the review comment" % (
            label, len(inline)))
        return
    if landed is None:
        # It timed out on this side, so it may already be on the pull
        # request. Sending it again would post the review twice.
        return

    if not inline:
        # Nothing to strip out, so the same request again. A review with no
        # inline comments cannot have been refused over an anchor, which
        # leaves the transient failures a second attempt is exactly right
        # for. Without this a clean review, or the reviewer's own words, met
        # one 502 and the pull request received nothing at all, for good:
        # the outcome is recorded reviewed and `review_on_push` is false.
        log("%s: retrying the review comment" % label)
        submit_review(repo, pr, payload, env)
        return

    # The endpoint took none of it, and the likeliest reason is an anchor it
    # disagrees with: a comment lands only on a line inside the diff *it*
    # computed, and checkout() deliberately carries on when the base branch
    # cannot be refreshed, which widens the local diff past GitHub's. Rather
    # than lose ten findings to one line number, say all of it in the comment
    # that needs no anchor at all.
    log("%s: retrying with every finding in the review comment" % label)
    payload.pop("comments")
    payload["body"] = review_body(
        label, pr, config, [], findings,
        heading="GitHub refused the inline comments, so all of it is here:")
    submit_review(repo, pr, payload, env)


def review(path, repo, pr, config, env, tokens):
    prompt = "/code-review %s %d" % (config["effort"], pr["number"])

    # The review reads a diff that Vinegar did not write, so it runs under
    # vinegar's own settings file and loads none of the user, project, or
    # local settings.json an interactive session would, and no MCP server.
    # This does not cover a CLAUDE.md in the checkout, which is still read as
    # project instructions. See "What the reviewer is allowed to do".
    #
    # ReportFindings is withheld, and that is what makes any of this work.
    # /code-review picks its output contract from whether that tool is in the
    # session: with it, the review is told to report through the tool and
    # *not* to print the findings as text; without it, to end with a JSON
    # array in its final message. Only the final message reaches this process.
    # So while the tool was available the findings went somewhere Vinegar
    # cannot see, and asking for the array in the system prompt just argued
    # with the command. Two live reviews were lost that way, both coming back
    # as prose with no array. Removing the tool makes /code-review ask for the
    # array itself, which agrees with reviewer_brief() instead of fighting it.
    #
    # It is a flag rather than a deny in review-settings.json because that
    # file is the security boundary, and this is not about danger. Withholding
    # it costs nothing: the tool renders findings for an interactive UI that
    # a daemon does not have.
    cmd = ["claude", "-p", prompt,
           "--append-system-prompt", reviewer_brief(pr),
           "--disallowedTools", "ReportFindings",
           "--output-format", "json",
           "--settings", SETTINGS_PATH,
           "--setting-sources", "",
           "--strict-mcp-config"]
    if config["model"]:
        cmd += ["--model", config["model"]]

    label = "%s#%d" % (repo, pr["number"])
    log("%s: reviewing at %s effort%s" % (
        label, config["effort"], "" if config["comment"] else ", dry run"))

    started = time.monotonic()
    try:
        result = run(cmd, cwd=path, timeout=config["review_timeout"], env=env)
    except subprocess.TimeoutExpired:
        # A timeout burned the budget it burned. Retrying would burn it again.
        log("%s: killed after %ds" % (label, config["review_timeout"]))
        if config["comment"]:
            announce(label, lambda: post_timeout(
                repo, pr, config, config["review_timeout"],
                github_env(config, repo, tokens, good_for=POST_GRACE)))
        return DONE
    took = round(time.monotonic() - started)

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = (result.stderr or result.stdout).strip()
        log("%s: claude printed no result after %ds: %s" % (
            label, took, detail[:400]))
        return FAILED

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
        tools = sorted({entry.get("tool_name", "?") for entry in denied})
        log("%s: %d permission denial(s) for %s. The review ran with less "
            "than it asked for; widen review-settings.json" % (
                label, len(denied), ", ".join(tools)))

    # `or ""`, not a `get` default: the key is present and null on some
    # outcomes, and a default only applies to a key that is absent. Missing
    # that turned "the review said nothing" into the four characters "None",
    # posted as though the reviewer had written them.
    text = str(output.get("result") or "")

    # The turn limit is the one error that spends the whole subscription and
    # still hands back the review: `result` holds the findings rather than a
    # message about why there are none. Treating it as FAILED lost them and
    # then charged for them twice more, since FAILED means "worth retrying"
    # and MAX_ATTEMPTS allows three.
    #
    # Named explicitly, because every other error also arrives with text in
    # `result` and that text is an error message. Deciding on whether there
    # was any text posted "Claude AI usage limit reached" to the pull request
    # as though the reviewer had written it, and recorded the pull request
    # reviewed for good, which is worse than the loss it was meant to fix.
    # An unrecognised subtype is FAILED for the same reason: retrying costs
    # at most three attempts, and MAX_ATTEMPTS bounds it, while the other
    # mistake is silent and permanent.
    if output.get("is_error"):
        log("%s: review failed after %ds: %s" % (label, took, text[:400]))
        if output.get("subtype") != "error_max_turns" or not text.strip():
            return FAILED
        log("%s: it ran out of turns with a review in hand, so that is what "
            "is posted" % label)

    transcript = save_transcript(repo, pr, text)
    cost = output.get("total_cost_usd")
    priced = ", %.2f USD equivalent" % cost if isinstance(cost, float) else ""
    log("%s: reviewed in %ds%s, transcript at %s" % (
        label, took, priced, transcript))

    # After the transcript is on disk, so a review whose findings cannot be
    # posted is still a review someone can read. The outcome stays DONE
    # either way: the subscription is spent by this point, and re-running a
    # review to recover from a failed post would spend it again.
    # A fresh token, rather than the one minted before the checkout. That one
    # had to outlast the clone and the whole review, and at a `review_timeout`
    # near the hour a token lives it cannot be guaranteed to. Posting is the
    # entire output of the run, and this is the last moment it can be made
    # safe cheaply. A dry run mints nothing, having nothing to post.
    announce(label, lambda: post_review(
        repo, pr, path, text, config,
        github_env(config, repo, tokens, good_for=POST_GRACE)
        if config["comment"] else env))
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
    if done.get("outcome") == FAILED and done.get("sha") == head:
        if done.get("attempts", 0) >= MAX_ATTEMPTS:
            return

    reason = skip_reason(pr, config)
    if reason:
        # Deciding again is free. Saying so every minute is noise, so this
        # logs only when the decision is new or its reason changed.
        if (done.get("outcome") != "skipped" or done.get("sha") != head
                or done.get("reason") != reason):
            log("%s: skipped, %s" % (key, reason))
            state[key] = {"outcome": "skipped", "sha": head, "reason": reason}
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
                     good_for=CHECKOUT_GRACE + config["review_timeout"])

    try:
        path = checkout(repo, pr, env)
    except Exception as err:
        # A checkout spends no subscription budget, so leave this pull request
        # unrecorded and try it again on the next poll.
        log("%s: checkout failed: %s" % (key, err))
        return

    outcome = review(path, repo, pr, config, env, tokens)
    attempts = done.get("attempts", 0) + 1 if done.get("sha") == head else 1
    state[key] = {"outcome": outcome, "sha": head, "attempts": attempts}
    save_state(state)

    if outcome == FAILED and attempts >= MAX_ATTEMPTS:
        log("%s: %d failed attempts, leaving it alone. Fix the cause, then "
            "delete its entry from %s to try again" % (
                key, attempts, STATE_PATH))


def find_pr(repo, number, env):
    """Read the one pull request named by an `owner/repo#number` target."""
    target = "%s#%s" % (repo, number)
    result = run(["gh", "pr", "view", number, "-R", repo,
                  "--json", PR_FIELDS], env=env)
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
            if not number.isdigit() or repo.count("/") != 1 or not all(
                    part for part in repo.split("/")):
                sys.exit("--pr wants owner/repo#number, got %s" % args.pr)
            # The same sum handle_pr() asks for, and for the same reason: one
            # token covers the checkout and the review that follows it. The
            # posting asks for its own, in review().
            env = github_env(config, repo, tokens,
                             good_for=CHECKOUT_GRACE + config["review_timeout"])
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
