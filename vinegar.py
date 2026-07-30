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
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser(os.environ.get("VINEGAR_HOME", "~/.vinegar"))
STATE_PATH = os.path.join(HOME, "state.json")
LOCK_PATH = os.path.join(HOME, "vinegar.pid")
CHECKOUT_DIR = os.path.join(HOME, "checkouts")
REVIEW_DIR = os.path.join(HOME, "reviews")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "review-settings.json")

# `ultra` is deliberately absent. It runs in the cloud and bills usage credits
# separately, so automation must never reach it.
EFFORTS = ("low", "medium", "high", "xhigh", "max")

PR_FIELDS = ("number,title,headRefOid,isDraft,author,additions,deletions,"
             "isCrossRepository,url")

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
}


def log(message):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("%s %s" % (stamp, message), flush=True)


def run(cmd, cwd=None, timeout=None):
    return subprocess.run(cmd, cwd=cwd, timeout=timeout, text=True,
                          stdin=subprocess.DEVNULL, capture_output=True)


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
    if not config["repos"]:
        sys.exit("%s: no repos listed" % path)
    if config["effort"] not in EFFORTS:
        sys.exit("%s: effort must be one of %s" % (path, ", ".join(EFFORTS)))
    return config


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


def open_prs(repo):
    result = run(["gh", "pr", "list", "-R", repo, "--state", "open",
                  "--limit", "50", "--json", PR_FIELDS])
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
    if config["skip_bots"] and pr["author"].get("is_bot"):
        return "opened by the bot %s" % pr["author"]["login"]
    if config["authors"] and pr["author"]["login"] not in config["authors"]:
        return "author %s is not in the authors list" % pr["author"]["login"]
    changed = pr["additions"] + pr["deletions"]
    if changed > config["max_changed_lines"]:
        return "%d changed lines, over the %d cap" % (
            changed, config["max_changed_lines"])
    return None


def checkout(repo, pr):
    """Put a detached checkout on the pull request's head commit.

    With a pull request target, /code-review reads local files only when the
    checkout matches that branch. Otherwise it fetches each file over the API,
    which costs more and reviews worse.
    """
    path = os.path.join(CHECKOUT_DIR, repo.replace("/", "__"))
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(CHECKOUT_DIR, exist_ok=True)
        log("%s: cloning into %s" % (repo, path))
        result = run(["gh", "repo", "clone", repo, path, "--", "--quiet"])
        if result.returncode != 0:
            raise RuntimeError("clone failed: %s" % result.stderr.strip())

    steps = (["git", "fetch", "--quiet", "origin",
              "pull/%d/head" % pr["number"]],
             ["git", "checkout", "--quiet", "--detach", pr["headRefOid"]],
             ["git", "clean", "-qfd"])
    for step in steps:
        result = run(step, cwd=path)
        if result.returncode != 0:
            raise RuntimeError("%s failed: %s" % (" ".join(step),
                                                  result.stderr.strip()))
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


def review(path, repo, pr, config):
    prompt = "/code-review %s %d" % (config["effort"], pr["number"])
    if config["comment"]:
        prompt += " --comment"

    # The review reads a diff that Vinegar did not write, so it runs under
    # vinegar's own settings file and never loads the user, project, or local
    # settings that an interactive session would.
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
        result = run(cmd, cwd=path, timeout=config["review_timeout"])
    except subprocess.TimeoutExpired:
        log("%s: killed after %ds" % (label, config["review_timeout"]))
        return
    took = round(time.monotonic() - started)

    try:
        output = json.loads(result.stdout)
    except json.JSONDecodeError:
        detail = (result.stderr or result.stdout).strip()
        log("%s: claude printed no result after %ds: %s" % (
            label, took, detail[:400]))
        return

    denied = output.get("permission_denials") or []
    if denied:
        tools = sorted({entry.get("tool_name", "?") for entry in denied})
        log("%s: %d permission denial(s) for %s. The review ran with less "
            "than it asked for; widen review-settings.json" % (
                label, len(denied), ", ".join(tools)))

    if output.get("is_error"):
        log("%s: review failed after %ds: %s" % (
            label, took, str(output.get("result"))[:400]))
        return

    transcript = save_transcript(repo, pr, str(output.get("result", "")))
    cost = output.get("total_cost_usd")
    priced = ", %.2f USD equivalent" % cost if isinstance(cost, float) else ""
    log("%s: reviewed in %ds%s, transcript at %s" % (
        label, took, priced, transcript))


def poll_once(config, state):
    for repo in config["repos"]:
        for pr in open_prs(repo):
            key = "%s#%d" % (repo, pr["number"])
            seen = state.get(key)
            if seen == pr["headRefOid"] or (seen and not config["review_on_push"]):
                continue

            reason = skip_reason(pr, config)
            if reason:
                log("%s: skipped, %s" % (key, reason))
            else:
                try:
                    path = checkout(repo, pr)
                except RuntimeError as err:
                    # A checkout costs no subscription budget, so leave this
                    # pull request unrecorded and try it again next poll.
                    log("%s: %s" % (key, err))
                    continue
                review(path, repo, pr, config)

            # A review that failed is recorded all the same. Retrying it on
            # every poll would spend real budget on every retry.
            state[key] = pr["headRefOid"]
            save_state(state)


def find_pr(target):
    """Resolve an `owner/repo#number` target to a repo and a pull request."""
    if "#" not in target:
        sys.exit("--pr wants owner/repo#number, got %s" % target)
    repo, _, number = target.partition("#")
    result = run(["gh", "pr", "view", number, "-R", repo,
                  "--json", PR_FIELDS])
    if result.returncode != 0:
        sys.exit("cannot read %s: %s" % (target, result.stderr.strip()))
    return repo, json.loads(result.stdout)


def acquire_lock():
    """Refuse to start when another Vinegar already holds the lock."""
    os.makedirs(HOME, exist_ok=True)
    if os.path.exists(LOCK_PATH):
        with open(LOCK_PATH) as handle:
            other = handle.read().strip()
        try:
            os.kill(int(other), 0)
            sys.exit("vinegar is already running as pid %s" % other)
        except (ValueError, ProcessLookupError):
            pass  # stale lock from a process that is gone
        except PermissionError:
            sys.exit("pid %s is running and is not ours" % other)
    with open(LOCK_PATH, "w") as handle:
        handle.write(str(os.getpid()))


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

    config = load_config(args.config)
    if args.dry_run:
        config["comment"] = False

    if args.pr:
        repo, pr = find_pr(args.pr)
        reason = skip_reason(pr, config)
        if reason:
            log("%s: would be skipped (%s), reviewing anyway" % (args.pr, reason))
        review(checkout(repo, pr), repo, pr, config)
        return

    acquire_lock()
    try:
        state = load_state()
        log("watching %s every %ds" % (", ".join(config["repos"]),
                                       config["poll_interval"]))
        while True:
            poll_once(config, state)
            if args.once:
                return
            time.sleep(config["poll_interval"])
    except KeyboardInterrupt:
        log("stopped")
    finally:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)


if __name__ == "__main__":
    main()
