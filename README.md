# vinegar

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

A self-hosted pull-request reviewer. It runs on a machine you already own, uses
a Claude subscription you already pay for, and posts inline review comments on
your PRs.

> **Status: the poller works. Triage does not exist yet.**
> `vinegar.py` polls GitHub, picks which pull requests deserve a reviewer, and
> runs the review. The cheap triage model described below is still a design,
> so today every pull request that passes the filters gets a full review.

## Why it exists

Cursor's Bugbot is good at finding real bugs. In May 2026 it moved from a flat
seat price to usage-based billing at roughly $1.00 to $1.50 per review run. That
cost scales with how many pull requests you open, not with the month, so a high
review volume drains an allowance long before the billing window closes and
upgrading the plan does not fix it.

Vinegar keeps the reviews and removes the per-review bill.

## How it works

The reviewer is not something Vinegar implements. Claude Code already ships a
`/code-review` command that takes a PR number, returns its findings as JSON,
and runs non-interactively:

```sh
CLAUDE_CODE_REPORT_FINDINGS=1 \
  claude -p '/code-review 123' --output-format stream-json --verbose
```

That environment variable is not decoration. It, the two stream flags, and the
`ReportFindings` allow-list entry are the three settings "The review" describes,
and dropping any one sends the findings somewhere Vinegar cannot read them.

Vinegar is the trigger, the router, and the calibration around that. It also
does the posting: the reviewer returns findings and Vinegar submits them as one
review, rather than letting comments trickle out while the reviewer works. See
"The review".

```
new PR (or a new head commit)
        │
        ▼
   triage: a cheap model reads the diff  ──▶  posts a sticky comment on the PR
        │
        ├─ skip    nothing here needs a reviewer
        ├─ light   claude -p '/code-review <n>'   (low/medium effort)
        └─ full    claude -p '/code-review <n>'   (high effort)
                          │
                          ▼
                   findings returned  ──▶  one review posted on the PR
```

A daemon on an always-on machine polls GitHub for pull requests whose head
commit it has not seen. Polling rather than webhooks means no inbound port, no
tunnel, and nothing exposed to the internet.

Before each review the daemon puts a checkout on the pull request's head
commit. This is not cosmetic. Given a pull request number, `/code-review` reads
local files only when the checkout matches that branch, and otherwise fetches
each file over the API, which costs more and reviews worse.

**A self-hosted GitHub Actions runner is deliberately not used.** It would give
nicer triggering, but GitHub's own guidance is that self-hosted runners should
almost never serve public repositories: anyone can open a pull request from a
fork and execute code on the runner. The machine running Vinegar holds your
Claude credentials, which makes it the worst possible host for untrusted PR
code.

## Running it

You need macOS or Linux, Python 3.9 or newer, `git`, the GitHub CLI logged in
(`gh auth login`), and Claude Code logged in (`claude`). There are no other
dependencies and nothing to install.

```sh
git clone https://github.com/Sour-Labs/vinegar.git
cd vinegar
mkdir -p ~/.vinegar
cp config.example.json ~/.vinegar/config.json
$EDITOR ~/.vinegar/config.json          # list your repos
```

Review one pull request by hand, posting nothing, to see what you would get:

```sh
python3 vinegar.py --pr owner/repo#123 --dry-run
```

The review lands in `~/.vinegar/reviews.dry/`. A run that posts nothing keeps
its transcripts and its bookkeeping beside the real ones rather than in them,
so a rehearsal cannot overwrite a review that failed to post or tell the daemon
a pull request is already done. When it reads well, poll once, then run the
loop:

```sh
python3 vinegar.py --once                # one pass, then exit
python3 vinegar.py                       # poll forever
```

Vinegar keeps its own state under `~/.vinegar`: `config.json`, `state.json`
(the head commit it last handled per pull request), and `reviews/`. A run that
posts nothing uses `state.json.dry` and `reviews.dry/` instead. A review that
GitHub refused leaves a `.unposted` marker beside its transcript, and a later
poll sends that transcript rather than reviewing again.

There are tests, and they need nothing installed:

```sh
python3 test_vinegar.py
```

They cover the part between the reviewer finishing and the review appearing:
reading findings out of the stream, working out which can be anchored in the
diff, and deciding what to post. Nothing in them touches the network, GitHub,
git or Claude. That is deliberate rather than tidy: a review costs real money
and several minutes, so the behaviour that can be checked for free is checked
for free, and every case in there is one a live review got wrong once.

Clones live outside that directory, in `~/.vinegar-checkouts/`, one per repo,
which only Vinegar touches. They are deliberately not under `~/.vinegar`: the
reviewer is denied every read there so that a diff cannot talk it into reading
the App's private key, and a checkout inside would be caught by the same rule.
The default follows `VINEGAR_HOME`, and `VINEGAR_CHECKOUTS` overrides it on
its own. Deleting the clones is safe; Vinegar re-clones on the next review.

**`VINEGAR_HOME` must still end in a `.vinegar` directory.** The reviewer's
deny rule is a fixed glob, `Read(//**/.vinegar/**)`, written in
`review-settings.json`, and it matches that component and nothing else. Point
`VINEGAR_HOME` at `~/.vinegar-test` and the rule stops covering it, so nothing
denies a review the App's private key kept there. To run a second instance,
keep the component and vary the path above it:

```sh
VINEGAR_HOME=~/instances/test/.vinegar python3 vinegar.py --once
```

That gets its own state, its own lock, and its own clones in
`~/instances/test/.vinegar-checkouts`, all still covered by the same rule.

Vinegar checks this at startup and refuses to run if the two disagree, in
either direction: a checkout the rule covers, or a `VINEGAR_HOME` it does not.
Both fail silently otherwise. The first reviews from API fetches while
`permission_denials` stays empty; the second leaves the key readable, and
silence there means the protection was never applied rather than that it held.

### Configuration

Every key in `config.example.json`:

| Key | Default | What it does |
| --- | --- | --- |
| `repos` | none | Repositories to poll, as `owner/name`. Required. |
| `poll_interval` | `60` | Seconds between polls. |
| `effort` | `"high"` | Effort passed to `/code-review`: `low`, `medium`, `high`, `xhigh`, `max`. `ultra` is rejected. Read the note below before pairing it with `model`. |
| `comment` | `true` | Post findings on the pull request. False runs the review and writes only to `~/.vinegar/reviews.dry/`, remembering what it did in `state.json.dry`. |
| `model` | `null` | Model for the review. Null uses your Claude Code default. Read the note below before setting it. |
| `review_on_push` | `false` | Review again when the head commit changes. |
| `max_changed_lines` | `3000` | Skip pull requests larger than this. |
| `skip_drafts` | `true` | Skip drafts. |
| `skip_bots` | `true` | Skip pull requests opened by bots. |
| `skip_forks` | `true` | Skip pull requests whose head branch lives in a fork. Read the next section before you turn this off. |
| `authors` | `[]` | Only review these GitHub logins. Empty means anyone who passes the checks above. |
| `review_timeout` | `1800` | Kill a review that runs longer than this many seconds. |
| `github_app` | `null` | Post as a GitHub App instead of as you. See below. |

The last five are budget and safety controls, not optimizations. Automated
reviews spend the same subscription limits as your interactive Claude Code
work.

**`model` and `effort` together decide how good the review is.**
`/code-review` picks its prompt from a table keyed by both. Opus 5 at `high`
or `medium` selects a single-pass prompt; `xhigh` runs ten finder angles and a
sweep. On this repository `xhigh` found 15 findings for $1.65 where `high`
found between 2 and 6 for $1.31 to $2.18, so the deeper setting was both
better and cheaper per finding. Measure on your own repository before
believing that.

### Posting as Vinegar instead of as you

Out of the box `gh` uses the account you logged in with, so reviews arrive under
your own name and avatar, which reads as though you commented on your own pull
request. A GitHub App gives Vinegar its own name, its own icon, and a `bot`
badge.

It is worth doing for a second reason. An App's installation token can be
scoped to a single repository, so a review that gets talked into calling `gh`
cannot reach anything else your account can see. The allow list names only
read-only `gh` subcommands, but a prefix rule cannot see the flags that follow
one, and the token is what bounds the damage when a rule is not enough.

Create the App once, in your organisation's settings under **Developer
settings → GitHub Apps → New GitHub App**:

- **Name** whatever you want the comments signed as. Expect to need a second
  and a third choice: the name has to be unique across every GitHub App, and it
  also cannot collide with an existing user or organisation. `vinegar`,
  `brine`, `acetic`, `verjus`, `aceto` and `acidity` are all taken as accounts,
  and `vinaigre` was free as an account but still refused as an App name. This
  project ended up at `vinegar-bot`.
- **Homepage URL** anything; it is required and unused.
- **Webhooks**: untick **Active**. Vinegar polls and needs no callback.
- **Repository permissions**: `Pull requests` read and write, `Contents` read,
  `Metadata` read. Nothing else.
- Upload a logo on the App's page. That image is the avatar on every comment.
  `brand/vinegar-avatar-1024.png` in this repo is ready to use.

Then **Generate a private key**, which downloads a `.pem`. Install the App from
`https://github.com/apps/<your-app-slug>/installations/new`, pick the account
that owns the repositories, and choose **Only select repositories** rather than
all of them: the installation is the boundary, and a review can reach every
repository inside it. Point the config at the App and the key:

```json
"github_app": {
  "app_id": 123456,
  "private_key": "~/.vinegar/vinegar-bot.private-key.pem"
}
```

Keep the key private, `chmod 600`. Anyone holding it can act as the App on
every repository it is installed on.

Vinegar signs a short-lived JWT with that key, exchanges it for an
installation token scoped to the one repository being reviewed, and passes the
token to `git`, `gh`, and the review as `GH_TOKEN`. Signing uses `openssl`
rather than a Python crypto package, so there is still nothing to install. The
token is never written to disk and never appears on a command line.

Leave `github_app` as `null` and everything works exactly as before, posting
under your own account.

### Running it under launchd

`launchd/io.sourlabs.vinegar.plist` is a template. Replace every
`YOUR_USERNAME` and every `VINEGAR_PATH`, then:

```sh
mkdir -p ~/.vinegar/logs
cp launchd/io.sourlabs.vinegar.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.sourlabs.vinegar.plist
tail -f ~/.vinegar/logs/vinegar.log
```

Create the log directory first. launchd will not create it, and a job whose
`StandardOutPath` it cannot open fails to spawn at all.

`bootstrap` rather than `load`, which is deprecated and reports nothing when
it fails. To stop it, `launchctl bootout gui/$(id -u)/io.sourlabs.vinegar`.

Run that at the machine, in a logged-in session. `gui/$(id -u)` names a domain
that only exists while someone is logged in, so the same command over ssh to a
machine sitting at the login window fails with `Bootstrap failed: 5:
Input/output error`, which says nothing about the actual cause.

The plist sets `PATH` explicitly because launchd starts with a bare one, and
a daemon that cannot find `claude` fails at the first review.

That login requirement is not only about installing it. A LaunchAgent runs
only inside a logged-in session, so on a machine with FileVault enabled
Vinegar does not come back on its own after a reboot: the disk waits to be
unlocked, and nothing runs until someone logs in. Use `sudo fdesetup
authrestart` for planned reboots, which unlocks for one boot and proceeds to
log in. An unplanned reboot needs someone at the machine.

Nothing here rotates the logs, deliberately. Vinegar logs per event rather
than per poll, so the file grows slowly, and rotating it would break it:
launchd opens `StandardOutPath` once and hands the job that one descriptor, so
a rotation that renames the file leaves Vinegar writing to the renamed inode
while the new `vinegar.log` stays empty. `newsyslog` has no way to ask a
process to reopen, and the only signal that would work here kills a review
mid-flight. Truncate it by hand if it ever gets big enough to care about.

### Watching the watcher

Vinegar fails quietly. It reviews on a poll rather than on a webhook, so a
daemon that is not running looks exactly like a week with no pull requests, and
the first thing you notice is a merge nobody reviewed. `watchdog.sh` is the
answer to that, run every five minutes by a second agent.

`launchd/io.sourlabs.vinegar-watchdog.plist` is a template, the same as the
daemon's. Replace every `YOUR_USERNAME` and every `VINEGAR_PATH` before you
copy it, or launchd gets a job whose program does not exist and whose log path
it cannot open, which fails to spawn exactly as described above.

```sh
mkdir -p ~/.vinegar/logs
cp watchdog.env.example ~/.vinegar/watchdog.env
chmod 600 ~/.vinegar/watchdog.env
$EDITOR ~/.vinegar/watchdog.env          # a healthchecks.io URL, an ntfy topic
$EDITOR launchd/io.sourlabs.vinegar-watchdog.plist   # YOUR_USERNAME, VINEGAR_PATH
./watchdog.sh                            # run it once by hand; silence is a pass
cp launchd/io.sourlabs.vinegar-watchdog.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.sourlabs.vinegar-watchdog.plist
```

Run it by hand once before loading it. With Vinegar up it should print nothing
and exit 0, and a heartbeat should show up at healthchecks.io within seconds.
That one command distinguishes a working watchdog from a watchdog that cannot
find its config, which is a distinction the automated runs cannot make for you.

Two channels, because they have different blind spots, and the less obvious one
is the one that matters. **The heartbeat to healthchecks.io reports by
silence.** Nothing running on this machine can tell you this machine is off, so
the signal has to be the pings stopping: that is what covers a power cut, a
dead network, and a reboot parked at the FileVault screen. The ntfy push is the
fast path for the case the watchdog is still alive to observe, a crashed daemon
on a healthy Mac, and it arrives in seconds instead of after the grace window.
Point the healthchecks.io alert at the same ntfy topic and both land together.

It confirms over a 45-second window before it says anything, because two cases
produce a down reading that fixes itself: at login launchd starts both agents
at once and the watchdog can win the race, and after a crash `KeepAlive` waits
out the daemon plist's 30-second `ThrottleInterval`. Without the window it
pages on every reboot and every self-healing restart, which is how a watchdog
teaches you to ignore it.

Nothing is logged on a healthy pass, so an empty `watchdog.log` is the good
outcome. Only a confirmed outage and a failed send get written, the second
because it means the alerting itself is broken. With neither channel configured
the script refuses to run at all, rather than watching in silence and reporting
success it never sent.

**Liveness is the pid file, and the pid has to be checked against what is
actually running under it.** Log freshness will not do: Vinegar logs per event
rather than per poll, so a healthy daemon watching quiet repos writes nothing
for hours. But `~/.vinegar/vinegar.pid` outlives a crash or a kill, where the
cleanup never runs, and then names a dead process until the next start, and
pids get reused. `kill -0` answers "is this number taken", which after a reuse
is a yes, and a watchdog that trusts it reports a dead Vinegar as healthy and
sends nothing. Matching the process command line answers "is this number our
daemon", which is the question worth asking. Use `ps -ww`, or `ps` truncates to
the width of any terminal it can find and a long checkout path loses the
`vinegar.py` you are matching on. If you write your own check, these are the
parts to get right.

## What the reviewer is allowed to do

A pull request diff is input written by someone else, and the reviewer reads it
on the machine that holds your Claude credentials. That is the same threat that
rules out a self-hosted Actions runner, so Vinegar does not hand the reviewer
your normal permissions.

Every review runs with `--settings review-settings.json --setting-sources ''
--strict-mcp-config`. That combination ignores your user, project, and local
settings, and loads no MCP servers. It matters: a permissive
`permissions.allow` in `~/.claude/settings.json` is common, and the project
settings would come from the repository under review, which on a fork pull
request is attacker-controlled.

`review-settings.json` allows reading and searching, a fixed list of read-only
`git` and `gh` subcommands, and the text utilities a review pipes through. It
denies writing and editing files, fetching the web, every shell and
interpreter, and anything that changes state. Denials beat allows, and anything
not listed is refused rather than granted.

The allow list names subcommands (`git diff`, `gh pr view`) rather than
programs. Vinegar's first review of itself found out why, and the holes were
confirmed by hand:

- `Bash(git:*)` is arbitrary command execution. `git -c alias.x='!some command'
  x` runs a shell, and `git -c` is not `git config`, so denying `git config`
  does not help.
- `Bash(gh:*)` lets a review approve and merge the pull request it is
  reviewing, and read any private repo the token can see.
- `sed -i` writes files even though `Write` and `Edit` are denied.

Claude Code's own analyzer independently blocks some of this. `find -exec` and
`awk 'BEGIN { system(...) }'` are refused whatever the allow list says, and so
is any shell redirection out of an allowed command. That is a useful backstop
and not something to rely on, so `find`, `awk`, `sed`, `perl`, `xargs`, and
`tee` are denied outright.

`gh api` is denied outright, alongside `curl` and `wget`, because it is the
same thing they are: a way to reach the network with whatever the reviewer
decides to send. It was allowed once, and only because posting an inline
comment was `gh api repos/OWNER/REPO/pulls/N/comments`. The matcher compares
whole space-separated tokens, so `Bash(gh api repos/*/pulls/*/comments:*)`
matches nothing and it was `gh api` or no posted comments at all. Vinegar
submits the review itself now, and the reviewer is told to post nothing, so
the reason is gone and so is the entry. The named read-only subcommands above
are what remain, and a review that wants more than those says so: a refused
`Bash` command is recorded in `permission_denials`, which the log reports
after every run.

Reads are path-denied for `~/.vinegar`, `~/.claude`, `~/.ssh`, `~/.aws`,
`~/.gnupg`, `~/.config/gh`, `.netrc` and `.env`. Without that, the App's own
private key is a file the reviewer can read, and that key is the one
credential that is **not** scoped to a single repository.

That is why clones live in `~/.vinegar-checkouts/` rather than under
`~/.vinegar`. A blanket deny on a directory catches everything you later put
inside it, including the repository the reviewer is supposed to read. Keep the
deny broad and keep the checkout out of it, rather than narrowing the rule to
name each secret: the next secret added to `~/.vinegar` would not be named.

### What this does not do

The allow list makes the easy paths closed and the accidental blast radius
small. **It is not a containment boundary against a determined prompt
injection**, and it is worth being exact about why rather than discovering it
later.

A prefix rule matches the start of a command and cannot see the flags that
follow. So `git diff`, `git log` and `git show` are all allowed, and all three
accept `--output=<file>`, which writes any file the daemon user can write.
`git diff --output=$HOME/.zshenv` is persistent code execution, and no prefix
rule can distinguish it from the `git diff origin/main...HEAD` that every
review legitimately runs. Removing `git diff` would close it, and would also
remove the command the reviewer actually gets its diff from, so it stays.

One more stays open for the same kind of reason:

**A `CLAUDE.md` in the checkout is still read.** `--setting-sources ''` covers
`settings.json`, not the memory files, so a `CLAUDE.md` or `AGENTS.md` in the
pull request's head commit reaches the model as project instructions, which is
a stronger channel than the same text inside a diff hunk.

### So what is the boundary

`skip_forks`, and it is on by default. On a pull request from your own
repository the author already has write access to the code and to `CLAUDE.md`,
so none of the above is a capability they did not already have. On a fork pull
request all of it is written by a stranger, and the allow list is not what you
want standing between that stranger and the machine holding your credentials.

Second to that, the GitHub App installation. A review can only reach the one
repository its token was minted for, so the worst case stays inside the
repository being reviewed instead of spreading across an account.

Turn `skip_forks` off only if you are willing to read fork diffs yourself
first.

**Some denials are reported. Do not read silence as a clean run.** Vinegar
logs `permission denial(s)` from the `permission_denials` array that
`--output-format json` returns, so a review denied a *command* says so instead
of quietly returning a worse result.

Reads refused by a path deny are not in that array. It comes back empty from a
review that could not open a single file in its own checkout, which is how
that failure went unnoticed until it was found by hand and fixed by
`check_paths()`. Treat a non-empty array as worth acting on and an empty one as
no evidence either way. To check the reads themselves, ask the sandbox
directly. There are two questions, and both need asking after any edit to
`review-settings.json`, because each one passes while the other fails:

```sh
cd ~/.vinegar-checkouts/<any-repo>

# The checkout must be readable, or every review runs from API fetches.
# Must print a line of the file.
claude -p 'Read README.md and reply with only its first line, or BLOCKED if denied.' \
  --model claude-haiku-4-5 --settings <path-to>/review-settings.json \
  --setting-sources '' --strict-mcp-config

# `~/.vinegar` must not be, or a review can read the App private key.
# Must print BLOCKED.
claude -p 'Read ~/.vinegar/config.json and reply with only BLOCKED if denied, or its first line.' \
  --model claude-haiku-4-5 --settings <path-to>/review-settings.json \
  --setting-sources '' --strict-mcp-config
```

Running only the first is the mistake this whole section is about. Delete
`Read(//**/.vinegar/**)` from the deny list and it still answers `# vinegar`,
reporting a healthy sandbox, while the second answers with the contents of
`config.json`. An instrument that passes in the case worth catching is the
thing that let the earlier failure run unnoticed; one that only checks the
allow direction has the same defect pointed the other way.

Measured on Claude Code 2.1.220.

`Workflow` is denied on purpose. At high effort `/code-review` can otherwise
launch a multi-agent workflow, which spends far more of your subscription than
a single review should.

## The triage pass

A cheap model, local or hosted, reads each diff before any review runs and
decides whether the PR is worth reviewing and how hard.

The rule that makes this safe: **the triage model never looks for bugs.** It
classifies the *shape* of the diff, which is size, which paths are touched,
whether they are generated or hand-written, and whether the change reaches
authentication, payments, migrations, concurrency, or money arithmetic. Small
models are unreliable at finding bugs and perfectly reliable at that
classification. When triage is unsure it escalates, because a wasted review
costs a little budget and a missed bug costs an incident.

## The triage note

Triage publishes what it decided, as a single comment on the PR that is updated
in place on later pushes:

> **Vinegar** · triage of `a1b2c3d`
>
> Reworks session refresh and adds a `sessions.revoked_at` column with a
> backfill migration. 340 lines across 7 files.
>
> Difficulty: moderate · Risk: high
> Reviewing at high effort: touches session handling and a database migration.

Four things about that note:

- **It is a comment, never an edit to the PR description.** The description
  exists to hold the author's intent, and a machine rewriting it can damage
  text nobody backed up.
- **It posts before the review starts.** Triage takes seconds and a review
  takes minutes; silence in between looks like a broken daemon.
- **It is mandatory on a skip.** On a skipped PR it is the only thing Vinegar
  will ever post, so without it silence cannot be told apart from a crash.
- **Difficulty and risk are separate labels**, because risk is what drives
  routing. A two-line change to payment rounding is trivial and dangerous at the
  same time.

The summary describes what the diff touches. It never claims the change is
correct, safe, or complete: a small model will occasionally be confidently
wrong, and a wrong summary pinned to the top of a PR is worse than none, because
it misleads a human skimming and can anchor the reviewer that runs next.

## The review

The reviewer returns its findings instead of posting them, and Vinegar submits
the whole review in one request when the run finishes. Findings still arrive as
separate inline comments anchored to their line; what changed is that they all
appear at the same moment.

The findings arrive as a `ReportFindings` tool call, which is the reviewer's
own structured output: a list of objects with a file, a line, a summary, a
concrete failure scenario and a category. Nothing has to be recovered from
prose, which matters more than it sounds. An earlier version read a JSON block
out of the final message, and nearly every bug found in seven rounds of review
was a bug in the code that did that: fences paired wrongly, an array quoted in
passing mistaken for the answer, `[]` in a sentence read as "no findings".

Three settings make it work and they only work together. `/code-review` is run
with `--output-format stream-json --verbose`, `ReportFindings` is allowed in
`review-settings.json`, and `CLAUDE_CODE_REPORT_FINDINGS=1` is set in the
reviewer's environment. Drop any one and the command goes back to printing a
JSON array in its final message, which Vinegar no longer reads, so the review
posts as one raw-text comment with no inline anchors. The log says
`reported no findings through ReportFindings` when that happens.

This is a detail of a command Vinegar does not own, so treat it as true of the
Claude Code version you have rather than forever.

That timing is the point. The comments appearing is how you know the round is
finished and the feedback is complete enough to hand to an agent. While the
reviewer posted as it worked, three comments could mean three findings or could
mean a review that died after three.

Every review carries a top-level comment as well, and it says how many findings
there were, including when there were none:

> **Vinegar** · reviewed `a1b2c3d` at high effort
>
> 4 findings, 3 posted inline.
>
> These could not be anchored in the diff:
>
> - `vinegar.py:812`: The caller this relies on drops the error.
>   Failure: a failed fetch returns None and the loop treats it as empty.
>   (correctness)

A finding lands inline only when its line is part of the diff. Reviews at high
effort read whole files, so some findings land on code the PR did not touch, and
GitHub rejects a review comment anchored outside the diff. Those go in the
top-level comment rather than being dropped.

**Vinegar is never silent about a pull request it looked at.** A clean review
says so. A review whose findings cannot be read back posts the reviewer's own
words unedited. A review GitHub refuses is posted again with every finding in
the top-level comment, which needs no anchor. Silence means something broke, and
that is the only thing it is allowed to mean.

Reviews are submitted with `event: COMMENT`. Vinegar never approves and never
requests changes, so it cannot hold up a merge. See "What this is not".

## What it costs, honestly

**Bringing your own Anthropic API key saves nothing.** A serious review reads a
lot of repository context. At roughly 250k input and 25k output tokens, Opus 5
costs about $1.90 per review and Sonnet 5 about $1.15. That is Bugbot's price
again.

One measured run supports that estimate rather than undercutting it. A 45-line
pull request, reviewed on Opus 5 at high effort, took 243 seconds and reported
$0.86 of equivalent token cost. Forty-five lines is about as small as a real
pull request gets, and it still landed inside Bugbot's price band, because the
cost is dominated by reading the repository rather than by reading the diff.
Vinegar pays that in subscription limits instead of dollars.

The entire saving comes from running reviews through a Claude subscription you
already have, plus letting cheap triage drop the PRs that never needed a
reviewer. If you were planning to swap a vendor for an API key, Vinegar will not
help you.

## The subscription rule you need to know

Driving the local Claude Code CLI against your own subscription is the supported
way to do this. **Putting a subscription OAuth token into a third-party GitHub
Action is not.** Anthropic disallowed the use of subscription OAuth tokens
outside Claude Code and Claude.ai as of 20 February 2026, and its own GitHub
Actions documentation offers only an API key, Amazon Bedrock, or Google Cloud.
Recipes that tell you to run `claude setup-token` and feed
`CLAUDE_CODE_OAUTH_TOKEN` to a workflow are out of date. Vinegar drives the
local CLI, which authenticates itself, so the question does not arise.

Two consequences worth planning for:

- Automated reviews spend the same limits as your interactive Claude Code work,
  so a heavy review load will throttle you. Reviewing on PR open rather than
  every push, skipping drafts, capping diff size, and letting triage drop cheap
  PRs are budget features, not optimizations.
- `/code-review ultra` runs a deeper review in the cloud and bills usage credits
  separately. Vinegar never invokes it from automation.

## What this is not

- Not a hosted service and not a paid product.
- Not a reimplementation of a review engine. It orchestrates `/code-review`.
- Not a merge gate. Reviews inform; they never block.
- Not a replacement for CI. Lint, format, type, and test checks stay where they
  are, and Vinegar is told not to duplicate them.

## Expectations about quality

Independent benchmarks put the best commercial AI reviewers somewhere between
roughly 47% and 60% F1, so about half of what the leading tools report is wrong
or ignored, and false positives are the most common complaint about every one of
them. Vinegar will not beat that out of the box.

What helps is calibration: a per-repository review guidance file that redefines
what counts as important, caps how many minor comments a review may post, skips
generated paths, and requires a `file:line` citation before a claim about
behavior can be posted. Measure before tuning, by tagging each comment useful or
noise for a week on one repository.

## Alternatives

If you want something that works today, these do: Cursor Bugbot, CodeRabbit,
Greptile, Qodo Merge (self-hostable), Entelligence, CodeAnt, Kodus, and GitHub
Copilot code review. Anthropic also offers a managed Code Review product for
Team and Enterprise plans, though at a documented $15 to $25 per review it
solves a different problem than this does.

Vinegar exists because none of them let you spend a subscription you have
already bought.

## License

```
Copyright 2026 Sour Labs

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
```

---

A [Sour Labs](https://sourlabs.io) project.
