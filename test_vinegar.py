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
import time

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
@@ -10,0 +20,1 @@
+after the forgery
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
last_post_timeout = [None]


def fake_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[0] == "git" and "diff" in cmd:
        last_git_diff[0], last_git_diff[1] = cmd, timeout
        return subprocess.CompletedProcess(
            cmd, fake_run.diff_rc,
            fake_run.diff_out if fake_run.diff_rc else DIFF, "boom")
    if cmd[:2] == ["gh", "api"] and "-X" in cmd:
        # The read that asks whether a review already landed.
        looked.append(cmd)
        return subprocess.CompletedProcess(
            cmd, fake_run.look_rc, fake_run.look_out, "")
    if cmd[:2] == ["gh", "api"]:
        posted.append((cmd, json.loads(stdin_text)))
        last_post_timeout[0] = timeout
        return subprocess.CompletedProcess(cmd, fake_run.rc, "",
                                           fake_run.post_err)
    raise AssertionError("unexpected command %r" % cmd)


fake_run.rc = 0
fake_run.diff_rc = 0
fake_run.diff_out = ""
fake_run.look_rc = 0
fake_run.look_out = ""
fake_run.post_err = "HTTP 422"
GENUINE_RUN = vinegar.run
vinegar.run = fake_run
vinegar.log = lambda message: None

fails = []
ran = []
reached_the_end = []


GENUINE = {name: getattr(vinegar, name) for name in (
    "run", "log", "save_transcript", "github_env", "review", "checkout",
    "save_state", "post_review", "find_pr")}


def reset_stubs():
    """Put the stubs back the way a section should find it.

    The stubs are module globals and attributes on fake_run, so without
    this each block inherits whatever the one above it happened to leave:
    an `rc` of 1 turns a later check about a clean post into a test of the
    refusal path that still passes, and reordering two blocks changes what
    a third one exercises without failing anything. Sections call this
    first so they say what they need rather than inheriting it.

    REVIEW_DIR is deliberately not one of them: the transcript sections
    point it at a temporary directory on purpose and manage it themselves,
    and resetting it here would undo that rather than protect it.
    """
    for name, genuine in GENUINE.items():
        setattr(vinegar, name, genuine)
    vinegar.log = lambda message: None
    fake_run.rc = 0
    fake_run.diff_rc = 0
    fake_run.diff_out = ""
    fake_run.look_rc = 0
    fake_run.look_out = ""
    fake_run.post_err = "HTTP 422"
    del posted[:]
    del looked[:]


def check(name, condition, detail=""):
    print("%-52s %s" % (name, "ok" if condition else "FAIL " + str(detail)))
    ran.append(name)
    if not condition:
        fails.append(name)


@atexit.register
def say_if_it_stopped_early():
    """Say so when the run ended somewhere other than its own ending.

    Most of the work here happens between the checks, at module level, so
    anything that raises ends the script where it stands and every check
    below that line is skipped rather than run. That exits 1 with a
    traceback, which reads like a failing check to anyone scanning, and it
    is how a broken guard gets recorded as a caught one: breaking clamp()
    left 11 of 344 checks running and the other 333 unreported, and the
    exit code alone said the suite had noticed.

    Registered after the one that removes the temporary home, so it prints
    before that runs. atexit calls handlers in reverse.
    """
    if not reached_the_end:
        print("\nABORTED after %d checks. The run stopped where the "
              "traceback points. Every check below that line was skipped, "
              "not passed." % len(ran))


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
# The same cap wherever that prose reaches a comment. A result event
# carrying 50000 characters used to go out whole under the very sentence
# that promises the reviewer's words follow unedited, while a killed run
# saying the same thing was cut at MAX_SPOKEN with a note.
del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, "z" * 50000, None, CONFIG, None)
check("a long result message is capped like a salvaged one",
      posted and "cut after" in posted[0][1]["body"]
      and len(posted[0][1]["body"]) < 10000,
      len(posted[0][1]["body"]) if posted else "nothing posted")
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
# A line that starts with `{` and then stops making sense, which is what a
# stream cut mid-write actually leaves. "not json" above never reaches
# json.loads at all: it fails the startswith and is skipped by the line
# before the try, so the except that catches a half-written event was
# unexercised and could be deleted with the suite still green.
#
# Wrapped, because what deleting that except does is raise, and an unwrapped
# condition here would end the run instead of failing one check. In the
# daemon the same raise escapes review() and leaves handle_pr no state, so
# the pull request is re-reviewed at full cost on every poll after it.
_half = '{"type":"assist\n' + stream(call(REAL), DONE_EVENT)
try:
    _half_read = vinegar.read_stream(_half)
except Exception as err:
    _half_read = err
check("a half-written event does not cost the rest of the stream",
      not isinstance(_half_read, Exception) and _half_read[1] == REAL,
      _half_read)
check("a half-written event does not cost the result that follows it",
      not isinstance(_half_read, Exception) and _half_read[0] is not None,
      _half_read)
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
reset_stubs()
covered = vinegar.diff_lines(ROOT, "release-2", None, "o/r#12")
check("added lines are covered", covered.get("vinegar.py") == {11, 12, 13, 43},
      covered.get("vinegar.py"))
# Redundant with the check below it, and knowingly so: a deletion's hunk is
# always `+0,0`, so the empty-hunk gate already blocks the write whatever
# `name` holds. Deleting the /dev/null branch in diff_lines leaves this green.
# Kept because it states the intent at the point the reader looks for it,
# and no fixture git can actually produce would make it bite.
check("deleted file contributes nothing", "gone.py" not in covered, covered)
check("deletion-only hunk contributes nothing", "README.md" not in covered,
      covered)
check("a path with a space is keyed without git's trailing tab",
      covered.get("my file.py") == {2}, list(covered))
check("a carriage return cannot forge a hunk header",
      covered.get("crlf.py") == {2}, covered.get("crlf.py"))
# The hunk after the forged `+++` is what does the work here. Asserting only
# that spoofed.py is absent passes with the `heading` gate deleted: nothing
# follows the forgery to be misfiled, so both readings agree. The second hunk
# has somewhere to go wrong, and lands on doc.md only while the gate holds.
check("an added line that looks like a file header is content",
      "spoofed.py" not in covered and covered.get("doc.md") == {2, 3, 20},
      covered)
check("git diff is asked for the prefixes the parser expects",
      "--src-prefix=a/" in last_git_diff[0]
      and "--dst-prefix=b/" in last_git_diff[0], last_git_diff[0])
check("the diff carries the context GitHub accepts comments on",
      "--unified=3" in last_git_diff[0], last_git_diff[0])

# With stdout, not without it. An empty stdout returns {} from parsing
# nothing whether the return code is consulted or not, so the gate this check
# is named for was never the reason it passed. git writes what it managed
# before it failed, and a partial diff is the dangerous case: half the hunks
# look like the whole truth, and every finding below the cut would be routed
# to the general comment while the ones above it anchor with confidence.
fake_run.diff_rc = 1
fake_run.diff_out = DIFF.split("diff --git a/doc.md")[0]
check("a failed diff anchors nothing rather than guessing",
      vinegar.diff_lines(ROOT, "release-2", None, "o/r#12") == {},
      vinegar.diff_lines(ROOT, "release-2", None, "o/r#12"))
fake_run.diff_rc = 0
fake_run.diff_out = ""
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

# --- checkout ------------------------------------------------------------
# Everything here is about one property: checkout() raising is not counted
# against MAX_ATTEMPTS, so a failure that persists logs a line per poll for
# ever with the pull request never reviewed and never given up on. That is
# the quietest way Vinegar can stop working, which is why the guards against
# it are worth checking rather than assuming.
reset_stubs()
_co_root = os.path.join(SCRATCH, "checkouts")
_co_dir = vinegar.CHECKOUT_DIR
vinegar.CHECKOUT_DIR = _co_root
_co_path = os.path.join(_co_root, "o__r")
_co_ran = []


def co_run(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    _co_ran.append((cmd, cwd, timeout))
    if cmd[:3] == ["gh", "repo", "clone"]:
        os.makedirs(os.path.join(cmd[4], ".git"), exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    if cmd[:2] == ["git", "rev-parse"]:
        return subprocess.CompletedProcess(cmd, co_run.usable, "", "not a repo")
    return subprocess.CompletedProcess(cmd, 0, "", "")


co_run.usable = 0
vinegar.run = co_run

# A SIGKILL during a fetch leaves .git/index.lock behind, and every later
# `git reset` fails with "Unable to create index.lock: File exists". Nothing
# clears it on its own, so without this the repository is finished.
os.makedirs(os.path.join(_co_path, ".git"))
_co_stale = os.path.join(_co_path, ".git", "index.lock")
open(_co_stale, "w").close()
del _co_ran[:]
check("checkout returns the path it prepared",
      vinegar.checkout("o/r", PR, None) == _co_path, _co_path)
check("a lock left by a killed run is cleared, not inherited",
      not os.path.exists(_co_stale), _co_stale)
# The head fetch reaches the network and the local steps do not. One bound
# for both is wrong in whichever direction it is set: the network budget on
# a local step parks the poll thread on a filesystem that stops answering,
# and the local budget on the fetch turned a slow repository into a pull
# request that was never reviewed and never given up on.
_co_fetch = [t for c, _w, t in _co_ran if c[:2] == ["git", "fetch"]]
_co_local = [t for c, _w, t in _co_ran if c[:2] == ["git", "reset"]]
check("the head fetch is given the network budget",
      _co_fetch and _co_fetch[0] == vinegar.FETCH_TIMEOUT, _co_fetch)
check("the local steps are bounded as well, and more tightly",
      _co_local and _co_local[0] == vinegar.DIFF_TIMEOUT, _co_local)
check("the credential helper is written on every pass, not only at clone",
      any(c[:3] == ["git", "config", "--local"] for c, _w, _t in _co_ran),
      [c[:3] for c, _w, _t in _co_ran])
# Every git step in the checkout it prepared, not wherever the daemon sits.
# run() defaults cwd to None, so dropping it does not fail: `git reset
# --quiet --hard` and `git clean -qfd` run somewhere else. Under launchd
# that is `/`, where they error and every poll loses its checkout instead;
# run by hand from the repository, as the README tells you to for --pr,
# they wipe your own working tree. The reviewer invocation records its cwd
# for exactly this reason and this section did not, so all of it passed
# with `cwd=path` deleted.
_co_where = [w for c, w, _t in _co_ran if c[0] == "git"]
check("every git step runs inside the checkout it prepared",
      _co_where and all(w == _co_path for w in _co_where), _co_where)
# A clone killed part-way leaves a .git git itself refuses to open. Skipping
# the clone because .git exists uses that repository for ever.
co_run.usable = 1
del _co_ran[:]
vinegar.checkout("o/r", PR, None)
check("a checkout git cannot open is cloned again rather than reused",
      any(c[:3] == ["gh", "repo", "clone"] for c, _w, _t in _co_ran),
      [c[:3] for c, _w, _t in _co_ran])
# The clone reaches the network on the one poll thread, and it ran unbounded
# until issue #13: a socket that is open and never answers is not an error
# anyone raises, so nothing polled and the watchdog called a live pid with
# no log lines healthy. Generous rather than tight, because a cap that bites
# costs the repository every review.
_co_clone = [t for c, _w, t in _co_ran if c[:3] == ["gh", "repo", "clone"]]
check("the clone is bounded so it cannot park the poll thread",
      _co_clone and _co_clone[0] == vinegar.CLONE_TIMEOUT, _co_clone)
check("the clone is given longer than the fetch, not less",
      vinegar.CLONE_TIMEOUT >= vinegar.FETCH_TIMEOUT,
      (vinegar.CLONE_TIMEOUT, vinegar.FETCH_TIMEOUT))


def clone_hangs(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:3] == ["gh", "repo", "clone"]:
        # The half-built checkout has to exist before the raise, or the
        # cleanup below has nothing to remove and the check that it did
        # passes either way. `git clone` writes .git during its init phase,
        # so this is the state a killed one leaves: a directory rev-parse
        # answers 0 for, with no refs in it.
        os.makedirs(os.path.join(cmd[4], ".git"), exist_ok=True)
        raise subprocess.TimeoutExpired(cmd, timeout or 0)
    return co_run(cmd, cwd, timeout, env, stdin_text)


# The bound is only half of it. Left to raise TimeoutExpired, the failure
# reaches handle_pr's generic handler and logs "checkout failed: Command
# '[...]' timed out", which names a subprocess rather than the clone and
# reads like the tree is broken rather than like the network is. The steps
# loop already converts its own timeout for that reason.
vinegar.run = clone_hangs
shutil.rmtree(_co_path, ignore_errors=True)
try:
    vinegar.checkout("o/r", PR, None)
    _co_hung = "returned normally"
except subprocess.TimeoutExpired:
    _co_hung = "TimeoutExpired escaped"
except RuntimeError as err:
    _co_hung = str(err)
check("a clone that hangs is reported as a clone that hung",
      "did not finish within" in _co_hung, _co_hung)
check("the message names the command, like the steps below it do",
      "gh repo clone" in _co_hung, _co_hung)
# What the timeout leaves behind matters more than what it says. `git clone`
# writes .git during init, long before it has refs, so a killed clone leaves
# a directory rev-parse answers 0 for. Left there, the probe calls it usable,
# the clone is skipped for ever, and the steps loop runs against an unborn
# HEAD: one log line, then that repository is never reviewed again, because
# checkout() failures are exempt from MAX_ATTEMPTS.
check("a clone that hangs leaves nothing behind to be mistaken for a repo",
      not os.path.exists(_co_path), _co_path)
# The property that actually matters, checked the way the unusable-repo case
# above is: with rev-parse answering 0, so a leftover would be accepted.
co_run.usable = 0
vinegar.run = co_run
del _co_ran[:]
vinegar.checkout("o/r", PR, None)
check("the poll after a hung clone clones again rather than limping on",
      any(c[:3] == ["gh", "repo", "clone"] for c, _w, _t in _co_ran),
      [c[:3] for c, _w, _t in _co_ran])
vinegar.CHECKOUT_DIR = _co_dir
vinegar.run = fake_run

# --- split_findings ------------------------------------------------------
reset_stubs()
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
# A fraction is not a line number: int() floors it and the comment lands
# one line above the code the finding is about.
check("a fractional line number anchors nothing rather than the line above",
      vinegar.finding_line({"line": 812.7}) is None,
      vinegar.finding_line({"line": 812.7}))
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
# Wrapped, because the failure this guards against is a raise: `file` comes
# from the reviewer's tool call and the schema is not enforced on its way in,
# so a number or an object reaches here intact. Without the isinstance the
# .strip() below it raises inside announce(), which loses every finding in
# the review, not just the one with the bad `file`.
try:
    _odd_file = (vinegar.repo_path(12, ROOT) is None
                 and vinegar.repo_path({"path": "a.py"}, ROOT) is None
                 and vinegar.repo_path(None, ROOT) is None)
except Exception as err:
    _odd_file = "raised %r" % err
check("a file that is not a string anchors nothing rather than raising",
      _odd_file is True, _odd_file)
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

# Routed on the paths as they arrive, which finish() has already made
# relative; anything that did not resolve simply fails to match the diff.
inline, general = vinegar.split_findings(
    vinegar.relative_findings(FINDINGS, ROOT), covered, L)
check("in-diff findings become inline comments", len(inline) == 2, inline)
check("absolute path is made repo-relative",
      all(c["path"] == "vinegar.py" for c in inline), inline)
check("inline comments anchor on the head side",
      all(c["side"] == "RIGHT" for c in inline), inline)
check("out-of-diff findings go general", len(general) == 4, general)
# Reviewers report absolute paths into the checkout, and every route to
# the pull request has to carry them relative. One helper does it, above
# the transcript and every posting path; the routes themselves are
# checked where finish() is driven.
_abs_rel = vinegar.relative_findings(
    [{"file": os.path.join(ROOT, "src/app.py"), "line": 9000,
      "summary": "far outside the diff"},
     {"file": "/etc/passwd", "line": 1, "summary": "outside the checkout"}],
    ROOT)
check("a finding's path is made relative where it resolves",
      _abs_rel[0]["file"] == "src/app.py"
      and ROOT not in vinegar.finding_bullet(_abs_rel[0]),
      _abs_rel[0]["file"])
check("a path that resolves nowhere is left as the reviewer sent it",
      _abs_rel[1]["file"] == "/etc/passwd", _abs_rel[1]["file"])
check("the category reaches the comment body",
      "(correctness)" in vinegar.describe(
          {"summary": "s", "category": "correctness"}),
      vinegar.describe({"summary": "s", "category": "correctness"}))
check("failure scenario reaches the comment body",
      "Failure: boom" in inline[0]["body"], inline[0]["body"])
# GitHub applies its ceiling to an inline comment too, and it applies the
# review or none of it, so one reviewer who writes an essay about line 12
# takes every other finding down with it. The top-level body's cap is
# checked elsewhere; this is the one on the comments array.
_essay = vinegar.split_findings(
    [{"file": "vinegar.py", "line": 12, "failure_scenario": "boom",
      "summary": "s" * (vinegar.MAX_BODY + 5000)}], covered, L)[0]
check("an overlong inline comment is cut to what GitHub accepts",
      len(_essay[0]["body"]) <= vinegar.MAX_BODY + 100,
      len(_essay[0]["body"]))
check("an inline comment that was cut says so",
      "cut to fit" in _essay[0]["body"], _essay[0]["body"][-80:])

# --- review_body ---------------------------------------------------------
reset_stubs()
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
reset_stubs()
text = "Summary of the review."

del posted[:]
# As finish() would hand them over: paths already made relative, which is
# where that rule lives now.
vinegar.post_review(L, "o/r", PR, ROOT, text,
                    vinegar.relative_findings(FINDINGS[:4], ROOT), CONFIG,
                    None)
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
last_post_timeout[0] = None
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a clean review is announced, not skipped",
      len(posted) == 1 and "No findings." in posted[0][1]["body"],
      posted[0][1]["body"] if posted else "nothing posted")
# The timeout the call received, not the constant it should have used. The
# earlier version of this check read POST_TIMEOUT and compared it to itself,
# so deleting `timeout=POST_TIMEOUT` from the gh invocation left it green,
# and the poll loop is one thread: a posting that never returns wedges every
# repository behind it. The diff and listing checks read the call; so does
# this one now.
check("the posting request carries a timeout",
      last_post_timeout[0] == vinegar.POST_TIMEOUT, last_post_timeout[0])

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

# And counted to the character. The note is appended as ["", note], which
# costs two newlines once joined, not one; budgeting one puts the body at
# exactly MAX_BODY + 1 and hands it to clamp(), which shears the note it
# just wrote and adds a second, contradicting one. Only a one-character
# step finds that, so the ceiling is lowered to keep the sweep cheap and
# every offset in a bullet's width is tried.
_real_max = vinegar.MAX_BODY
vinegar.MAX_BODY = 2000
_fine = []
for _pad in range(0, 160):
    _small = [{"file": "a.py", "summary": "s", "category": "correctness",
               "failure_scenario": "y" * 100} for _ in range(30)]
    _small[0]["failure_scenario"] = "y" * (100 + _pad)
    _b = vinegar.review_body(L, PR, CONFIG, [], _small)
    _fine.append(len(_b) <= vinegar.MAX_BODY
                 and "cut to fit GitHub's comment limit" not in _b)
vinegar.MAX_BODY = _real_max
check("the overflow note's own newlines are counted",
      all(_fine), _fine.count(False))

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
fake_run.post_err = "HTTP 502 Bad Gateway"
fake_run.look_out = ""
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a clean review lost to a transient failure is tried again",
      len(posted) == 2 and "comments" not in posted[1][1], len(posted))

# A definite refusal of a comment with nothing to strip out is not resent:
# the same bytes to the same endpoint buy one more guaranteed refusal.
del posted[:]
fake_run.post_err = "HTTP 422 Unprocessable Entity"
vinegar.post_review(L, "o/r", PR, ROOT, "clean", [], CONFIG, None)
check("a refused review with nothing to change is not resent",
      len(posted) == 1, len(posted))
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
        claude_run.cwd, claude_run.timeout = cwd, timeout
        return subprocess.CompletedProcess(cmd, 0, claude_run.stream, "")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


def result_event(**over):
    return dict({"type": "result", "subtype": "success", "is_error": False,
                 "result": text, "total_cost_usd": 1.0}, **over)


claude_run.saw, claude_run.env = [], {}
claude_run.cwd, claude_run.timeout = None, None
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
# The three flags that decide what the reviewer runs under. Without them it
# picks up whatever settings the machine has: `--setting-sources ""` is what
# stops ~/.claude and the checkout's own .claude/settings.json from loading,
# and a checkout is attacker-controlled content on a public repository.
# `--strict-mcp-config` is the same argument for MCP servers. None of the
# three had a check, so all three could be dropped with the suite green.
check("the reviewer loads no settings but the ones it is handed",
      "--setting-sources" in claude_run.saw
      and claude_run.saw[claude_run.saw.index("--setting-sources") + 1] == "",
      claude_run.saw)
check("the reviewer loads no MCP servers but the ones it is handed",
      "--strict-mcp-config" in claude_run.saw, claude_run.saw)
# The checkout, not wherever the daemon happens to be. Vinegar polls several
# repositories from one process, so a lost cwd does not fail: it reviews the
# wrong tree, and says nothing about having done so.
check("the reviewer is run inside the checkout it is reviewing",
      claude_run.cwd == ROOT, claude_run.cwd)
# A review with no timeout holds the poll thread for ever, and this one is
# the longest call Vinegar makes. The killed-run path below is what handles
# the expiry, and it is unreachable if nothing ever expires.
check("the review is bounded by the configured timeout",
      claude_run.timeout == CONFIG["review_timeout"], claude_run.timeout)
check("the reporting tool is allowed, which is what makes it reachable",
      vinegar.REPORT_TOOL in json.load(
          open(os.path.join(here_dir, "review-settings.json"))
      )["permissions"]["allow"])

# The reviewer runs under settings passed as JSON, not as a path, because
# the checkout deny is derived. A regression to `--settings SETTINGS_PATH`
# leaves the sandbox enabled and the workspace — where .git/config is —
# writable, which is the whole of the hole.
_settings_arg = (claude_run.saw[claude_run.saw.index("--settings") + 1]
                 if "--settings" in claude_run.saw else "")
try:
    _sent = json.loads(_settings_arg)
except ValueError:
    _sent = {}
check("the reviewer is given settings as JSON, not the file path",
      isinstance(_sent, dict) and _sent != {},
      _settings_arg[:120])
check("the reviewer's sandbox is enabled in what is actually sent",
      _sent.get("sandbox", {}).get("enabled") is True,
      json.dumps(_sent.get("sandbox")))
# The one rule no file can carry: CHECKOUT_DIR moves with VINEGAR_HOME.
check("the reviewer cannot write the checkout it is reviewing",
      vinegar.CHECKOUT_DIR in
      _sent.get("sandbox", {}).get("filesystem", {}).get("denyWrite", []),
      json.dumps(_sent.get("sandbox", {}).get("filesystem")))
# Denying the checkout must not cost the reviewer its own permissions.
check("the settings sent still carry the permission rules",
      vinegar.REPORT_TOOL in _sent.get("permissions", {}).get("allow", []),
      json.dumps(_sent.get("permissions", {}).get("allow"))[:120])
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

# The rule the two path checks reason about. They confirm HOME sits where
# the glob covers; nothing confirmed the glob was still there, so dropping
# it while widening the deny list left every check passing and the App
# private key readable.
_dh = vinegar.DENY_ALWAYS
vinegar.DENY_ALWAYS = ("Read(//**/.not-in-the-file/**)",)
try:
    vinegar.check_paths()
    _denied = "started"
except SystemExit as err:
    _denied = str(err)
vinegar.DENY_ALWAYS = _dh
check("a missing private-key deny rule refuses to start",
      "must deny" in _denied, _denied)
# Every credential read in that tuple, not just the first. Only the App
# key's rule used to be checked, out of 47 in the file, so deleting the
# ssh or gh-config rule while editing left every check passing and a
# review able to quote `~/.ssh/id_ed25519` into a published finding.
_settings_real = vinegar.SETTINGS_PATH
for _rule in ("Read(//**/.ssh/**)", "Read(//**/.config/gh/**)",
              "Read(//**/.aws/**)", "Read(//**/.netrc)"):
    _short = json.load(open(_settings_real))
    _short["permissions"]["deny"] = [r for r in
                                     _short["permissions"]["deny"]
                                     if r != _rule]
    _p = os.path.join(_home, "short-deny.json")
    with open(_p, "w") as h:
        json.dump(_short, h)
    vinegar.SETTINGS_PATH = _p
    try:
        vinegar.load_settings()
        _denied = "started"
    except SystemExit as err:
        _denied = str(err)
    finally:
        vinegar.SETTINGS_PATH = _settings_real
    check("dropping %s refuses to start" % _rule,
          "must deny" in _denied and _rule in _denied, _denied)
# And the one word that would make all of them decorative.
_bypass = json.load(open(_settings_real))
_bypass["permissions"]["defaultMode"] = "bypassPermissions"
_p = os.path.join(_home, "bypass.json")
with open(_p, "w") as h:
    json.dump(_bypass, h)
vinegar.SETTINGS_PATH = _p
try:
    vinegar.load_settings()
    _denied = "started"
except SystemExit as err:
    _denied = str(err)
finally:
    vinegar.SETTINGS_PATH = _settings_real
check("a permission mode that ignores the lists refuses to start",
      "defaultMode" in _denied, _denied)

# The sandbox stanza, which is what stops `git show --output=` writing any
# file the daemon user can write. The allow list cannot: it matches the
# start of a command and never sees the flag. Each key is checked
# separately because each fails differently and silently.
_sp_real = vinegar.SETTINGS_PATH
# The directory a review actually runs in, which is one level below
# CHECKOUT_DIR and is what reviewer_settings() has to be told about.
_workspace = os.path.join(vinegar.CHECKOUT_DIR, "o__r")


def _with_sandbox(sandbox):
    """What check_paths says when the settings carry this sandbox stanza."""
    path = os.path.join(_home, "sandbox-settings.json")
    real = json.load(open(_sp_real))
    real["sandbox"] = sandbox
    with open(path, "w") as handle:
        json.dump(real, handle)
    vinegar.SETTINGS_PATH = path
    try:
        vinegar.check_paths()
        return "started"
    except SystemExit as err:
        return str(err)
    finally:
        vinegar.SETTINGS_PATH = _sp_real


_good = {"enabled": True, "failIfUnavailable": True,
         "allowUnsandboxedCommands": False}
# Bound once each: `check` evaluates its detail argument whether or not the
# condition fails, so calling the helper there ran check_paths() a second
# time and reported a different execution than the one asserted on.
_said = _with_sandbox(_good)
check("the sandbox stanza as shipped starts cleanly",
      _said == "started", _said)
_said = _with_sandbox({})
check("no sandbox at all refuses to start", "sandbox.enabled" in _said, _said)
_said = _with_sandbox(dict(_good, enabled=False))
check("a disabled sandbox refuses to start",
      "sandbox.enabled" in _said, _said)
# `"true"` is a non-empty string, so reading these flags for truth rather
# than for value would take a hand-edited quote as an enabled sandbox. It
# is the same slip load_config refuses in config.json, in the file
# operators edit far more often.
_said = _with_sandbox(dict(_good, enabled="true"))
check("a quoted true is not an enabled sandbox",
      "sandbox.enabled" in _said, _said)
# And `1 == True` in Python, so comparing with `==` accepts a number where
# a flag belongs. `is` is what makes these three keys mean what they say.
_said = _with_sandbox(dict(_good, enabled=1))
check("a sandbox enabled with 1 rather than true refuses to start",
      "sandbox.enabled" in _said, _said)
_said = _with_sandbox(dict(_good, allowUnsandboxedCommands=0))
check("unsandboxed commands allowed as 0 rather than false refuses",
      "allowUnsandboxedCommands" in _said, _said)
# Without this, a sandbox that cannot start is skipped and the review runs
# unconfined, which is the failure that looks exactly like success.
_said = _with_sandbox(dict(_good, failIfUnavailable=False))
check("a sandbox allowed to be unavailable refuses to start",
      "failIfUnavailable" in _said, _said)
_said = _with_sandbox(dict(_good, allowUnsandboxedCommands=True))
check("letting commands run unsandboxed refuses to start",
      "allowUnsandboxedCommands" in _said, _said)
# A stanza that is not an object at all must produce that same sentence.
# Truthy and unguarded, it reached a .get outside the try that guards the
# read, so launchd restarted into a traceback every 30 seconds.
_said = _with_sandbox("true")
check("a sandbox that is not an object is refused with a sentence",
      "sandbox.enabled" in _said, _said)


def _settings_file(sandbox, permissions=None, raw=None):
    """Write a settings file and point SETTINGS_PATH at it."""
    path = os.path.join(_home, "built-settings.json")
    with open(path, "w") as handle:
        if raw is not None:
            handle.write(raw)
        else:
            doc = {"permissions": permissions if permissions is not None
                   else {"allow": [vinegar.REPORT_TOOL],
                         "deny": list(vinegar.DENY_ALWAYS)}}
            if sandbox is not _absent:
                doc["sandbox"] = sandbox
            json.dump(doc, handle)
    vinegar.SETTINGS_PATH = path
    return path


def _built_with(sandbox, permissions=None, raw=None):
    """The settings reviewer_settings() sends when the file holds this."""
    _settings_file(sandbox, permissions, raw)
    try:
        return json.loads(vinegar.reviewer_settings(_workspace))
    finally:
        # In a finally like _with_sandbox above, because reviewer_settings()
        # can exit: without it one failure leaves this global pointing at a
        # temp file for the remaining two thousand checks.
        vinegar.SETTINGS_PATH = _sp_real


def _sending(sandbox, permissions=None, raw=None):
    """What reviewer_settings() says when it refuses to send anything."""
    _settings_file(sandbox, permissions, raw)
    try:
        vinegar.reviewer_settings(_workspace)
        return "sent"
    except SystemExit as err:
        return str(err)
    finally:
        vinegar.SETTINGS_PATH = _sp_real


_absent = object()
# A file that has lost the sandbox stops the review rather than being
# quietly corrected. check_paths() reads it once at startup; this runs
# again for every review, so a file edited to chase a denial — or replaced
# by `git pull` — would otherwise launch the next review with nothing
# refusing and nothing logged.
for _label, _stanza in (("carries no sandbox", _absent),
                        ("has a null sandbox", None),
                        ("disables the sandbox", {"enabled": False})):
    _said = _sending(_stanza)
    check("a review is refused when the file %s" % _label,
          "sandbox.enabled" in _said, _said)
# Inside a stanza that passes, the deny list is still built rather than
# read, so these shapes cannot reach the review path. Each of them used to
# raise out of it — setdefault hands back an existing null rather than
# replacing it — and a raise there is charged as a failed attempt, so three
# polls later every open pull request carried a give-up comment.
for _label, _stanza in (("nulls the filesystem", dict(_good, filesystem=None)),
                        ("nulls the deny list",
                         dict(_good, filesystem={"denyWrite": None})),
                        ("makes the deny list a string",
                         dict(_good, filesystem={
                             "denyWrite": "~/.vinegar-checkouts"}))):
    _sent = _built_with(_stanza)["sandbox"]
    check("a settings file that %s still sends an enabled sandbox" % _label,
          _sent.get("enabled") is True, json.dumps(_sent))
    check("a settings file that %s still denies the checkout" % _label,
          vinegar.CHECKOUT_DIR in _sent["filesystem"]["denyWrite"],
          json.dumps(_sent["filesystem"]))
# Every key that can turn confinement off is set here, so one left in the
# file cannot ride along beside three keys that still read true, true,
# false. Checking only the top level accepted a `filesystem.allowWrite`,
# which hands a hand-run the whole disk while the daemon looks fine — and
# the README said such a key was refused, so the file was the one telling
# the truth about a promise the code did not keep.
_said = _sending(dict(_good, filesystem={"allowWrite": ["/"]}))
check("a widening key nested under filesystem is refused",
      "sandbox.filesystem.allowWrite" in _said, _said)
_said = _sending(dict(_good, filesystem={"denyWrite": [], "denyRead": ["/"]}))
check("any other filesystem key is refused too",
      "sandbox.filesystem.denyRead" in _said, _said)
# The network rule is pinned rather than assumed. Measured: with no
# network key the reviewer already has none — `gh` gets Forbidden — but
# that was a default nothing stated, and a release that changed it would
# reopen the network while the brief still promised it was closed.
_sent = _built_with(_good)["sandbox"]
check("the settings sent pin the network closed",
      _sent.get("network") == {"allowedDomains": []},
      json.dumps(_sent.get("network")))
_said = _sending(dict(_good, network={"allowedDomains": ["api.github.com"]}))
check("a file that opens a domain is refused",
      "sandbox.network" in _said, _said)

# The read side is re-checked on the same schedule the write side is
# rebuilt on. Validating once at startup and forwarding blindly per review
# left the deny rule guarding the App private key as whatever the file
# said on the next poll — the failure this function exists to end, on the
# half of the file it was not applied to.
_said = _sending(_good, permissions={"allow": [vinegar.REPORT_TOOL],
                                     "deny": []})
check("a review is refused when the file has dropped the key deny rule",
      "must deny" in _said, _said)
_said = _sending(_good, permissions={"allow": [], "deny": [vinegar.DENY_HOME]})
check("a review is refused when the file no longer allows the report tool",
      "does not allow" in _said, _said)
# A list collapsed to the string it held is truthy, so `or []` kept it and
# the membership test became a substring test that passes: the key deny
# rule reads as present while Claude Code has a malformed value.
_said = _sending(_good, permissions={"allow": [vinegar.REPORT_TOOL],
                                     "deny": vinegar.DENY_HOME})
check("a deny list collapsed to a string is not read as containing itself",
      "must deny" in _said, _said)
_said = _sending(_good, permissions={"allow": vinegar.REPORT_TOOL,
                                     "deny": [vinegar.DENY_HOME]})
check("an allow list collapsed to a string is refused the same way",
      "does not allow" in _said, _said)
# `permissions` itself, for the same reason and with the same treatment. It
# is guarded inside the try either way, so the difference is which sentence
# the operator gets: the one naming the missing rule, or a parse error that
# describes a file which parsed perfectly well.
_said = _sending(_good, permissions=vinegar.REPORT_TOOL)
check("a permissions block that is not an object names the missing rule",
      "does not allow" in _said, _said)
# Malformed JSON must be a sentence, not an exception out of review(). Left
# to raise, handle_pr recorded FAILED with the attempt already charged, and
# three polls later every open pull request carried a give-up comment and
# was abandoned for good over a trailing comma.
_said = _sending(None, raw='{"permissions": {"allow": ["X"],}}')
check("a settings file saved mid-edit is refused with a sentence",
      "cannot be read" in _said, _said)
_said = _sending(None, raw='["not", "an", "object"]')
check("a settings file that is not an object is refused with a sentence",
      "cannot be read" in _said, _said)
# A key Vinegar does not send would change what a hand-run does and
# nothing else, so the file would describe something the program does not
# do. Measured: `network` is one of those — allowing domains routes the
# reviewer through a TLS-terminating proxy that `gh` will not trust.
_said = _sending(dict(_good, credentials={"files": []}))
check("a sandbox key Vinegar does not send is refused",
      "does not send" in _said, _said)

# The path the kernel judges the write by, not the one that was typed.
# Measured against the real binary: a sandbox given only the symlink path
# let `git show --output=` create the file inside it.
_link_root = os.path.join(_home, "linked-checkouts")
os.makedirs(os.path.join(_home, "real-checkouts"), exist_ok=True)
if not os.path.islink(_link_root):
    os.symlink(os.path.join(_home, "real-checkouts"), _link_root)
_cd = vinegar.CHECKOUT_DIR
vinegar.CHECKOUT_DIR = _link_root
try:
    _linked = json.loads(vinegar.reviewer_settings(_link_root))["sandbox"][
        "filesystem"]["denyWrite"]
finally:
    vinegar.CHECKOUT_DIR = _cd
# realpath on the expectation too: the temp root this suite runs under is
# itself reached through a symlink on macOS (/var/folders -> /private/...),
# so the literal join would differ from what the kernel resolves to for a
# reason that has nothing to do with what is being checked.
check("a symlinked checkout is denied by the path it resolves to",
      os.path.realpath(os.path.join(_home, "real-checkouts")) in _linked,
      _linked)

# And the workspace itself, which is one level below CHECKOUT_DIR and is
# where the review actually runs. Denying only the parent missed the
# shortcut that moves one large clone to another disk: the workspace then
# resolves outside every denied entry, the sandbox's workspace grant
# applies, and `.git/config` is writable again — which buys the next poll,
# since Vinegar runs reset, clean and checkout there unsandboxed.
_real_repo = os.path.join(_home, "elsewhere", "o__r")
os.makedirs(_real_repo, exist_ok=True)
_linked_repo = os.path.join(vinegar.CHECKOUT_DIR, "linked__repo")
os.makedirs(vinegar.CHECKOUT_DIR, exist_ok=True)
if not os.path.islink(_linked_repo):
    os.symlink(_real_repo, _linked_repo)
_sent = json.loads(vinegar.reviewer_settings(_linked_repo))["sandbox"]
check("the workspace itself is denied, not only the directory above it",
      _linked_repo in _sent["filesystem"]["denyWrite"],
      json.dumps(_sent["filesystem"]))
check("a symlinked workspace is denied by the path it resolves to",
      os.path.realpath(_real_repo) in _sent["filesystem"]["denyWrite"],
      json.dumps(_sent["filesystem"]))

# What these checks cannot reach, said plainly rather than left implied.
# They prove what Vinegar sends. Whether `sandbox.filesystem.denyWrite`
# actually beats the sandbox's own workspace-writable grant is a fact
# about Claude Code, and the only honest way to learn it is to run one —
# which costs money and needs a login, so it stays out of the suite.
#
# Measured by hand on Claude Code 2.1.221, macOS 26.6, and worth
# repeating whenever that version moves. From a git repository inside a
# directory the emitted denyWrite names:
#
#   claude -p 'Run exactly: git show -s --format=%B --output=./x.txt HEAD' \
#     --settings "$(python3 -c 'import vinegar, os;
#         print(vinegar.reviewer_settings(os.getcwd()))')" \
#     --setting-sources "" --strict-mcp-config --model claude-haiku-4-5
#
# Then: `git log` exits 0, the write exits 128 with "Operation not
# permitted", and no file appears — inside the checkout or in $HOME.
# Use a repository whose commit messages are innocuous, or the model
# refuses the command on its own and measures nothing.

# And the key has to be somewhere that rule reaches. check_paths argues
# the key is safe because HOME carries the denied component; the key's
# own location is a separate setting, free to point anywhere.
_loose_key = os.path.join(_home, "loose-key.pem")
with open(_loose_key, "w") as h:
    h.write("not really a key\n")
os.makedirs(os.environ["VINEGAR_HOME"], exist_ok=True)
_covered_key = os.path.join(os.environ["VINEGAR_HOME"], "covered-key.pem")
with open(_covered_key, "w") as h:
    h.write("not really a key\n")


def _config_with_key(where):
    path = os.path.join(_home, "app-config.json")
    with open(path, "w") as handle:
        json.dump({"repos": ["o/r"],
                   "github_app": {"app_id": 1, "private_key": where}}, handle)
    try:
        vinegar.load_config(path)
        return "started"
    except SystemExit as err:
        return str(err)


check("a private key a review could read refuses to start",
      "review can read" in _config_with_key(_loose_key),
      _config_with_key(_loose_key))
check("a private key under the denied component is accepted",
      _config_with_key(_covered_key) == "started",
      _config_with_key(_covered_key))

# The numbers are read as numbers. A hand-edited string sails past every
# other check and then raises inside checkout_grace on every pull request
# on every poll, with nothing reviewed and no give-up ever announced.
def _config_with(**over):
    path = os.path.join(_home, "num-config.json")
    with open(path, "w") as handle:
        json.dump(dict({"repos": ["o/r"]}, **over), handle)
    try:
        vinegar.load_config(path)
        return "started"
    except SystemExit as err:
        return str(err)


check("a timeout given as a string refuses to start",
      "whole number of seconds" in _config_with(review_timeout="1800"),
      _config_with(review_timeout="1800"))
check("a zero poll interval refuses to start",
      "greater than zero" in _config_with(poll_interval=0),
      _config_with(poll_interval=0))
check("a boolean is not a number here either",
      "whole number" in _config_with(max_changed_lines=True),
      _config_with(max_changed_lines=True))
check("the numbers as numbers still start",
      _config_with(poll_interval=30, review_timeout=900,
                   max_changed_lines=100) == "started")
# The effort is not validated again after this: it goes into the prompt as
# `/code-review <effort> <number>`, so a value the slash command does not
# know is not refused anywhere downstream. It reviews at whatever the
# command falls back to and reports the effort the operator asked for, and
# the two are then different for every review until someone reads a
# transcript closely. EFFORTS is the only thing standing between the two.
check("an effort the review command does not know refuses to start",
      "effort must be one of" in _config_with(effort="ultra"),
      _config_with(effort="ultra"))
check("every effort the config allows still starts",
      all(_config_with(effort=e) == "started" for e in vinegar.EFFORTS),
      [e for e in vinegar.EFFORTS if _config_with(effort=e) != "started"])
# Said at startup rather than discovered on the token bill. Past this the
# checkout and review together ask for a whole token's life, no cached token
# can satisfy that, and every call mints a new one. Said and not refused,
# because it is waste rather than breakage: a daemon that will not start is
# worse than one that mints too often, and refusing took the deploy of its
# own change down for exactly that reason.
_cap = vinegar.TOKEN_LIFE - vinegar.CHECKOUT_GRACE
_APP_CFG = {"app_id": 1, "private_key": _covered_key}


def _cap_warning(app=True, **over):
    """What load_config says for this config, and whether it started.

    The patch is undone in a finally, and the sink is local. Restored with
    a bare assignment, a load_config that raised anything but SystemExit —
    a TypeError out of the message's own %-format, which is exactly what
    these checks are for — would leave `log` patched for the rest of the
    file, pointed at a name other blocks rebind to a string. The failure
    then surfaces two thousand lines away with a traceback naming the
    wrong function. `_refuses()` below already has this shape.

    An App by default, because without one github_env() returns before
    installation_token() and there is nothing to mint or to warn about.
    """
    settings = dict(over, github_app=_APP_CFG) if app else dict(over)
    said = []
    keep = vinegar.log
    vinegar.log = lambda message: said.append(message)
    try:
        return _config_with(**settings), said
    finally:
        vinegar.log = keep


# Over, exactly on, and under. The boundary is the case worth having: the
# comparison is `>=` because the cache condition is a strict `<`, so a sum
# of exactly a token's life already fails it. Straddling the boundary
# without landing on it lets `>=` weaken to `>` with every check still
# green, which is the one configuration that would then start in silence.
_over_start, _over_said = _cap_warning(review_timeout=_cap + 500)
_at_start, _at_said = _cap_warning(review_timeout=_cap)
_under_start, _under_said = _cap_warning(review_timeout=_cap - 1)
_noapp_start, _noapp_said = _cap_warning(app=False, review_timeout=_cap + 500)
check("a review_timeout over the cap still starts, rather than refusing",
      _over_start == "started", _over_start)
check("a review_timeout over the cap says so at startup",
      any("mints a fresh one" in m for m in _over_said), _over_said)
check("a review_timeout exactly on the cap says so as well",
      _at_start == "started" and any("mints a fresh one" in m
                                     for m in _at_said),
      (_at_start, _at_said))
# The remedy clause verbatim, not the number loose in the message. `_cap`
# appears in the echoed setting too, and in the interpolated temp path,
# so a bare substring match passes with the remedy deleted.
check("the warning names the value that would fix it",
      any("Set it under %d" % _cap in m for m in _over_said), _over_said)
check("the largest review_timeout inside the cap still starts",
      _under_start == "started", _under_start)
check("a review_timeout inside the cap says nothing",
      not _under_said, _under_said)
# Nothing mints without an App, so the warning would name a cost that
# cannot be incurred, on the configuration the README ships.
check("no App configured means the cap is not worth mentioning",
      _noapp_start == "started" and not _noapp_said,
      (_noapp_start, _noapp_said))
# The bound the downgraded refusal used to provide incidentally. One review
# holds the only poll thread, so an extra zero parks the daemon for hours
# while the watchdog reads the pid and calls it healthy.
check("an absurd review_timeout refuses to start",
      "at most" in _config_with(
          review_timeout=vinegar.MAX_REVIEW_TIMEOUT + 1),
      _config_with(review_timeout=vinegar.MAX_REVIEW_TIMEOUT + 1))
check("the longest review_timeout allowed still starts",
      _cap_warning(review_timeout=vinegar.MAX_REVIEW_TIMEOUT)[0] == "started",
      _cap_warning(review_timeout=vinegar.MAX_REVIEW_TIMEOUT)[0])


def _refuses(**over):
    """Whether check_paths exits with these module paths in place."""
    keep = {name: getattr(vinegar, name) for name in over}
    for name, value in over.items():
        setattr(vinegar, name, value)
    try:
        vinegar.check_paths()
        return False
    except SystemExit:
        return True
    finally:
        for name, value in keep.items():
            setattr(vinegar, name, value)


# The checkout must not sit where the reviewer is denied every read: it
# would review from API fetches instead, worse and more expensive, while
# permission_denials stays empty and the log reads healthy.
check("a checkout inside the denied component refuses to start",
      _refuses(CHECKOUT_DIR="/Users/x/.vinegar/checkouts"))
check("a checkout whose case only differs still refuses",
      _refuses(CHECKOUT_DIR="/Users/x/.Vinegar/checkouts"))
# And the direction that exposes the App private key: HOME has to carry
# the component the sandbox denies, or nothing stops a review reading the
# one credential that is not scoped to a single repository.
check("a home the sandbox does not cover refuses to start",
      _refuses(HOME="/Users/x/vinegar-home"))
check("the paths as configured are still accepted",
      not _refuses())
check("the environment asks for the tool contract",
      (claude_run.env or {}).get("CLAUDE_CODE_REPORT_FINDINGS") == "1",
      sorted(claude_run.env or {})[:5])

# The reviewer is given no GitHub credential, and this is a leak rather
# than tidiness. The environment handed to review() is the one checkout()
# used, so it carries the App installation token, and enabling the sandbox
# stopped the allow list gating Bash: measured, `env` is refused without
# the sandbox and runs with it. A reviewer reading an attacker-authored
# branch could print the token, and finding text reaches the pull request
# verbatim.
_tokened = {"GH_TOKEN": "ghs_installation_token",
            "GITHUB_TOKEN": "ghp_operator_token", "PATH": "/usr/bin"}
del posted[:]
# Passed by reference deliberately: handing over a copy would make the
# untouched-caller check below unable to fail, which is what it did first.
vinegar.review(ROOT, "o/r", PR, CONFIG, _tokened, {})
check("the reviewer is not given the installation token",
      "GH_TOKEN" not in (claude_run.env or {}), sorted(claude_run.env or {}))
check("nor an operator's own GitHub token",
      "GITHUB_TOKEN" not in (claude_run.env or {}),
      sorted(claude_run.env or {}))
check("the rest of the environment still reaches the reviewer",
      (claude_run.env or {}).get("PATH") == "/usr/bin"
      and (claude_run.env or {}).get("CLAUDE_CODE_REPORT_FINDINGS") == "1",
      sorted(claude_run.env or {}))
# And the caller's own copy is untouched, because posting_env() falls back
# to it when minting a fresh token fails — the path that gets a finished
# review posted during a GitHub blip.
check("removing it from the reviewer does not disarm the posting fallback",
      _tokened.get("GH_TOKEN") == "ghs_installation_token", _tokened)
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
fake_run.post_err = "HTTP 502 Bad Gateway"
fake_run.look_out = ""
del posted[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("the killed notice is retried when the failure is transient",
      len(posted) == 2, len(posted))
fake_run.rc = 0
fake_run.post_err = "HTTP 422"


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
check("the posting timeout is short enough to be worth having",
      vinegar.POST_TIMEOUT and vinegar.POST_TIMEOUT <= 300,
      vinegar.POST_TIMEOUT)

# --- token life ----------------------------------------------------------
# A review runs for the best part of an hour on a token that lives an hour,
# so the token the run started on can have seconds left by the time there is
# a review to post. `good_for` is what stops that: the caller says how much
# life it needs and a token with less is replaced before it is handed over.
# Two commits are about this and neither left a check, so the whole argument
# could be deleted with the suite green.
reset_stubs()
APP = {"app_id": 1, "private_key": "/dev/null"}

# The signing openssl is the one subprocess in the file that does not go
# through run(), so it is invisible to anyone auditing run()'s callers, and
# it ran unbounded. A private key on a mount that stops answering parks it in
# the kernel, and the one poll thread with it, while the watchdog reads a
# live pid as healthy. Swapping the module is how the call is reachable at
# all from here; app_jwt takes no injection point.
_openssl = []


class FakeSubprocess(object):
    """Enough of the module for app_jwt, recording what it was handed."""
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self, hang=False):
        self.hang = hang

    def run(self, cmd, **kw):
        _openssl.append(kw.get("timeout"))
        if self.hang:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return subprocess.CompletedProcess(cmd, 0, b"signature", b"")


_real_subprocess = vinegar.subprocess
vinegar.subprocess = FakeSubprocess()
vinegar.app_jwt(1, "/dev/null")
check("the signing openssl is bounded like everything else on the thread",
      _openssl == [vinegar.DIFF_TIMEOUT], _openssl)
vinegar.subprocess = FakeSubprocess(hang=True)
try:
    vinegar.app_jwt(1, "/dev/null")
    _sign_hung = "returned normally"
except subprocess.TimeoutExpired:
    _sign_hung = "TimeoutExpired escaped"
except RuntimeError as err:
    _sign_hung = str(err)
vinegar.subprocess = _real_subprocess
# Converted, not raised through: installation_token's caller logs "cannot
# mint a token" and a bare TimeoutExpired there names neither openssl nor
# the key, which is the file the operator has to go and look at.
check("a signing that hangs says openssl and names the key",
      "openssl" in _sign_hung and "/dev/null" in _sign_hung, _sign_hung)

_real_jwt, _real_api = vinegar.app_jwt, vinegar.github_api


def stub_app_jwt(app_id, key_path):
    return "jwt"


def stub_github_api(path, jwt, payload=None):
    if path.endswith("/installation"):
        return {"id": 7}
    return {"token": "fresh"}


vinegar.app_jwt, vinegar.github_api = stub_app_jwt, stub_github_api
# Thirty seconds of life: plenty for a caller that is about to make one
# call, useless for one that is about to spend five minutes posting.
_short = {"o/r": ("stale", time.time() + 30)}
check("a token with life left is reused rather than minted again",
      vinegar.installation_token(APP, "o/r", dict(_short)) == "stale")
check("a token that would expire mid-post is replaced before it is handed on",
      vinegar.installation_token(APP, "o/r", dict(_short),
                                 good_for=300) == "fresh")
check("an expired token is replaced whatever the caller asked for",
      vinegar.installation_token(
          APP, "o/r", {"o/r": ("stale", time.time() - 1)}) == "fresh")
vinegar.app_jwt, vinegar.github_api = _real_jwt, _real_api

_asked = []
_real_env = vinegar.github_env


def recording_env(config, repo, cache, good_for=0):
    _asked.append(good_for)
    return None


vinegar.github_env = recording_env
vinegar.posting_env(L, dict(CONFIG, github_app=APP), "o/r", {}, None)
check("the posting mints a token with time to finish posting",
      _asked == [vinegar.POST_GRACE], _asked)
# The grace has to be worth asking for. Left at nothing, the argument is
# threaded through three call sites and changes no decision anywhere.
check("the posting grace is long enough to cover a slow post",
      vinegar.POST_GRACE >= vinegar.POST_TIMEOUT, vinegar.POST_GRACE)
# One token covers the checkout and the review, and the checkout runs first,
# so the grace has to outlast what the checkout can actually spend. The head
# fetch alone is FETCH_TIMEOUT; a grace under that hands the review a token
# that died during the fetch it was minted to survive.
check("the checkout grace outlasts the fetch it has to survive",
      vinegar.CHECKOUT_GRACE >= vinegar.FETCH_TIMEOUT, vinegar.CHECKOUT_GRACE)
# And the sum stays inside a token's life, or the cache can never serve one
# and every call mints a fresh token. Nothing fails when it does: it is
# silent, and it once ran at about 1440 tokens a day per open pull request.
check("the checkout and review together stay inside a token's life",
      vinegar.checkout_grace(CONFIG) < vinegar.TOKEN_LIFE,
      (vinegar.checkout_grace(CONFIG), vinegar.TOKEN_LIFE))
vinegar.github_env = _real_env

# --- reviewer_brief ------------------------------------------------------
brief = vinegar.reviewer_brief(PR)
check("brief names the pull request's real base",
      "git diff refs/heads/release-2...HEAD" in brief, brief)
check("brief tells the reviewer not to fall back to main",
      "do not assume `main`" in brief, brief)
check("brief names the reporting tool, not a competing format",
      "ReportFindings" in brief and "```json" not in brief, brief)
# The remote-tracking ref, which `gh repo clone` leaves behind whether or
# not the base fetch later succeeded. Sending the reviewer to `HEAD~1`
# instead was wrong on any pull request with more than one commit: it
# would report that it could not establish the scope while the right ref
# sat in the clone, and a full review budget bought nothing.
check("brief gives a fallback for a base ref that does not resolve",
      "refs/remotes/origin/release-2...HEAD" in brief, brief)
check("brief does not promise the base ref is definitely there",
      "already fetched" not in brief, brief)
# The old fallback was `gh pr diff`, and under the sandbox it cannot run:
# measured, `gh` gets "Forbidden" with the network closed and a TLS trust
# failure when domains are allowed, because the proxy terminates TLS. A
# reviewer told to reach for it spends turns discovering that and then has
# no scope at all.
check("brief does not send the reviewer to a command with no network",
      "gh pr diff" not in brief, brief)
check("brief says the network is closed rather than leaving it to be found",
      "no network" in brief, brief)

# --- handle_pr: the guard that bounds every crash --------------------------
reset_stubs()
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


def _skipped(pr_over=None, config_over=None):
    """What skip_reason says about PR_LIVE with these changes made to it."""
    return vinegar.skip_reason(dict(PR_LIVE, **(pr_over or {})),
                               dict(CONFIG, **(config_over or {})))


# handle_pr below reaches skip_reason, but only ever with a draft, so the
# draft gate was the only one of these that could fail. Each of the others
# is a way to spend a review the operator said not to spend: a fork's head
# is attacker-controlled content, `authors` is the allow list that bounds
# who can cost money, and the size cap is what stops one 40000-line branch
# costing more than a day of ordinary reviews. None of that is recoverable
# after the fact, because the money is spent by the time anyone looks.
check("a draft is skipped while drafts are skipped",
      _skipped({"isDraft": True}) == "draft", _skipped({"isDraft": True}))
check("a draft is reviewed once drafts are not skipped",
      _skipped({"isDraft": True}, {"skip_drafts": False}) is None,
      _skipped({"isDraft": True}, {"skip_drafts": False}))
check("a pull request from a fork is skipped while forks are skipped",
      "fork" in (_skipped({"isCrossRepository": True}) or ""),
      _skipped({"isCrossRepository": True}))
check("a bot's pull request is skipped while bots are skipped",
      "bot" in (_skipped({"author": {"login": "dependabot",
                                     "is_bot": True}}) or ""),
      _skipped({"author": {"login": "dependabot", "is_bot": True}}))
check("an author outside the authors list is skipped",
      "authors list" in (_skipped(None, {"authors": ["someone"]}) or ""),
      _skipped(None, {"authors": ["someone"]}))
check("an author inside the authors list is reviewed",
      _skipped(None, {"authors": ["kevin"]}) is None,
      _skipped(None, {"authors": ["kevin"]}))
# additions plus deletions, not either alone: a branch that rewrites a file
# in place is 20000 changed lines and 0 net, and reading one side only lets
# it through the cap it is exactly the shape of.
check("a branch over the changed-lines cap is skipped",
      "over the" in (_skipped({"additions": 2000, "deletions": 2000}) or ""),
      _skipped({"additions": 2000, "deletions": 2000}))
check("a branch under the changed-lines cap is reviewed",
      _skipped({"additions": 2000, "deletions": 999}) is None,
      _skipped({"additions": 2000, "deletions": 999}))
# A deleted account leaves no author object at all, and reading .get on it
# is what turns "skip this one" into a traceback on every poll.
#
# Wrapped, because that traceback is the failure. Called plainly, breaking
# the guard ends the run here rather than failing this check, and the 163
# checks below never run: measured, 221 of 384. Every other raise-guard
# check in this file is wrapped for the same reason, and this one, whose
# own comment says the failure is a raise, was not.
try:
    _gone_author = _skipped({"author": None})
except Exception as err:
    _gone_author = "raised %r" % err
check("a pull request whose author is gone is still judged, not raised",
      _gone_author is None, _gone_author)


def blowing_up_review(*a, **k):
    raise AttributeError("'list' object has no attribute 'get'")


vinegar.review = blowing_up_review
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _hp_state, {})
check("a review that raises is still recorded, so it cannot loop for ever",
      _hp_state.get(L, {}).get("outcome") == vinegar.FAILED, _hp_state)

# The marker handle_pr writes before the review runs, and the outcome it
# writes after, leave a window: a process killed in between records
# FAILED for a review that did post. The retry must ask before posting a
# complete second review over the first.
_cw_real = vinegar.review
vinegar.review = _real_review
_cw_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": 1}}
vinegar.run = claude_run
claude_run.stream = stream(call(FINDINGS[:1]), result_event())
fake_run.look_out = "%s reviewed `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _cw_state, {})
check("a retry after a crash does not post the review twice",
      not posted, (len(posted), _cw_state))
fake_run.look_out = ""
vinegar.run, vinegar.review = fake_run, _cw_real
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
del _asked[:]
_env_at_give_up = vinegar.github_env
vinegar.github_env = recording_env
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_state, {})
vinegar.github_env = _env_at_give_up
check("a give-up interrupted by a crash is announced on restart",
      len(posted) == 1 and "gave up on" in posted[0][1]["body"],
      (len(posted), _ga_state))
# This branch mints its own token rather than reusing one, because there is
# no run to inherit from: the attempt that spent the budget was killed. A
# token with no life asked of it can expire between the mint and the post,
# and then the one thing this path exists to say never gets said.
check("the rediscovered give-up mints a token with time to post it",
      _asked and all(g == vinegar.POST_GRACE for g in _asked), _asked)

vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_state, {})
check("the crash-discovered give-up is announced once, not every poll",
      len(posted) == 1 and _ga_state[L].get("announced") is True,
      (len(posted), _ga_state))

# The crash this branch exists for kills the process between posting and
# recording, so `announce_tries` is 0 on the very run that must not post
# again. Trusting the count skipped the check exactly there.
_gc_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
fake_run.look_out = "%s gave up on `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gc_state, {})
check("a give-up already up is not repeated after a crash",
      not posted, (len(posted), _gc_state))

# The give-up writes no marker, so anything it forgets on its way out can
# only be somebody else's — a saved review still waiting to be sent.
_gu_marker = vinegar.unposted_path("o/r", PR)
os.makedirs(vinegar.REVIEW_DIR, exist_ok=True)
with open(_gu_marker, "w") as h:
    h.write("%s\n" % PR["headRefOid"])
fake_run.look_out = ""
vinegar.give_up(L, "o/r", PR, CONFIG, vinegar.MAX_ATTEMPTS, {})
check("a give-up does not delete another review's mark",
      os.path.exists(_gu_marker), _gu_marker)
vinegar.forget(_gu_marker)
fake_run.look_out = ""

# A rate limit refuses the give-up without judging it, so spending an
# attempt on it marked the entry announced after three throttled polls
# and the pull request was never told anything at all.
_ga_throttle = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                    "attempts": vinegar.MAX_ATTEMPTS}}
fake_run.rc = 1
fake_run.post_err = "gh: API rate limit exceeded (HTTP 403)"
for _ in range(vinegar.MAX_ATTEMPTS):
    vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_throttle, {})
check("a rate-limited give-up does not spend its budget",
      _ga_throttle[L].get("announced") is not True
      and _ga_throttle[L].get("announce_tries") is None, _ga_throttle)
for _ in range(vinegar.MAX_ATTEMPTS * 2):
    vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ga_throttle, {})
check("a give-up throttled for ever still ends",
      _ga_throttle[L].get("announced") is True, _ga_throttle)
fake_run.rc, fake_run.post_err = 0, "HTTP 422"

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

# A dry run must not tell the live daemon the give-up was already said:
# they share state.json, and the daemon would then never post it.
_gd_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS}}
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, dict(CONFIG, comment=False), _gd_state, {})
check("a dry run does not record the give-up as announced",
      not posted and _gd_state[L].get("announced") is not True, _gd_state)

# It must not come back to say it again either. A dry run has nothing to
# announce, so the discovery branch is not entered at all; entering it
# logged the give-up afresh on every poll, for ever, which is the loop the
# budget exists to bound. Watched through the log, because with nothing
# posted that is the only trace the branch leaves.
_gd_log = []
vinegar.log = lambda m: _gd_log.append(m)
for _ in range(5):
    vinegar.handle_pr("o/r", PR_LIVE, dict(CONFIG, comment=False),
                      _gd_state, {})
vinegar.log = lambda message: None
check("a dry run's give-up does not re-announce on every poll",
      not [m for m in _gd_log if "leaving it alone" in m or "gave up" in m],
      _gd_log[:3])

# And the in-process give-up, which a dry run does reach: it writes its
# transcript, and nothing about the announcement, because that state is
# shared with the live daemon.
_di_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": vinegar.MAX_ATTEMPTS - 1}}
_di_review, _di_checkout = vinegar.review, vinegar.checkout
vinegar.review = lambda *a, **k: vinegar.FAILED
vinegar.checkout = lambda repo, pr, env: ROOT
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, dict(CONFIG, comment=False), _di_state, {})
check("a dry run's own give-up leaves the announce state alone",
      not posted and _di_state[L].get("announced") is None
      and _di_state[L].get("announce_tries") is None, _di_state)
vinegar.review, vinegar.checkout = _di_review, _di_checkout
del posted[:]
vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _gd_state, {})
check("the live daemon still announces after a dry run looked",
      len(posted) == 1, (len(posted), _gd_state))

check("a dry run's give-up counts as said, not as a silence",
      vinegar.post_review(L, "o/r", PR, ROOT, "", None,
                          dict(CONFIG, comment=False), None)
      == vinegar.POSTED)

# A checkout that keeps failing is retried, deliberately, but it must not
# say so once a minute for ever: the failures here are mostly transient and
# retrying costs no subscription, so the noise is what was worth fixing.
_ck_state = {L: {"outcome": vinegar.FAILED, "sha": PR["headRefOid"],
                 "attempts": 2}}
_ck_log = []
vinegar.review = lambda *a, **k: vinegar.DONE
vinegar.save_state = lambda st: None
vinegar.github_env = lambda *a, **k: None


def broken_checkout(repo, pr, env):
    raise RuntimeError("could not clone")


vinegar.checkout = broken_checkout
vinegar.log = lambda m: _ck_log.append(m)
for _ in range(4):
    vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ck_state, {})
vinegar.log = lambda message: None
check("a failing checkout is reported once, not once a poll",
      len([m for m in _ck_log if "could not clone" in m]) == 1, _ck_log)
# But not silent for ever. A checkout that fails permanently is exempt
# from MAX_ATTEMPTS on purpose, so nothing else ever mentions the pull
# request again and one line from weeks ago reads as a verdict.
del _ck_log[:]
vinegar.log = lambda m: _ck_log.append(m)
for _ in range(12):
    vinegar.handle_pr("o/r", PR_LIVE, CONFIG, _ck_state, {})
vinegar.log = lambda message: None
check("a checkout that stays broken is mentioned again",
      any("still true after 10 polls" in m for m in _ck_log), _ck_log[:2])
check("a failing checkout is still retried rather than abandoned",
      _ck_state[L]["outcome"] == "checkout", _ck_state)
# The mirror of "a skip keeps the attempts burned at this head". A bare
# entry here hands the budget back, and a flapping clone then re-reviews
# at full cost on every other poll for ever.
check("a failing checkout keeps the attempts burned at this head",
      _ck_state[L].get("attempts") == 2, _ck_state)

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

# A file open() cannot read at all is the same outage, and the narrow
# catch did not cover it. A directory in its place rather than chmod 0:
# root ignores the mode bits, so under sudo that test failed against
# perfectly correct code, and this repository's own docs describe a
# ~/.vinegar left root-owned by a sudo run as a thing that happens.
if os.path.exists(vinegar.STATE_PATH):
    os.remove(vinegar.STATE_PATH)
os.makedirs(vinegar.STATE_PATH)
try:
    _st = vinegar.load_state()
except Exception as err:
    _st = "raised %s" % type(err).__name__
check("a state file that cannot be opened is quarantined too",
      _st == {} and len(_quarantined()) == 4, (_st, _quarantined()))
for _f in _quarantined():
    _aside = os.path.join(os.path.dirname(vinegar.STATE_PATH), _f)
    shutil.rmtree(_aside) if os.path.isdir(_aside) else os.remove(_aside)

# An entry that is not an object is the same crash one level down, and it
# is what the give-up log's own "delete its entry" advice invites.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": "reviewed", "o/r#13": {"outcome": "reviewed"}}')
_mixed = vinegar.load_state()
check("an entry that is not an object is dropped, not kept to crash",
      _mixed == {"o/r#13": {"outcome": "reviewed"}}, _mixed)

# A counter that is not a whole number is not a counter: `attempts` is
# compared with >= on every poll, and the hand-edit this file's own log
# recommends is exactly where a string one comes from.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": {"outcome": "failed", "sha": "a", "attempts": "3"},'
            ' "o/r#13": {"outcome": "failed", "sha": "a", '
            '"announce_tries": true},'
            ' "o/r#14": {"outcome": "failed", "sha": "a", "attempts": 2}}')
_typed = vinegar.load_state()
check("an attempts that is not a number is dropped",
      "o/r#12" not in _typed and "o/r#13" not in _typed
      and _typed.get("o/r#14", {}).get("attempts") == 2, _typed)

# Every counter, not the two that existed when the check was written:
# post_tries is compared with < on the poll path the same way.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": {"outcome": "reviewed", "sha": "a", '
            '"post_tries": "1"}}')
check("a post_tries that is not a number is dropped too",
      vinegar.load_state() == {}, vinegar.load_state())

# The flags are hand-edited too, and a bare truthiness test reads
# `"announced": "no"` as yes — the opposite of what someone writing that
# means, and a pull request never told Vinegar gave up.
with open(vinegar.STATE_PATH, "w") as h:
    h.write('{"o/r#12": {"outcome": "failed", "sha": "a", '
            '"announced": "no"},'
            ' "o/r#13": {"outcome": "reviewed", "sha": "a", '
            '"unposted": "false"},'
            ' "o/r#14": {"outcome": "reviewed", "sha": "a", '
            '"announced": true}}')
_flags = vinegar.load_state()
check("a flag that is not a boolean is dropped",
      "o/r#12" not in _flags and "o/r#13" not in _flags
      and _flags.get("o/r#14", {}).get("announced") is True, _flags)
check("dropping a bad entry does not quarantine the good ones",
      not _quarantined(), _quarantined())
os.remove(vinegar.STATE_PATH)

# --- a resend must not duplicate a review that already landed --------------
reset_stubs()
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

# The anchor-stripping retry sends no inline comments, so the line that
# says how many were posted must not report the ones it just removed.
_il_logged = []
_il_sent = []


def refuse_then_take(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    """Refuse the anchored post, take the anchorless retry."""
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        _il_sent.append(cmd)
        fake_run.rc = 0 if len(_il_sent) > 1 else 1
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.log = lambda m: _il_logged.append(m)
vinegar.run = refuse_then_take
fake_run.look_out = ""
fake_run.post_err = "HTTP 422"
del posted[:]
vinegar.post_review(L, "o/r", PR, ROOT, "x", FINDINGS[:1], CONFIG, None)
vinegar.log = lambda message: None
check("the anchorless retry does not claim inline comments it removed",
      any("posted 0 inline comment(s)" in m for m in _il_logged)
      and not any("posted 1 inline comment(s)" in m for m in _il_logged),
      [m for m in _il_logged if "inline comment" in m])
# Back to what the checks below were left expecting: a refused post that
# is ambiguous, and no review already up.
vinegar.run = claude_run
fake_run.rc, fake_run.post_err = 1, "HTTP 502"

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
check("a definite refusal never asks whether it landed",
      len(looked) == 0, (len(posted), len(looked)))
check("a definite refusal of a clean review is not resent",
      len(posted) == 1, len(posted))

# With inline comments there *is* something to change, so the anchorless
# retry still runs, and still without the read a 4xx already answered.
claude_run.stream = stream(call(FINDINGS[:1]), result_event())
del posted[:]
del looked[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a refused anchor is still retried without the read",
      len(posted) == 2 and len(looked) == 0
      and "comments" in posted[0][1] and "comments" not in posted[1][1],
      (len(posted), len(looked)))
claude_run.stream = stream(call([]), result_event())

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
# THROTTLED, not False: a limit refuses without judging the request and
# lifts on its own clock, so a caller counting failures against a budget
# has to be able to tell the two apart.
check("a rate-limited post is not retried in the same millisecond",
      len(_tries) == 1 and _rl == vinegar.THROTTLED, (len(_tries), _rl))
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

# A run that fails has still spent the money. The failing path returned
# before the only line that says what the daemon costs, so the runs that
# bought nothing were the ones that never reported a price. Seen live: a
# 529 after eight and a half minutes of xhigh, with no cost in the log.
claude_run.stream = stream(result_event(is_error=True, result="529 boom",
                                        total_cost_usd=2.5))
del logged[:]
check("a failed review is retried and its cost still reported",
      vinegar.review(ROOT, "o/r", PR, CONFIG, None, {}) == vinegar.FAILED
      and any("2.50 USD" in m for m in logged),
      [m for m in logged if "failed" in m])

# A run that failed with findings already reported must not also announce
# itself as completed: the cost would be counted twice by anyone totalling
# the log, and the run counted as a finished review by anyone grepping.
claude_run.stream = stream(call(FINDINGS[:2]),
                           result_event(is_error=True, total_cost_usd=1.5,
                                        subtype="error_max_turns"))
del logged[:]
vinegar.review(ROOT, "o/r", PR, CONFIG, None, {})
check("a failed run is not also logged as a completed one",
      len([m for m in logged if "USD equivalent" in m]) == 1
      and not any("reviewed in" in m for m in logged),
      [m for m in logged if "USD equivalent" in m or "reviewed in" in m])
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
reset_stubs()
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
# A blank line inside the reviewer's own prose — describe() strips the
# ends, so only a middle one survives — used to leave a two-space line,
# which GitHub reads as the end of the list item: the failure scenario
# and the category then rendered outside the bullet naming the finding.
_blank = vinegar.finding_bullet(
    {"file": "a.py", "line": 1, "summary": "one\n\n\ntwo",
     "failure_scenario": "boom", "category": "correctness"})
check("a blank line in a finding does not break out of its bullet",
      "\n  \n" not in _blank and "\n\n" not in _blank
      and "Failure: boom" in _blank, repr(_blank))
# A repository with more open pull requests than one listing asks for
# must say so: anything past the cap is never handed to handle_pr at all,
# which is indistinguishable from Vinegar having judged it.
_cap_log = []


def listing_at_cap(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:3] == ["gh", "pr", "list"]:
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps([dict(PR, number=n)
                                for n in range(vinegar.PR_LIMIT)]), "")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = listing_at_cap
vinegar.log = lambda m: _cap_log.append(m)
_capped = vinegar.open_prs("o/r", None)
vinegar.log = lambda message: None
vinegar.run = fake_run
check("a repository at the listing cap says so",
      len(_capped) == vinegar.PR_LIMIT
      and any("not seen at all" in m for m in _cap_log), _cap_log)

# A listing that is not pull requests must not reach handle_pr: the
# catch-all that keeps one bad pull request from stopping the daemon
# builds its message with `pr.get`, which raises from inside the except
# and takes the process out.
def listing_not_prs(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:3] == ["gh", "pr", "list"]:
        return subprocess.CompletedProcess(cmd, 0, '["o/r#12", "o/r#13"]', "")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = listing_not_prs
del _cap_log[:]
vinegar.log = lambda m: _cap_log.append(m)
_not_prs = vinegar.open_prs("o/r", None)
vinegar.log = lambda message: None
vinegar.run = fake_run
check("a listing that is not pull requests is refused, not iterated",
      _not_prs == []
      and any("did not answer with pull requests" in m for m in _cap_log),
      (_not_prs, _cap_log))

# A blank line that carries indentation is how a model writes one inside
# an indented block, and it ends the markdown list item just as surely.
_spaced = vinegar.finding_bullet(
    {"file": "a.py", "line": 1, "summary": "one\n   \ntwo",
     "failure_scenario": "boom", "category": "correctness"})
check("an indented blank line does not break out of its bullet either",
      "\n  \n" not in _spaced and "\n   \n" not in _spaced
      and "Failure: boom" in _spaced, repr(_spaced))

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
               note="Vinegar tried to review this 3 times.", preserve=True)
_gave_body = open(vinegar.transcript_path("o/r", PR_GAVE)).read()
check("the give-up leaves the words the attempts saved",
      "Twenty minutes of analysis." in _gave_body, _gave_body[:200])
# Its own name, and asserted here rather than ninety lines further down
# past three blocks that read other files into other names.
check("the give-up itself is recorded beneath them",
      "tried to review this 3 times" in _gave_body
      and _gave_body.find("Twenty minutes") < _gave_body.find(
          "tried to review"), _gave_body[:300])

# Through give_up() itself, which is what has to ask for that: the check
# above calls finish() directly and would pass even if nothing did.
PR_GU = dict(PR, headRefOid="cafe1234beef")
vinegar.keep(L, "o/r", PR_GU, "What attempt three said.", "the review failed")
vinegar.give_up(L, "o/r", PR_GU, dict(CONFIG, comment=False), 3, {})
_gu_body = open(vinegar.transcript_path("o/r", PR_GU)).read()
check("give_up asks for the attempts' words to be kept",
      "What attempt three said." in _gu_body
      and "tried to review this 3 times" in _gu_body, _gu_body[:200])

# A give-up retried on a later poll asks whether the earlier attempt
# landed after all. Without that, a post that succeeded while Vinegar
# could not tell is said again on the next poll, and again on the one
# after: the duplicate already_posted() exists to prevent, arriving
# through the one path that retries across polls rather than within a call.
_rs_posts = []


def watch_posts(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        _rs_posts.append(cmd)
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = watch_posts
fake_run.rc, fake_run.post_err = 0, "HTTP 422"
fake_run.look_out = "%s gave up on `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
del _rs_posts[:]
_said = vinegar.give_up(L, "o/r", PR, CONFIG, 3, {}, tries=1)
check("a give-up already up is not announced a second time",
      _said == vinegar.POSTED and not _rs_posts, (_said, len(_rs_posts)))
del _rs_posts[:]
_said = vinegar.give_up(L, "o/r", PR, CONFIG, 3, {}, tries=0)
check("the first announcement does not pay for that read",
      len(_rs_posts) == 1, len(_rs_posts))

# A hand re-run of `--pr` is the one path a person repeats, and it knows
# nothing of the state file, so it asks before posting a second complete
# review over the first.
vinegar.run = watch_posts
fake_run.look_out = "%s reviewed `a1b2c3d` at high effort\n" % (
    vinegar.BODY_MARK)
del _rs_posts[:]
vinegar.post_review(L, "o/r", PR, ROOT, "x", [], CONFIG, None, resent=True)
check("a re-run against a reviewed commit does not post twice",
      not _rs_posts, len(_rs_posts))
# And it asks before doing the work it would throw away: routing findings
# means a full `git diff` over the pull request, which is the same reason
# the dry-run check sits above the routing rather than below it.
del last_git_diff[0][:]
vinegar.post_review(L, "o/r", PR, ROOT, "x", FINDINGS[:2], CONFIG, None,
                    resent=True)
check("a review already up costs no diff to discover",
      not last_git_diff[0], last_git_diff[0])
# But a dry run asks GitHub nothing at all. It mints no token, so the
# read would go out under whatever ambient credentials exist and warn
# about a resend a dry run was never going to make.
del looked[:]
vinegar.post_review(L, "o/r", PR, ROOT, "x", [], dict(CONFIG, comment=False),
                    None, resent=True)
check("a dry run makes no live call to see if a review is up",
      not looked, looked)
del _rs_posts[:]
vinegar.post_review(L, "o/r", PR, ROOT, "x", [], CONFIG, None)
check("the daemon's own first post does not ask",
      len(_rs_posts) == 1, len(_rs_posts))
fake_run.look_out = ""
vinegar.run = fake_run

# Through main(), so it is the entry point that is pinned and not a
# string in its source.
_pr_kw = {}
_pr_real = (vinegar.review, vinegar.find_pr, vinegar.checkout,
            vinegar.github_env, sys.argv)
os.makedirs(os.environ["VINEGAR_HOME"], exist_ok=True)
with open(os.path.join(os.environ["VINEGAR_HOME"], "config.json"), "w") as h:
    json.dump({"repos": ["o/r"]}, h)
vinegar.review = lambda *a, **k: _pr_kw.update(k) or vinegar.DONE
vinegar.find_pr = lambda repo, number, env: PR_LIVE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
sys.argv = ["vinegar.py", "--pr", "o/r#12"]
try:
    vinegar.main()
except SystemExit as err:
    _pr_kw["exited"] = str(err)
finally:
    (vinegar.review, vinegar.find_pr, vinegar.checkout,
     vinegar.github_env, sys.argv) = _pr_real
# Deliberately not `resent`: a person running --pr has asked for a review
# and expects to see one. Asking first makes the stated reason for a
# second run — trying another model — impossible, because the first run's
# own line is up at that commit and the second review would be paid for
# and discarded.
check("a --pr run posts what it reviewed rather than deferring",
      _pr_kw.get("resent") in (None, False), _pr_kw)

# A manual run writes no state, which is right. But a refused post leaves
# a marker the daemon only honours when an entry stands behind it, so
# without one the next poll reads it as left over and deletes it — and on
# a pull request the daemon skips, nothing would ever replace the review.
_mr_home = os.environ["VINEGAR_HOME"]
_mr_marker = vinegar.unposted_path("o/r", PR_LIVE)


def _review_that_could_not_post(*a, **k):
    os.makedirs(vinegar.REVIEW_DIR, exist_ok=True)
    with open(_mr_marker, "w") as handle:
        handle.write("%s\n" % PR_LIVE["headRefOid"])
    return vinegar.DONE


vinegar.review = _review_that_could_not_post
vinegar.find_pr = lambda repo, number, env: PR_LIVE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
sys.argv = ["vinegar.py", "--pr", "o/r#12"]
try:
    vinegar.main()
except SystemExit:
    pass
finally:
    (vinegar.review, vinegar.find_pr, vinegar.checkout,
     vinegar.github_env, sys.argv) = _pr_real
_mr_state = vinegar.load_state()
check("a manual run's unpostable review is left for the daemon to send",
      _mr_state.get(L, {}).get("outcome") == vinegar.DONE
      and _mr_state.get(L, {}).get("sha") == PR_LIVE["headRefOid"],
      _mr_state)
vinegar.forget(_mr_marker)

# And a manual run whose post landed records it too. Leaving no trace
# meant the daemon reviewed the same head a minute later at full cost
# and posted a second complete review, because its first attempt does
# not ask GitHub — the state file is what usually tells it.
vinegar.review = lambda *a, **k: vinegar.DONE
vinegar.find_pr = lambda repo, number, env: PR_LIVE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
sys.argv = ["vinegar.py", "--pr", "o/r#12"]
try:
    vinegar.main()
except SystemExit:
    pass
finally:
    (vinegar.review, vinegar.find_pr, vinegar.checkout,
     vinegar.github_env, sys.argv) = _pr_real
_ok_state = vinegar.load_state()
check("a manual run that posted is recorded, so the daemon does not repeat it",
      _ok_state.get(L, {}).get("outcome") == vinegar.DONE
      and _ok_state.get(L, {}).get("unposted") is None, _ok_state)

# And a manual review that never ran is recorded as failed, not done.
# Written as DONE, the daemon returns at the DONE check for ever and the
# pull request is closed off with no comment and no retry.
vinegar.review = lambda *a, **k: vinegar.FAILED
vinegar.find_pr = lambda repo, number, env: PR_LIVE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.github_env = lambda *a, **k: None
sys.argv = ["vinegar.py", "--pr", "o/r#12"]
try:
    vinegar.main()
except SystemExit:
    pass
finally:
    (vinegar.review, vinegar.find_pr, vinegar.checkout,
     vinegar.github_env, sys.argv) = _pr_real
check("a manual review that failed is recorded as failed, so it is retried",
      vinegar.load_state().get(L, {}).get("outcome") == vinegar.FAILED,
      vinegar.load_state().get(L))

# Announcing is retried while it fails to land, and each retry used to
# append the same ending again to the file a dry run is judged by.
vinegar.finish(L, "o/r", PR_GAVE, ROOT, "", None,
               dict(CONFIG, comment=False), None, {},
               note="Vinegar tried to review this 3 times.", preserve=True)
body = open(vinegar.transcript_path("o/r", PR_GAVE)).read()
check("a retried give-up does not stack endings in the transcript",
      body.count("tried to review this 3 times") == 1,
      body.count("tried to review this 3 times"))

# Only the give-up asks for preservation. A review that ran to completion
# and reported nothing writes its own transcript, or the file keeps an
# earlier attempt's words under a header saying they are not a review
# while the comment says the run produced nothing.
PR_QUIET2 = dict(PR, headRefOid="beadfeedcafe")
vinegar.keep(L, "o/r", PR_QUIET2, "Attempt one's words.", "the review failed")
vinegar.finish(L, "o/r", PR_QUIET2, ROOT, "", None,
               dict(CONFIG, comment=False), None, {})
_quiet2 = open(vinegar.transcript_path("o/r", PR_QUIET2)).read()
check("a finished review that said nothing writes its own transcript",
      "Attempt one's words." not in _quiet2 and "not a review" not in _quiet2,
      _quiet2[:200])

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

reset_stubs()
# A review GitHub refused is finished work waiting on one API call. It is
# marked, and a later poll sends it from the transcript rather than
# re-running a review the subscription already paid for.
PR_LOST = dict(PR_LIVE, headRefOid="1051051051aa")
_lost_key = "o/r#%d" % PR_LOST["number"]
_lost_marker = vinegar.unposted_path("o/r", PR_LOST)
fake_run.rc, fake_run.post_err = 1, "HTTP 403 Resource not accessible"
fake_run.look_out = ""
vinegar.run, vinegar.github_env = claude_run, lambda *a, **k: None
claude_run.stream = stream(call(FINDINGS[:1]), result_event())
del posted[:]
vinegar.review(ROOT, "o/r", PR_LOST, CONFIG, None, {})
check("a review GitHub refused is marked for another attempt",
      os.path.exists(_lost_marker), _lost_marker)

# A rate-limited first post promises the review will be sent again, so it
# must leave the one thing able to send it. THROTTLED is a string and
# every string is true, so testing truthiness deleted the marker.
PR_TL = dict(PR_LIVE, headRefOid="7407407407aa")
_tl_marker = vinegar.unposted_path("o/r", PR_TL)
fake_run.rc = 1
fake_run.post_err = "gh: API rate limit exceeded (HTTP 403)"
vinegar.run = claude_run
claude_run.stream = stream(call(FINDINGS[:1]), result_event())
vinegar.review(ROOT, "o/r", PR_TL, CONFIG, None, {})
vinegar.run = fake_run
check("a rate-limited first post keeps the review for later",
      os.path.exists(_tl_marker), _tl_marker)
vinegar.forget(_tl_marker)
fake_run.rc, fake_run.post_err = 1, "HTTP 403 Resource not accessible"

# An empty marker is not a marker for another commit either, and that
# answer is the one that deletes the saved review.
_empty = os.path.join(_tx_home, "empty.md.unposted")
with open(_empty, "w") as h:
    h.write("   \n")
check("an empty marker is not read as another commit",
      vinegar.read_mark(_empty) is None, vinegar.read_mark(_empty))

# A limit met on the anchor-stripping retry is still a limit. Flattening
# it to False there would drop the marker that resends the review.
PR_TR = dict(PR_LIVE, headRefOid="8508508508aa")
_tr_marker = vinegar.unposted_path("o/r", PR_TR)
fake_run.look_out = ""


def refuse_then_throttle(cmd, cwd=None, timeout=None, env=None,
                         stdin_text=None):
    if cmd[:2] == ["gh", "api"] and "-X" not in cmd:
        fake_run.rc = 1
        fake_run.post_err = ("HTTP 422 Unprocessable Entity" if not posted
                             else "gh: API rate limit exceeded (HTTP 403)")
    return fake_run(cmd, cwd, timeout, env, stdin_text)


vinegar.run = refuse_then_throttle
del posted[:]
_tr = vinegar.finish(L, "o/r", PR_TR, ROOT, "words", FINDINGS[:1], CONFIG,
                     None, {})
vinegar.run = fake_run
check("a limit on the anchorless retry is reported as a limit",
      _tr == vinegar.THROTTLED and os.path.exists(_tr_marker),
      (_tr, os.path.exists(_tr_marker)))
vinegar.forget(_tr_marker)
fake_run.rc, fake_run.post_err = 1, "HTTP 403 Resource not accessible"
check("the entry says a saved review is waiting behind it",
      vinegar.state_entry("x", vinegar.DONE, 1, unposted=True)["unposted"]
      is True)

# And handle_pr records it, which is what lets the next poll find the
# saved review without listing the whole reviews directory.
PR_FLAG = dict(PR_LIVE, headRefOid="f1a9f1a9f1a9")
_flag_marker = vinegar.unposted_path("o/r", PR_FLAG)
_flag_real = (vinegar.review, vinegar.checkout, vinegar.save_state)


def _review_that_leaves_a_marker(*a, **k):
    with open(_flag_marker, "w") as handle:
        handle.write("%s\n" % PR_FLAG["headRefOid"])
    return vinegar.DONE


vinegar.review = _review_that_leaves_a_marker
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.save_state = lambda st: None
_flag_state = {}
vinegar.handle_pr("o/r", PR_FLAG, CONFIG, _flag_state, {})
(vinegar.review, vinegar.checkout, vinegar.save_state) = _flag_real
check("handle_pr records that a saved review is waiting",
      _flag_state[vinegar.pr_key("o/r", PR_FLAG)].get("unposted") is True,
      _flag_state)
vinegar.forget(_flag_marker)

# Every route to the pull request carries the same paths: the anchorless
# retry and the transcript the repost later sends, not only the inline
# one. A reviewer's absolute path used to reach both verbatim.
PR_ABS = dict(PR_LIVE, headRefOid="abcabcabc001")
_abs_finding = [{"file": os.path.join(ROOT, "vinegar.py"), "line": 9000,
                 "summary": "outside the diff"}]
fake_run.rc, fake_run.post_err = 1, "HTTP 502 Bad Gateway"
fake_run.look_out = ""
vinegar.run = fake_run
del posted[:]
vinegar.finish(L, "o/r", PR_ABS, ROOT, "words", _abs_finding, CONFIG, None,
               {})
_abs_bodies = " ".join(p[1]["body"] for p in posted)
_abs_transcript = open(vinegar.transcript_path("o/r", PR_ABS)).read()
check("no route to the pull request carries the daemon's own paths",
      posted and ROOT not in _abs_bodies and ROOT not in _abs_transcript
      and "vinegar.py:9000" in _abs_bodies,
      (ROOT in _abs_bodies, ROOT in _abs_transcript))
vinegar.forget(vinegar.unposted_path("o/r", PR_ABS))
fake_run.rc, fake_run.post_err = 1, "HTTP 403 Resource not accessible"
# This block's ambiguous post consulted the landed-review read; the
# checks below count those calls for themselves.
del looked[:]
del posted[:]

_lost_state = {_lost_key: {"outcome": vinegar.DONE,
                           "sha": PR_LOST["headRefOid"], "attempts": 1}}
fake_run.rc = 0
vinegar.run = fake_run
del posted[:]
del _asked[:]
# Put back whatever this section was using, not the genuine function: the
# blocks around here install their own and restoring the wrong one changes
# what the checks below exercise without failing anything.
_env_here = vinegar.github_env
vinegar.github_env = recording_env
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _lost_state, {})
vinegar.github_env = _env_here
check("the saved review is posted on a later poll, without re-reviewing",
      len(posted) == 1
      and "posted from the transcript" in posted[0][1]["body"], len(posted))
# The repost is the path where the token is oldest: the review it is sending
# ran to completion, was refused, and has been sitting on disk since. Minting
# without asking for life here hands the post a token that may have minutes
# left of an hour that started before the review did.
check("the repost mints a token with time to finish posting",
      _asked == [vinegar.POST_GRACE], _asked)
# Asked every time, including the first. Skipping it there assumed
# post_review had established that nothing landed, which holds only when
# its own landed-review read succeeded — and that read answers "no" when
# it times out, which is what happens during the incident that made the
# post ambiguous. The saved review then went on top of one already up.
check("every repost asks whether the review is already up",
      len(looked) == 1, looked)
check("a posted review clears the mark",
      not os.path.exists(_lost_marker), _lost_marker)

# The same cleanup on the ordinary path: a review that posts must clear a
# mark an earlier attempt at the same commit left, or the next poll sends
# the transcript over a review that is already up. Checked without going
# through repost(), which does its own cleanup and would hide this.
fake_run.rc = 1
vinegar.run = claude_run
del posted[:]
vinegar.review(ROOT, "o/r", PR_LOST, CONFIG, None, {})
_marked = os.path.exists(_lost_marker)
fake_run.rc = 0
vinegar.review(ROOT, "o/r", PR_LOST, CONFIG, None, {})
check("a review that posts clears an earlier attempt's mark",
      _marked and not os.path.exists(_lost_marker),
      (_marked, os.path.exists(_lost_marker)))

# A transcript that could not be written must not be marked for
# reposting. save_or_log swallows the failure, and an earlier attempt may
# have left a file under the same name saying "It is not a review" —
# which the repost would then publish as one.
PR_NOWRITE = dict(PR_LIVE, headRefOid="badbadbad001")
_nw_marker = vinegar.unposted_path("o/r", PR_NOWRITE)
vinegar.keep(L, "o/r", PR_NOWRITE, "Attempt one narration.",
             "the review failed")
_nw_save = vinegar.save_transcript
vinegar.save_transcript = exploding_save
fake_run.rc = 1
vinegar.run = claude_run
del posted[:]
vinegar.review(ROOT, "o/r", PR_NOWRITE, CONFIG, None, {})
vinegar.save_transcript = _nw_save
check("a transcript that could not be written is not marked for reposting",
      not os.path.exists(_nw_marker)
      and "not a review" in open(
          vinegar.transcript_path("o/r", PR_NOWRITE)).read(), _nw_marker)
# And the operator is not told a retry is scheduled when none is. The
# message named a file that was never written, while the outcome was
# recorded done and the pull request closed off for good.
_nw_log = []
vinegar.log = lambda m: _nw_log.append(m)
vinegar.save_transcript = exploding_save
vinegar.review(ROOT, "o/r", PR_NOWRITE, CONFIG, None, {})
vinegar.save_transcript = _nw_save
vinegar.log = lambda message: None
check("a review with nothing saved does not promise a retry",
      any("nothing was saved to send later" in m for m in _nw_log)
      and not any("posting it again is scheduled" in m for m in _nw_log),
      [m for m in _nw_log if "nothing reached" in m])
fake_run.rc = 0
vinegar.run = fake_run

# Bounded, or an App that can never post retries every minute for ever.
fake_run.rc, fake_run.post_err = 1, "HTTP 403 Resource not accessible"
vinegar.run = claude_run
del posted[:]
vinegar.review(ROOT, "o/r", PR_LOST, CONFIG, None, {})
vinegar.run = fake_run
_bound_state = {_lost_key: {"outcome": vinegar.DONE,
                            "sha": PR_LOST["headRefOid"], "attempts": 1}}
_bound_posts = 0
for _ in range(6):
    del posted[:]
    vinegar.handle_pr("o/r", PR_LOST, CONFIG, _bound_state, {})
    _bound_posts += len(posted)
check("a review that can never post stops being retried",
      _bound_posts == vinegar.MAX_ATTEMPTS
      and not os.path.exists(_lost_marker),
      (_bound_posts, _bound_state))

# The budget is what stops it, not the mark: removing the mark can itself
# fail, and a mark left behind with the budget spent must not restart the
# retries.
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_spent_state = {_lost_key: {"outcome": vinegar.DONE,
                            "sha": PR_LOST["headRefOid"], "attempts": 1,
                            "post_tries": vinegar.MAX_ATTEMPTS}}
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _spent_state, {})
check("a spent post budget is not retried even if the mark remains",
      not posted, (len(posted), _spent_state))

# And the budget survives whatever else rewrites the entry. Every site
# that rebuilt one through state_entry dropped post_tries, so a spent
# repost budget was handed back and "three sends and stop" became three
# per rewrite.
_launder = {_lost_key: {"outcome": vinegar.FAILED,
                        "sha": PR_LOST["headRefOid"],
                        "attempts": vinegar.MAX_ATTEMPTS,
                        "post_tries": vinegar.MAX_ATTEMPTS}}
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _launder, {})
check("the repost budget is not handed back by the give-up",
      _launder[_lost_key].get("post_tries") == vinegar.MAX_ATTEMPTS,
      _launder)
PR_SKIP = dict(PR_LOST, number=77, isDraft=True)
_skip_key = vinegar.pr_key("o/r", PR_SKIP)
_launder_skip = {_skip_key: {"outcome": vinegar.FAILED,
                             "sha": PR_SKIP["headRefOid"], "attempts": 1,
                             "post_tries": 2}}
vinegar.handle_pr("o/r", PR_SKIP, CONFIG, _launder_skip, {})
# And by a review that runs. Both of handle_pr's own rebuilds dropped it,
# so the budget was already gone before the one site that preserves it
# looked.
PR_RERUN = dict(PR_LIVE, number=88, headRefOid="c0ffeec0ffee")
_rerun_key = vinegar.pr_key("o/r", PR_RERUN)
_rerun = {_rerun_key: {"outcome": vinegar.FAILED,
                       "sha": PR_RERUN["headRefOid"], "attempts": 1,
                       "post_tries": 2}}
_rerun_real = (vinegar.review, vinegar.checkout, vinegar.save_state)
_rerun_saved = []
vinegar.review = lambda *a, **k: vinegar.DONE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.save_state = lambda st: _rerun_saved.append(dict(st[_rerun_key]))
vinegar.handle_pr("o/r", PR_RERUN, CONFIG, _rerun, {})
(vinegar.review, vinegar.checkout, vinegar.save_state) = _rerun_real
# A fresh review voids the budget rather than inheriting it: this run
# writes its own transcript over any saved one, so the count that
# governed the old copy no longer applies. Carried forward, a spent
# budget met the new marker already at its cap, and neither the repost
# branch nor the forget branch would touch it again.
check("a fresh review starts the repost budget over",
      _rerun[_rerun_key].get("post_tries") is None, _rerun)
check("the marker written before a review starts it over too",
      _rerun_saved and _rerun_saved[0].get("post_tries") is None,
      _rerun_saved[:1])

# But it does not follow the pull request to the next head. A budget
# spent on one head's saved review made every later head's review
# unpostable the moment it was written.
_moved_budget = {_rerun_key: {"outcome": vinegar.DONE, "sha": "0ldhead0",
                              "attempts": 1,
                              "post_tries": vinegar.MAX_ATTEMPTS}}
vinegar.review = lambda *a, **k: vinegar.DONE
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.save_state = lambda st: None
vinegar.handle_pr("o/r", PR_RERUN, dict(CONFIG, review_on_push=True),
                  _moved_budget, {})
(vinegar.review, vinegar.checkout, vinegar.save_state) = _rerun_real
check("a spent repost budget does not follow the head",
      _moved_budget[_rerun_key].get("sha") == PR_RERUN["headRefOid"]
      and _moved_budget[_rerun_key].get("post_tries", 0) == 0,
      _moved_budget)

check("the repost budget is not handed back by a skip",
      _launder_skip[_skip_key].get("outcome") == "skipped"
      and _launder_skip[_skip_key].get("post_tries") == 2, _launder_skip)
vinegar.forget(_lost_marker)

# A marker whose state entry is gone is left over from a review nobody
# remembers. Reposting it defeats the repair the log recommends: the
# operator deletes the entry to get a fresh review and gets the stale
# transcript posted instead, then a real review after that.
with open(_lost_marker, "w") as h:
    h.write("orphan\n")
_orphan_state = {}
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _orphan_state, {})
check("a marker with no entry behind it is forgotten, not posted",
      not [p for p in posted if "posted from the transcript" in p[1]["body"]]
      and not os.path.exists(_lost_marker), (len(posted), _lost_marker))

# A saved review that lands leaves the pull request reviewed, whatever
# the entry said before. Left as FAILED, the next poll announced "Vinegar
# tried to review this 3 times and each attempt failed" on a pull request
# that had received a full review a minute earlier.
vinegar.save_transcript("o/r", PR_LOST, "The review that was refused.", [])
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_crash_done = {_lost_key: {"outcome": vinegar.FAILED,
                           "sha": PR_LOST["headRefOid"],
                           "attempts": vinegar.MAX_ATTEMPTS}}
fake_run.rc = 0
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _crash_done, {})
check("a landed repost leaves the pull request recorded as reviewed",
      _crash_done[_lost_key].get("outcome") == vinegar.DONE, _crash_done)
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _crash_done, {})
check("and no give-up is announced over the review it just posted",
      not posted, (len(posted), _crash_done))
fake_run.rc = 1
fake_run.look_out = ""
vinegar.forget(_lost_marker)

# The marker is written inside review(), before handle_pr records how the
# review ended, so a process killed in that window leaves it beside a
# FAILED entry. Requiring DONE here deleted the finished review that the
# marker exists to protect.
vinegar.save_transcript("o/r", PR_LOST, "Findings from the killed run.", [])
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_killed_state = {_lost_key: {"outcome": vinegar.FAILED,
                             "sha": PR_LOST["headRefOid"], "attempts": 1}}
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _killed_state, {})
check("a marker left by a killed run is sent, not deleted",
      len(posted) == 1
      and "Findings from the killed run." in posted[0][1]["body"],
      (len(posted), _killed_state))

# Answering "" for a marker that cannot be opened made it identical to a
# marker for another commit, and the caller's answer to that is to delete
# the saved review. A directory in its place rather than chmod 0, because
# root ignores the mode bits.
_unopenable = os.path.join(_tx_home, "cannot-open.md.unposted")
os.makedirs(_unopenable, exist_ok=True)
check("a marker that cannot be opened is not read as a commit",
      vinegar.read_mark(_unopenable) is None,
      vinegar.read_mark(_unopenable))

# An unreadable marker is not a marker for another commit, and answering
# the same way for both threw a paid-for review away.
vinegar.save_transcript("o/r", PR_LOST, "Findings behind a bad marker.", [])
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_unreadable = vinegar.read_mark
vinegar.read_mark = lambda path: None
_bad_state = {_lost_key: {"outcome": vinegar.DONE,
                          "sha": PR_LOST["headRefOid"], "attempts": 1}}
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _bad_state, {})
vinegar.read_mark = _unreadable
check("an unreadable marker keeps the review rather than dropping it",
      len(posted) == 1
      and "Findings behind a bad marker." in posted[0][1]["body"],
      (len(posted), _bad_state))
vinegar.forget(_lost_marker)

# An entry the operator deleted, with a marker that cannot be read: the
# missing sha and the unreadable one were both None, so the comparison
# said "same commit" and the stale transcript went to the pull request
# the operator had just cleared in order to have it reviewed afresh.
vinegar.save_transcript("o/r", PR_LOST, "Stale findings.", [])
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_gone = vinegar.read_mark
_gone_review, _gone_checkout = vinegar.review, vinegar.checkout
vinegar.read_mark = lambda path: None
vinegar.checkout = lambda repo, pr, env: ROOT
vinegar.review = lambda *a, **k: vinegar.DONE
_gone_state = {}
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _gone_state, {})
vinegar.read_mark = _gone
vinegar.review, vinegar.checkout = _gone_review, _gone_checkout
check("a marker with no entry at all is forgotten, not posted",
      not os.path.exists(_lost_marker)
      and not [p for p in posted
               if "posted from the transcript" in p[1]["body"]],
      (len(posted), _gone_state))

# The mint failure inside repost() must clear the marker when the budget
# runs out, or it survives to restart the cycle later. Reachable only with
# a github_env that can fail, which the stub above cannot.
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_mint_state = {_lost_key: {"outcome": vinegar.DONE,
                           "sha": PR_LOST["headRefOid"], "attempts": 1,
                           "post_tries": vinegar.MAX_ATTEMPTS - 1}}
_mint_env = vinegar.github_env


def _mint_fails(*a, **k):
    raise RuntimeError("the App key is gone")


vinegar.github_env = _mint_fails
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, dict(CONFIG, github_app={"id": 1}),
                  _mint_state, {})
vinegar.github_env = _mint_env
check("a token that never mints does not leave the mark behind",
      not os.path.exists(_lost_marker)
      and _mint_state[_lost_key]["post_tries"] == vinegar.MAX_ATTEMPTS,
      (os.path.exists(_lost_marker), _mint_state))

# Anything that raises must still spend the attempt. Charging it after
# the send meant a missing `gh`, or a fork that cannot allocate, escaped
# with the counter unmoved and the marker in place: a mint, a file read
# and a log line a minute, per pull request, for ever.
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_raise_state = {_lost_key: {"outcome": vinegar.DONE,
                            "sha": PR_LOST["headRefOid"], "attempts": 1}}


def _no_gh(cmd, cwd=None, timeout=None, env=None, stdin_text=None):
    raise FileNotFoundError("[Errno 2] No such file or directory: 'gh'")


vinegar.run = _no_gh
for _ in range(4):
    vinegar.handle_pr("o/r", PR_LOST, CONFIG, _raise_state, {})
vinegar.run = fake_run
# A rate limit refuses the request without judging it, and resets on its
# own clock. Spending an attempt on it meant three polls — three minutes
# against a limit that resets hourly — abandoned a finished review.
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_rl_state = {_lost_key: {"outcome": vinegar.DONE,
                         "sha": PR_LOST["headRefOid"], "attempts": 1,
                         "unposted": True}}
fake_run.rc = 1
fake_run.post_err = "gh: API rate limit exceeded (HTTP 403)"
for _ in range(vinegar.MAX_ATTEMPTS):
    vinegar.handle_pr("o/r", PR_LOST, CONFIG, _rl_state, {})
check("a rate-limited repost does not spend the budget",
      _rl_state[_lost_key].get("post_tries", 0) == 0
      and os.path.exists(_lost_marker), _rl_state)
# Waived, but not for ever: handle_pr returns straight after a repost, so
# an endless refund pins the pull request there and it is never reviewed
# at a new head, never re-skipped and never abandoned.
for _ in range(vinegar.MAX_ATTEMPTS * 2):
    vinegar.handle_pr("o/r", PR_LOST, CONFIG, _rl_state, {})
check("a throttle that never lifts still ends",
      not os.path.exists(_lost_marker)
      and _rl_state[_lost_key].get("post_tries") == vinegar.MAX_ATTEMPTS,
      _rl_state)
fake_run.post_err = "HTTP 403 Resource not accessible"
vinegar.forget(_lost_marker)

# The transcript puts its findings last, so an oversized one has to be
# cut from the front: cutting the end kept the narration and dropped
# exactly what the repost exists to deliver.
vinegar.save_transcript("o/r", PR_LOST,
                        "narration " * 9000, FINDINGS[:1])
with open(_lost_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
_big_state = {_lost_key: {"outcome": vinegar.DONE,
                          "sha": PR_LOST["headRefOid"], "attempts": 1,
                          "unposted": True}}
fake_run.rc = 0
del posted[:]
vinegar.handle_pr("o/r", PR_LOST, CONFIG, _big_state, {})
check("an oversized saved review keeps its findings, not its narration",
      len(posted) == 1
      and len(posted[0][1]["body"]) <= vinegar.MAX_BODY
      and "## Findings" in posted[0][1]["body"]
      and "the beginning was cut" in posted[0][1]["body"],
      len(posted[0][1]["body"]) if posted else "nothing posted")
check("a posted saved review stops arming the directory scan",
      _big_state[_lost_key].get("unposted") is None, _big_state)
fake_run.rc = 1

check("a repost that raises still spends its attempt and stops",
      _raise_state[_lost_key].get("post_tries") == vinegar.MAX_ATTEMPTS
      and not os.path.exists(_lost_marker), _raise_state)

# The head can move between the refusal and the retry. Keyed on the
# current head the marker was never found again, and with review_on_push
# false that review was the only one there would ever be.
_moved_marker = vinegar.unposted_path("o/r", PR_LOST)
vinegar.save_transcript("o/r", PR_LOST, "Findings at the old head.", [])
with open(_moved_marker, "w") as h:
    h.write("%s\n" % PR_LOST["headRefOid"])
PR_MOVED = dict(PR_LOST, headRefOid="9999999999ff")
_moved_state = {_lost_key: {"outcome": vinegar.DONE,
                            "sha": PR_LOST["headRefOid"], "attempts": 1,
                            "unposted": True}}
del posted[:]
vinegar.handle_pr("o/r", PR_MOVED, CONFIG, _moved_state, {})
check("a saved review survives the head moving on",
      len(posted) == 1
      and posted[0][1]["commit_id"] == PR_LOST["headRefOid"]
      and "Findings at the old head." in posted[0][1]["body"],
      posted[0][1]["commit_id"] if posted else "nothing posted")
fake_run.rc, fake_run.post_err = 0, "HTTP 422"

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
# Both stubs put back, not just the runner. Leaving the recorder in place
# costs nothing today and would silently make the next check that writes
# a transcript exercise the stub instead of the writer.
reset_stubs()

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
    "import os\n"
    "import vinegar\n"
    "p = vinegar.save_transcript('o/r', {'number': 1, 'headRefOid':"
    " 'abc1234def', 'url': 'u'}, 'caf\\u00e9', None)\n"
    "print(ascii(open(p, encoding='utf-8').read()))\n")
# The child writes under this run's own home, which is removed on the way
# out. Letting it mkdtemp() for itself left one directory per run in the
# system temp directory, which is the invariant this file's header claims
# to keep.
_tx_proc = subprocess.run(
    [sys.executable, "-c", _tx_script], capture_output=True, text=True,
    cwd=here_dir, env=dict(os.environ, LC_ALL="C", LANG="C",
                           PYTHONUTF8="0", PYTHONCOERCECLOCALE="0",
                           VINEGAR_HOME=os.path.join(_home, "c-locale")))
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

# A run that posts nothing must not tell the live daemon those pull
# requests are done. Through the real entry point, because the choice is
# made in main() and the whole point is which file gets written.
# `--pr nonsense` so the run exits on the target guard, which sits just
# after the choice this pins and before anything is asked of GitHub.
def _entry(*flags):
    return subprocess.run(
        [sys.executable, os.path.join(here_dir, "vinegar.py"),
         "--pr", "nonsense"] + list(flags),
        capture_output=True, text=True,
        env=dict(os.environ, VINEGAR_HOME=os.environ["VINEGAR_HOME"]))


_dry_out = _entry("--dry-run").stdout
_live_out = _entry().stdout
check("a dry run keeps its bookkeeping out of the live state file",
      "state.json.dry" in _dry_out, _dry_out[-200:])
# Bound once: `detail` is evaluated whether or not the check fails, so
# calling _entry() there launched a second interpreter every run to build
# a message a passing check throws away.
check("a live run still uses the live state file",
      "state.json.dry" not in _live_out, _live_out[-200:])
# The transcripts too: they are named from repo, number and sha, so a
# rehearsal of a pull request the daemon already reviewed would write over
# that review's only copy — the one the log tells the operator to send.
check("a dry run writes its transcripts somewhere else as well",
      "reviews.dry" in _dry_out, _dry_out[-200:])

# --- poll_once -----------------------------------------------------------
# Both guards here are about the daemon surviving one bad thing. Under
# launchd an escaping exception restarts the process every 30 seconds and
# polls nothing in between, so a single unreviewable pull request would stop
# every repository rather than itself.
reset_stubs()
_polled = []
_real_listing, _real_handling = vinegar.open_prs, vinegar.handle_pr
del _asked[:]
vinegar.github_env = recording_env


def refusing_listing(repo, env):
    if repo == "o/down":
        raise RuntimeError("GitHub said 502")
    return [dict(PR, number=1), dict(PR, number=2)]


def one_bad_pr(repo, pr, config, state, tokens):
    _polled.append((repo, pr["number"]))
    if pr["number"] == 1:
        raise ValueError("this pull request cannot be handled")


vinegar.open_prs, vinegar.handle_pr = refusing_listing, one_bad_pr
# Wrapped, because both guards fail by letting the exception out, and an
# unwrapped call here would end the run rather than fail these checks. In
# the daemon it ends the process instead, which is the whole point.
try:
    vinegar.poll_once(dict(CONFIG, repos=["o/down", "o/r"]), {}, {})
    _poll_escaped = None
except Exception as err:
    _poll_escaped = err
check("nothing one repository does escapes the poll", _poll_escaped is None,
      _poll_escaped)
check("a repository whose listing fails does not stop the ones after it",
      ("o/r", 1) in _polled, _polled)
check("one pull request that raises does not stop the ones after it",
      ("o/r", 2) in _polled, _polled)
# The listing asks for its own grace, and a shorter one than the posting:
# it is a read that either answers or is skipped, where a post that expires
# half way loses a finished review. Without any, a token minted seconds
# before a seven-page paginated read can expire inside it.
check("the listing mints a token with time to finish listing",
      _asked and all(g == vinegar.LISTING_GRACE for g in _asked), _asked)
vinegar.open_prs, vinegar.handle_pr = _real_listing, _real_handling
reset_stubs()

# --- acquire_lock --------------------------------------------------------
# Two Vinegars sharing a checkout is what this stops: the second one runs
# `git reset --hard` under the first one's review, which then reports
# findings about a commit nobody asked about. The kernel's flock decides,
# not the pid written in the file, so this has to be a real second lock
# attempt. A second flock from this process contends with the first, because
# the lock belongs to the open file description and each os.open makes a new
# one; that was measured here rather than assumed.
reset_stubs()
# The file is already here: main() ran earlier in this file and the lock is
# deliberately never unlinked, which is also the deployed state from the
# first start onwards. So this first call is the case the pid-file design
# would get wrong, and it is checked by making the call rather than by
# asking whether the file exists. Asserting the file's presence said nothing
# about starting: a version that refused whenever the file was there passed
# it. The precondition is asserted rather than assumed, because the check
# means nothing if the file happens to be absent.
_lock_was_there = os.path.exists(vinegar.LOCK_PATH)
# Wrapped like the calls below it. A refusal here is a SystemExit, so an
# unwrapped call would end the run instead of failing this check.
try:
    vinegar.acquire_lock()
    _first = "started"
except SystemExit as err:
    _first = str(err)
check("a lock file left behind does not by itself refuse a start",
      _lock_was_there and _first == "started", (_lock_was_there, _first))
check("the lock file records the pid holding it",
      vinegar.locked_by() == os.getpid(), vinegar.locked_by())
try:
    vinegar.acquire_lock()
    _second = "started"
except SystemExit as err:
    _second = str(err)
check("a second Vinegar is refused while the first holds the lock",
      "already running" in _second, _second)
vinegar.release_lock()
try:
    vinegar.acquire_lock()
    _after = "started"
except SystemExit as err:
    _after = str(err)
check("the lock is free again once it is released", _after == "started",
      _after)
vinegar.release_lock()
# Released, not unlinked. Removing it would open the race the lock closes:
# a second Vinegar holding the old inode while a third creates a new file
# at the same path and locks that, leaving two holders who cannot see each
# other.
check("releasing the lock leaves the file where it was",
      os.path.exists(vinegar.LOCK_PATH), vinegar.LOCK_PATH)

# A settings file whose allow list is present-and-null must produce the
# sentence that says what to add, not a traceback every 30 seconds.
_null_settings = os.path.join(_home, "null-allow.json")
with open(_null_settings, "w") as h:
    json.dump({"permissions": {"allow": None, "deny": []}}, h)
_sp = vinegar.SETTINGS_PATH
vinegar.SETTINGS_PATH = _null_settings
try:
    vinegar.check_paths()
    _null_said = "started"
except SystemExit as err:
    _null_said = str(err)
except TypeError as err:
    _null_said = "raised TypeError: %s" % err
vinegar.SETTINGS_PATH = _sp
check("an allow list that is null is refused with a sentence, not a stack",
      "does not allow" in _null_said, _null_said)

reached_the_end.append(True)
print()
print("FAILED: %s" % ", ".join(fails) if fails else "all checks passed")
sys.exit(1 if fails else 0)
