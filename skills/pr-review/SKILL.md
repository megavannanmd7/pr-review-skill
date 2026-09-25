---
name: pr-review
description: Reviews a GitHub pull request and posts only genuinely new, actionable findings as inline PR comments. Use when the user asks to review a pull request, for example "Review PR 842 in paxiai-event-processor", optionally with extra focus areas or a context .md file. Deduplicates against comments already on the PR, so the same PR can be reviewed repeatedly without repeating comments.
---

# PR Review (inline, deduplicated)

Review a GitHub PR and post only findings that are **new** and **actionable** as inline
comments. Running this twice on an unchanged PR must post nothing.

Authentication is the local `gh` CLI. Never ask for, read, or create a Personal Access
Token, and never print credentials.

## Invocation

```
/pr-review 842 paxiai-event-processor
Review PR 842 in paxiai-event-processor
Review PR 842 in jpteam/paxiai-event-processor --dry-run
```

Anything else in the message is review context. Two forms:

- Free text (`Pay special attention to: Redis usage, error handling, race conditions`)
  becomes an explicit priority list for the review.
- `Additional context: <path>.md` means read that file (relative to the current
  directory, then the workspace root) and treat its contents as review guidelines.

Flags: `--dry-run` (summarise only, never post), `--max N` (cap findings, default 10).

## Step 0 — Preflight

Run `gh auth status`. If `gh` is missing or unauthenticated, stop and tell the user
exactly this, then end the turn:

```
winget install --id GitHub.cli      # macOS: brew install gh   Linux: see cli.github.com
gh auth login                       # GitHub.com -> HTTPS -> login with a browser
```

## Step 1 — Resolve the target

Turn the request into `owner/repo` and a PR number:

1. If the user gave `owner/repo`, use it.
2. Otherwise look for a local clone of that repo name under the workspace and read
   `git -C <clone> remote get-url origin`.
3. Otherwise try `gh repo list <likely-org> --limit 200 | grep <name>`.
4. If still ambiguous, ask — do not guess.

Create a working directory for this run, e.g. `$TMPDIR/pr-review/<repo>-<number>/`
(on Windows `%TEMP%\pr-review\<repo>-<number>\`). All intermediate files go there,
never in the user's repo.

## Step 2 — Fetch the PR

```
python <skill>/scripts/pr_fetch.py <owner/repo> <number> --out <workdir>/bundle.json
```

`bundle.json` contains PR metadata, the diff per file, `commentable` (the exact lines
GitHub will accept comments on, per side), every existing review comment with its
`resolved` / `outdated` state, issue comments, review bodies, and
`posted_fingerprints` (what this skill has already posted on this PR).

Read the bundle. If `state` is closed/merged, say so and ask before continuing.

## Step 3 — Build context

The diff alone is not enough to judge correctness.

- Read the full current version of each changed file at the PR head:
  `gh api repos/<owner/repo>/contents/<path>?ref=<head_sha> --jq .content | base64 -d`
- If a local clone exists, you can also get whole-repo grep without touching the
  user's working tree or branch:
  ```
  git -C <clone> fetch origin pull/<number>/head:refs/remotes/pr/<number>
  git -C <clone> worktree add --detach <workdir>/wt refs/remotes/pr/<number>
  ```
  Never `checkout`, `stash`, or `reset` in the user's own working tree.
- Follow callers and callees of changed functions when a finding depends on them.

## Step 4 — Review

Produce findings across: logic and edge cases (null/undefined, off-by-one, missing
`await`, unhandled rejection), concurrency (races, non-atomic read-modify-write,
missing idempotency), error handling (swallowed errors, missing timeouts/retries,
partial failure), security (injection, authz/tenant isolation, secret leakage),
performance (N+1, unbounded queries, event-loop blocking, leaks), contracts (breaking
API/event-schema/migration changes), and data-store misuse (key collisions, missing
TTL, unbounded growth).

Apply the user's stated priorities first, then the rest.

Hard rules:

- **Only lines that appear in the diff.** Check `commentable[path][side]` before
  writing a finding; if the issue is in unchanged code, mention it in the summary
  instead of anchoring a comment to it.
- **No formatting or style nits.** No whitespace, import order, naming preferences.
- **No restating what the code does.** A comment must assert a risk, a bug, or a
  concrete improvement.
- **Evidence, not speculation.** Do not claim a build or test failure unless you ran
  it. Say "can be null when X" only if you traced X.
- Prefer few strong findings over many weak ones. Cap at `--max` (default 10),
  highest severity first. Zero findings is a valid outcome.

Each finding is an object:

```json
{
  "path": "src/foo.ts",
  "line": 42,
  "side": "RIGHT",
  "severity": "blocker | warning | suggestion | question",
  "issue_class": "null-deref",
  "anchor": "handleEvent",
  "title": "Possible null dereference",
  "body": "Why it is wrong, when it breaks, what to do instead.",
  "suggestion": "optional replacement code for the commented line(s)"
}
```

`anchor` is the enclosing function/class/symbol, and `issue_class` is one tag from:

`null-deref`, `race-condition`, `missing-await`, `unhandled-rejection`,
`missing-error-handling`, `swallowed-error`, `missing-timeout`, `retry-misuse`,
`resource-leak`, `injection`, `authz`, `secret-leak`, `input-validation`,
`n-plus-one`, `blocking-event-loop`, `unbounded-growth`, `cache-key`,
`schema-compat`, `migration-risk`, `idempotency`, `logic-error`, `off-by-one`,
`dead-code`, `test-gap`, `observability`, `other`.

Together these two fields make the fingerprint `sha1(path|anchor|issue_class)`, which
stays stable when line numbers shift, when the PR is pushed to again, and when you
word the same finding differently on a later run. Choose them deliberately.

## Step 5 — Deduplicate

This is the point of the skill. Work through three stages, in order.

**5a. Fingerprint match (exact).** Drop any finding whose fingerprint is in
`posted_fingerprints`. This alone makes reruns on an unchanged PR silent.

**5b. Semantic match (judgement).** For each surviving finding, collect existing
comments on the **same file** that sit in the same hunk or within ~15 lines of the
finding, including replies in those threads. Ask yourself, per pair: *does this
existing comment already raise the same underlying issue?* Judge the issue, not the
wording. If yes, drop the finding.

These are the same issue:

```
existing: "This can fail when project is null."
new:      "project.id is accessed without checking whether project exists."
```

These are not:

```
existing: "This can fail when project is null."
new:      "project.id is read from an untrusted request body and used in a SQL string."
```

A reply saying the issue is handled, intentional, or out of scope also counts as
covered — drop it. Author replies that merely acknowledge ("will fix") still count:
the issue is already known.

**5c. Resolved and outdated.** If the match is on a thread marked `resolved` or
`outdated`, do not repost. Count it under "existing issues already resolved" in the
summary, and mention it in the summary body only if the problem is demonstrably still
present at the head SHA.

Finally, deduplicate the findings against each other: one root cause gets one comment,
placed at the most relevant line.

## Step 6 — Summary, then wait

Print exactly this shape:

```
Review complete

Findings:
- 3 new comments
- 2 duplicates skipped
- 1 existing issue already resolved

New comments:
1. src/foo.ts:42 — Possible null dereference
2. src/bar.ts:118 — Redis key collision
3. src/baz.ts:76 — Missing error handling
```

Add a "Skipped as duplicate" list naming which existing comment each one matched, so
the dedup is auditable.

Then **stop and wait for approval**. Do not post anything yet. Accept `post`,
`post 1 and 3`, `skip 2`, or `no`. With `--dry-run`, stop here permanently.

## Step 7 — Post

Write the approved findings to `<workdir>/findings.json`:

```json
{ "summary": "optional markdown for the review body", "findings": [ ... ] }
```

Then:

```
python <skill>/scripts/pr_post.py <owner/repo> <number> \
    --findings <workdir>/findings.json --bundle <workdir>/bundle.json
```

The script appends the fingerprint marker to every comment, skips anything already on
the PR, validates each line against the diff, posts one batched review, and falls back
to posting comment by comment if GitHub rejects the batch. Findings that cannot be
anchored go into the review body rather than being dropped.

Report its JSON result plainly: how many posted, what was skipped, what failed, and
the PR URL. If `failed` is non-empty, say so — do not report success.

## Step 8 — Clean up

```
git -C <clone> worktree remove --force <workdir>/wt     # if one was created
```

Leave `bundle.json` and `findings.json` in the temp working directory; they are useful
if the user asks why something was skipped, and they are not state — the PR itself is
the only source of truth for what has already been said.
