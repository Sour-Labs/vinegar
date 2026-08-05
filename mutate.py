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

"""Break a line the suite defends, and check that the suite turns red.

    python3 mutate.py                 # every mutation, about four minutes
    python3 mutate.py post-timeout    # one, by name
    python3 mutate.py --list

A check that has never been seen to fail is a claim rather than a test, and
this is what turns the claims into tests. Each entry below names a guard in
vinegar.py, the exact text that implements it, and what to replace that text
with to break it. Running it edits vinegar.py in place and puts it back; the
restore is verified, and the run stops rather than leaving a mutation behind.

**Run this in a scratch worktree, not in a checkout a daemon executes.** A
broken vinegar.py is on disk for the few seconds each entry takes, thirty
times a run, and anything that *starts* the program inside one of those
windows gets the broken copy rather than the restored one. Under the launchd
setup the README describes, a KeepAlive restart landing in one of those
windows brings the daemon back with, say, `acquire_lock`'s flock removed or
`MAX_ATTEMPTS` at 99. A running process is unaffected, because Python reads
the source once at import, so this is a hazard only for restarts.

    git worktree add /tmp/vinegar-mutate HEAD
    cd /tmp/vinegar-mutate && python3 mutate.py
    cd - && git worktree remove /tmp/vinegar-mutate

Add an entry whenever you add a check. Four checks shipped once that passed
against the very regression they were named for, and each was found only by
running the mutation.

Anchors are unique substrings, not line numbers. An anchored line number
stops meaning anything the moment an edit lands above it, which is why the
first set of these was thrown away rather than re-anchored. An anchor that
no longer matches exactly once is reported, not silently skipped.

Outcomes:
    KILLED   the suite failed and named a check. What every entry wants.
    SURVIVED the suite passed with the guard broken. The check is a claim.
    ABORTED  the suite raised instead of failing, so the checks below the
             raise never ran. Coverage was voided rather than exercised,
             and the exit code alone cannot tell the two apart.
    ANCHOR   the text was not found exactly once. Fix the entry.
"""
import atexit
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(REPO, "vinegar.py")
SUITE = os.path.join(REPO, "test_vinegar.py")
TIMEOUT = 300

CACHE = tempfile.mkdtemp(prefix="vinegar-mutate-pycache-")
atexit.register(shutil.rmtree, CACHE, True)

# Every entry is expected to be KILLED except the two named here.
EXPECT = {
    # Not a guard: the suite's own reporting, checked like anything else.
    "SELFTEST-abort": "ABORTED",
    # Unreachable, and listed to record that it was measured rather than
    # missed. A deletion's hunk is always `+0,0`, so the empty-hunk gate
    # blocks the write whatever `name` holds. No diff git can produce
    # reaches the /dev/null branch, so no fixture in the suite can either.
    "deleted-file": "SURVIVED",
}

# name, the text that implements a guard in vinegar.py, what breaks it
MUTATIONS = [
    # This one raises at module level rather than failing a check, so it
    # must come back ABORTED. Kept permanently: the day it comes back
    # KILLED is the day the suite stopped being able to say it was cut off.
    ("SELFTEST-abort",
     "def clamp(label, body):", "def clamp(label, body, required):"),

    # --- reading the reviewer's stream ---------------------------------
    ("unreadable-line",
     "        except json.JSONDecodeError:\n"
     "            # A single unreadable line must not cost the whole stream: the\n"
     "            # result event may still be further down it.\n"
     "            continue",
     "        except json.JSONDecodeError:\n"
     "            raise"),

    # --- posting -------------------------------------------------------
    ("post-timeout",
     "                     env=env, timeout=POST_TIMEOUT,",
     "                     env=env,"),
    ("already-posted-gate",
     "        if already_posted(label, repo, pr, env, verb):\n"
     '            log("%s: the review is already on the pull request" % label)\n'
     "            return POSTED",
     "        pass"),

    # --- token life ----------------------------------------------------
    ("good-for-expiry",
     "    if token and time.time() + good_for < expires:",
     "    if token and time.time() < expires:"),
    ("good-for-posting-env",
     "        return github_env(config, repo, tokens, good_for=POST_GRACE)",
     "        return github_env(config, repo, tokens)"),
    ("good-for-repost",
     "            env = github_env(config, repo, tokens, good_for=POST_GRACE)",
     "            env = github_env(config, repo, tokens)"),
    ("good-for-give-up",
     "                    post_env = github_env(config, repo, tokens,\n"
     "                                          good_for=POST_GRACE)",
     "                    post_env = github_env(config, repo, tokens)"),
    ("good-for-listing",
     "            prs = open_prs(repo, github_env(config, repo, tokens,\n"
     "                                            good_for=LISTING_GRACE))",
     "            prs = open_prs(repo, github_env(config, repo, tokens))"),

    # --- anchoring, in diff_lines --------------------------------------
    ("diff-failure-gate",
     "    if result is None or result.returncode != 0:",
     "    if result is None:"),
    ("heading-gate",
     '        elif heading and line.startswith("+++ "):',
     '        elif line.startswith("+++ "):'),
    ("deleted-file",
     '            name = None if target == "/dev/null" else target[2:].rstrip("\\t")',
     '            name = target[2:].rstrip("\\t")'),
    ("empty-hunk",
     "                if count:\n"
     "                    covered.setdefault(name, set()).update(\n"
     "                        range(start, start + count))",
     "                covered.setdefault(name, set()).update(\n"
     "                    range(start, start + count))"),
    ("repo-path-nonstring",
     '    if not isinstance(name, str) or not name.strip() or "\\x00" in name:',
     '    if not name.strip() or "\\x00" in name:'),
    ("inline-clamp",
     '                           "body": clamp(label, describe(finding))})',
     '                           "body": describe(finding)})'),

    # --- what the reviewer runs under ----------------------------------
    ("claude-settings",
     '           "--settings", reviewer_settings(path),',
     "           "),
    ("setting-sources",
     '           "--setting-sources", "",',
     "           "),
    ("strict-mcp-config",
     '           "--strict-mcp-config"]',
     "           ]"),
    ("review-timeout",
     '        result = run(cmd, cwd=path, timeout=config["review_timeout"],\n'
     "                     env=reviewing)",
     "        result = run(cmd, cwd=path,\n"
     "                     env=reviewing)"),
    ("review-cwd",
     '        result = run(cmd, cwd=path, timeout=config["review_timeout"],\n'
     "                     env=reviewing)",
     '        result = run(cmd, timeout=config["review_timeout"],\n'
     "                     env=reviewing)"),

    # --- which pull requests are reviewed at all -----------------------
    ("skip-drafts",
     '    if config["skip_drafts"] and pr["isDraft"]:',
     "    if False:"),
    ("skip-forks",
     '    if config["skip_forks"] and pr["isCrossRepository"]:',
     "    if False:"),
    ("skip-authors",
     '    if config["authors"] and login not in config["authors"]:',
     "    if False:"),
    ("skip-size-cap",
     '    if changed > config["max_changed_lines"]:',
     "    if False:"),
    ("skip-bots",
     '    if config["skip_bots"] and author.get("is_bot"):',
     "    if False:"),
    ("skip-author-gone",
     '    author = pr.get("author") or {}',
     '    author = pr["author"]'),
    ("effort-gate",
     '    if config["effort"] not in EFFORTS:\n'
     '        sys.exit("%s: effort must be one of %s" % (path, ", ".join(EFFORTS)))',
     "    pass"),

    # --- the checkout --------------------------------------------------
    ("checkout-stale-lock",
     "    if os.path.exists(stale):\n"
     '        log("%s: clearing a lock left by a killed run" % repo)\n'
     "        forget(stale)",
     "    pass"),
    ("clone-timeout",
     "            result = run(clone, env=env, timeout=CLONE_TIMEOUT)",
     "            result = run(clone, env=env)"),
    ("clone-timeout-message",
     '            raise RuntimeError("%s did not finish within %ds"\n'
     '                               % (" ".join(clone), CLONE_TIMEOUT))',
     "            raise"),
    ("clone-partial-cleanup",
     "            shutil.rmtree(path, ignore_errors=True)\n"
     '            raise RuntimeError("%s did not finish within %ds"',
     '            raise RuntimeError("%s did not finish within %ds"'),
    # --- token life the checkout has to survive on ----------------------
    ("openssl-timeout",
     "            input=signing_input, capture_output=True, timeout=DIFF_TIMEOUT)",
     "            input=signing_input, capture_output=True)"),
    ("openssl-timeout-message",
     "    except subprocess.TimeoutExpired:\n"
     '        raise RuntimeError("openssl did not finish signing with %s within "\n'
     '                           "%ds" % (key_path, DIFF_TIMEOUT))',
     "    except subprocess.TimeoutExpired:\n"
     "        raise"),
    ("grace-cap-guard",
     "    if checkout_grace(config) >= TOKEN_LIFE:",
     "    if False:"),
    ("checkout-cwd",
     "            result = run(step, cwd=path, env=env, timeout=bound)",
     "            result = run(step, env=env, timeout=bound)"),
    ("checkout-unusable-repo",
     "        if not usable:\n"
     '            log("%s: the checkout is not a usable repository, cloning it "\n'
     '                "again" % repo)\n'
     "            shutil.rmtree(path, ignore_errors=True)",
     "        pass"),

    # --- the poll loop surviving one bad thing -------------------------
    ("poll-listing-guard",
     '            log("%s: cannot list pull requests: %s" % (repo, err))\n'
     "            continue",
     "            raise"),
    ("poll-pr-guard",
     '                log("%s#%s: unhandled error: %s" % (\n'
     '                    repo, pr.get("number", "?"), err))',
     "                raise"),
    ("acquire-flock",
     "        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)",
     "        pass"),
    # The pid-file design the lock docstring argues against: a file that
    # outlives its process would then refuse every start after a crash.
    ("acquire-refuses-on-file",
     "    global _lock_handle\n"
     "    os.makedirs(HOME, exist_ok=True)",
     "    global _lock_handle\n"
     "    os.makedirs(HOME, exist_ok=True)\n"
     "    if os.path.exists(LOCK_PATH):\n"
     '        sys.exit("vinegar is already running as pid %s" % locked_by())'),

    # --- constants -----------------------------------------------------
    ("max-attempts", "MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 99"),
    # The value, not just the argument. Removing `timeout=` leaves the
    # constant intact, so the check that the clone gets longer than the
    # fetch was covered by neither of the two entries above it.
    ("clone-timeout-value", "CLONE_TIMEOUT = 1800", "CLONE_TIMEOUT = 60"),
    ("checkout-grace-value", "CHECKOUT_GRACE = 1500", "CHECKOUT_GRACE = 60"),
    ("efforts-ultra",
     'EFFORTS = ("low", "medium", "high", "xhigh", "max")',
     'EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")'),
]


def read():
    with open(TARGET, encoding="utf-8") as handle:
        return handle.read()


def write(text):
    with open(TARGET, "w", encoding="utf-8") as handle:
        handle.write(text)


def run_suite():
    """Run the suite and say what it did: red, green, or neither.

    Bytecode caching off, and pointed at a directory that stays empty. The
    import system decides a cached .pyc is current from the source's size
    and its mtime in whole seconds, so two mutations that change vinegar.py
    by the same number of bytes inside one second are the same file as far
    as it is concerned, and the second one runs the first one's code while
    reporting under its own name. Measured: dropping `"--strict-mcp-config"`
    and dropping `, good_for=POST_GRACE` are both exactly -21 bytes, and
    whichever ran first decided the verdict for both. Every result after the
    first was a false positive until this was found.

    Nothing in the tree shows it, which is what made it worth a paragraph:
    macOS system Python sets sys.pycache_prefix, so the stale file sits in
    ~/Library/Caches/com.apple.python and there is no __pycache__ here.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPYCACHEPREFIX=CACHE)
    done = subprocess.run([sys.executable, SUITE], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=TIMEOUT)
    out = done.stdout
    ran = out.count(" ok\n") + out.count("FAIL ")
    if "ABORTED after" in out:
        # The suite says so itself. Exit 1 alone cannot tell a run that was
        # cut off from one that caught the regression, which is the mistake
        # this whole file exists to stop making.
        return "ABORTED", ran, "raised after %d checks" % ran
    if "all checks passed" in out:
        return "SURVIVED", ran, ""
    if "FAILED: " in out:
        return "KILLED", ran, out.rsplit("FAILED: ", 1)[1].strip()
    tail = (done.stderr or out).strip().splitlines()
    return "ABORTED", ran, tail[-1] if tail else "no output"


def apply_one(name, old, new):
    original = read()
    seen = original.count(old)
    if seen != 1:
        return "ANCHOR", 0, "matched %d times, expected 1" % seen
    try:
        write(original.replace(old, new))
        return run_suite()
    finally:
        write(original)
        # Cheap, and the alternative is a mutation reaching a commit.
        if read() != original:
            sys.exit("%s: vinegar.py was NOT restored. Fix that before "
                     "doing anything else." % name)


def main():
    if "--list" in sys.argv:
        for name, _old, _new in MUTATIONS:
            print(name)
        return 0

    wanted = [a for a in sys.argv[1:] if not a.startswith("-")]
    chosen = [m for m in MUTATIONS if not wanted or m[0] in wanted]
    unknown = set(wanted) - {m[0] for m in MUTATIONS}
    if unknown:
        sys.exit("no mutation named %s" % ", ".join(sorted(unknown)))

    verdict, expected, _detail = run_suite()
    if verdict != "SURVIVED":
        sys.exit("the suite is not green to begin with (%s), so nothing "
                 "below it would mean anything. Fix that first." % verdict)
    print("baseline: %d checks, all green\n" % expected)

    loose = []
    for name, old, new in chosen:
        verdict, ran, detail = apply_one(name, old, new)
        want = EXPECT.get(name, "KILLED")
        # A mutation that leaves fewer checks running than the baseline did
        # not fail them, it prevented them, and that hides behind a red
        # suite exactly as well as behind a green one.
        short = "" if verdict == "ANCHOR" or ran >= expected else \
            "  [%d of %d checks ran]" % (ran, expected)
        print("%-22s %-9s %s%s" % (name, verdict, detail[:60], short))
        if verdict != want or (want == "KILLED" and short):
            loose.append(name)

    print()
    print("NOT KILLED CLEANLY: %s" % ", ".join(loose) if loose
          else "every mutation behaved as expected")
    return 1 if loose else 0


if __name__ == "__main__":
    sys.exit(main())
