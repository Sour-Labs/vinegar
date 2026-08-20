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

# The condition that five entries below take apart, a term at a time.
# Hoisted because writing it out five times is five anchors to repair the
# next time one of its terms moves, and threading `whole` through this
# file already broke six.
CLEAN = "    clean = findings == [] and whole and landed and not resent"

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
     "        prs = open_prs(repo, github_env(config, repo, tokens,\n"
     "                                        good_for=LISTING_GRACE))",
     "        prs = open_prs(repo, github_env(config, repo, tokens))"),

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
    # Both of these carry the line above them, because the severity pass
    # sends the same two flags and the bare text now matches twice.
    ("setting-sources",
     '           "--settings", reviewer_settings(path),\n'
     '           "--setting-sources", "",',
     '           "--settings", reviewer_settings(path),'),
    ("strict-mcp-config",
     '           "--setting-sources", "",\n'
     '           "--strict-mcp-config"]',
     '           "--setting-sources", ""]'),
    ("review-timeout",
     "                         cwd=path, timeout=left, env=reviewing)",
     "                         cwd=path, env=reviewing)"),
    ("review-cwd",
     "                         cwd=path, timeout=left, env=reviewing)",
     "                         timeout=left, env=reviewing)"),

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
    ("token-cap-silent",
     '    if config.get("github_app") and checkout_grace(config) >= TOKEN_LIFE:',
     "    if False:"),
    ("token-cap-cries-wolf",
     '    if config.get("github_app") and checkout_grace(config) >= TOKEN_LIFE:',
     "    if True:"),
    # The boundary itself. A sum of exactly a token's life already fails the
    # cache's strict `<`, so `>` would start that one config in silence.
    ("token-cap-boundary",
     '    if config.get("github_app") and checkout_grace(config) >= TOKEN_LIFE:',
     '    if config.get("github_app") and checkout_grace(config) > TOKEN_LIFE:'),
    # Without an App nothing mints, so the warning would name a cost that
    # cannot be incurred, on the configuration the README ships.
    ("token-cap-no-app-guard",
     '    if config.get("github_app") and checkout_grace(config) >= TOKEN_LIFE:',
     "    if checkout_grace(config) >= TOKEN_LIFE:"),
    ("token-cap-no-remedy",
     "               TOKEN_LIFE - CHECKOUT_GRACE))",
     "               0))"),
    # It must stay a warning. A refusal here took down the deploy of the
    # change that introduced it, which is the whole subject of issue #15.
    ("token-cap-refuses",
     '        log("%s: review_timeout is %d, and with the %ds the checkout "',
     '        sys.exit("%s: review_timeout is %d, and with the %ds the checkout "'),
    # The ceiling the downgraded refusal used to provide by accident.
    ("review-timeout-ceiling",
     '    if config["review_timeout"] > MAX_REVIEW_TIMEOUT:',
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
     '        log("%s: cannot list pull requests: %s" % (repo, err))\n'
     "        return",
     "        raise"),
    ("poll-pr-guard",
     '            log("%s#%s: unhandled error: %s" % (\n'
     '                repo, pr.get("number", "?"), err))',
     "            raise"),

    # --- polling more than one repository at a time --------------------
    ("parallel-repos-checked",
     '    for name in ("poll_interval", "review_timeout", '
     '"max_changed_lines",\n'
     '                 "parallel_repos"):',
     '    for name in ("poll_interval", "review_timeout", '
     '"max_changed_lines"):'),
    ("parallel-repos-unit",
     '    units = {"max_changed_lines": "lines", '
     '"parallel_repos": "repositories"}',
     '    units = {"max_changed_lines": "lines"}'),
    # Carrying the line below it, because main() computes the same width
    # for the startup line and the bare assignment matches there too.
    ("parallel-fan-out",
     "    width = poll_width(config)\n"
     "    if width <= 1:",
     "    width = 1\n"
     "    if width <= 1:"),
    ("parallel-width-cap",
     '    return min(config["parallel_repos"], len(config["repos"]))',
     '    return config["parallel_repos"]'),
    ("parallel-serial-default",
     "    if width <= 1:\n"
     '        for repo in config["repos"]:\n'
     "            poll_repo(repo, config, state, tokens)\n"
     "        return",
     "    pass"),
    # The word that told an operator the daemon was gone while its passes
    # were still reviewing and still holding the lock.
    ("stop-claims-it-has-stopped",
     '        log("stopping")',
     '        log("stopped")'),
    # A width the repositories cannot use, clamped and never mentioned, so
    # the setting does nothing and the startup line reads exactly as it
    # did before the operator edited the file.
    ("clamped-width-said-nothing",
     '        log("%s: parallel_repos is %d and there %s %d repositor%s to poll, "',
     '        (lambda *a, **k: None)('
     '"%s: parallel_repos is %d and there %s %d repositor%s to poll, "'),
    # The stop reaching a pass that is already listing. Without it a
    # repository works through every pull request the listing returned
    # before it notices, which is the whole interrupt window again.
    ("parallel-stop-between-pull-requests",
     "        if STOPPING.is_set():\n"
     "            return",
     "        if False:\n"
     "            return"),
    # The shape the entry guard's own message promises. A dropped owner
    # starts the daemon and then fails on every poll for ever, with
    # nothing at startup saying the name is wrong.
    ("repos-entry-shape-unchecked",
     "        if not REPO_NAME.match(name):\n"
     '            sys.exit("%s: repos wants owner/name, got %r" % (path, name))',
     "        pass"),
    # A duplicate left in the list, which above one repository is two
    # passes on the one checkout that repository has.
    ("repos-duplicate-kept",
     "    if twice:\n"
     '        config["repos"] = kept',
     "    if False:\n"
     '        config["repos"] = kept'),
    # And collapsed without saying so, which is the disagreement refusing
    # was meant to prevent: a daemon polling a shorter list than the file
    # names, with nothing explaining it.
    ("repos-duplicate-dropped-in-silence",
     '        log("%s: repos names %s more than once, matched without case. A "',
     '        (lambda *a, **k: None)('
     '"%s: repos names %s more than once, matched without case. A "'),
    # The pattern back to counting the slash and testing both halves,
    # which is well-formed and unusable: an organisation's display name
    # starts the daemon and then fails on every poll for ever.
    ("repo-name-shape-only-counts-the-slash",
     r'REPO_NAME = re.compile(r"\A[A-Za-z0-9._-]+/[A-Za-z0-9._-]+\Z")',
     r'REPO_NAME = re.compile(r"\A[^/]+/[^/]+\Z")'),
    # The ceiling, without which one file asks a laptop for twenty-four
    # reviewers and twenty-four clones at once.
    ("parallel-repos-has-no-ceiling",
     '    if config["parallel_repos"] > MAX_PARALLEL_REPOS:',
     "    if False:"),
    # And applied only when there are that many repositories, so the same
    # file means something different as repositories are added.
    ("parallel-repos-ceiling-follows-the-repo-count",
     '    if config["parallel_repos"] > MAX_PARALLEL_REPOS:',
     '    if config["parallel_repos"] > max(MAX_PARALLEL_REPOS,\n'
     '                                      len(config["repos"])):'),
    # No entry for the clamp notice reading poll_width() rather than
    # re-deriving `min()`, and it was measured rather than missed. The
    # notice only runs when `parallel_repos` is above the repository
    # count, and poll_width() is `min()` of the two, so inside that branch
    # the call and the count are the same number: a mutation swapping one
    # for the other changes no behaviour and cannot be killed. The reason
    # to call poll_width() there is that a future clamp which is not a
    # `min()` would leave the message naming a width nothing uses, which
    # is the bug poll_width() was extracted to end. That is a property of
    # the next change, not of this code, so no check can hold it.
    # Matched exactly, which walks past `Sour-Labs/vinegar` beside
    # `sour-labs/vinegar`: two entries listing the same pull requests into
    # one clone directory.
    ("repos-duplicates-matched-with-case",
     "        if name.casefold() in seen:",
     "        if name in seen:"),
    ("repos-duplicate-seen-not-folded",
     "        seen.add(name.casefold())",
     "        seen.add(name)"),
    # Every repository has to leave the queue, not just the first `width`
    # of them.
    ("parallel-queue-drains",
     "    def passes():\n"
     "        while not STOPPING.is_set():\n"
     "            try:\n"
     "                repo = todo.get_nowait()\n"
     "            except queue.Empty:\n"
     "                return",
     "    def passes():\n"
     "        for _ in (1,):\n"
     "            try:\n"
     "                repo = todo.get_nowait()\n"
     "            except queue.Empty:\n"
     "                return"),
    # Daemon workers are killed at interpreter finalization without
    # unwinding, so handle_pr's finally never closes the checks entry and
    # the pull request keeps a Vinegar check spinning for ever.
    ("parallel-daemon-threads",
     "    workers = [threading.Thread(target=passes,\n"
     "                                name=POLL_WORKER + str(nth))",
     "    workers = [threading.Thread(target=passes, daemon=True,\n"
     "                                name=POLL_WORKER + str(nth))"),
    # The one line that keeps poll_once from returning while passes are
    # still running, which is the shape of the bug this whole change fixes.
    ("parallel-workers-joined",
     "        for worker in workers:\n"
     "            worker.start()\n"
     "        for worker in workers:\n"
     "            worker.join()\n"
     "    except BaseException:",
     "        for worker in workers:\n"
     "            worker.start()\n"
     "    except BaseException:"),
    # The stop asked for on the way out. Best effort by design, but
    # without it an interrupted pass drains the whole queue and the poll
    # is paid for in full.
    ("parallel-stopping-set-on-escape",
     "        STOPPING.set()\n"
     "        # Said so the wait that follows is not read as a hang.",
     "        # Said so the wait that follows is not read as a hang."),
    # One try over both loops. Split in two, a start() that fails left
    # STOPPING clear and the workers already running drained the queue.
    ("parallel-one-try-over-both-loops",
     "    try:\n"
     "        for worker in workers:\n"
     "            worker.start()\n"
     "        for worker in workers:\n"
     "            worker.join()",
     "    for worker in workers:\n"
     "        worker.start()\n"
     "    try:\n"
     "        for worker in workers:\n"
     "            worker.join()"),
    # The line that says why the process has not exited yet.
    ("parallel-say-the-lock-is-held",
     '        log("stopping: the passes already running keep the lock until '
     'they "\n'
     '            "finish; kill the process to force it")',
     "        pass"),

    # --- the lock outliving the passes under it ------------------------
    # What the whole parallel path rests on. Released while a pass is
    # alive, a `--pr` run takes it and resets a tree a live review is
    # reading.
    ("lock-held-while-a-pass-runs",
     "    running = [thread for thread in threading.enumerate()\n"
     "               if thread.name.startswith(POLL_WORKER)]\n"
     "    if running:",
     "    running = []\n"
     "    if running:"),
    # No entry for enumerate() being used rather than is_alive(), and it
    # is missing on purpose. The two differ only for a thread whose
    # start() was interrupted, which is running, answers False to
    # is_alive() until it sets its started flag, and is listed by
    # enumerate() anyway because start() puts it in limbo first. Reaching
    # that state needs a signal landing inside Thread.start(), which no
    # check here can arrange without deciding the outcome by timing.
    # Measured directly instead, with a probe that delayed the bootstrap
    # and interrupted the start: is_alive() said False and enumerate()
    # listed it. The commit message says the same, so the choice is
    # recorded rather than looking arbitrary.

    # --- what two repositories polled at once share --------------------
    ("state-lock-save",
     "    with STATE_LOCK:\n"
     "        os.makedirs(HOME, exist_ok=True)\n"
     "        write_atomic(STATE_PATH, json.dumps(state, indent=2, "
     "sort_keys=True))",
     "    os.makedirs(HOME, exist_ok=True)\n"
     "    write_atomic(STATE_PATH, json.dumps(state, indent=2, "
     "sort_keys=True))"),
    ("state-lock-remember",
     "    with STATE_LOCK:\n"
     "        state[key] = entry\n"
     "        if write:\n"
     "            save_state(state)",
     "    state[key] = entry\n"
     "    if write:\n"
     "        save_state(state)"),
    ("remember-write-flag",
     "        state[key] = entry\n"
     "        if write:\n"
     "            save_state(state)",
     "        state[key] = entry\n"
     "        save_state(state)"),
    ("log-lock",
     "    with LOG_LOCK:\n"
     '        print("%s %s" % (utc_stamp(), message), flush=True)',
     '    print("%s %s" % (utc_stamp(), message), flush=True)'),
    # The stamp read before the lock rather than under it, which is how
    # two lines end up carrying timestamps in the opposite order to the
    # order they were written in.
    ("log-stamp-under-the-lock",
     "    with LOG_LOCK:\n"
     '        print("%s %s" % (utc_stamp(), message), flush=True)',
     '    line = "%s %s" % (utc_stamp(), message)\n'
     "    with LOG_LOCK:\n"
     "        print(line, flush=True)"),
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

    # --- the severity pass ---------------------------------------------
    # read_tiers is the whole output surface of a call that reads text
    # quoted out of the branch under review, so what it refuses is as much
    # of the guard as what it accepts.
    ("severity-answer-covers-every-finding",
     "    return [seen[i] for i in range(count)] if len(seen) == count else None",
     "    return [seen[i] for i in sorted(seen)]"),
    ("severity-answer-index-in-range",
     "        if tier in TIERS and 0 <= index < count and index not in seen:",
     "        if tier in TIERS and index not in seen:"),
    ("severity-answer-first-wins",
     "        if tier in TIERS and 0 <= index < count and index not in seen:",
     "        if tier in TIERS and 0 <= index < count:"),
    ("severity-answer-anchored-at-line-start",
     "        match = TIER_LINE.match(line)",
     "        match = TIER_LINE.search(line)"),
    # The alternation in TIER_LINE is the first filter and the membership
    # test is what enforces it, so the mutation lands on the test. Breaking
    # the alternation alone changes nothing a caller can see: a word it
    # then lets through is refused one line later.
    ("severity-answer-known-tiers-only",
     "        if tier in TIERS and 0 <= index < count and index not in seen:",
     "        if 0 <= index < count and index not in seen:"),
    ("severity-answer-any-case",
     "                       re.IGNORECASE)", "                       0)"),

    # Turning it off, and the two shapes of nothing to do.
    ("severity-off-switch",
     "    if not chooser or not findings:", "    if not findings:"),
    ("severity-nothing-to-tier",
     "    if not chooser or not findings:", "    if not chooser:"),

    # Every failure has to land on the findings as they arrived. The review
    # is paid for by this point and an ordering step must not cost it.
    ("severity-failure-is-not-fatal",
     "    except Exception as err:\n"
     "        # The exception's own text is not logged, and that is the point of",
     "    except json.JSONDecodeError as err:\n"
     "        # The exception's own text is not logged, and that is the point of"),
    # The disclosure guard: TimeoutExpired stringifies the whole command,
    # and this one carries every finding's text plus the settings JSON.
    ("severity-timeout-does-not-log-the-prompt",
     "        if isinstance(err, subprocess.TimeoutExpired):\n"
     '            why = "it ran longer than %ds" % SEVERITY_TIMEOUT\n'
     "        elif isinstance(err, OSError):\n"
     "            why = str(err)\n"
     "        else:\n"
     "            why = type(err).__name__",
     "        why = str(err)"),

    # The tier is triage()'s to set, so one arriving on a finding is
    # discarded before anything renders or counts it.
    ("severity-smuggled-tier-discarded",
     '    if findings and any("tier" in finding for finding in findings):',
     "    if False:"),
    ("severity-error-answer-ignored",
     '        if event.get("is_error"):\n'
     '            log("%s: the severity pass failed, so findings are posted in "\n'
     '                "the order they were reported: %s" % (label, said[:200]))\n'
     "            return findings",
     "        pass"),

    # A subprocess on the single poll thread, between a finished review and
    # the posting of it.
    ("severity-timeout-arg",
     "                     timeout=SEVERITY_TIMEOUT, env=env)",
     "                     env=env)"),

    # What the call runs under. The findings quote a branch Vinegar does
    # not trust, so the model reading them gets no tools and no credential.
    ("severity-token-stripped",
     "    env = dict(os.environ)\n"
     '    for carried in ("GH_TOKEN", "GITHUB_TOKEN"):\n'
     "        env.pop(carried, None)",
     "    env = dict(os.environ)"),
    ("severity-tools-denied",
     '        "deny": ["Bash", "Read", "Write", "Edit", "NotebookEdit", "Glob",\n'
     '                 "Grep", "Task", "WebFetch", "WebSearch", "Workflow"],',
     '        "deny": [],'),
    ("severity-sandboxed",
     '    "sandbox": dict(\n'
     "        ((name, wanted) for name, wanted, _ in SANDBOX_RULES),",
     '    "sandbox": dict(\n'
     '        (("enabled", False), ("failIfUnavailable", False)),'),
    # Measured: with the sandbox on and no `filesystem` stanza, a
    # permitted Write reached `$HOME`. These are the paths that cannot be
    # recovered from.
    ("severity-denies-writes-to-home-and-checkouts",
     '        filesystem={"denyWrite": sorted(\n'
     "            {form for path in (HOME, CHECKOUT_DIR)\n"
     "             for form in (path, os.path.realpath(path))})},",
     '        filesystem={"denyWrite": []},'),

    # One definition of where a finding points, and it collapses rather
    # than trims: a newline in `file` forged an extra numbered block in
    # the severity prompt.
    ("severity-where-is-collapsed",
     '    where = " ".join(str(finding.get("file") or "").split()) or "(no file)"',
     '    where = str(finding.get("file") or "").strip() or "(no file)"'),
    ("severity-every-field-collapsed",
     "    def flat(finding, name):\n"
     '        return " ".join(str(finding.get(name) or "").split())',
     "    def flat(finding, name):\n"
     '        return str(finding.get(name) or "").strip()'),
    ("severity-no-bypass-mode",
     '        "defaultMode": PERMISSION_MODE,',
     '        "defaultMode": "bypassPermissions",'),

    # What the tiers are for: an order, and a label on each comment.
    ("severity-sorts-most-serious-first",
     '    return sorted(tiered, key=lambda finding: TIERS.index(finding["tier"])\n'
     '                  if finding["tier"] in TIERS else len(TIERS))',
     "    return tiered"),
    # The sort is past triage()'s except, so this one does not fail an
    # ordering step, it loses a finished review whole.
    ("severity-sort-ranks-an-unknown-tier-last",
     '    return sorted(tiered, key=lambda finding: TIERS.index(finding["tier"])\n'
     '                  if finding["tier"] in TIERS else len(TIERS))',
     '    return sorted(tiered, key=lambda finding: TIERS.index(finding["tier"]))'),
    ("severity-copies-rather-than-writes",
     "    tiered = [dict(finding, tier=tier)\n"
     "              for finding, tier in zip(findings, tiers)]",
     "    tiered = findings\n"
     "    for finding, tier in zip(findings, tiers):\n"
     '        finding["tier"] = tier'),
    ("severity-label-opens-the-comment",
     '        summary = "%s**%s** \u00b7 %s" % (dot + " " if dot else "", tier, summary)',
     "        pass"),
    # The dot and the word are one label and neither half stands alone. A
    # comment with no dot is the plain text this replaced, and one with no
    # word says nothing to a reader who does not know the three colors.
    ("severity-label-opens-with-a-dot",
     '        summary = "%s**%s** \u00b7 %s" % (dot + " " if dot else "", tier, summary)',
     '        summary = "**%s** \\u00b7 %s" % (tier, summary)'),
    ("severity-label-keeps-its-word",
     '        summary = "%s**%s** \u00b7 %s" % (dot + " " if dot else "", tier, summary)',
     '        summary = "%s %s" % (dot, summary)'),
    # One color for all three is a dot that costs a character and tells the
    # reader nothing, and it is what a careless palette edit produces.
    ("severity-each-tier-has-its-own-dot",
     'TIER_DOTS = {"blocker": "\\U0001f534",    # red circle\n'
     '             "advisory": "\\U0001f535",   # blue circle\n'
     '             "note": "\\u26aa"}           # white circle',
     'TIER_DOTS = {"blocker": "\\U0001f534",\n'
     '             "advisory": "\\U0001f534",\n'
     '             "note": "\\U0001f534"}'),
    # Read with a default, which is what lets the drift below fail a check
    # instead of raising out of one: it came back ABORTED as a subscript,
    # 146 checks of 670, with everything under it skipped rather than run.
    ("severity-dot-read-with-a-default",
     '        dot = TIER_DOTS.get(tier)',
     '        dot = TIER_DOTS[tier]'),
    ("severity-every-tier-has-a-dot",
     '             "note": "\\u26aa"}           # white circle',
     '             "n0te": "\\u26aa"}           # white circle'),
    ("severity-tally-counts",
     '    return ", ".join("%d %s" % (count, tier)\n'
     "                     for tier, count in counted if count)",
     '    return ""'),
    ("severity-tally-drops-empty-tiers",
     '    return ", ".join("%d %s" % (count, tier)\n'
     "                     for tier, count in counted if count)",
     '    return ", ".join("%d %s" % (count, tier)\n'
     "                     for tier, count in counted)"),
    ("severity-tally-reaches-the-comment",
     '            " (%s)" % tally if tally else "", len(inline))]',
     '            "", len(inline))]'),
    ("severity-tally-passed-to-the-body",
     "                                   note=note, verb=verb,\n"
     "                                   tally=severity_tally(findings),\n"
     "                                   since=since, blockers=blockers,",
     "                                   note=note, verb=verb,\n"
     "                                   since=since, blockers=blockers,"),

    # What a findings prompt can carry into argv. One entry per condition
    # exec imposes, plus the choice the code argues for at length: a NUL
    # replaced rather than dropped, because dropping it turns a quoted
    # "a\\0.md" into a name that would pass the check being reported.
    ("severity-prompt-drops-nul",
     '    return text.encode("utf-8", "replace").decode("utf-8")'
     '.replace("\\0", " ")',
     '    return text.encode("utf-8", "replace").decode("utf-8")'),
    ("severity-prompt-survives-a-surrogate",
     '    return text.encode("utf-8", "replace").decode("utf-8")'
     '.replace("\\0", " ")',
     '    return text.replace("\\0", " ")'),
    ("severity-prompt-replaces-nul-not-drops-it",
     '    return text.encode("utf-8", "replace").decode("utf-8")'
     '.replace("\\0", " ")',
     '    return text.encode("utf-8", "replace").decode("utf-8")'
     '.replace("\\0", "")'),

    # In finish(), so that all four routes to the pull request agree.
    ("severity-runs-in-finish",
     "    findings = triage(label, findings, config)",
     "    pass"),
    # One loop now covers severity_model, model and fallback_model, so one
    # entry covers the predicate all three share. Storing the stripped name
    # is a separate guard: without it a hand-edited "claude-opus-5 " passes
    # the check and then 404s every review of every repository.
    ("model-names-validated",
     "        if named is not None and not (isinstance(named, str)\n"
     "                                      and named.strip()):",
     "        if False:"),
    ("model-names-are-stripped",
     "        if isinstance(named, str):\n"
     "            config[name] = named.strip()",
     "        if False:\n"
     "            config[name] = named.strip()"),

    # --- the fallback model --------------------------------------------
    # A pinned model that stops resolving takes every review with it, so
    # the fallback is an availability switch. Each half of it: that a
    # second attempt happens at all, that only a routing failure buys one,
    # that findings already in hand are never spent to get it, and that it
    # is one extra attempt rather than a loop.
    ("fallback-model-is-tried",
     "    if config[\"fallback_model\"]:\n"
     "        models.append(config[\"fallback_model\"])",
     "    if False:\n"
     "        models.append(config[\"fallback_model\"])"),
    # Each clause of unroutable() separately. Together they are the whole
    # claim that a second attempt is free: a failed run, refused for the
    # model rather than anything else, holding no findings, carrying a
    # result event to read at all, and having spent nothing.
    ("fallback-only-on-a-routing-failure",
     '            and output.get("api_error_status") == 404\n',
     ""),
    ("fallback-only-on-a-failed-run",
     '            and output.get("is_error")\n',
     ""),
    ("fallback-only-when-it-spent-nothing",
     '            and output.get("total_cost_usd") == 0)',
     "            )"),
    # A missing cost field is not a report of having spent nothing, and
    # `not output.get(...)` cannot tell the two apart.
    ("fallback-cost-must-be-reported",
     '            and output.get("total_cost_usd") == 0)',
     '            and not output.get("total_cost_usd"))'),
    ("fallback-never-spends-findings",
     "    return (findings is None and output is not None",
     "    return (output is not None"),
    ("fallback-needs-a-result-event",
     "    return (findings is None and output is not None\n",
     "    return (findings is None\n"),
    ("fallback-stops-at-the-last-model",
     "        if index == len(models) - 1"
     " or not unroutable(output, findings):",
     "        if not unroutable(output, findings):"),
    # The floor under the inherited bound. Without it a first attempt that
    # rounds up to the whole bound hands the fallback a zero, and
    # subprocess.run kills it before it has read a byte.
    ("fallback-bound-never-reaches-zero",
     "        left = max(1, left - took)",
     "        left = left - took"),
    # The pull request is told which model actually reviewed it. Otherwise
    # a dead pin looks exactly like a healthy one to anyone reading GitHub.
    ("fallback-is-disclosed-on-the-pull-request",
     "        abandoned = model",
     "        pass"),
    # The kill is reported against the bound that killed it, not the
    # configured one. On a fallback attempt those differ.
    ("killed-note-quotes-the-bound-used",
     '                note = partial_note("was killed after %ds" % left)',
     '                note = partial_note("was killed after %ds"\n'
     '                                    % config["review_timeout"])'),
    ("killed-with-nothing-quotes-the-bound-used",
     '                        "review not finishing, not as the change being '
     'clean."\n                        % left)',
     '                        "review not finishing, not as the change being '
     'clean."\n                        % config["review_timeout"])'),
    # The two attempts share one bound. A fresh review_timeout for the
    # fallback lets one pull request park the only poll thread for twice
    # it, which load_config exits with a sentence saying cannot happen.
    ("fallback-shares-the-review-timeout",
     "        left = max(1, left - took)",
     '        left = config["review_timeout"]'),
    # The pinned model reaching argv at all. Without it every review runs
    # on whatever the machine defaults to and says nothing about it.
    ("review-runs-the-configured-model",
     "            result = run(cmd + ([\"--model\", model] if model else []),",
     "            result = run(cmd,"),
    ("fallback-differs-from-the-model",
     "    if (config[\"fallback_model\"] is not None\n"
     "            and config[\"fallback_model\"] == config[\"model\"]):",
     "    if False:"),

    # --- the checks-list indicator -------------------------------------
    # A check that can fail is a merge gate, and the README promises
    # Vinegar is not one. Both halves: the constant and its use.
    ("check-conclusion-never-fails",
     'CHECK_CONCLUSION = "neutral"', 'CHECK_CONCLUSION = "failure"'),
    ("check-conclusion-is-used",
     '"status": "completed", "conclusion": conclusion,',
     '"status": "completed", "conclusion": "success",'),
    # Green is the one ending that is a pass. Six entries: one for the
    # constant, one for claiming it always, and one per term of `clean`,
    # which is an unreadable answer, a killed run, a review that never
    # landed, and a retry whose posting was an earlier attempt's.
    ("check-clean-is-a-pass",
     'CHECK_CLEAN = "success"', 'CHECK_CLEAN = "neutral"'),
    ("check-green-only-when-nothing-was-found",
     CLEAN, "    clean = True"),
    ("check-green-not-for-an-unreadable-answer",
     CLEAN,
     "    clean = not findings and whole and landed and not resent"),
    ("check-green-not-for-a-killed-run",
     CLEAN, "    clean = findings == [] and landed and not resent"),
    ("check-green-not-for-a-review-that-never-landed",
     CLEAN, "    clean = findings == [] and whole and not resent"),
    # post_review answers POSTED without posting when a retry finds the
    # review already up, and that earlier review is the one on the commit.
    ("check-green-not-for-a-retry-that-posted-nothing",
     CLEAN, "    clean = findings == [] and whole and landed"),
    # Each reviewed commit gets its own run, and the entry a pull request
    # shows is the one on its head. Closing anything but the run it was
    # handed would let one review's conclusion stand for another's.
    ("check-closes-the-run-it-was-handed",
     '        label, check["repo"], "check-runs/%s" % check["id"], "PATCH", {',
     '        label, check["repo"], "check-runs/1", "PATCH", {'),
    # A refused PATCH is retried by a backstop carrying the grey
    # conclusion, so without this a clean review ends grey under a title
    # still saying it found nothing.
    ("check-conclusion-rides-with-the-title",
     '    conclusion = check.get("conclusion") or conclusion\n',
     ""),
    # Only an App can own a check run, so without one this is a 403 per
    # review about a permission the operator cannot grant.
    ("check-needs-an-app",
     '    if not config["comment"] or not config.get("github_app"):\n'
     "        return None",
     "    if False:\n        return None"),
    # An indicator an earlier attempt left running is reused rather than
    # joined by a second one that also never finishes.
    ("check-reuses-a-running-one",
     "    if mine:\n"
     '        log("%s: reusing the check run an earlier attempt left running"\n'
     "            % label)\n"
     '        return {"repo": repo, "id": mine[0], "closed": False}',
     "    if False:\n        pass"),
    ("check-ignores-another-apps",
     '            if str((was.get("app") or {}).get("id"))\n'
     '            == str(config["github_app"].get("app_id")) and was.get("id")\n'
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]',
     "            if was.get(\"id\")]"),
    # A handle with no id would PATCH `check-runs/None` on every ending.
    ("check-handle-needs-an-id",
     '    return {"repo": repo, "id": made["id"], "closed": False} \\\n'
     "        if made and made.get(\"id\") else None",
     '    return {"repo": repo, "id": (made or {}).get("id"),\n'
     '            "closed": False}'),
    # A handle holding a token is one log line from publishing it.
    ("check-handle-holds-no-credential",
     '        return {"repo": repo, "id": mine[0], "closed": False}',
     '        return {"repo": repo, "id": mine[0], "env": env,\n'
     '                "closed": False}'),
    ("check-closes-once",
     '    if not check or check["closed"]:\n        return',
     "    if not check:\n        return"),
    # GitHub refuses a title over 255 characters, and refuses the whole
    # update with it, leaving the indicator running.
    ("check-title-fits",
     '"output": {"title": title[:255], "summary": summary or title}},',
     '"output": {"title": title, "summary": summary or title}},'),
    # An empty details_url is not a URL and GitHub judges the whole
    # request on it, so the create fails and no indicator appears at all.
    ("check-omits-an-empty-url",
     '    if pr.get("url"):\n        asked["details_url"] = pr["url"]',
     '    asked["details_url"] = pr.get("url") or ""'),
    # The title is the whole of what the checks list communicates.
    ("check-title-counts-findings",
     '        tally = severity_tally(findings)\n'
     '        title = "%d finding%s%s" % (\n'
     '            len(findings), "" if len(findings) == 1 else "s",\n'
     '            " (%s)" % tally if tally else "")',
     '        title = "Reviewed"'),
    ("check-title-not-clean-when-unreadable",
     '        title = "Nothing Vinegar could read"',
     '        title = "No findings"'),
    ("check-title-says-a-partial-run",
     "    if not whole:\n"
     '        title = "%s, and the review did not finish" % title',
     "    if False:\n        pass"),
    # Off `whole` and not off the note, or a review that ran to the end on
    # the fallback model is titled as one that was cut short.
    ("check-title-partial-off-whole-not-the-note",
     "    if not whole:\n"
     '        title = "%s, and the review did not finish" % title',
     "    if note:\n"
     '        title = "%s, and the review did not finish" % title'),
    # The scope had never reached the closed title, which mattered less
    # while a narrowed clean round was grey like every other ending.
    ("check-title-says-what-was-read",
     "    if since:\n"
     '        title = "%s in what was added since `%s`" % (title, since[:7])',
     "    if False:\n        pass"),
    ("check-closed-in-finish",
     "    close_check(label, check, title,",
     "    (lambda *a, **k: None)(label, check, title,"),
    # Left open, the pull request lists a Vinegar check that spins for
    # ever and the next attempt reuses it rather than clearing it.
    ("check-closed-when-the-review-fails",
     "        close_check(key, check, ended_title(outcome, attempts),",
     "        (lambda *a, **k: None)(key, check, ended_title(outcome, attempts),"),

    # --- what the first review pass found ------------------------------
    ("check-close-retryable-after-a-refusal",
     '    check["closed"] = settled is not None',
     '    check["closed"] = True'),
    ("check-reuse-needs-an-id",
     '            == str(config["github_app"].get("app_id")) and was.get("id")\n'
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]',
     '            == str(config["github_app"].get("app_id"))\n'
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]'),
    ("check-body-matches-the-flag",
     "    body = json.dumps(payload) if payload is not None else None",
     "    body = json.dumps(payload) if payload else None"),
    ("check-closed-even-if-recording-raises",
     "    finally:\n"
     "        # Its own credentials, minted now. The ones above were asked to",
     "    except BaseException:\n"
     "        raise\n"
     "    else:\n"
     "        # Its own credentials, minted now. The ones above were asked to"),
    ("check-done-that-posted-nothing-is-not-finished",
     '    return "The review ran but nothing reached the pull request"',
     '    return "The review finished"'),
    ("check-closed-on-fresh-credentials",
     "        close_check(key, check, ended_title(outcome, attempts),\n"
     "                    posting_env(key, config, repo, tokens, env) or env)",
     "        close_check(key, check, ended_title(outcome, attempts), env)"),
    # Not the extraction, which changes no behaviour and so nothing can
    # catch: the format itself, which GitHub rejects the update over.
    ("utc-stamp-format",
     '    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")',
     '    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")'),

    # --- what the second review pass found -----------------------------
    # A refused close must not let the backstop relabel a posted review as
    # one that never posted.
    ("check-retry-repeats-the-first-title",
     '    title = check.get("said") or title\n'
     '    summary = check.get("summary") or summary',
     "    pass"),
    # app_jwt signs with str(app_id), so a quoted one mints and matched
    # nothing here.
    ("check-app-id-compared-as-strings",
     '            if str((was.get("app") or {}).get("id"))\n'
     '            == str(config["github_app"].get("app_id")) and was.get("id")\n'
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]',
     '            if (was.get("app") or {}).get("id")\n'
     '            == config["github_app"].get("app_id") and was.get("id")\n'
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]'),
    # Opening it is the one call here that parses a reply GitHub sent.
    ("check-opened-inside-the-try",
     "        check = open_check(key, repo, pr, config, env, blockers)\n"
     "        try:",
     "        try:"),
    # The hand-run path: reachable by no check and anchored by no
    # mutation until the second pass said so.
    ("check-hand-run-opens-one",
     "                hand = open_check(args.pr, repo, pr, config, env, "
     "blockers)",
     "                hand = None"),
    ("check-hand-run-closes-it",
     "                close_check(args.pr, hand, ended_title(outcome),",
     "                (lambda *a, **k: None)(args.pr, hand, ended_title(outcome),"),
    ("check-hand-run-records-through-ctrl-c",
     "            finally:\n"
     "                # Not \"finished\" for a review that answered DONE. finish()",
     "            except BaseException:\n"
     "                raise\n"
     "            else:\n"
     "                # Not \"finished\" for a review that answered DONE. finish()"),
    # The reuse lookup's query. Dropping the status filter adopts a
    # completed run, and a completed run cannot be reopened.
    ("check-reuse-asks-for-running-only",
     '        "commits/%s/check-runs?check_name=%s&status=in_progress"',
     '        "commits/%s/check-runs?check_name=%s"'),

    # --- constants -----------------------------------------------------
    ("max-attempts", "MAX_ATTEMPTS = 3", "MAX_ATTEMPTS = 99"),
    ("severity-timeout-value",
     "SEVERITY_TIMEOUT = 300", "SEVERITY_TIMEOUT = 0"),
    # The value, not just the argument. Removing `timeout=` leaves the
    # constant intact, so the check that the clone gets longer than the
    # fetch was covered by neither of the two entries above it.
    ("clone-timeout-value", "CLONE_TIMEOUT = 1800", "CLONE_TIMEOUT = 60"),
    ("checkout-grace-value", "CHECKOUT_GRACE = 1500", "CHECKOUT_GRACE = 60"),
    ("efforts-ultra",
     'EFFORTS = ("low", "medium", "high", "xhigh", "max")',
     'EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")'),

    # --- scoping a pass to what it has not reviewed --------------------
    # Each probe deleted on its own. Breaking both at once is the mistake
    # the suite's own _missing() helper documents: with either one left,
    # a test that refuses every probe stays green.
    # Written as a whole-tuple swap rather than a line deletion. Deleting
    # the two lines leaves `in ((list, str)):`, whose outer parens are
    # grouping rather than a tuple, so the loop unpacks the probe itself
    # and raises. That comes back ABORTED, which hides whether any check
    # would have caught it.
    ("scope-commit-probe",
     "            ([\"git\", \"cat-file\", \"-e\", since + \"^{commit}\"],\n"
     "             \"the commit its last review finished at is not in this clone\",\n"
     "             by_exit),\n"
     "",
     ""),
    ("scope-ancestor-probe",
     "            ([\"git\", \"merge-base\", \"--is-ancestor\", since, pr[\"headRefOid\"]],\n"
     "             \"the branch was rewritten since its last review\", by_exit),\n"
     "",
     ""),
    # `-e <sha>` answers yes for a blob or a tree carrying that id.
    ("scope-commit-peel",
     '(["git", "cat-file", "-e", since + "^{commit}"],',
     '(["git", "cat-file", "-e", since],'),
    # The two ways a probe says no, each narrowing instead of widening.
    ("scope-probe-timeout",
     '            log("%s: %s did not finish within %ds, so the whole pull '
     'request "\n'
     '                "is reviewed" % (label, " ".join(probe), '
     'DIFF_TIMEOUT))\n'
     "            return None",
     "            return since"),
    ("scope-probe-refused",
     "        if refused_if(result):\n"
     "            log(\"%s: %s, so the whole pull request is reviewed\"\n"
     "                % (label, why))\n"
     "            return None",
     "        pass"),
    # --- what may be recorded as reviewed ------------------------------
    ("state-entry-sha-shape",
     "    if reviewed_sha and FULL_SHA.match(reviewed_sha):",
     "    if reviewed_sha:"),
    ("load-state-sha-shape",
     "                if seen is not None and not (isinstance(seen, str)\n"
     "                                             and FULL_SHA.match(seen)):",
     "                if False:"),
    # Unanchored, `re.match` takes a good sha with anything after it.
    ("full-sha-anchored",
     'FULL_SHA = re.compile(r"\\A[0-9a-f]{40}\\Z")',
     'FULL_SHA = re.compile(r"[0-9a-f]{40}")'),

    # --- telling the reviewer, and telling the pull request ------------
    ("brief-since",
     '           "--append-system-prompt", reviewer_brief(pr, config, since,\n'
     '                                                    blockers),',
     '           "--append-system-prompt", reviewer_brief(pr, config, None,\n'
     '                                                    blockers),'),
    ("brief-scope-name",
     '           "the pull request\'s full diff" if since '
     'else "the review scope",',
     '           "the review scope",'),
    # A denial the reviewer has to discover costs it a turn each time. Six
    # of PR #24's sixteen were sed, find and awk doing what Read, Grep and
    # Glob already do; most of the rest were python3 running the suite.
    ("brief-names-the-read-tools",
     '        "it. Use Read, Grep and Glob to read this checkout: `sed`, `awk`, "\n'
     '        "`find` and every interpreter, `python3` among them, are denied, so "\n'
     '        "reaching for one costs a turn and returns nothing. You cannot run "',
     '        "it. You cannot run "'),
    # Separate, because it is a different failure: told only which commands
    # are denied, a reviewer plans a review around running the tests.
    ("brief-says-the-code-cannot-be-run",
     '        "reaching for one costs a turn and returns nothing. You cannot run "\n'
     '        "this repository\'s tests or any of its code. Do not substitute a "',
     '        "reaching for one costs a turn and returns nothing. Do not substitute a "'),
    ("brief-may-read-anything",
     '        "`git diff %s..HEAD` is the review scope. Read anything in '
     'the "',
     '        "`git diff %s..HEAD` is the review scope. Consider the "'),
    ("body-says-what-it-read",
     '    if since:\n'
     '        lines += ["", "This pass reviewed only what was added since '
     '`%s`, "\n'
     '                      "which is where the last review of this pull '
     'request "\n'
     '                      "finished. Earlier findings are already on the '
     'pull "\n'
     '                      "request as their own comments." % since[:7]]',
     "    pass"),

    # The trap the whole change turns on. GitHub takes an inline comment
    # only on a line in the pull request's diff, and the reviews endpoint
    # applies the review whole or not at all, so narrowing the anchors
    # along with the reading scope loses every finding to one bad anchor.
    # --- what review() answers about its own coverage ------------------
    ("covered-needs-a-whole-reading",
     '            if whole and findings is not None and config["comment"]:',
     '            if findings is not None and config["comment"]:'),
    ("covered-needs-findings",
     '            if whole and findings is not None and config["comment"]:',
     '            if whole and config["comment"]:'),
    ("covered-needs-a-pull-request",
     '            if whole and findings is not None and config["comment"]:',
     "            if whole and findings is not None:"),
    ("whole-is-not-the-note",
     "        notes.append(partial_note(\"failed before it finished\"))\n"
     "        whole = False",
     "        notes.append(partial_note(\"failed before it finished\"))"),
    ("whole-reaches-deliver",
     "    deliver(text, findings, \" \".join(notes) or None, whole=whole)",
     "    deliver(text, findings, \" \".join(notes) or None, whole=True)"),
    ("covered-needs-the-post-to-land",
     "                note, resent=resent, check=check, since=since,\n"
     "                blockers=blockers, whole=whole)) == POSTED:",
     "                note, resent=resent, check=check, since=since,\n"
     "                blockers=blockers, whole=whole)) or True:"),
    # `whole` is passed rather than read off the note for the reason
    # deliver's own comment gives, and finish() now needs it for the tick
    # as well as the title.
    ("check-whole-reaches-finish",
     "                blockers=blockers, whole=whole)) == POSTED:",
     "                blockers=blockers)) == POSTED:"),
    # The two the third review found anchored by nothing.
    ("load-state-drops-the-entry",
     '                    del done["reviewed_sha"]',
     "                    done.clear()"),
    ("covered-is-not-the-note-either",
     '            if whole and findings is not None and config["comment"]:',
     "            if (whole and not note and findings is not None\n"
     '                    and config["comment"]):'),
    ("reviewed-through-rule",
     "    return {\"reviewed_sha\": head if covered else was.get(\"reviewed_sha\")}",
     "    return {\"reviewed_sha\": head}"),
    ("scope-same-head",
     '    if since == pr["headRefOid"]:\n'
     '        log("%s: nothing has been pushed since its last review, so the '
     'whole "\n'
     '            "pull request is reviewed" % label)\n'
     "        return None",
     "    pass"),
    ("scope-probe-raises",
     "        except Exception as err:\n"
     '            log("%s: %s could not be run (%s), so the whole pull '
     'request is "\n'
     '                "reviewed" % (label, " ".join(probe), err))\n'
     "            return None",
     "        except Exception:\n"
     "            return since"),

    # --- saying so where a repost will find it -------------------------
    ("transcript-says-the-scope",
     "    if since:\n"
     "        marks.append(\"%s`%s`.\" % (SCOPE_MARK, since[:7]))",
     "    if False:\n"
     "        marks.append(\"%s`%s`.\" % (SCOPE_MARK, since[:7]))"),
    ("transcript-gets-the-scope",
     "            label, save_transcript(repo, pr, text, findings, note, since,\n"
     "                                   blockers))))",
     "            label, save_transcript(repo, pr, text, findings, note,\n"
     "                                   blockers=blockers))))"),

    # --- the brief's two instructions for one decision -----------------
    ("brief-no-contradictory-give-up",
     '           "If `%s` does not resolve either, review the whole branch '
     'and "\n'
     '           "say which refs you could not reach." % since if since else\n'
     '           "If neither resolves, say you could not establish the scope "\n'
     '           "rather than guessing at one.",',
     '           "If neither resolves, say you could not establish the scope "\n'
     '           "rather than guessing at one.",'),

    # --- the manual half, which no check reached before ----------------
    ("hand-run-since",
     "                    where, repo, pr, config, env, tokens, check=hand,\n"
     "                    since=since, blockers=blockers)",
     "                    where, repo, pr, config, env, tokens, check=hand,\n"
     "                    blockers=blockers)"),
    ("hand-run-whole-flag",
     "            since = None if args.whole else review_scope(",
     "            since = None or review_scope("),
    ("hand-run-records-the-start",
     "                           **reviewed_through(covered, pr[\"headRefOid\"],\n"
     "                                              was),\n"
     "                           **rounds_done(reached, was)))",
     "                           **rounds_done(reached, was)))"),

    ("anchors-from-the-base",
     '            findings, diff_lines(path, pr["baseRefName"], env, label), '
     "label)",
     "            findings, diff_lines(path, since or pr[\"baseRefName\"], "
     "env, label), label)"),

    # --- what the second review found ----------------------------------
    ("scope-merge-honours-exit-code",
     "        return result.returncode != 0 or result.stdout.strip()",
     "        return result.stdout.strip()"),
    ("scope-probe-bound",
     "            result = run(probe, cwd=path, env=env, "
     "timeout=DIFF_TIMEOUT)",
     "            result = run(probe, cwd=path, env=env)"),
    ("state-sha-drops-only-itself",
     "                    log(\"%s: its reviewed_sha in %s is not a commit id, so \"\n"
     "                        \"the whole pull request is reviewed\" % (\n"
     "                            key, STATE_PATH))\n"
     "                    del done[\"reviewed_sha\"]",
     "                    done[\"reviewed_sha\"] = \"0\" * 40"),
    ("whole-flag-needs-pr",
     "    if args.whole and not args.pr:\n"
     '        sys.exit("--whole only means something with --pr; the '
     'daemon\'s own "\n'
     '                 "scoping is not a command-line choice")',
     "    pass"),
    ("repost-keeps-the-scope",
     "            sep = body.find(TRANSCRIPT_SEP)\n"
     "            starts = sep + len(TRANSCRIPT_SEP) if sep != -1 else -1\n"
     '            end = (body.find("\\n\\n", starts)\n'
     "                   if starts != -1 and body.startswith(LIFTED_MARKS, starts)\n"
     "                   else -1)\n"
     "            if end != -1:\n"
     '                opening += "%s\\n\\n" % body[starts:end]\n'
     "                body = body[:starts] + body[end + 2:]",
     "            pass"),
    # Read anywhere in the file rather than at the offset the separator
    # gives, which is what let the reviewer's own prose be hoisted into
    # the resend's opening and cut out of the review.
    ("repost-scope-read-unanchored",
     '            end = (body.find("\\n\\n", starts)\n'
     "                   if starts != -1 and body.startswith(LIFTED_MARKS, starts)\n"
     "                   else -1)",
     "            starts = body.find(SCOPE_MARK)\n"
     '            end = body.find("\\n\\n", starts) if starts != -1 else -1'),
    # Matching one mark rather than either. Harmless on a transcript that
    # carries both, since they are one newline apart and the lift reads to
    # the blank line past them either way; what it loses is the pass that
    # read everything and reported narrowly, whose only mark is the
    # blockers one. That transcript then keeps its mark in the body for the
    # cut to take, and a review that reported only what breaks at runtime
    # arrives days later as a review that found one thing.
    ("repost-lifts-only-the-scope-mark",
     "                   if starts != -1 and body.startswith(LIFTED_MARKS, starts)",
     "                   if starts != -1 and body.startswith(SCOPE_MARK, starts)"),

    # --- a later review reports only blockers ---------------------------
    # The by-one that costs a whole round of findings nobody is shown.
    ("blockers-round-boundary",
     "    return after is not None and round_number > after",
     "    return after is not None and round_number >= after"),
    # Counting the rounds already done rather than the one about to run,
    # which is the same round early by another route.
    ("blockers-counts-the-review-about-to-run",
     '    number = entry.get("rounds", 0) + 1',
     '    number = entry.get("rounds", 0)'),
    # A round charged for a review that never reported anything. Three bad
    # minutes at GitHub then decide that the next real review is narrowed.
    ("rounds-only-for-a-review-that-ran",
     '    return {"rounds": was.get("rounds", 0) + (1 if reached else 0)}',
     '    return {"rounds": was.get("rounds", 0) + 1}'),
    # Read off the head-scoped copy, which is empty whenever the head has
    # moved — and the head moving is the normal way a round ends, so the
    # count never reaches two and nothing is ever narrowed.
    ("rounds-survive-the-head-moving",
     "                   **reviewed_through(covered, head, done),\n"
     "                   **rounds_done(reached, done)))",
     "                   **reviewed_through(covered, head, done),\n"
     "                   **rounds_done(reached, kept)))"),
    # The rebuilds that are not reviews handing the count back. One draft
    # toggle or one failed clone and the pull request reports everything
    # again.
    ("rounds-survive-a-skip",
     "                        **dict(carry_forward(kept),\n"
     "                               **reviewed_through(False, head, done),\n"
     "                               **rounds_done(False, done)))",
     "                        **dict(carry_forward(kept),\n"
     "                               **reviewed_through(False, head, done)))"),
    # The reviewer told nothing, so the narrowing is a sentence on the pull
    # request about a review that was never asked to hold anything back.
    ("blockers-reach-the-reviewer",
     '           blockers_brief(config) if blockers else ""))',
     '           ""))'),
    # The paragraph put where `since` goes, ahead of the reporting contract
    # rather than after it. The contract then has the last word, and it
    # ends "a finding you leave out of it is a finding nobody sees".
    ("blockers-answer-the-reporting-contract",
     '           since_brief(since) if since else "", REPORT_TOOL,\n'
     '           blockers_brief(config) if blockers else ""))',
     '           blockers_brief(config) if blockers else "", REPORT_TOOL,\n'
     '           since_brief(since) if since else ""))'),
    # Read as a licence to look for less rather than to report less. The
    # judgement of whether a thing is a blocker is the expensive judgement
    # this program buys, and it cannot be made from a skimmed diff.
    ("blockers-narrow-reporting-not-reading",
     '        "Read and judge exactly as carefully as you would on any other "\n'
     '        "pass. Only what you report is narrowed: report every blocker you "',
     '        "Look only for blockers and skim the rest. Report every blocker "'),
    # The permission to find nothing removed, which is the sentence that
    # stands between this and the inflation the severity pass measured.
    ("blockers-may-report-nothing",
     '        "all is the expected outcome here, and it is the right answer "',
     '        "all would be a surprise, so look until you have one, and it is "'),
    # The pull request not told, so a quiet later review reads as the change
    # being clean when it only means nothing in it breaks at runtime.
    ("blockers-said-on-the-pull-request",
     '        lines += ["", "The first %s of a pull request %s everything %s "\n'
     '                      "find%s. This is a later one, so it was asked for "',
     '        lines += ["", "" if True else "%s%s%s%s"'),
    # The sentence going back to promising an output Vinegar never
    # filters. This is what `wonky-flow#107` round three posted: one
    # finding tiered `advisory` directly under a claim that nothing
    # smaller was listed.
    ("narrowed-comment-promises-the-output",
     '                      "at runtime. Anything smaller it found, it was told "\n'
     '                      "to leave out." % (',
     '                      "at runtime. Anything smaller it found is not listed "\n'
     '                      "here." % ('),
    # The paragraph that explains a tier under blocker, gone. The tag then
    # stands alone under a paragraph about blockers, which is what a
    # reader has no second pass to explain.
    ("disagreement-never-explained",
     "        if disagreed:\n"
     "            # The constant, not a copy: save_transcript() writes the same",
     "        if False:\n"
     "            # The constant, not a copy: save_transcript() writes the same"),
    # And said on every narrowed round, including the ordinary one that
    # found nothing, where it answers a question nobody asked.
    ("disagreement-explained-unasked",
     "        if disagreed:",
     "        if True:"),
    # Lifted out of the narrowing entirely, which is the regression the
    # two entries above cannot reach: both keep the block nested, so an
    # ordinary first-round review is unaffected by either and the check
    # for that stayed green under the whole run. Dedented here, so every
    # review carries a paragraph about a severity pass nobody narrowed.
    ("disagreement-explained-off-a-full-review",
     "        if disagreed:\n"
     "            # The constant, not a copy: save_transcript() writes the same",
     "    if disagreed:\n"
     "            # The constant, not a copy: save_transcript() writes the same"),
    # The wording that points at tags rendered somewhere else. An anchored
    # finding's tag goes into its inline comment on the diff, so on the
    # common case a paragraph promising tags below it points at nothing.
    ("disagreement-points-below-itself",
     '    "The tier tag on each finding is set after the review, by a separate "',
     '    "The tier tags below are set after the review, by a separate "'),
    # The comment building its own copy of the sentence again, which is
    # the drift this pull request is about: one sentence, two spellings,
    # and the next wording fix reaching only one of them.
    ("disagreement-said-twice",
     '            lines += ["", DISAGREED_SAID]',
     '            lines += ["", "The tier tag on each finding is set '
     'afterwards by a separate pass."]'),
    # The mark going back to claiming what came back. It is the transcript's
    # half of the same sentence the comment and the check title dropped,
    # and repost() lifts it into the opening of a review delivered days
    # later, above bullets carrying the severity pass's tier dots.
    ("transcript-mark-claims-the-output",
     'BLOCKERS_MARK = "Asked for: blockers only."',
     'BLOCKERS_MARK = "Reported: blockers only."'),
    # And any other verb making that claim, which is what the check caught
    # only by forbidding one word before it was written both ways.
    ("transcript-mark-claims-it-in-another-word",
     'BLOCKERS_MARK = "Asked for: blockers only."',
     'BLOCKERS_MARK = "Returned: blockers only."'),
    # The lift left knowing only the spelling this version writes. Every
    # transcript written by an older one is then unmatched, and for the
    # oversized transcript the lift exists for the cut shears the
    # narrowing off the front.
    ("lift-forgets-the-spelling-already-on-disk",
     'LIFTED_MARKS = (SCOPE_MARK, BLOCKERS_MARK, "Reported: blockers only.")',
     "LIFTED_MARKS = (SCOPE_MARK, BLOCKERS_MARK)"),
    # The transcript's copy of the disagreement paragraph, which is the
    # only copy a review delivered from disk can carry.
    ("transcript-never-explains-the-disagreement",
     "        if below_blocker(findings):\n"
     "            marks.append(DISAGREED_SAID)",
     "        pass"),
    ("transcript-explains-it-unasked",
     "        if below_blocker(findings):\n"
     "            marks.append(DISAGREED_SAID)",
     "        marks.append(DISAGREED_SAID)"),
    # Lifted out of the narrowing, where it opens the block on an ordinary
    # review and repost() then matches nothing.
    ("transcript-explanation-outside-the-narrowing",
     "    if blockers:\n"
     "        marks.append(BLOCKERS_MARK)\n"
     "        # Nested, not a third `if`, because DISAGREED_SAID explains the\n"
     "        # line above it and must never open the block: repost() finds the\n"
     "        # block by matching its first line and would leave a block opening\n"
     "        # with this one in the body, unlifted.\n"
     "        if below_blocker(findings):\n"
     "            marks.append(DISAGREED_SAID)",
     "    if blockers:\n"
     "        marks.append(BLOCKERS_MARK)\n"
     "    if below_blocker(findings):\n"
     "        marks.append(DISAGREED_SAID)"),
    # Written outside the block the repost lifts, so the mark survives on
    # disk and is lost from every review delivered from a transcript.
    ("blockers-mark-inside-the-lifted-block",
     '    if marks:\n'
     '        body = "%s\\n\\n%s" % ("\\n".join(marks), body)',
     '    if marks:\n'
     '        body = "%s\\n\\n%s" % ("\\n\\n".join(marks), body)'),
    # The checks list left saying a full review ran. `gh pr checks` is the
    # half of this an agent reads, and the comment does not reach it.
    ("blockers-in-the-checks-list",
     '                 "title": "Reviewing at %s effort%s" % (\n'
     '                     config["effort"], ", blockers only" if blockers else ""),',
     '                 "title": "Reviewing at %s effort" % config["effort"],'),
    # The retry rebuilds the body from nothing, so a scope dropped here is
    # dropped from the only comment the author gets when GitHub refuses the
    # anchors.
    ("blockers-survive-the-anchor-retry",
     "            tally=severity_tally(findings), since=since, "
     "blockers=blockers,",
     "            tally=severity_tally(findings), since=since,"),
    # The wire the paragraph rides. Dropped on either posting path it is a
    # paragraph no real review ever carries, while every direct check on
    # review_body stays green, which is how `since` and `blockers` each
    # shipped uncovered on this same code.
    ("disagreement-passed-to-the-body",
     "                                   disagreed=below_blocker(findings))}",
     "                                   disagreed=False)}"),
    ("disagreement-survives-the-anchor-retry",
     "            disagreed=below_blocker(findings),\n",
     ""),
    # `note` is as much under the bar as `advisory`, and reading only the
    # one word leaves the commonest smallest tier unexplained.
    ("below-blocker-forgets-the-smallest-tier",
     '    return any(finding.get("tier") in under for finding in findings or ())',
     '    return any(finding.get("tier") == "advisory"\n'
     "               for finding in findings or ())"),
    # And counting `blocker` itself, which explains a disagreement on
    # every narrowed round that agreed.
    ("below-blocker-counts-blockers-too",
     '    under = TIERS[TIERS.index("blocker") + 1:] if "blocker" in TIERS else ()',
     "    under = TIERS"),
    # "Everything but the most severe" rather than "under blocker". The
    # same set today, which is why only the check that reorders TIERS
    # notices, and the day a tier is added above `blocker` the slice takes
    # in `blocker` itself.
    ("below-blocker-reads-position-not-name",
     '    under = TIERS[TIERS.index("blocker") + 1:] if "blocker" in TIERS else ()',
     "    under = TIERS[1:]"),
    # And raising when the name is gone, on the path that saves the
    # transcript and posts the review: a ValueError there is a finished
    # review that reaches neither disk nor the pull request while the
    # outcome is recorded DONE.
    ("below-blocker-raises-without-the-name",
     '    under = TIERS[TIERS.index("blocker") + 1:] if "blocker" in TIERS else ()',
     '    under = TIERS[TIERS.index("blocker") + 1:]'),
    # Counting a round for a review whose findings never reached the pull
    # request. Two refused postings and the third round tells the author
    # that the first two "reported everything they found, and those
    # findings are on the pull request already", on a pull request that
    # carries nothing at all.
    ("rounds-need-the-post-to-land",
     "                   **rounds_done(reached, done)))",
     "                   **rounds_done(outcome == DONE, done)))"),
    ("hand-run-rounds-need-the-post-to-land",
     "                           **rounds_done(reached, was)))",
     "                           **rounds_done(outcome == DONE, was)))"),
    # Read off the filesystem rather than off review()'s answer. The
    # marker is written only when the transcript write succeeded, so a run
    # that could neither save nor post leaves none and reads as a round
    # the author never saw.
    ("rounds-not-inferred-from-the-marker",
     "                   **rounds_done(reached, done)))",
     "                   **rounds_done(outcome == DONE and not os.path.exists(\n"
     "                       unposted_path(repo, pr)), done)))"),
    # The `comment` guard, which is what keeps a dry run from counting.
    # post_review answers POSTED for correctly posting nothing.
    ("rounds-need-a-pull-request-to-reach",
     '            if config["comment"]:\n'
     "                reached.append(True)",
     "            reached.append(True)"),
    # And the other side of that rule: the send that finally lands is the
    # one moment a refused review reaches the author, so the round it never
    # got is counted there. Dropped, a pull request whose posting failed
    # twice reports everything for ever.
    ("rounds-counted-when-the-repost-lands",
     "            entry.update(rounds_done(True, done))",
     "            entry.update(rounds_done(False, done))"),
    # The marker written before the review runs, which is what a process
    # killed mid-review leaves behind. Dropping the carry there hands back
    # every round already spent.
    ("rounds-survive-the-pre-review-marker",
     "        **dict(carry_forward(kept), post_tries=0, waivers=0,\n"
     "               **reviewed_through(False, head, done),\n"
     "               **rounds_done(False, done))))",
     "        **dict(carry_forward(kept), post_tries=0, waivers=0,\n"
     "               **reviewed_through(False, head, done))))"),
    # The give-up rebuild, which rounds_done()'s own docstring names as a
    # case it exists for and which nothing was holding.
    ("rounds-survive-a-give-up",
     "                               **reviewed_through(False, head, was),\n"
     "                               **rounds_done(False, was)))",
     "                               **reviewed_through(False, head, was)))"),
    # The narrowing reaching the checks list only while the review runs.
    # close_check overwrites the in_progress title on the way out, and the
    # one it leaves behind stands for the rest of the pull request's life.
    ("blockers-in-the-finished-check",
     '        title = "%s, asked for blockers only" % title',
     "        pass"),
    # And the verb in it. "reporting blockers only" is a claim about what
    # came back, beside a tally that can say `1 advisory`; "asked for" is
    # the claim the title can keep. Deliberately the same anchor as the
    # entry above: an edit to that line takes both out at once, and two
    # ANCHOR lines naming the same string is the clearest thing to repair.
    ("finished-check-claims-the-output",
     '        title = "%s, asked for blockers only" % title',
     '        title = "%s, reporting blockers only" % title'),
    # The hand-run path's own wire, which shipped uncovered once before on
    # this same code and did again here.
    ("hand-run-blockers",
     "            blockers = narrows and not args.whole",
     "            blockers = False"),
    ("hand-run-whole-widens-severity-too",
     "            blockers = narrows and not args.whole",
     "            blockers = narrows"),
    # The transcript's flag, which travelled beside `since` with no guard
    # of its own. Dropped, BLOCKERS_MARK reaches no transcript a real
    # review produced, and the repost reads the mark rather than the flag.
    ("transcript-gets-the-blockers-flag",
     "            label, save_transcript(repo, pr, text, findings, note, since,\n"
     "                                   blockers))))",
     "            label, save_transcript(repo, pr, text, findings, note, "
     "since))))"),
    # A zero accepted, which means a first review that reports only
    # blockers: the pull request is never told anything smaller, once, and
    # nothing on it says why.
    ("blockers-only-after-refuses-zero",
     "    if rounds is not None and (not isinstance(rounds, int)\n"
     "                               or isinstance(rounds, bool) or rounds <= 0):",
     "    if rounds is not None and not isinstance(rounds, int):"),

    # --- closing the checks a stopped Vinegar left spinning -------------
    # The wire, which every check on sweep_checks() itself is blind to:
    # they call it directly, so this shipped uncovered would leave all of
    # them green and no deployment sweeping anything.
    ("sweep-reaches-the-daemon",
     "        sweep_checks(config, tokens)",
     "        pass"),
    # And after the first poll rather than before it, where it closes the
    # indicator that poll just opened: the pull request then shows a
    # review running under a neutral entry saying it was interrupted.
    ("sweep-before-the-first-poll",
     "        sweep_checks(config, tokens)\n"
     "        while True:\n"
     "            poll_once(config, state, tokens)",
     "        while True:\n"
     "            poll_once(config, state, tokens)\n"
     "            sweep_checks(config, tokens)"),
    # `pr_key` back above the per-pull-request try, where a listing that
    # answers 0 with entries carrying no `number` raises out of
    # sweep_checks and past main()'s KeyboardInterrupt-only handler: the
    # daemon dies at startup and launchd restarts it into the same line.
    ("sweep-pr-key-outside-the-guard",
     "            try:\n"
     "                label = pr_key(repo, pr)\n"
     "                found = running_checks(label, repo, pr[\"headRefOid\"],\n"
     "                                       config, env)",
     "            label = pr_key(repo, pr)\n"
     "            try:\n"
     "                found = running_checks(label, repo, pr[\"headRefOid\"],\n"
     "                                       config, env)"),
    # One bad pull request taking the rest of the repository with it.
    ("sweep-stops-at-the-first-bad-pr",
     "                log(\"%s#%s: could not read its old checks: %s\"\n"
     "                    % (repo, pr.get(\"number\", \"?\"), err))\n"
     "                continue",
     "                log(\"%s#%s: could not read its old checks: %s\"\n"
     "                    % (repo, pr.get(\"number\", \"?\"), err))\n"
     "                break"),
    # A repository whose checks cannot be read asked once per open pull
    # request, which is check_api's three-line permission paragraph times
    # the number of them, on every start, every thirty seconds.
    ("sweep-asks-an-unreadable-repo-once-per-pr",
     "            if found is None:\n"
     "                if answered:\n"
     "                    continue\n"
     "                log(\"%s: cannot read its check runs, so the rest of this \"\n"
     "                    \"repository is swept on a later start\" % repo)\n"
     "                break",
     "            if found is None:\n"
     "                found = []"),
    # The bound applied to the whole sweep rather than to one repository,
    # so a deployment whose first repository has not accepted the
    # permission never sweeps the others at all, on every start.
    ("sweep-drops-every-later-repo-too",
     "                    \"repository is swept on a later start\" % repo)\n"
     "                break",
     "                    \"repository is swept on a later start\" % repo)\n"
     "                return"),
    # And read as "any failure ends the repository", where one 502 on the
    # twentieth of thirty pull requests abandons the last ten.
    ("sweep-ends-a-repo-on-a-transient-failure",
     "            if found is None:\n"
     "                if answered:\n"
     "                    continue",
     "            if found is None:\n"
     "                if False:\n"
     "                    continue"),
    ("sweep-never-marks-a-repo-answered",
     "            answered = True",
     "            answered = False"),
    # Which Vinegar a run belongs to, unstamped at creation, so every
    # instance on the machine reads every run as its own.
    ("check-run-carries-no-deployment",
     '             "external_id": DEPLOYMENT,\n',
     ""),
    # And matched on the App alone, which two instances share: the sweep
    # then closes the other one's live indicator and reuse adopts a run
    # it is still writing to.
    ("check-run-deployment-not-matched",
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]',
     "            ]"),
    # A run with no stamp refused rather than adopted, which strands
    # every run open at the moment of the upgrade.
    ("check-run-legacy-stamp-refused",
     '            and str(was.get("external_id") or DEPLOYMENT) == DEPLOYMENT]',
     '            and str(was.get("external_id")) == DEPLOYMENT]'),
    # And the answer that makes that distinction possible at all.
    ("running-checks-hides-a-failed-read",
     "    if said is None:\n"
     "        return None\n"
     '    return [was.get("id") for was in said.get("check_runs") or []',
     '    return [was.get("id") for was in (said or {}).get("check_runs") or []'),
    # `failure` makes the stuck merge the outcome rather than the thing
    # being repaired, on a check that read nothing and reported nothing.
    ("sweep-closes-as-a-failure",
     '                            "review, the next poll starts one.")',
     '                            "review, the next poll starts one.",\n'
     '                            "failure")'),
    # And the title going back to claiming the review said something,
    # on a run that read nothing and reported nothing.
    ("sweep-title-claims-a-result",
     '                            "The review was interrupted", env,',
     '                            "No findings", env,'),
    # Sweeping where open_check refuses to open: a dry run has nothing on
    # the pull request to close, and without an App these runs are not
    # Vinegar's to PATCH.
    ("sweep-runs-without-an-app-or-a-post",
     '    if not config["comment"] or not config.get("github_app"):\n'
     "        return\n"
     '    for repo in config["repos"]:\n'
     "        try:\n"
     "            env = github_env(config, repo, tokens, good_for=LISTING_GRACE)",
     '    for repo in config["repos"]:\n'
     "        try:\n"
     "            env = github_env(config, repo, tokens, good_for=LISTING_GRACE)"),
    # One repository's failed listing ending the sweep, which is a daemon
    # that never reaches its loop and polls nothing at all.
    ("sweep-listing-failure-escapes",
     "        except Exception as err:\n"
     '            log("%s: cannot list pull requests to close old checks: %s"\n'
     "                % (repo, err))\n"
     "            continue",
     "        except Exception:\n"
     "            raise"),

    # --- discovering repositories from the App -------------------------
    # The verb github_api() used to guess. A bodyless POST inferred as a
    # GET 404s on a path whose 404 means "not installed on this
    # repository", so the guess accused the wrong thing.
    ("api-explicit-method",
     '        method=method or ("POST" if body else "GET"))',
     '        method="POST" if body else "GET")'),
    ("discovery-mint-verb",
     '            jwt, method="POST")["token"]',
     '            jwt)["token"]'),
    # An archived repository is a review paid for in full and then a 403
    # where the comment would go, on every push, silently.
    ("discovery-skips-archived",
     '                (archived if repo.get("archived") else found).append(\n'
     '                    repo["full_name"])',
     '                found.append(repo["full_name"])'),
    # A hundred-and-first repository never read is one never reviewed.
    ("discovery-pages",
     "            if len(covered) < per_page:",
     "            if len(covered) <= per_page:"),
    ("discovery-order",
     "    return sorted(found), sorted(archived)",
     "    return found, sorted(archived)"),
    # The token that lists an installation is broader than any the
    # reviewer may hold, so it is spent and dropped.
    ("discovery-token-escapes",
     "    return sorted(found), sorted(archived)",
     "    return sorted(found), [token]"),
    ("discovery-takes-no-cache",
     "def discover_repos(app):",
     "def discover_repos(app, cache=None):"),
    # Asking every minute for an answer that changes monthly.
    ("discovery-interval",
     "    if time.time() - asked_at < DISCOVERY_INTERVAL:",
     "    if time.time() - asked_at < 0:"),
    # One failed listing read as "the App covers nothing" stops every
    # repository being reviewed. Measured 1.1% of attempts on this network.
    ("discovery-failure-keeps-list",
     '        log("cannot ask the App which repositories it covers, so the %d "\n'
     '            "already being polled stay: %s" % (len(config["repos"]), err))\n'
     "        return asked_at",
     '        config["repos"] = []\n'
     "        return asked_at"),
    # A failed ask counted as an ask is an hour of watching nothing, and
    # at startup there is no previous list to fall back to.
    ("discovery-failure-retries-soon",
     '            "already being polled stay: %s" % (len(config["repos"]), err))\n'
     "        return asked_at",
     '            "already being polled stay: %s" % (len(config["repos"]), err))\n'
     "        return time.time()"),
    # A repository that starts or stops being reviewed with nothing saying
    # why. The cause is a checkbox on another machine.
    ("discovery-announces-change",
     "    if new or gone:",
     "    if not (new or gone):"),
    ("discovery-counts-archived",
     '            changed.append("%d archived and left out" % len(archived))',
     "            pass"),
    # A daemon that comes up, polls an empty list once a minute for ever,
    # and says nothing.
    ("config-empty-repos-needs-app",
     '    if not config["repos"] and not config["github_app"]:',
     '    if not config["repos"] and config["github_app"]:'),
    # "there are 0 repositories to poll", printed at a daemon about to
    # discover seventeen.
    ("config-width-skips-undiscovered",
     '    if watched and config["parallel_repos"] > watched:',
     '    if config["parallel_repos"] > watched:'),
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
