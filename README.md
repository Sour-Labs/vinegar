# vinegar

A self-hosted pull-request reviewer. It runs on a machine you already own, uses
a Claude subscription you already pay for, and posts inline review comments on
your PRs.

> **Status: design stage. There is no working code in this repo yet.**
> The architecture and the constraints are settled and written down below.
> Nothing here has been run against a real PR.

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

**A self-hosted GitHub Actions runner is deliberately not used.** It would give
nicer triggering, but GitHub's own guidance is that self-hosted runners should
almost never serve public repositories: anyone can open a pull request from a
fork and execute code on the runner. The machine running Vinegar holds your
Claude credentials, which makes it the worst possible host for untrusted PR
code.

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

Not chosen yet. Until a license file is added, default copyright applies and
this code is not yet reusable by others.

---

A [Sour Labs](https://sourlabs.io) project.
