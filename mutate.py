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

    # --- the severity pass ---------------------------------------------
    # read_tiers is the whole output surface of a call that reads text
    # quoted out of the branch under review, so what it refuses is as much
    # of the guard as what it accepts.
    ("severity-answer-covers-every-finding",
     "    return [seen[i] for i in range(count)] if len(seen) == count else None",
     "    return [seen[i] for i in sorted(seen)]"),
    ("severity-answer-index-in-range",
     "        if 0 <= index < count and index not in seen:",
     "        if index not in seen:"),
    ("severity-answer-first-wins",
     "        if 0 <= index < count and index not in seen:",
     "        if 0 <= index < count:"),
    ("severity-answer-anchored-at-line-start",
     "        match = TIER_LINE.match(line)",
     "        match = TIER_LINE.search(line)"),
    ("severity-answer-known-tiers-only",
     r'TIER_LINE = re.compile(r"\s*\[?(\d+)\]?[\s:.)-]+(%s)\b" % "|".join(TIERS),',
     r'TIER_LINE = re.compile(r"\s*\[?(\d+)\]?[\s:.)-]+([a-z]+)\b" % (),'),
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
     '    return sorted(tiered, key=lambda finding: TIERS.index(finding["tier"]))',
     "    return tiered"),
    ("severity-copies-rather-than-writes",
     "    tiered = [dict(finding, tier=tier)\n"
     "              for finding, tier in zip(findings, tiers)]",
     "    tiered = findings\n"
     "    for finding, tier in zip(findings, tiers):\n"
     '        finding["tier"] = tier'),
    ("severity-label-opens-the-comment",
     '        summary = "**%s** · %s" % (tier, summary)',
     "        pass"),
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
     "                                   since=since, blockers=blockers)}",
     "                                   note=note, verb=verb,\n"
     "                                   since=since, blockers=blockers)}"),

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
     '"status": "completed", "conclusion": CHECK_CONCLUSION,',
     '"status": "completed", "conclusion": "success",'),
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
     '        return {"repo": repo, "id": mine[0].get("id"), "closed": False}',
     "    if False:\n        pass"),
    ("check-ignores-another-apps",
     '            if str((was.get("app") or {}).get("id"))\n'
     '            == str(config["github_app"].get("app_id")) and was.get("id")]',
     "            if was.get(\"id\")]"),
    # A handle with no id would PATCH `check-runs/None` on every ending.
    ("check-handle-needs-an-id",
     '    return {"repo": repo, "id": made["id"], "closed": False} \\\n'
     "        if made and made.get(\"id\") else None",
     '    return {"repo": repo, "id": (made or {}).get("id"),\n'
     '            "closed": False}'),
    # A handle holding a token is one log line from publishing it.
    ("check-handle-holds-no-credential",
     '        return {"repo": repo, "id": mine[0].get("id"), "closed": False}',
     '        return {"repo": repo, "id": mine[0].get("id"), "env": env,\n'
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
     "    if note:\n"
     '        title = "%s, and the review did not finish" % title',
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
     '            == str(config["github_app"].get("app_id")) and was.get("id")]',
     '            == str(config["github_app"].get("app_id"))]'),
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
     '            == str(config["github_app"].get("app_id")) and was.get("id")]',
     '            if (was.get("app") or {}).get("id")\n'
     '            == config["github_app"].get("app_id") and was.get("id")]'),
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
     "                blockers=blockers)) == POSTED:",
     "                note, resent=resent, check=check, since=since,\n"
     "                blockers=blockers)) or True:"),
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
     "                   if starts != -1 and body.startswith(\n"
     "                       (SCOPE_MARK, BLOCKERS_MARK), starts)\n"
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
     "                   if starts != -1 and body.startswith(\n"
     "                       (SCOPE_MARK, BLOCKERS_MARK), starts)\n"
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
     "                   if starts != -1 and body.startswith(\n"
     "                       (SCOPE_MARK, BLOCKERS_MARK), starts)",
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
     "                             **dict(carry_forward(kept),\n"
     "                                    **reviewed_through(False, head, done),\n"
     "                                    **rounds_done(False, done)))",
     "                             **dict(carry_forward(kept),\n"
     "                                    **reviewed_through(False, head, done)))"),
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
     "                                    waivers=0,\n"
     "                                    **reviewed_through(False, head, done),\n"
     "                                    **rounds_done(False, done)))",
     "                                    waivers=0,\n"
     "                                    **reviewed_through(False, head, done)))"),
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
     '    if blockers:\n'
     '        title = "%s, reporting blockers only" % title',
     "    pass"),
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
