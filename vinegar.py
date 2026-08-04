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
`claude -p '/code-review <n> --comment'` in it.
"""

import argparse
import base64
import errno
import fcntl
import json
import os
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

PR_FIELDS = ("number,title,headRefOid,baseRefName,isDraft,author,additions,"
             "deletions,isCrossRepository,url")

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


def run(cmd, cwd=None, timeout=None, env=None):
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True)


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
    `gh api` cannot reach anything else the owner can see, even other
    repositories in the same installation.

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
    repository, and `Bash(gh api:*)` is a channel out. Silence here means the
    protection was never applied, not that it held.

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


def review(path, repo, pr, config, env):
    prompt = "/code-review %s %d" % (config["effort"], pr["number"])
    if config["comment"]:
        prompt += " --comment"

    # The review reads a diff that Vinegar did not write, so it runs under
    # vinegar's own settings file and loads none of the user, project, or
    # local settings.json an interactive session would, and no MCP server.
    # This does not cover a CLAUDE.md in the checkout, which is still read as
    # project instructions. See "What the reviewer is allowed to do".
    cmd = ["claude", "-p", prompt,
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

    if output.get("is_error"):
        log("%s: review failed after %ds: %s" % (
            label, took, str(output.get("result"))[:400]))
        return FAILED

    transcript = save_transcript(repo, pr, str(output.get("result", "")))
    cost = output.get("total_cost_usd")
    priced = ", %.2f USD equivalent" % cost if isinstance(cost, float) else ""
    log("%s: reviewed in %ds%s, transcript at %s" % (
        label, took, priced, transcript))
    return DONE


def poll_once(config, state, tokens):
    for repo in config["repos"]:
        try:
            prs = open_prs(repo, github_env(config, repo, tokens))
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
    # comments would then fail to post. `good_for` asks for one that outlives
    # the review it is handed to.
    #
    # But asking for that before the checks above meant minting one for every
    # open pull request on every poll, including the ones that return one line
    # later as already reviewed. Nothing deduplicated it, because a token lives
    # an hour and `good_for` is `review_timeout`, which is also an hour, so the
    # cache test `now + good_for < expires` is false even against a token minted
    # this instant. That is roughly 1440 installation tokens a day per open pull
    # request, and a transient 5xx on that endpoint took the pull request out of
    # the pass. Raising `review_timeout` from 1800 to 3600 is what silently
    # turned the cache off, so keep the two apart: `good_for` at or above the
    # token's own lifetime means every call is a fresh mint.
    env = github_env(config, repo, tokens, good_for=config["review_timeout"])

    try:
        path = checkout(repo, pr, env)
    except Exception as err:
        # A checkout spends no subscription budget, so leave this pull request
        # unrecorded and try it again on the next poll.
        log("%s: checkout failed: %s" % (key, err))
        return

    outcome = review(path, repo, pr, config, env)
    attempts = done.get("attempts", 0) + 1 if done.get("sha") == head else 1
    state[key] = {"outcome": outcome, "sha": head, "attempts": attempts}
    save_state(state)

    if outcome == FAILED and attempts >= MAX_ATTEMPTS:
        log("%s: %d failed attempts, leaving it alone. Fix the cause, then "
            "delete its entry from %s to try again" % (
                key, attempts, STATE_PATH))


def find_pr(target, env):
    """Read the one pull request named by an `owner/repo#number` target."""
    if "#" not in target:
        sys.exit("--pr wants owner/repo#number, got %s" % target)
    repo, _, number = target.partition("#")
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
            repo = args.pr.partition("#")[0]
            env = github_env(config, repo, tokens,
                             good_for=config["review_timeout"])
            pr = find_pr(args.pr, env)
            reason = skip_reason(pr, config)
            if reason:
                log("%s: would be skipped (%s), reviewing anyway" % (
                    args.pr, reason))
            review(checkout(repo, pr, env), repo, pr, config, env)
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
