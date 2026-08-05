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
import inspect
import json
import os
import atexit
import shutil
import subprocess
import sys
import tempfile

here_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, here_dir)
# Before the import: the module reads this at import time to place its state,
# and a test run must not go looking at the real one.
_home = tempfile.mkdtemp(prefix="vinegar-test-home-")
atexit.register(shutil.rmtree, _home, True)
os.environ["VINEGAR_HOME"] = os.path.join(_home, ".vinegar")
import vinegar

GENUINE_SAVE_TRANSCRIPT = vinegar.save_transcript

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
diff --git a/my file.py b/my file.py
--- a/my file.py	
+++ b/my file.py	
@@ -1,0 +2,1 @@
+spaced
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
looked = []
last_git_diff = [[], None]


def fake_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "git" and "diff" in cmd:
        last_git_diff[0], last_git_diff[1] = cmd, timeout
        return subprocess.CompletedProcess(
            cmd, fake_run.diff_rc, "" if fake_run.diff_rc else DIFF, "boom")
    if cmd[:2] == ["gh", "api"] and "-X" in cmd:
        # The read that asks whether a review already landed.
        looked.append(cmd)
        return subprocess.CompletedProcess(
            cmd, fake_run.look_rc, fake_run.look_out, "")
    if cmd[:2] == ["gh", "api"]:
        posted.append((cmd, json.loads(stdin_text)))
        return subprocess.CompletedProcess(cmd, fake_run.rc, "",
                                           fake_run.post_err)
    raise AssertionError("unexpected command %r" % cmd)


fake_run.rc = 0
fake_run.diff_rc = 0
fake_run.look_rc = 0
fake_run.look_out = ""
fake_run.post_err = "HTTP 422"
GENUINE_RUN = vinegar.run
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

result, found, _said = vinegar.read_stream(stream(call(REAL), DONE_EVENT))
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
check("a tool call whose input is not an object does not crash the review",
      vinegar.read_stream(stream(
          {"type": "assistant", "message": {"content": [
              {"type": "tool_use", "name": "ReportFindings",
               "input": [{"file": "a.py"}]}]}},
          DONE_EVENT))[1] is None)
check("an empty last block does not erase what the reviewer said",
      vinegar.read_stream(stream(
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "The risk is here."}]}},
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "  \n "}]}},
          DONE_EVENT))[2] == "The risk is here.")
check("only the reviewer's last words are kept, not the whole run",
      vinegar.read_stream(stream(
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "Let me start by gathering the diff."}]}},
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "Now the enclosing function."}]}},
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "Closing summary."}]}},
          DONE_EVENT))[2] == "Closing summary.")
_long = vinegar.read_stream(stream(
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "x" * 90000}]}},
    DONE_EVENT))[2]
check("the reviewer's words are capped before they reach a comment",
      len(_long) < 90000 and _long.startswith("x"), len(_long))
check("a cut is admitted rather than passed off as the whole message",
      "cut after" in _long, _long[-60:])
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
check("a subagent's result event is not the review's ending",
      vinegar.read_stream(stream(
          call(REAL),
          dict(DONE_EVENT, parent_tool_use_id="toolu_01")))[0] is None)
check("injected instructions are not mistaken for the reviewer's words",
      "ReportFindings instructions" not in vinegar.read_stream(stream(
          {"type": "user", "message": {"content": [
              {"type": "text",
               "text": "ReportFindings instructions: report through it."}]}},
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "My own words."}]}},
          DONE_EVENT))[2])
check("the reviewer's own words come back for the transcript",
      "Reviewed it." in vinegar.read_stream(stream(
          {"type": "assistant", "message": {"content": [
              {"type": "text", "text": "Reviewed it."}]}},
          DONE_EVENT))[2])

# --- diff_lines ----------------------------------------------------------
covered = vinegar.diff_lines(ROOT, "release-2", None, "o/r#12")
check("added lines are covered", covered.get("vinegar.py") == {11, 12, 13, 43},
      covered.get("vinegar.py"))
check("deleted file contributes nothing", "gone.py" not in covered, covered)
check("deletion-only hunk contributes nothing", "README.md" not in covered,
      covered)
check("a path with a space is keyed without git's trailing tab",
      covered.get("my file.py") == {2}, list(covered))
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
check("the diff is bounded so it cannot wedge the poll loop",
      last_git_diff[1] is not None and last_git_diff[1] <= 600,
      last_git_diff[1])


def git_hangs(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "git":
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = git_hangs
try:
    _hung = vinegar.diff_lines(ROOT, "release-2", None, L) == {}
except Exception as err:
    _hung = "raised %r" % err
check("a git diff that times out anchors nothing rather than crashing",
      _hung is True, _hung)
vinegar.run = fake_run

listing = [None]


def gh_list(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:3] == ["gh", "pr", "list"]:
        listing[0] = timeout
        return subprocess.CompletedProcess(cmd, 0, "[]", "")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = gh_list
check("the listing returns and is bounded",
      vinegar.open_prs("o/r", None) == [] and listing[0] is not None,
      listing[0])


def gh_list_hangs(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:3] == ["gh", "pr", "list"]:
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = gh_list_hangs
try:
    _listed = vinegar.open_prs("o/r", None) == []
except Exception as err:
    _listed = "raised %r" % err
check("a listing that hangs is skipped rather than wedging the poll",
      _listed is True, _listed)
vinegar.run = fake_run

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
check("the base is named unambiguously, so a tag cannot win",
      any(a.startswith("refs/heads/release-2...") for a in last_git_diff[0]),
      last_git_diff[0])

inline, general = vinegar.split_findings(FINDINGS, covered, ROOT, L)
check("in-diff findings become inline comments", len(inline) == 2, inline)
check("absolute path is made repo-relative",
      all(c["path"] == "vinegar.py" for c in inline), inline)
check("inline comments anchor on the head side",
      all(c["side"] == "RIGHT" for c in inline), inline)
check("out-of-diff findings go general", len(general) == 4, general)
check("the category reaches the comment body",
      "(correctness)" in vinegar.describe(
          {"summary": "s", "category": "correctness"}),
      vinegar.describe({"summary": "s", "category": "correctness"}))
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
check("a blank file name reads the same everywhere it is rendered",
      vinegar.finding_bullet({"file": "   ", "summary": "s"})
      == "- `(no file)`: s")
check("a finding with no line is still named",
      "- `vinegar.py`: no line at all" in mixed, mixed)

raw = vinegar.review_body(L, PR, CONFIG, [], [], "the reviewer rambled")
check("unreadable output is quoted verbatim",
      raw.rstrip().endswith("the reviewer rambled")
      and "did not return its findings" in raw, raw)

# --- post_review ---------------------------------------------------------
text = "Summary of the review."

del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, text, FINDINGS[:4], CONFIG, None)
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
vinegar.post_review(L, "o/r", PR, ROOT, "no json here", None, CONFIG, None)
check("unparseable review still posts something", len(posted) == 1,
      len(posted))
check("unparseable review posts no inline comments",
      "comments" not in posted[0][1], posted[0][1])
check("unparseable review body keeps the reviewer's words",
      "no json here" in posted[0][1]["body"], posted[0][1]["body"])

del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a clean review is announced, not skipped",
      len(posted) == 1 and "No findings." in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")

del posted[:]
fake_run.rc = 1
vinegar.post_review(L, "o/r", PR, ROOT, text, FINDINGS[:4], CONFIG, None)
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

try:
    _nul = vinegar.repo_path("/checkouts/o__r/a\x00b.py", ROOT)
except ValueError:
    _nul = "raised"
check("a NUL byte in a finding's path is refused, not raised",
      _nul is None, repr(_nul))

check("a verdict rides with the category when one arrives",
      "(correctness, CONFIRMED)" in vinegar.describe(
          {"summary": "s", "category": "correctness",
           "verdict": "CONFIRMED"}))
check("no category and no verdict adds no empty parenthesis",
      "(" not in vinegar.describe({"summary": "s"}))

_big = [{"file": "a.py", "summary": "s%d" % i,
         "failure_scenario": "x" * 9000, "category": "correctness"}
        for i in range(10)]
_body = vinegar.review_body(L, PR, CONFIG, [], _big)
check("overflow drops whole findings and says how many",
      len(_body) <= vinegar.MAX_BODY and "did not fit" in _body
      and "cut to fit GitHub's comment limit" not in _body, len(_body))

# Every bullet dropped: the heading would introduce nothing at all.
_huge = [{"file": "a.py", "summary": "s", "failure_scenario": "x" * 70000,
          "category": "correctness"}]
_body1 = vinegar.review_body(L, PR, CONFIG, [], _huge)
check("a heading is not left introducing nothing",
      "could not be anchored" not in _body1 and "did not fit" in _body1
      and len(_body1) <= vinegar.MAX_BODY, _body1[:200])

# The note itself takes room. With bullets small enough that dropping one
# leaves less slack than the note needs, forgetting to count it puts the
# body back over the limit and hands it to clamp, which shears a bullet in
# half. Swept across the boundary rather than guessed at.
_sweep = []
for _pad in range(0, 20):
    # 254 bullets of ~279 characters overshoot MAX_BODY by about 11000, so
    # the loop runs; the padding walks the last kept bullet across the
    # boundary, and six of these twenty leave less slack than the note.
    _many = [{"file": "a.py", "summary": "s", "category": "correctness",
              "failure_scenario": "y" * 240} for _ in range(254)]
    _many[0]["failure_scenario"] = "y" * (240 + _pad * 40)
    _b = vinegar.review_body(L, PR, CONFIG, [], _many)
    _sweep.append(len(_b) <= vinegar.MAX_BODY
                  and "cut to fit GitHub's comment limit" not in _b)
check("the overflow note is counted in what has to fit",
      all(_sweep), _sweep)

del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, text, FINDINGS[:4],
                    dict(CONFIG, comment=False), None)
check("a dry run posts nothing at all", not posted, posted)

del posted[:]
huge = "x" * 90000
vinegar.post_review(L, "o/r", PR, ROOT, huge, None, CONFIG, None)
check("an oversize body is cut rather than refused",
      len(posted) == 1 and len(posted[0][1]["body"]) < 65536,
      len(posted[0][1]["body"]) if posted else "nothing posted")


del posted[:]
fake_run.rc = 1
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a refused clean review is tried again, not abandoned",
      len(posted) == 2 and "comments" not in posted[1][1], len(posted))
fake_run.rc = 0

del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, "", None, CONFIG, None)
check("a review that said nothing says so distinctly",
      len(posted) == 1
      and "produced nothing" in posted[0][1]["body"]
      and "own words follow" not in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")

del posted[:]
boom = []


def exploding_callback():
    boom.append(1)
    raise RuntimeError("GitHub is unreachable")


vinegar.announce("o/r#12", exploding_callback)
check("a posting failure cannot escape and cost a re-review",
      boom == [1] and not posted, (boom, posted))

# The same thing through review(), which is where it actually matters:
# handle_pr does not wrap the call, so anything escaping leaves no state and
# the pull request is re-reviewed at full cost on every poll from then on.
real_env, real_transcript = vinegar.github_env, vinegar.save_transcript
# Signature-checked against the real function. A stub that quietly rejects
# the call it stands in for sends every test through finish()'s
# transcript-failed branch while still passing, which is what happened when
# save_transcript gained its `note` argument.
_tx_calls = []


def stub_transcript(repo, pr, text, findings=None, note=None):
    _tx_calls.append((repo, pr, text, findings, note))
    return "/dev/null"


assert (inspect.signature(stub_transcript).parameters.keys()
        == inspect.signature(vinegar.save_transcript).parameters.keys()), \
    "the save_transcript stub no longer matches the real signature"
vinegar.save_transcript = stub_transcript


def exploding_env(*a, **k):
    raise RuntimeError("GitHub is unreachable")


def exploding_post_review(*a, **k):
    raise RuntimeError("the endpoint is unreachable")


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
check("a mint failure falls back rather than losing the review",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))

# Posting itself raising, which is what announce() exists for: an exception
# escaping review() leaves handle_pr with no state and re-reviews the pull
# request at full cost on every poll.
_real_post = vinegar.post_review
vinegar.post_review = exploding_post_review
vinegar.github_env = lambda *a, **k: None
del posted[:]
check("a review whose posting raises is still recorded as done",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and not posted, (posted,))
vinegar.post_review = _real_post

_real_save = vinegar.save_transcript


def exploding_save(*a, **k):
    raise PermissionError("~/.vinegar/reviews is not writable")


vinegar.save_transcript = exploding_save
del posted[:]
check("a transcript that cannot be written still posts the review",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))
vinegar.save_transcript = _real_save

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
check("the reporting tool is allowed, which is what makes it reachable",
      vinegar.REPORT_TOOL in json.load(
          open(os.path.join(here_dir, "review-settings.json"))
      )["permissions"]["allow"])
# check_paths() is the startup guard for that agreement, so first prove it
# passes as configured, or the refusal below would prove nothing.
try:
    vinegar.check_paths()
    _agreed = "started"
except SystemExit as err:
    _agreed = "refused: %s" % err
check("the paths and settings under test start cleanly",
      _agreed == "started", _agreed)
_rt = vinegar.REPORT_TOOL
vinegar.REPORT_TOOL = "SomethingElse"
try:
    vinegar.check_paths()
    _agreed = "started"
except SystemExit:
    _agreed = "refused"
vinegar.REPORT_TOOL = _rt
check("a reporting tool the settings do not allow refuses to start",
      _agreed == "refused", _agreed)
check("the environment asks for the tool contract",
      (claude_run.env or {}).get("CLAUDE_CODE_REPORT_FINDINGS") == "1",
      sorted(claude_run.env or {})[:5])
# A stream that stops before its result event, with findings already in it.
claude_run.stream = stream(call(FINDINGS[:4]))
del posted[:]
check("findings survive a stream that never reached its result event",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1
      and "not a finished round" in posted[0][1]["body"], (len(posted),))

claude_run.stream = ""
del posted[:]
check("a truncated stream with nothing reported is retried",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.FAILED
      and not posted, (posted,))

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
check("a salvaged review says it was not a finished round",
      posted and "not a finished round" in posted[0][1]["body"],
      posted[0][1]["body"][:160] if posted else "nothing posted")
check("a salvaged review is not announced as having produced nothing",
      posted and "It reported nothing" not in posted[0][1]["body"],
      posted[0][1]["body"][:160] if posted else "nothing posted")


# Killed after reporting a genuinely clean review.
def timing_out_clean(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        raise subprocess.TimeoutExpired(
            cmd, timeout or 0, output=stream(call([])).encode())
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out_clean
del posted[:]
check("a kill after a clean report is not called 'returned nothing'",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1
      and "returned nothing" not in posted[0][1]["body"]
      and "reported nothing before it stopped" in posted[0][1]["body"],
      posted[0][1]["body"][:200] if posted else "nothing posted")


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
check("a killed review does not read as clean",
      posted and "not as the change being clean" in posted[0][1]["body"]
      and "No findings" not in posted[0][1]["body"],
      posted[0][1]["body"][:200] if posted else "nothing posted")
check("under a note the empty ending adds no sentence of its own",
      posted and "said nothing before it stopped"
      not in posted[0][1]["body"]
      and "The review finished" not in posted[0][1]["body"],
      posted[0][1]["body"][:200] if posted else "nothing posted")

fake_run.rc = 1
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("the killed notice is retried when refused",
      len(posted) == 2, len(posted))
fake_run.rc = 0


# Killed after narrating but before reporting: the words are in the buffer
# and the pull request gets them, not a claim that nothing came back.
def timing_out_prose(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        raise subprocess.TimeoutExpired(cmd, timeout or 0, output=stream(
            {"type": "assistant", "message": {"content": [
                {"type": "text",
                 "text": "Halfway through: the diff looks risky."}]}}
        ).encode())
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out_prose
del posted[:]
check("a kill after prose alone still posts the reviewer's words",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1
      and "the diff looks risky" in posted[0][1]["body"]
      and "killed after" in posted[0][1]["body"],
      posted[0][1]["body"][:200] if posted else "nothing posted")

vinegar.run = claude_run
claude_run.stream = stream(call(FINDINGS[:4]),
                           result_event(is_error=True,
                                        subtype="error_during_execution"))
del posted[:]
check("an error after the findings arrived posts them, not a retry",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.DONE
      and len(posted) == 1, (len(posted),))
check("a failed run says so rather than reading as finished",
      posted and "failed before it finished" in posted[0][1]["body"],
      posted[0][1]["body"][:160] if posted else "nothing posted")

claude_run.stream = stream(call([]),
                           result_event(is_error=True,
                                        subtype="error_during_execution"))
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a failed run reporting nothing is never called clean",
      posted and "No findings." not in posted[0][1]["body"]
      and "reported nothing before it stopped" in posted[0][1]["body"],
      posted[0][1]["body"][:200] if posted else "nothing posted")

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
      vinegar.submit_review(L, "o/r", PR, {"body": "x"}, None)
      == vinegar.UNSURE)
vinegar.run = fake_run
fake_run.rc = 1
for _err, _want, _name in (
        ("HTTP 422 Unprocessable Entity", vinegar.REFUSED, "a 422"),
        ("HTTP 502 Bad Gateway", vinegar.UNSURE, "a 502"),
        ("boom, no status at all", vinegar.UNSURE, "an error with no status"),
        ("gh: API rate limit exceeded (HTTP 403)", vinegar.THROTTLED,
         "a rate-limited 403"),
        ("HTTP 429 Too Many Requests", vinegar.THROTTLED, "a 429"),
        ("HTTP 403 Resource not accessible by integration", vinegar.REFUSED,
         "a 403 that is a permission error")):
    fake_run.post_err = _err
    check("%s settles as %s" % (_name, _want),
          vinegar.submit_review(L, "o/r", PR, {"body": "x"}, None) == _want,
          _err)
fake_run.rc = 0
fake_run.post_err = "HTTP 422"
vinegar.run = fake_run
check("the posting request carries a timeout",
      vinegar.POST_TIMEOUT and vinegar.POST_TIMEOUT <= 300,
      vinegar.POST_TIMEOUT)

# --- reviewer_brief ------------------------------------------------------
brief = vinegar.reviewer_brief(PR)
check("brief names the pull request's real base",
      "git diff refs/heads/release-2...HEAD" in brief, brief)
check("brief tells the reviewer not to fall back to main",
      "do not assume `main`" in brief, brief)
check("brief names the reporting tool, not a competing format",
      "ReportFindings" in brief and "```json" not in brief, brief)
check("brief gives a fallback for a base ref that does not resolve",
      "gh pr diff 12" in brief, brief)
check("brief does not promise the base ref is definitely there",
      "already fetched" not in brief, brief)

# --- handle_pr: the guard that bounds every crash --------------------------
# An exception anywhere in review() used to leave no state at all, so the
# pull request was checked out and reviewed again at full cost every poll,
# for ever, with MAX_ATTEMPTS never reaching it.
_hp_state = {}
_hp_saved = []
_real_review, _real_save_state = vinegar.review, vinegar.save_state
_real_checkout, _real_github_env = vinegar.checkout, vinegar.github_env
vinegar.save_state = lambda st: _hp_saved.append(dict(st))
PR_LIVE = dict(PR, isDraft=False, isCrossRepository=False,
               author={"login": "kevin"}, additions=1, deletions=0)


def blowing_up_review(*a, **k):
    raise AttributeError("'list' object has no attribute 'get'")


vinegar.review = blowing_up_review
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _hp_state, {})
check("a review that raises is still recorded, so it cannot loop for ever",
      _hp_state.get(L, {}).get("outcome") == vinegar.FAILED, _hp_state)
check("a review that raises counts as an attempt against MAX_ATTEMPTS",
      _hp_state.get(L, {}).get("attempts") == 1, _hp_state)

# And the marker written before the review runs, which is what survives a
# process that is killed outright rather than raising.
_hp_state.clear()
del _hp_saved[:]
vinegar.review = lambda *a, **k: vinegar.DONE
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _hp_state, {})
check("an attempt is on disk before the review starts",
      len(_hp_saved) == 2 and _hp_saved[0][L]["outcome"] == vinegar.FAILED,
      [d.get(L) for d in _hp_saved])
check("the real outcome replaces the marker when the review finishes",
      _hp_state[L]["outcome"] == vinegar.DONE, _hp_state)

# A skip must not hand back the retry budget the attempts already burned.
_sk_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": 2}}
vinegar.review = lambda *a, **k: vinegar.FAILED
vinegar.handle_pr("o/r", dict(PR_LIVE, isDraft=True), CONFIG, _sk_state, {})
check("a skip keeps the attempts burned at this head",
      _sk_state[L]["outcome"] == "skipped"
      and _sk_state[L].get("attempts") == 2, _sk_state)
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _sk_state, {})
check("a skip that lifts resumes the budget rather than restarting it",
      _sk_state[L].get("attempts") == 3, _sk_state)
vinegar.handle_pr("o/r", dict(PR_LIVE, isDraft=True), CONFIG, _sk_state, {})
_sk_ran = []
vinegar.review = lambda *a, **k: _sk_ran.append(1) or vinegar.DONE
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _sk_state, {})
check("a lifted skip cannot re-run a review whose budget is spent",
      not _sk_ran, (_sk_state, _sk_ran))

# The pre-review marker can be the last thing an attempt writes: a kill
# mid-review leaves a spent budget that nothing announced. The next poll's
# discovery must say so on the pull request, once.
_ga_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_state, {})
check("a give-up interrupted by a crash is announced on restart",
      len(posted) == 1 and "gave up on" in posted[0][1]["body"],
      (len(posted), _ga_state))
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_state, {})
check("the crash-discovered give-up is announced once, not every poll",
      len(posted) == 1 and _ga_state[L].get("announced") is True,
      (len(posted), _ga_state))

# A give-up whose announcement never landed must not be marked as said,
# or the pull request stays silent for ever on the strength of one bad
# minute at GitHub.
_gf_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
_real_post_for_gu = vinegar.post_review
vinegar.post_review = lambda *a, **k: False
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gf_state, {})
check("a give-up that did not reach GitHub is not marked as said",
      _gf_state[L].get("announced") is not True, _gf_state)
vinegar.post_review = lambda *a, **k: (_ for _ in ()).throw(
    RuntimeError("GitHub is unreachable"))
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gf_state, {})
check("a give-up whose posting raised is not marked as said",
      _gf_state[L].get("announced") is not True, _gf_state)
vinegar.post_review = _real_post_for_gu
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gf_state, {})
check("the give-up is retried on a later poll until it lands",
      _gf_state[L].get("announced") is True, _gf_state)

# Retried, but not for ever: an App without write access, a locked pull
# request or an archived repository refuses every time, and unbounded that
# is two API calls and a log line a minute per stuck pull request.
_gb_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
vinegar.post_review = lambda *a, **k: False
_gb_posts = 0
for _ in range(8):
    _before = _gb_state[L].get("announce_tries", 0)
    vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gb_state, {})
    if _gb_state[L].get("announce_tries", 0) != _before:
        _gb_posts += 1
check("a give-up that can never post stops trying",
      _gb_posts == vinegar.MAX_ATTEMPTS
      and _gb_state[L].get("announced") is True, (_gb_posts, _gb_state))
vinegar.post_review = _real_post_for_gu

# github_env answers None when no App is configured, and ambient `gh` is
# then the credential the operator chose. Reading that as "no token" once
# disabled the announcement for every non-App deployment.
_gn_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
vinegar.github_env = lambda *a, **k: None
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, dict(CONFIG, github_app=None), _gn_state,
                  {})
check("a deployment with no App still announces its give-up",
      len(posted) == 1, (len(posted), _gn_state))

# And a mint that raises is counted, or a repository whose token never
# mints retries every minute for ever.
_gm_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}


def _no_token(*a, **k):
    raise RuntimeError("the App is not installed on o/r")


vinegar.github_env = _no_token
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gm_state, {})
check("a token that cannot be minted counts as an attempt",
      not posted and _gm_state[L].get("announce_tries") == 1,
      (len(posted), _gm_state))
vinegar.github_env = lambda *a, **k: None

check("a dry run's give-up counts as said, not as a silence",
      vinegar.post_review(L, "o/r", PR, ROOT, "", None,
                          dict(CONFIG, comment=False), None) is True)

vinegar.review, vinegar.save_state = _real_review, _real_save_state
vinegar.checkout, vinegar.github_env = _real_checkout, _real_github_env

# state.json is a file the give-up log tells the operator to hand-edit,
# and under launchd KeepAlive an exit here is a 30-second crash loop that
# stops every repository. A bad file is quarantined instead.
os.makedirs(os.path.dirname(vinegar.STATE_PATH), exist_ok=True)
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": {,}')
try:
    _st = vinegar.load_state()
except SystemExit:
    _st = "exited"
check("an unreadable state file is quarantined, not fatal", _st == {}, _st)


def _quarantined():
    where = os.path.dirname(vinegar.STATE_PATH)
    return sorted(f for f in os.listdir(where) if f.endswith(".unreadable"))


_kept = _quarantined()
check("the quarantined state is kept for the operator",
      len(_kept) == 1 and "{,}" in open(
          os.path.join(os.path.dirname(vinegar.STATE_PATH), _kept[0])).read(),
      _kept)

# A second corruption must not overwrite the copy the first one was kept
# for: repairing from it is usually how the second one gets made.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": [[[')
vinegar.load_state()
check("a second quarantine does not overwrite the first",
      len(_quarantined()) == 2, _quarantined())

# Valid JSON that is not an object parses, then raises on the first .get
# in handle_pr, once per pull request per poll, for ever.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('["o/r#12"]')
check("state that is not an object is refused like unreadable state",
      vinegar.load_state() == {} and len(_quarantined()) == 3,
      _quarantined())

# A root-owned or otherwise unreadable file is the same outage, and the
# narrow catch did not cover it.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": {}}')
os.chmod(vinegar.STATE_PATH, 0)
try:
    _st = vinegar.load_state()
except Exception as err:
    _st = "raised %s" % type(err).__name__
check("a state file that cannot be opened is quarantined too",
      _st == {} and len(_quarantined()) == 4, (_st, _quarantined()))
for _f in _quarantined():
    os.remove(os.path.join(os.path.dirname(vinegar.STATE_PATH), _f))

# --- a resend must not duplicate a review that already landed --------------
# A 5xx is ambiguous the way a timeout is; a 4xx created nothing and is
# retried without the read. The stub answers 502 for the ambiguous half.
claude_run.stream = stream(call([]), result_event())
vinegar.run, vinegar.github_env = claude_run, lambda *a, **k: None
fake_run.rc = 1
fake_run.post_err = "HTTP 502"
fake_run.look_out = vinegar.BODY_MARK + " reviewed `a1b2c3d` ...\n"
del posted[:]
del looked[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("an ambiguous refusal asks before resending",
      len(looked) == 1, (len(looked), len(posted)))
check("a review that already landed is not posted twice",
      len(posted) == 1, len(posted))
fake_run.look_out = "A human reviewed this at the same commit.\n"
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("someone else's review at the same commit does not suppress ours",
      len(posted) == 2, len(posted))

check("the landed-review question asks every page",
      "--paginate" in looked[0], looked[0])
check("the landed-review question survives a review with no body",
      any('// ""' in a for a in looked[0]), looked[0])
check("the landed-review question reads full pages",
      any("per_page=100" in a for a in looked[0]), looked[0])
check("the landed-review jq carries the escape, not a raw newline",
      all("\n" not in a for a in looked[0]), looked[0])

# What `gh api --jq` prints when it works: one raw first line per review,
# oldest first, a blank for a review with no body. The suite stubs run, so
# the jq half runs only in production; the Python half is pinned here.
fake_run.look_out = ("Looks good to me overall.\n\n%s reviewed `a1b2c3d` at "
                     "high effort\n" % vinegar.BODY_MARK)
check("vinegar's line is found among humans' and blanks",
      vinegar.already_posted(L, "o/r", PR, None) is True)
fake_run.look_out = "LGTM\n\nQuoting: %s reviewed `a1b2c3d` ...\n" % (
    vinegar.BODY_MARK)
check("vinegar's marker quoted mid-line is not vinegar's review",
      vinegar.already_posted(L, "o/r", PR, None) is False)
# splitlines() also breaks on \v, \f, \x1c-\x1e and \x85, so a body
# carrying one before a quoted marker would forge a line that starts with
# it. That answer suppresses the resend, and a suppressed resend is silence.
# A give-up already posted at this commit must not make a later, finished
# review look like a duplicate of itself: the marker has to name which
# kind of comment is up, not merely that Vinegar wrote one.
fake_run.look_out = "%s gave up on `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
check("a give-up already up does not suppress a real review",
      vinegar.already_posted(L, "o/r", PR, None) is False)
check("the give-up still recognises its own note",
      vinegar.already_posted(L, "o/r", PR, None, "gave up on") is True)
fake_run.look_out = "%s reviewed `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
check("a review already up is still recognised as one",
      vinegar.already_posted(L, "o/r", PR, None) is True)
check("a review already up is not mistaken for a give-up",
      vinegar.already_posted(L, "o/r", PR, None, "gave up on") is False)
fake_run.look_out = "%s reviewed `9999999` at high effort\n" % (
    vinegar.BODY_MARK)
check("a review of another commit is not this commit's review",
      vinegar.already_posted(L, "o/r", PR, None) is False)

for _sep in ("\v", "\f", "\x1c", "\x85", " "):
    fake_run.look_out = "A human said:%s%s reviewed `a1b2c3d` ...\n" % (
        _sep, vinegar.BODY_MARK)
    check("a %r inside someone's review cannot forge vinegar's line"
          % _sep,
          vinegar.already_posted(L, "o/r", PR, None) is False,
          fake_run.look_out)
fake_run.look_out = ""

# The anchored retry is the common path and was the unguarded one.
claude_run.stream = stream(call(FINDINGS[:1]), result_event())
fake_run.look_out = vinegar.BODY_MARK + " reviewed `a1b2c3d` ...\n"
del posted[:]
del looked[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a landed review is not resent by the anchored retry either",
      len(posted) == 1 and len(looked) == 1, (len(posted), len(looked)))

claude_run.stream = stream(call([]), result_event())
fake_run.look_out = ""
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a review that did not land is resent",
      len(posted) == 2, len(posted))

fake_run.post_err = "HTTP 422"
del posted[:]
del looked[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a definite refusal retries without the landed-review read",
      len(posted) == 2 and len(looked) == 0, (len(posted), len(looked)))

# An ambiguous first post whose resend is then judged must still reach the
# anchor-stripping retry: that retry is the only thing that saves the
# findings when one anchor is bad, and a 5xx before it used to lose them.
_seq = []


def flaky_post(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        _seq.append(json.loads(stdin_text))
        fake_run.post_err = "HTTP 502" if len(_seq) == 1 else "HTTP 422"
        fake_run.rc = 1
    return fake_run(cmd, cwd, timeout, env, stdin_text)


fake_run.look_out = ""
claude_run.stream = stream(call(FINDINGS[:1]), result_event())


def claude_then_flaky(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "claude":
        return claude_run(cmd, cwd, timeout, env, stdin_text)
    return flaky_post(cmd, cwd, timeout, env, stdin_text)


vinegar.run = claude_then_flaky
del posted[:]
del _seq[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a resend refused over an anchor still reaches the anchorless retry",
      len(_seq) == 3 and "comments" in _seq[0] and "comments" in _seq[1]
      and "comments" not in _seq[2], [sorted(p) for p in _seq])
vinegar.run = claude_run
fake_run.rc = 0
fake_run.post_err = "HTTP 422"

# A rate limit is refused as well, but retrying in the same millisecond
# cannot help, so the pointless second request is not made.
_tries = []


def throttled_post(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        _tries.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 1, "", "gh: API rate limit exceeded (HTTP 403)")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = throttled_post
_logged_rl = []
vinegar.log = lambda m: _logged_rl.append(m)
_rl = vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
vinegar.log = lambda message: None
check("a rate-limited post is not retried in the same millisecond",
      len(_tries) == 1 and _rl is False, (len(_tries), _rl))
check("a rate-limited post says where the review actually is",
      any("only in the transcript" in m for m in _logged_rl), _logged_rl)
# Back to the reviewer stub the checks below this one expect.
vinegar.run = claude_run

# A result field that is empty must not discard what the reviewer said.
fake_run.rc = 0
claude_run.stream = stream(
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "I could not finish, but the risk is here."}]}},
    result_event(result=None))
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("an empty result field falls back to the reviewer's own words",
      posted and "the risk is here" in posted[0][1]["body"]
      and "produced nothing" not in posted[0][1]["body"],
      posted[0][1]["body"][:180] if posted else "nothing posted")
fake_run.rc = 0
vinegar.run = fake_run

# A post that times out may never have landed. Left unchecked that was
# recorded DONE, and with review_on_push false the pull request kept no
# review for ever: the one silence the README does not allow.
post_tries = []


def timing_out_post(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        post_tries.append(cmd)
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out_post
fake_run.look_out = ""
del looked[:]
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
# Two reads, not one: the second asks whether the *resend* landed. Without
# it an ambiguous resend is recorded as "not posted", and the give-up path
# says the whole thing again on the next poll.
check("a timed-out post that did not land is sent again",
      len(post_tries) == 2 and len(looked) == 2,
      (len(post_tries), len(looked)))
del post_tries[:]
fake_run.look_out = vinegar.BODY_MARK + " reviewed `a1b2c3d` ...\n"
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a timed-out post that landed is not sent twice",
      len(post_tries) == 1, len(post_tries))
fake_run.look_out = ""
vinegar.run = fake_run

# The duplicate check answering "no" because it could not tell must say so:
# that answer is what resends, and the resend is what duplicates.
_ap_logged = []
vinegar.log = lambda m: _ap_logged.append(m)
fake_run.look_rc = 1
check("a landed-review read that fails says so in the log",
      vinegar.already_posted(L, "o/r", PR, None) is False
      and any("landed-review read failed" in m for m in _ap_logged),
      _ap_logged)
fake_run.look_rc = 0


def timing_out_look(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" in cmd:
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = timing_out_look
del _ap_logged[:]
check("a landed-review read that hangs says so in the log",
      vinegar.already_posted(L, "o/r", PR, None) is False
      and any("timed out" in m for m in _ap_logged), _ap_logged)
vinegar.run = fake_run
vinegar.log = lambda message: None

logged = []
vinegar.log = lambda m: logged.append(m)
vinegar.run, vinegar.github_env = claude_run, lambda *a, **k: None
for value, shown in ((1.5, "1.50 USD"), (2, "2.00 USD"), (0, "0.00 USD")):
    claude_run.stream = stream(call([]), result_event(total_cost_usd=value))
    del logged[:]
    vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
    check("a cost of %r is reported, whatever its JSON type" % (value,),
          any(shown in m for m in logged),
          [m for m in logged if "reviewed in" in m])
vinegar.log = lambda message: None
vinegar.run = fake_run

# A failed attempt posts nothing, correctly, but must not lose what it said.
vinegar.save_transcript = stub_transcript
vinegar.run, vinegar.github_env = claude_run, lambda *a, **k: None
claude_run.stream = stream(
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Twenty minutes of analysis."}]}},
    result_event(is_error=True, result=None,
                 subtype="error_during_execution"))
del posted[:]
del _tx_calls[:]
check("a failed attempt is retried, not posted",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.FAILED
      and not posted, (posted,))
check("a failed attempt still keeps the reviewer's words on disk",
      _tx_calls and "Twenty minutes of analysis." in _tx_calls[-1][2],
      _tx_calls[-1][2][:80] if _tx_calls else "never called")
check("a failed attempt's transcript does not claim to be a review",
      _tx_calls and "not a review" in (_tx_calls[-1][4] or ""),
      _tx_calls[-1][4] if _tx_calls else "never called")

# Giving up after MAX_ATTEMPTS must be said on the pull request.
_gu_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS - 1}}
_real_review2 = vinegar.review
vinegar.review = lambda *a, **k: vinegar.FAILED
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.save_state = lambda st: None
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gu_state, {})
check("giving up is announced on the pull request, not only in the log",
      len(posted) == 1 and "not as the change being clean"
      in posted[0][1]["body"],
      posted[0][1]["body"][:160] if posted else "nothing posted")
check("the give-up does not claim a review happened",
      posted and "gave up on `a1b2c3d`" in posted[0][1]["body"]
      and " reviewed `" not in posted[0][1]["body"],
      posted[0][1]["body"][:120] if posted else "nothing posted")
vinegar.review = _real_review2
vinegar.checkout, vinegar.github_env = _real_checkout, _real_github_env
vinegar.save_state = _real_save_state
vinegar.run = fake_run

# --- save_transcript, unstubbed ------------------------------------------
# The stub above hides this everywhere else, and on a dry run the transcript
# is the only artifact there is.
_tx_home = tempfile.mkdtemp(prefix="vinegar-test-tx-")
atexit.register(shutil.rmtree, _tx_home, True)
_tx_real = vinegar.REVIEW_DIR
vinegar.REVIEW_DIR = _tx_home
vinegar.save_transcript = GENUINE_SAVE_TRANSCRIPT

_first = vinegar.save_transcript("o/r", PR, "Reviewed it.", FINDINGS[:2])
check("the transcript lands whole under its final name",
      os.path.exists(_first)
      and not [f for f in os.listdir(_tx_home) if f.endswith(".tmp")],
      os.listdir(_tx_home))

written = vinegar.save_transcript("o/r", PR, "Reviewed it.", FINDINGS[:2])
body = open(written).read()
check("the transcript records the findings, not just the summary",
      "## Findings" in body and "in diff" in body and "absolute path" in body,
      body[:200])
check("the transcript renders findings the same way the comment does",
      vinegar.finding_bullet(FINDINGS[0]) in body, body[:200])

body = open(vinegar.save_transcript("o/r", PR, "Nothing.", [])).read()
check("a clean transcript says so plainly", "None." in body, body[:160])

body = open(vinegar.save_transcript(
    "o/r", PR, "Killed.", [], note="This review was killed after 1800s.")).read()
check("a killed transcript is never a clean transcript",
      "killed after 1800s" in body
      and "None reported before it stopped." in body,
      body[:240])

body = open(vinegar.save_transcript("o/r", PR, "Text.", None)).read()
check("a transcript with nothing reported has no findings section",
      "## Findings" not in body, body[:160])

# The give-up after MAX_ATTEMPTS arrives with nothing to say, seconds after
# keep() saved what the last attempt said. It must not replace that file.
PR_GAVE = dict(PR, headRefOid="feedfacecafe")
vinegar.keep(L, "o/r", PR_GAVE, "Twenty minutes of analysis.",
             "the review failed")
vinegar.finish(L, "o/r", PR_GAVE, ROOT, "", None,
               dict(CONFIG, comment=False), None, {},
               note="Vinegar tried to review this 3 times.")
body = open(vinegar.transcript_path("o/r", PR_GAVE)).read()
check("the give-up leaves the words the attempts saved",
      "Twenty minutes of analysis." in body, body[:200])
check("the give-up itself is recorded beneath them",
      "tried to review this 3 times" in body
      and body.find("Twenty minutes") < body.find("tried to review"),
      body[:300])

PR_QUIET = dict(PR, headRefOid="deadbeefcafe")
vinegar.finish(L, "o/r", PR_QUIET, ROOT, "", None,
               dict(CONFIG, comment=False), None, {},
               note="Vinegar tried to review this 3 times.")
_quiet = vinegar.transcript_path("o/r", PR_QUIET)
check("a give-up with no words still leaves a trace",
      os.path.exists(_quiet)
      and "tried to review this 3 times" in open(_quiet).read(), _quiet)

check("the transcript write leaves nothing temporary behind",
      not [f for f in os.listdir(_tx_home) if f.endswith(".tmp")],
      os.listdir(_tx_home))

vinegar.REVIEW_DIR = _tx_real

# What finish() actually hands the transcript, which only the recorder sees.
del _tx_calls[:]
vinegar.save_transcript = stub_transcript
vinegar.run = timing_out
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("the killed-run marker reaches the transcript, not just the comment",
      _tx_calls and _tx_calls[-1][4] and "killed after" in _tx_calls[-1][4],
      _tx_calls[-1][4] if _tx_calls else "never called")
vinegar.run = fake_run

# The reviewer's diff is whatever bytes the repository holds. A Latin-1
# source file used to raise UnicodeDecodeError out of run() itself, and the
# exception surfaced inside announce(), which swallowed the finished review
# it was carrying.
try:
    _bytes_out = GENUINE_RUN(["/bin/sh", "-c", "printf 'caf\\351\\n'"]).stdout
except UnicodeDecodeError:
    _bytes_out = "raised"
check("bytes that are not UTF-8 are read, not raised",
      _bytes_out == "caf�\n", repr(_bytes_out))

# And written back out the same way: an ASCII default encoding must not
# cost a dry run its only artifact when a finding quotes é. Plain LC_ALL=C
# is not enough to reproduce that, because CPython turns UTF-8 mode on for
# the C locale (PEP 540); the two PYTHON* variables switch that rescue off,
# which is also only one operator-set environment variable away in life.
_tx_script = (
    "import os, tempfile\n"
    "os.environ['VINEGAR_HOME'] = tempfile.mkdtemp()\n"
    "import vinegar\n"
    "p = vinegar.save_transcript('o/r', {'number': 1, 'headRefOid':"
    " 'abc1234def', 'url': 'u'}, 'caf\\u00e9', None)\n"
    "print(ascii(open(p, encoding='utf-8').read()))\n")
_tx_proc = subprocess.run(
    [sys.executable, "-c", _tx_script], capture_output=True, text=True,
    cwd=here_dir, env=dict(os.environ, LC_ALL="C", LANG="C",
                           PYTHONUTF8="0", PYTHONCOERCECLOCALE="0"))
check("a transcript survives the C locale with its quotes intact",
      _tx_proc.returncode == 0 and "caf\\xe9" in _tx_proc.stdout,
      (_tx_proc.returncode, (_tx_proc.stderr or _tx_proc.stdout)[:200]))

# --- the --pr guard, through the real entry point -------------------------
# A mistyped target must be refused before anything is minted or asked of
# GitHub. Unicode digits are the trap: "²".isdigit() and "١٢".isdigit() are
# both true, and either would otherwise reach the gh call and fail there,
# reporting the wrong problem. These run vinegar.py for real, so they need
# the config the module would otherwise exit over; nothing else is touched
# because the guard exits first.
os.makedirs(os.environ["VINEGAR_HOME"], exist_ok=True)
with open(os.path.join(os.environ["VINEGAR_HOME"], "config.json"), "w") as h:
    json.dump({"repos": ["o/r"]}, h)
for target in ("nonsense", "o/r#2x", "o/r#²", "o/r#١٢"):
    proc = subprocess.run(
        [sys.executable, os.path.join(here_dir, "vinegar.py"), "--pr", target],
        capture_output=True, text=True)
    refused = (proc.returncode != 0
               and "wants owner/repo#number" in (proc.stdout + proc.stderr)
               and "cannot read" not in (proc.stdout + proc.stderr))
    check("--pr %r is refused before anything is asked of GitHub" % target,
          refused, (proc.returncode, (proc.stdout + proc.stderr)[:120]))

print()
print("FAILED: %s" % ", ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
