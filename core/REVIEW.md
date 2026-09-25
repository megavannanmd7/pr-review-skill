# PR Review — the procedure

This is the platform-neutral core of the `pr-review` skill. Every adapter (Antigravity,
Claude Code, Cursor, Gemini CLI) is a thin pointer at this file, so the review rubric and
the dedup rules live here once and never drift between tools.

Review a GitHub PR and post only findings that are **new** and **actionable** as inline
comments. Running this twice on an unchanged PR must post nothing.

Authentication is the local `gh` CLI. Never ask for, read, or create a Personal Access
Token, and never print credentials.

Throughout, `<CORE>` means the directory containing this file — normally
`~/.pr-review-skill/core/` (`%USERPROFILE%\.pr-review-skill\core\` on Windows). The
scripts are at `<CORE>/scripts/`. Use `python` or `python3`, whichever exists.

## Invocation

The adapter passes you the user's arguments. All of these are equivalent ways to name
the same PR:

```
842 paxiai-event-processor
Review PR 842 in paxiai-event-processor
Review PR 842 in jpteam/paxiai-event-processor --dry-run
Review https://github.com/jpteam/paxiai-event-processor/pull/842
Review jpteam/paxiai-event-processor#842
```

Parse whichever form appears: a bare number plus a repo name, `owner/repo` plus a
number, a full PR URL, or the `owner/repo#number` shorthand. If only a bare number is
given with no repo at all, resolve it the same way as Step 1 resolves a bare repo name
(current workspace, then ask).

Anything else in the message is review context. Two forms:

- Free text (`Pay special attention to: Redis usage, error handling, race conditions`)
  becomes an explicit priority list for the review.
- `Additional context: <path>.md` means read that file (relative to the current
  directory, then the workspace root) and treat its contents as review guidelines.

Flags, forwarded to the scripts in Step 2 and Step 7:

- `--dry-run` — summarise only, never post.
- `--max N` — cap findings (default 10), applied by you when writing findings.json.
- `--min-severity warning|suggestion|question|blocker` — passed to `pr_post.py`.
  Default posting behaviour (when the user hasn't asked for anything else): pass
  `--min-severity warning` yourself in Step 7, so `blocker`/`warning` findings post
  inline and `suggestion`/`question` findings land in the review body summary only,
  not as separate inline comments. If the user's message says something like "show
  me everything" or explicitly types `--min-severity question`, pass that instead.
- `--event COMMENT|REQUEST_CHANGES|APPROVE` — passed to `pr_post.py`, default
  `COMMENT`. Only pass something else if the user explicitly asked for it in this
  invocation; never choose `REQUEST_CHANGES` or `APPROVE` on your own judgement.
- `--include-lockfiles` — passed to `pr_fetch.py` if the user explicitly wants
  lockfiles/generated files reviewed (skipped by default, see Step 2).

## Step 0 — Preflight

1. Run `gh auth status`. If `gh` is missing or unauthenticated, stop and tell the user
   exactly this, then end the turn:

   ```
   winget install --id GitHub.cli      # macOS: brew install gh   Linux: see cli.github.com
   gh auth login                       # GitHub.com -> HTTPS -> login with a browser
   ```

2. Permission is checked automatically as part of Step 2 (`pr_fetch.py` reports
   `viewer_permission` in the bundle). If it comes back `read` or `none`, tell the user
   before doing any review work — posting in Step 7 will fail with `403` otherwise —
   and ask whether to continue read-only (summary only, no posting) or stop.

## Step 1 — Resolve the target

Turn the request into `owner/repo` and a PR number (see Invocation above for the
accepted input forms):

1. If the user gave `owner/repo` or a full URL, use it directly.
2. Otherwise look for a local clone of that repo name under the workspace and read
   `git -C <clone> remote get-url origin`.
3. Otherwise try `gh repo list <likely-org> --limit 200 | grep <name>`.
4. If still ambiguous, ask — do not guess.

Create a working directory for this run, e.g. `$TMPDIR/pr-review/<repo>-<number>/`
(on Windows `%TEMP%\pr-review\<repo>-<number>\`). All intermediate files go there,
never in the user's repo.

## Step 2 — Fetch the PR

```
python <CORE>/scripts/pr_fetch.py <owner/repo> <number> --out <workdir>/bundle.json
```

`bundle.json` contains PR metadata, `viewer_permission` (your access level on this
repo), the diff per file, `commentable` (the exact lines GitHub will accept comments
on, per side), `skipped_files` (lockfiles/generated files left out by default —
pass `--include-lockfiles` to include them), every existing comment/review tagged
with a stable `id` (`rc:`/`ic:`/`rv:` prefix — see Step 5), each inline comment's
`resolved` / `outdated` state, and `posted_fingerprints` (what this skill has already
posted on this PR).

Read the bundle. If `state` is closed/merged, say so and ask before continuing. If
`viewer_permission` is `read` or `none`, see Step 0.2.

A file can have `patch_omitted: true` if GitHub couldn't produce a diff for it at all
(genuinely binary, or unavailable from both the files endpoint and the whole-PR-diff
fallback the script already tries). Do not anchor a comment to such a file; mention any
concern about it in the summary instead.

## Step 3 — Build context

The diff alone is not enough to judge correctness.

- Read the full current version of each changed file at the PR head in one call, no
  decoding step needed:
  ```
  gh api repos/<owner/repo>/contents/<path>?ref=<head_sha> -H "Accept: application/vnd.github.raw"
  ```
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
  "suggestion": "optional replacement code for the commented line(s)",
  "duplicate_of_id": "rc:123456789"
}
```

`anchor` is the enclosing function/class/symbol, and `issue_class` is one tag from:

`null-deref`, `race-condition`, `missing-await`, `unhandled-rejection`,
`missing-error-handling`, `swallowed-error`, `missing-timeout`, `retry-misuse`,
`resource-leak`, `injection`, `authz`, `secret-leak`, `input-validation`,
`n-plus-one`, `blocking-event-loop`, `unbounded-growth`, `cache-key`,
`schema-compat`, `migration-risk`, `idempotency`, `logic-error`, `off-by-one`,
`dead-code`, `test-gap`, `observability`, `other`.

`path`, `anchor` and `issue_class` together make a fingerprint the script computes
itself (`sha1(path|anchor|issue_class)`), a best-effort secondary signal for "this
skill already said this" that survives line-number shifts and rewording. It is not
the primary defense — see `duplicate_of_id` in Step 5 for that. Still choose `anchor`
and `issue_class` deliberately; a consistent naming choice makes the fingerprint more
useful across reruns, it just isn't load-bearing on its own anymore.

`duplicate_of_id` is optional and is set in Step 5b, once you've checked the
finding against `bundle.existing`.

## Step 5 — Deduplicate

This is the point of the skill. Work through three stages, in order.

**5a. Fingerprint match (exact, code-enforced).** `pr_post.py` drops any finding whose
computed fingerprint is already in `posted_fingerprints` — you don't need to do
anything for this stage, it's automatic. It's also the weakest stage: it only catches
a finding that happens to get the same `anchor`/`issue_class` twice, so don't rely on
it alone. Stage 5b below is what actually matters.

**5b. Named-duplicate match (judgement, but code-enforced once you name it).** For
each finding, check it against **every** entry in `bundle.existing` — all of
`review_comments`, `issue_comments`, and `reviews`, not just nearby inline comments.
A general PR-level comment ("we're migrating off Redis, don't add new keys there") can
make a specific inline finding moot even if it's nowhere near that finding's line, so
check the whole set, not just a line window.

For each existing entry, ask: *does this already raise the same underlying issue as
my finding?* Judge the issue, not the wording:

```
existing: "This can fail when project is null."
new:      "project.id is accessed without checking whether project exists."
→ same issue.

existing: "This can fail when project is null."
new:      "project.id is read from an untrusted request body and used in a SQL string."
→ not the same issue — different risk, keep it.
```

A reply saying the issue is handled, intentional, or out of scope also counts as
covered. Author replies that merely acknowledge ("will fix") still count: the issue is
already known.

If you find a match, **set `"duplicate_of_id"` on the finding to that entry's `id`**
(e.g. `"rc:123456789"`) instead of just silently dropping it yourself. `pr_post.py`
checks that id is real and enforces the skip in code — this is what makes
deduplication reliable even when your own judgement about wording or anchor naming
varies between runs. If you're confident it's a duplicate but can't find the exact
entry it matches, drop the finding yourself and say why in the summary; don't invent
an id.

**5c. Resolved vs. outdated — these are not the same thing.** GitHub sets a thread's
`outdated: true` automatically the moment a new commit changes the diff context around
it — whether or not anyone actually fixed the problem. Do not treat `outdated` as
"handled".

- `resolved: true` → the issue was explicitly marked resolved by a human. Never
  repost; count it under "existing issues already resolved" in the summary.
- `outdated: true` and `resolved: false` → the thread is stale (its anchor moved) but
  **nobody confirmed the issue is fixed**. Check the current file content at the PR
  head: if the same problem is still there, still raise it — either as a fresh finding
  at the new location, or noted in the summary as "still unresolved, see the outdated
  thread at `<id>`". Only skip it if you can confirm from the current code that it's
  actually been fixed.

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

Add a "Skipped as duplicate" list naming which existing comment/review each one
matched (its `id`), so the dedup is auditable. If `pr_fetch.py` reported any
`skipped_files` (lockfiles etc.), mention the count in one line. If you used
`--min-severity` to hold back low-severity findings, say how many and that they're in
the review body summary rather than dropped.

Then **stop and wait for approval**. Do not post anything yet. Accept `post`,
`post 1 and 3`, `skip 2`, or `no`. With `--dry-run`, stop here permanently.

## Step 7 — Post

Write the approved findings to `<workdir>/findings.json`:

```json
{ "summary": "optional markdown for the review body", "findings": [ ... ] }
```

Then:

```
python <CORE>/scripts/pr_post.py <owner/repo> <number> \
    --findings <workdir>/findings.json --bundle <workdir>/bundle.json \
    --min-severity warning
```

(Add `--event` only if the user explicitly asked for `REQUEST_CHANGES` or `APPROVE`
in this invocation.)

The script appends the fingerprint marker and a visible "AI-assisted review" line to
every comment, enforces `duplicate_of_id` and the fingerprint check, validates each
line against the diff, posts one batched review, and falls back to posting comment by
comment if GitHub rejects the batch. Findings that cannot be anchored go into the
review body in full (not just the title) rather than being dropped or truncated.

Report its JSON result plainly: how many posted, what was skipped and why (`fingerprint`
vs `duplicate_of_id`), what failed, and the PR URL. If `unverified_duplicate_claims` is
non-empty, mention it — it means you named an id that turned out not to be on the PR,
worth double-checking. If `failed` is non-empty, say so — do not report success.

## Step 8 — Clean up

```
git -C <clone> worktree remove --force <workdir>/wt     # if one was created
```

If this fails (a locked file, an antivirus scan, an IDE language server still holding
a handle), don't treat it as blocking — tell the user the worktree is at `<workdir>/wt`
and can be removed later with the same command; never retry destructively or touch the
user's actual working tree to work around it.

Leave `bundle.json` and `findings.json` in the temp working directory; they are useful
if the user asks why something was skipped, and they are not state — the PR itself is
the only source of truth for what has already been said.
