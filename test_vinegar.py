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

"""What Vinegar does with what the reviewer hands back.

    python3 test_vinegar.py

No dependencies, nothing to install, and nothing here touches the network,
GitHub, git or Claude: `run` is replaced with a stub and the assertions are
about what Vinegar would have sent. A review costs real money and eight
minutes, so the parts that can be checked for free are checked for free.

What is covered is the part between the reviewer finishing and the review
appearing: reading the findings out of the stream, working out which of them
can be anchored in the diff, and deciding what to post. Those are pure
functions over strings, and every one of them exists because a live review
went wrong in a way the docstrings now describe.

The event shapes were copied from a stream a real review produced, not
invented.
"""
import json
import os
import atexit
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Before the import: the module reads this at import time to place its state,
# and a test run must not go looking at the real one.
_home = tempfile.mkdtemp(prefix="vinegar-test-home-")
atexit.register(shutil.rmtree, _home, True)
os.environ["VINEGAR_HOME"] = os.path.join(_home, ".vinegar")
import vinegar

PR = {"number": 12, "headRefOid": "a1b2c3d4e5f6", "baseRefName": "release-2",
      "url": "https://github.com/o/r/pull/12"}
CONFIG = dict(vinegar.DEFAULTS, effort="high", comment=True)
ROOT = "/checkouts/o__r"
L = "o/r#12"

DIFF = """diff --git a/vinegar.py b/vinegar.py
index 111..222 100644
--- a/vinegar.py
+++ b/vinegar.py
@@ -10,0 +11,3 @@ def f():
+one
+two
+three
@@ -40 +43 @@ def g():
+changed
diff --git a/doc.md b/doc.md
--- a/doc.md
+++ b/doc.md
@@ -1,0 +2,2 @@
+++ b/spoofed.py
+@@ -1 +9999 @@
diff --git a/crlf.py b/crlf.py
--- a/crlf.py
+++ b/crlf.py
@@ -1,0 +2,1 @@
+text\r@@ -1 +9999 @@
diff --git a/gone.py b/gone.py
--- a/gone.py
+++ /dev/null
@@ -1,5 +0,0 @@
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -7,2 +7,0 @@
"""

posted = []
last_git_diff = [[]]


def fake_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "git" and "diff" in cmd:
        last_git_diff[0] = cmd
        return subprocess.CompletedProcess(
            cmd, fake_run.diff_rc, "" if fake_run.diff_rc else DIFF, "boom")
    if cmd[:2] == ["gh", "api"]:
        posted.append((cmd, json.loads(stdin_text)))
        return subprocess.CompletedProcess(cmd, fake_run.rc, "", "HTTP 422")
    raise AssertionError("unexpected command %r" % cmd)


fake_run.rc = 0
fake_run.diff_rc = 0
vinegar.run = fake_run
vinegar.log = lambda message: None

fails = []


def check(name, condition, detail=""):
    print("%-52s %s" % (name, "ok" if condition else "FAIL " + str(detail)))
    if not condition:
        fails.append(name)


# --- read_stream ----------------------------------------------------------
def stream(*events):
    return "\n".join(json.dumps(e) for e in events)


def call(findings, name="ReportFindings"):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name,
         "input": {"level": "xhigh", "findings": findings}}]}}


DONE_EVENT = {"type": "result", "subtype": "success", "is_error": False,
              "result": "Reviewed it.", "total_cost_usd": 1.5}
REAL = [{"file": "a.py", "line": 2, "summary": "s", "failure_scenario": "f"}]

result, found = read = vinegar.read_stream(stream(call(REAL), DONE_EVENT))
check("findings come back from the tool call", found == REAL, found)
check("the result event comes back with them",
      result and result["total_cost_usd"] == 1.5, result)

check("no tool call is None, not an empty review",
      vinegar.read_stream(stream(DONE_EVENT))[1] is None)
check("an empty findings list is a clean review, not silence",
      vinegar.read_stream(stream(call([]), DONE_EVENT))[1] == [])
check("a later call corrects an earlier one",
      vinegar.read_stream(stream(call([]), call(REAL), DONE_EVENT))[1] == REAL)
check("a subagent's call does not replace the review's own",
      vinegar.read_stream(stream(
          call(REAL),
          dict(call([{"file": "sub.py", "line": 1, "summary": "candidate"}]),
               parent_tool_use_id="toolu_01"),
          DONE_EVENT))[1] == REAL)
check("a findings list holding a non-object is refused whole",
      vinegar.read_stream(
          stream(call(["a bug", {"file": "a.py"}]), DONE_EVENT))[1] is None)
check("another tool's call is not mistaken for findings",
      vinegar.read_stream(stream(call(REAL, name="Bash"), DONE_EVENT))[1]
      is None)
check("a findings array quoted in prose is no longer readable at all",
      vinegar.read_stream(
          '{"type":"assistant","message":{"content":[{"type":"text",'
          '"text":"```json\\n[{\\"file\\": \\"a.py\\"}]\\n```"}]}}\n'
          + json.dumps(DONE_EVENT))[1] is None)
check("one unreadable line does not cost the rest of the stream",
      vinegar.read_stream("not json\n" + stream(call(REAL), DONE_EVENT))[1]
      == REAL)
check("no result event at all is reported as no result",
      vinegar.read_stream(stream(call(REAL)))[0] is None)

# --- diff_lines ----------------------------------------------------------
covered = vinegar.diff_lines(ROOT, "release-2", None, "o/r#12")
check("added lines are covered", covered.get("vinegar.py") == {11, 12, 13, 43},
      covered.get("vinegar.py"))
check("deleted file contributes nothing", "gone.py" not in covered, covered)
check("deletion-only hunk contributes nothing", "README.md" not in covered,
      covered)
check("a carriage return cannot forge a hunk header",
      covered.get("crlf.py") == {2}, covered.get("crlf.py"))
check("an added line that looks like a file header is content",
      "spoofed.py" not in covered and covered.get("doc.md") == {2, 3}, covered)
check("git diff is asked for the prefixes the parser expects",
      "--src-prefix=a/" in last_git_diff[0]
      and "--dst-prefix=b/" in last_git_diff[0], last_git_diff[0])
check("the diff carries the context GitHub accepts comments on",
      "--unified=3" in last_git_diff[0], last_git_diff[0])

fake_run.diff_rc = 1
check("a failed diff anchors nothing rather than guessing",
      vinegar.diff_lines(ROOT, "release-2", None, "o/r#12") == {})
fake_run.diff_rc = 0

# A checkout reached through a symlink must resolve the same either way.
# Everything this run creates lives under one directory, removed on the way
# out, so repeated runs do not fill the system temp directory.
SCRATCH = tempfile.mkdtemp(prefix="vinegar-test-")
atexit.register(shutil.rmtree, SCRATCH, True)
_real = os.path.join(SCRATCH, "real")
_link = os.path.join(SCRATCH, "link")
os.mkdir(_real)
os.symlink(_real, _link)

# --- split_findings ------------------------------------------------------
FINDINGS = [
    {"file": "vinegar.py", "line": 12, "summary": "in diff",
     "failure_scenario": "boom"},
    {"file": os.path.join(ROOT, "vinegar.py"), "line": 43,
     "summary": "absolute path", "failure_scenario": "boom"},
    {"file": "vinegar.py", "line": 900, "summary": "outside the diff"},
    {"file": "/etc/passwd", "line": 1, "summary": "outside the checkout"},
    {"file": "vinegar.py", "line": True, "summary": "bool line"},
    {"file": "vinegar.py", "summary": "no line at all"},
]
check("a line number given as a string still anchors",
      vinegar.finding_line({"line": "43"}) == 43)
check("a line number given as a float still anchors",
      vinegar.finding_line({"line": 43.0}) == 43)
check("an infinite line number anchors nothing rather than raising",
      vinegar.finding_line({"line": float("inf")}) is None)
check("a boolean line number anchors nothing",
      vinegar.finding_line({"line": True}) is None)
check("a missing line number anchors nothing",
      vinegar.finding_line({}) is None
      and vinegar.finding_line({"line": "top"}) is None)
check("a dot-slash path is normalised to the diff's key",
      vinegar.repo_path("./vinegar.py", ROOT) == "vinegar.py"
      and vinegar.repo_path("src//app.py", ROOT) == "src/app.py")
check("a leading .. in a file name is not a path escape",
      vinegar.repo_path("..config.py", ROOT) == "..config.py")
check("a path outside the checkout is still refused",
      vinegar.repo_path("/etc/passwd", ROOT) is None)
check("a symlinked checkout root still resolves its own files",
      vinegar.repo_path(_link + "/vinegar.py", _real) == "vinegar.py"
      and vinegar.repo_path(_real + "/vinegar.py", _link) == "vinegar.py")
check("git diff is asked not to quote non-ascii paths",
      "core.quotepath=false" in last_git_diff[0], last_git_diff[0])
check("git diff is pinned against textconv and external drivers",
      "--no-textconv" in last_git_diff[0]
      and "--no-ext-diff" in last_git_diff[0], last_git_diff[0])
check("git diff is pinned against colour escapes",
      "color.ui=false" in last_git_diff[0]
      and "--no-color" in last_git_diff[0], last_git_diff[0])

inline, general = vinegar.split_findings(FINDINGS, covered, ROOT, L)
check("in-diff findings become inline comments", len(inline) == 2, inline)
check("absolute path is made repo-relative",
      all(c["path"] == "vinegar.py" for c in inline), inline)
check("inline comments anchor on the head side",
      all(c["side"] == "RIGHT" for c in inline), inline)
check("out-of-diff findings go general", len(general) == 4, general)
check("failure scenario reaches the comment body",
      "Failure: boom" in inline[0]["body"], inline[0]["body"])

# --- review_body ---------------------------------------------------------
empty = vinegar.review_body(L, PR, CONFIG, [], [])
check("nothing found still says so", "No findings." in empty, empty)
check("body names the reviewed sha", "a1b2c3d" in empty, empty)

allinline = vinegar.review_body(L, PR, CONFIG, inline, [])
check("all-inline body carries no finding list",
      "2 findings, 2 posted inline." in allinline
      and "could not be anchored" not in allinline, allinline)

mixed = vinegar.review_body(L, PR, CONFIG, inline, general)
check("mixed body lists the out-of-diff findings",
      "6 findings, 2 posted inline." in mixed
      and "- `vinegar.py:900`: outside the diff" in mixed, mixed)
check("a finding with no line is still named",
      "- `vinegar.py`: no line at all" in mixed, mixed)

raw = vinegar.review_body(L, PR, CONFIG, [], [], "the reviewer rambled")
check("unreadable output is quoted verbatim",
      raw.rstrip().endswith("the reviewer rambled")
      and "did not return its findings" in raw, raw)

# --- post_review ---------------------------------------------------------
text = "Summary of the review."

del posted[:]
vinegar.post_review("o/r", PR, ROOT, text, FINDINGS[:4], CONFIG, None)
check("one review request, not one per finding", len(posted) == 1, len(posted))
cmd, payload = posted[0]
check("posts to the reviews endpoint",
      cmd[2] == "repos/o/r/pulls/12/reviews", cmd)
check("submits as COMMENT, never an approval",
      payload["event"] == "COMMENT", payload["event"])
check("pins the review to the sha it reviewed",
      payload["commit_id"] == "a1b2c3d4e5f6", payload)
check("carries the inline comments in one array",
      len(payload["comments"]) == 2, payload.get("comments"))
check("carries a body alongside them", bool(payload["body"]), payload)

del posted[:]
vinegar.post_review("o/r", PR, ROOT, "no json here", None, CONFIG, None)
check("unparseable review still posts something", len(posted) == 1,
      len(posted))
check("unparseable review posts no inline comments",
      "comments" not in posted[0][1], posted[0][1])
check("unparseable review body keeps the reviewer's words",
      "no json here" in posted[0][1]["body"], posted[0][1]["body"])

del posted[:]
vinegar.post_review("o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a clean review is announced, not skipped",
      len(posted) == 1 and "No findings." in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")

del posted[:]
fake_run.rc = 1
vinegar.post_review("o/r", PR, ROOT, text, FINDINGS[:4], CONFIG, None)
check("a rejected review is retried without anchors", len(posted) == 2,
      len(posted))
check("the retry drops the inline comments",
      "comments" not in posted[1][1], posted[1][1])
check("the retry keeps every finding in the body",
      all(str(f["summary"]) in posted[1][1]["body"] for f in FINDINGS[:4]),
      posted[1][1]["body"])
check("the retry does not blame anchoring for GitHub's refusal",
      "GitHub refused the inline comments" in posted[1][1]["body"]
      and "could not be anchored" not in posted[1][1]["body"],
      posted[1][1]["body"])
fake_run.rc = 0

del posted[:]
vinegar.post_review("o/r", PR, ROOT, text, FINDINGS[:4],
                    dict(CONFIG, comment=False), None)
check("a dry run posts nothing at all", not posted, posted)

del posted[:]
huge = "x" * 90000
vinegar.post_review("o/r", PR, ROOT, huge, None, CONFIG, None)
check("an oversize body is cut rather than refused",
      len(posted) == 1 and len(posted[0][1]["body"]) < 65536,
      len(posted[0][1]["body"]) if posted else "nothing posted")

del posted[:]
vinegar.post_timeout(L, PR, "o/r", 1800, None)
check("a killed review still says so on the pull request",
      len(posted) == 1 and "killed after 1800s" in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")
check("a killed review does not read as clean",
      "not as the change being clean" in posted[0][1]["body"],
      posted[0][1]["body"])

fake_run.rc = 1
del posted[:]
vinegar.post_timeout(L, PR, "o/r", 1800, None)
check("the timeout notice is retried when refused",
      len(posted) == 2, len(posted))
fake_run.rc = 0

del posted[:]
fake_run.rc = 1
vinegar.post_review("o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a refused clean review is tried again, not abandoned",
      len(posted) == 2 and "comments" not in posted[1][1], len(posted))
fake_run.rc = 0

del posted[:]
vinegar.post_review("o/r", PR, ROOT, "", None, CONFIG, None)
check("a review that said nothing says so distinctly",
      len(posted) == 1
      and "produced nothing" in posted[0][1]["body"]
      and "own words follow" not in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")

del posted[:]
boom = []


def exploding_post():
    boom.append(1)
    raise RuntimeError("GitHub is unreachable")


vinegar.announce("o/r#12", exploding_post)
check("a posting failure cannot escape and cost a re-review",
      boom == [1] and not posted, (boom, posted))

# The same thing through review(), which is where it actually matters:
# handle_pr does not wrap the call, so anything escaping leaves no state and
# the pull request is re-reviewed at full cost on every poll from then on.
real_env, real_transcript = vinegar.github_env, vinegar.save_transcript
vinegar.save_transcript = lambda repo, pr, text, findings=None: "/dev/null"


def exploding_env(*a, **k):
    raise RuntimeError("GitHub is unreachable")


def claude_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        claude_run.saw, claude_run.env = cmd, env
        return subprocess.CompletedProcess(cmd, 0, claude_run.stream, "")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


def result_event(**over):
    return dict({"type": "result", "subtype": "success", "is_error": False,
                 "result": text, "total_cost_usd": 1.0}, **over)


claude_run.saw, claude_run.env = [], {}
claude_run.stream = stream(call(FINDINGS[:4]), result_event())


vinegar.run, vinegar.github_env = claude_run, exploding_env
del posted[:]
check("a review whose posting dies is still recorded as done",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE)

vinegar.github_env = lambda *a, **k: None
del posted[:]
check("a review that posts cleanly is done and posted once",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))

del posted[:]
check("a dry run reviews without minting or posting",
      vinegar.review(ROOT, "o/r", PR, dict(CONFIG, comment=False), None,
                     {}) == vinegar.DONE and not posted, posted)

# An error that still produced a review must not be thrown away and charged
# for again; one that produced nothing is what retrying is for.
vinegar.github_env = lambda *a, **k: None
claude_run.stream = stream(call(FINDINGS[:4]),
                           result_event(is_error=True,
                                        subtype="error_max_turns"))
del posted[:]
check("an error holding a review is posted and recorded done",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))

claude_run.stream = stream(result_event(is_error=True, result=None,
                                        subtype="error_during_execution"))
del posted[:]
check("an error holding nothing is retried, not posted",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.FAILED
      and not posted, (posted,))

claude_run.stream = stream(result_event(result=None))
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a null result is announced as nothing produced",
      len(posted) == 1 and "produced nothing" in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")
check("a null result never posts the word None",
      posted and "None" not in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")

claude_run.stream = stream(call(FINDINGS[:4]), result_event())
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("the review asks for the stream the tool call arrives in",
      "stream-json" in claude_run.saw and "--verbose" in claude_run.saw,
      claude_run.saw)
check("the reporting tool is not withheld any more",
      "--disallowedTools" not in claude_run.saw, claude_run.saw)
check("the environment asks for the tool contract",
      (claude_run.env or {}).get("CLAUDE_CODE_REPORT_FINDINGS") == "1",
      sorted(claude_run.env or {})[:5])
# Killed mid-summary, after the findings were already reported.
def timing_out(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        raise subprocess.TimeoutExpired(
            cmd, timeout or 0,
            output=stream(call(FINDINGS[:4]), ).encode())
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out
del posted[:]
check("a kill after the findings were reported still posts them",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1
      and "comments" in posted[0][1], (len(posted),))
check("a salvaged review is not announced as having produced nothing",
      posted and "killed after" not in posted[0][1]["body"],
      posted[0][1]["body"][:120] if posted else "nothing posted")


def timing_out_early(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        raise subprocess.TimeoutExpired(cmd, timeout or 0, output=b"")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out_early
del posted[:]
check("a kill with nothing reported still says so on the pull request",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1 and "killed after" in posted[0][1]["body"],
      posted[0][1]["body"][:120] if posted else "nothing posted")

vinegar.run = claude_run
claude_run.stream = stream(call(FINDINGS[:4]),
                           result_event(is_error=True,
                                        subtype="error_during_execution"))
del posted[:]
check("an error after the findings arrived posts them, not a retry",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))

claude_run.stream = stream(call(FINDINGS[:4]), result_event())
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("the brief reaches the reviewer",
      "--append-system-prompt" in claude_run.saw
      and any("release-2...HEAD" in a for a in claude_run.saw),
      claude_run.saw)

vinegar.run, vinegar.github_env = fake_run, real_env
vinegar.save_transcript = real_transcript

# A posting request that never answers must end, or the poll loop never does.
def hanging_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"]:
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = hanging_run
check("a stalled post is ambiguous, not a refusal",
      vinegar.submit_review(L, "o/r", PR, {"body": "x"}, None) is None)
vinegar.run = fake_run
check("the posting request carries a timeout",
      vinegar.POST_TIMEOUT and vinegar.POST_TIMEOUT <= 300,
      vinegar.POST_TIMEOUT)

# --- reviewer_brief ------------------------------------------------------
brief = vinegar.reviewer_brief(PR)
check("brief names the pull request's real base",
      "git diff release-2...HEAD" in brief, brief)
check("brief tells the reviewer not to fall back to main",
      "do not assume `main`" in brief, brief)
check("brief names the reporting tool, not a competing format",
      "ReportFindings" in brief and "```json" not in brief, brief)
check("brief gives a fallback for a base ref that does not resolve",
      "gh pr diff 12" in brief, brief)
check("brief does not promise the base ref is definitely there",
      "already fetched" not in brief, brief)

print()
print("FAILED: %s" % ", ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
