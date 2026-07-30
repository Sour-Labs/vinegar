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
`/code-review` command that takes a PR number, posts its findings as inline
comments with `--comment`, and runs non-interactively:

```sh
claude -p '/code-review 123 --comment'
```

Vinegar is the trigger, the router, and the calibration around that.

```
new PR (or a new head commit)
        │
        ▼
   triage: a cheap model reads the diff  ──▶  posts a sticky comment on the PR
        │
        ├─ skip    nothing here needs a reviewer
        ├─ light   claude -p '/code-review <n> --comment'   (low/medium effort)
        └─ full    claude -p '/code-review <n> --comment'   (high effort)
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

The review lands in `~/.vinegar/reviews/`. When it reads well, poll once, then
run the loop:

```sh
python3 vinegar.py --once                # one pass, then exit
python3 vinegar.py                       # poll forever
```

Vinegar keeps everything under `~/.vinegar`: `config.json`, `state.json` (the
head commit it last handled per pull request), `checkouts/` (one clone per
repo, which only Vinegar touches), and `reviews/`.

### Configuration

Every key in `config.example.json`:

| Key | Default | What it does |
| --- | --- | --- |
| `repos` | none | Repositories to poll, as `owner/name`. Required. |
| `poll_interval` | `60` | Seconds between polls. |
| `effort` | `"high"` | Effort passed to `/code-review`: `low`, `medium`, `high`, `xhigh`, `max`. `ultra` is rejected. |
| `comment` | `true` | Post findings on the pull request. False runs the review and writes only to `~/.vinegar/reviews/`. |
| `model` | `null` | Model for the review. Null uses your Claude Code default. |
| `review_on_push` | `false` | Review again when the head commit changes. |
| `max_changed_lines` | `3000` | Skip pull requests larger than this. |
| `skip_drafts` | `true` | Skip drafts. |
| `skip_bots` | `true` | Skip pull requests opened by bots. |
| `skip_forks` | `true` | Skip pull requests whose head branch lives in a fork. Read the next section before you turn this off. |
| `authors` | `[]` | Only review these GitHub logins. Empty means anyone who passes the checks above. |
| `review_timeout` | `1800` | Kill a review that runs longer than this many seconds. |

The last five are budget and safety controls, not optimizations. Automated
reviews spend the same subscription limits as your interactive Claude Code
work.

### Running it under launchd

`launchd/io.sourlabs.vinegar.plist` is a template. Replace every
`YOUR_USERNAME` and every `VINEGAR_PATH`, then:

```sh
mkdir -p ~/.vinegar/logs
cp launchd/io.sourlabs.vinegar.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/io.sourlabs.vinegar.plist
tail -f ~/.vinegar/logs/vinegar.log
```

The plist sets `PATH` explicitly because launchd starts with a bare one, and
a daemon that cannot find `claude` fails at the first review.

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

Three limits worth stating plainly.

**`gh api` is allowed and cannot be narrowed.** Posting an inline comment is
`gh api repos/OWNER/REPO/pulls/N/comments`, and the permission matcher compares
whole space-separated tokens, so `Bash(gh api repos/*/pulls/*/comments:*)` does
not match anything. It is `gh api` or no posted comments. A review can
therefore reach any GitHub endpoint your token can.

**A `CLAUDE.md` in the checkout is still read.** `--setting-sources ''` covers
`settings.json`, not the memory files, so a `CLAUDE.md` or `AGENTS.md` in the
pull request's head commit reaches the model as project instructions, which is
a stronger channel than the same text inside a diff hunk.

**Denials are reported, not silenced.** Vinegar logs `permission denial(s)` so
you can see a review that ran with less than it asked for.

Those first two are why `skip_forks` stays on by default. On a pull request
from your own repository the author already has write access and none of this
is a new capability. On a fork pull request every one of those files is written
by someone else.

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
