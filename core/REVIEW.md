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

`bundle.json` contains PR metadata, `viewer_permission` (your access level), `commits`
(messages, for intent), `checks` (CI conclusions and per-file annotations), the diff
per file, `commentable` (the exact lines GitHub will accept comments on, per side),
`skipped_files` (lockfiles/generated files left out by default), every existing
comment/review tagged with a stable `id` (`rc:`/`ic:`/`rv:`), each inline comment's
`resolved` / `outdated` state, and `posted_fingerprints`.

Read the bundle. If `state` is closed/merged, say so and ask before continuing. If
`viewer_permission` is `read` or `none`, see Step 0.2.

A file can have `patch_omitted: true` if GitHub couldn't produce a diff for it at all
(genuinely binary, or unavailable from both the files endpoint and the whole-PR-diff
fallback the script already tries). Do not anchor a comment to such a file; mention any
concern about it in the summary instead.

## Step 3 — Build context

The diff alone is not enough to judge correctness. Four things to establish before
reviewing:

**3a. Intent.** Read the PR title, body, and `commits[].message`. A behaviour change
the author describes as deliberate is not a bug. If the PR says "findUser now throws
instead of returning null", do not report that as a defect — report the callers that
weren't updated (see 3d).

**3b. What CI already knows.** Check `checks` in the bundle. If `state` is `failure`,
the `annotations` array holds real compiler/linter/test output with file and line.
**Never report something CI has already flagged** — the author can see it, and
repeating it is pure noise. Do use it as evidence: a failing typecheck tells you the
contract is genuinely broken, where you would otherwise be speculating. Equally, never
claim a build or test failure that isn't in `checks` — you have not run anything.

**3c. The code around the change.** Read the full current version of each changed file
at the PR head, in one call, no decoding step:

```
gh api repos/<owner/repo>/contents/<path>?ref=<head_sha> -H "Accept: application/vnd.github.raw"
```

If a local clone exists, you can also get whole-repo grep without touching the user's
working tree or branch:

```
git -C <clone> fetch origin pull/<number>/head:refs/remotes/pr/<number>
git -C <clone> worktree add --detach <workdir>/wt refs/remotes/pr/<number>
```

Never `checkout`, `stash`, or `reset` in the user's own working tree.

**3d. Blast radius — required when the PR changes a declaration.** If any changed line
alters or removes a function, method, class, type, or exported constant declaration,
run:

```
python <CORE>/scripts/pr_impact.py --bundle <workdir>/bundle.json \
    --clone <clone-or-workdir/wt> --out <workdir>/impact.json
```

This lists call sites **outside** the PR's own files for every symbol whose
declaration the PR changed or removed. This is not optional garnish: the most damaging
PR bugs are invisible in the diff. A function that changes from returning `null` to
throwing looks perfectly correct in isolation while breaking every caller that checks
`if (!result)`. Read the sample call sites it reports and check whether they still
hold under the new behaviour.

Its output is a heuristic word-grep, not a compiler — a hit can be an unrelated symbol
with the same name. Verify a call site by reading it before you report it. If no local
clone exists, say so in the summary rather than skipping the question silently: the
review is weaker without it and the user should know.

## Step 4 — Review

### Scope: what you analyse vs. where you may comment

These are two different things, and conflating them is how real bugs get missed:

- **Analysis scope is the whole repository.** Read callers, callees, sibling modules,
  config, tests — anything needed to judge the change.
- **Anchor scope is the diff.** GitHub only accepts an inline comment on a line in a
  diff hunk, so an inline finding must sit on a line in `commentable[path][side]`.

A problem in unchanged code that this PR *causes* is a real finding. Report it in the
review summary, or anchor it to the changed line that causes it — never discard it just
because the broken caller isn't in the diff.

### Dimensions

Logic and edge cases (null/undefined, off-by-one, missing `await`, unhandled
rejection), concurrency (races, non-atomic read-modify-write, missing idempotency),
error handling (swallowed errors, missing timeouts/retries, partial failure), security
(injection, authz/tenant isolation, secret leakage), performance (N+1, unbounded
queries, event-loop blocking, leaks), contracts (breaking API/event-schema/migration
changes, and callers left behind — see 3d), data-store misuse (key collisions, missing
TTL, unbounded growth), and tests (assertions deleted or weakened so a change passes;
new branching logic with no test touching it).

Apply the user's stated priorities first, then the rest.

### Evidence rules — these are what keep the false-positive rate down

**1. Every finding needs a concrete failure trigger.** State the specific input, state,
or sequence that produces the failure, and the causal chain from it to the damage. If
you cannot name one, you are speculating — drop the finding.

```
Good:  "GET /documents with no ?filter= → filter is undefined →
        JSON.parse(undefined) throws SyntaxError → 500 instead of 400"
Bad:   "filter might be undefined here"
```

**2. Trace the caller before claiming a value can be null.** A parameter is only
nullable if something can actually pass null. Before flagging `user.tier`, find at
least one caller and check. If every caller is guarded — by middleware, a type, a
schema validator, an earlier check — there is no bug, and telling the author to add
`?.` is asking them to write dead code. If you genuinely cannot see the callers, say
"if X can be null here" and mark it `question`, not `warning`.

**3. Check the convention before recommending a pattern.** Before saying "wrap this in
try/catch" or "validate this input", grep sibling files for how they do it. If no
sibling controller has a try/catch, the framework is handling it globally (a NestJS
`@Catch()` filter, Express error middleware, a Rails `rescue_from`) and your comment is
noise. If a pattern is absent *everywhere* in the codebase, you are questioning the
architecture, not finding a bug — that belongs in the summary, at most, not inline.

**4. Performance findings need a scale.** Sequential `await` in a loop is frequently
deliberate — to respect a rate limit, a connection pool, or ordering. Do not suggest
`Promise.all` (or any parallelisation) unless you can state roughly how large the
collection gets *and* you have checked for a rate limiter, pool bound, or ordering
requirement. Unbounded parallelism suggested into a rate-limited API is how a review
comment causes an outage.

**5. Don't re-report what CI already said.** See 3b.

### Other hard rules

- **No formatting or style nits.** No whitespace, import order, naming preferences.
- **No restating what the code does.** A comment must assert a risk, a bug, or a
  concrete improvement.
- **Prefer few strong findings over many weak ones.** `--max` is a ceiling, not a
  quota. A clean PR should produce zero findings, and reporting zero on a good PR is a
  correct, expected outcome — never manufacture findings to look thorough. Three real
  findings beat ten padded with "have you considered a Map here?".

### Finding schema

```json
{
  "path": "src/foo.ts",
  "line": 42,
  "side": "RIGHT",
  "severity": "blocker | warning | suggestion | question",
  "issue_class": "null-deref",
  "anchor": "handleEvent",
  "title": "Uncaught TypeError when filter is absent",
  "failure_trigger": "GET /documents with no ?filter= query parameter",
  "mechanism": "filter is undefined -> JSON.parse(undefined) throws SyntaxError -> 500",
  "body": "Why it is wrong, when it breaks, what to do instead.",
  "suggestion": "optional replacement code for the commented line(s)",
  "duplicate_of_id": "rc:123456789"
}
```

`failure_trigger` and `mechanism` are required on every `blocker` and `warning`. If a
finding can't carry them, it is a `question` at best, and probably shouldn't be posted.
Include them in the comment body you write, so the author can check your reasoning
rather than take it on faith.

`anchor` is the enclosing function/class/symbol, and `issue_class` is one tag from:

`null-deref`, `race-condition`, `missing-await`, `unhandled-rejection`,
`missing-error-handling`, `swallowed-error`, `missing-timeout`, `retry-misuse`,
`resource-leak`, `injection`, `authz`, `secret-leak`, `input-validation`,
`n-plus-one`, `blocking-event-loop`, `unbounded-growth`, `cache-key`,
`schema-compat`, `migration-risk`, `idempotency`, `broken-caller`, `logic-error`,
`off-by-one`, `dead-code`, `test-gap`, `test-weakened`, `observability`, `other`.

`path`, `anchor` and `issue_class` together make a fingerprint the script computes
itself (`sha1(path|anchor|issue_class)`), a best-effort secondary signal for "this
skill already said this" that survives line-number shifts and rewording. It is not the
primary defense — see `duplicate_of_id` in Step 5.

## Step 5 — Deduplicate

This is the point of the skill. Work through three stages, in order.

**5a. Fingerprint match (exact, code-enforced).** `pr_post.py` drops any finding whose
computed fingerprint is already in `posted_fingerprints` — automatic, nothing for you
to do. It's also the weakest stage: it only catches a finding that happens to get the
same `anchor`/`issue_class` twice. Stage 5b is what actually matters.

**5b. Named-duplicate match (judgement, code-enforced once you name it).** For each
finding, check it against **every** entry in `bundle.existing` — all of
`review_comments`, `issue_comments`, and `reviews`, not just nearby inline comments.
A general PR-level comment ("we're migrating off Redis, don't add new keys there") can
make a specific inline finding moot even if it's nowhere near that line.

For each existing entry, ask: *does this already raise the same underlying issue?*
Judge the issue, not the wording:

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
(e.g. `"rc:123456789"`) instead of silently dropping it yourself. `pr_post.py` verifies
the id is real and enforces the skip in code — this is what makes deduplication
reliable even when your own wording varies between runs. If you're confident it's a
duplicate but can't find the exact entry, drop it yourself and say why in the summary;
don't invent an id.

**5c. Resolved vs. outdated — these are not the same thing.** GitHub sets a thread's
`outdated: true` the moment a new commit changes the diff context around it — whether
or not anyone fixed the problem. Do not treat `outdated` as "handled".

- `resolved: true` → explicitly marked resolved by a human. Never repost; count it
  under "existing issues already resolved" in the summary.
- `outdated: true`, `resolved: false` → the thread is stale but **nobody confirmed the
  fix**. Check the current file content at the PR head: if the problem is still there,
  still raise it — as a fresh finding at the new location, or noted in the summary as
  "still unresolved, see the outdated thread at `<id>`". Only skip it if the current
  code shows it's actually fixed.

Finally, deduplicate the findings against each other: one root cause gets one comment,
placed at the most relevant line.

## Step 6 — Self-check, summarise, then wait

**Before writing the summary, re-read every surviving finding and try to refute it.**
For each one ask: *could the author of this PR immediately dismiss this with context I
didn't check?* Common ways a finding dies at this stage:

- The value can't actually be null — a caller, type, or validator guarantees it (rule 2)
- The framework already handles it globally (rule 3)
- The sequential loop is deliberate (rule 4)
- CI already reported it (rule 5)
- The PR description says the behaviour change is intentional (3a)

If you can't answer the refutation, go check — or drop the finding. Dropping a shaky
finding costs nothing. Posting one costs the author's trust in every other finding you
made.

Then print exactly this shape:

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
matched (its `id`), so the dedup is auditable. Also mention, each in one line if
relevant: findings dropped by the self-check and why; callers outside the diff that
`pr_impact.py` flagged; the count of `skipped_files`; low-severity findings held back
by `--min-severity`; and whether CI was passing, failing, or absent.

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

(Add `--event` only if the user explicitly asked for `REQUEST_CHANGES` or `APPROVE`.)

The script appends the fingerprint marker and a visible "AI-assisted review" line to
every comment, enforces `duplicate_of_id` and the fingerprint check, validates each
line against the diff, posts one batched review, and falls back to posting comment by
comment if GitHub rejects the batch. Findings that cannot be anchored go into the
review body in full, not just the title.

Report its JSON result plainly: how many posted, what was skipped and why (`fingerprint`
vs `duplicate_of_id`), what failed, and the PR URL. If `unverified_duplicate_claims` is
non-empty, mention it — you named an id that isn't on the PR, worth double-checking. If
`failed` is non-empty, say so — do not report success.

## Step 8 — Clean up

```
git -C <clone> worktree remove --force <workdir>/wt     # if one was created
```

If this fails (a locked file, an antivirus scan, an IDE language server holding a
handle), don't treat it as blocking — tell the user the worktree is at `<workdir>/wt`
and can be removed later with the same command; never retry destructively or touch the
user's actual working tree to work around it.

Leave `bundle.json`, `impact.json` and `findings.json` in the temp working directory;
they are useful if the user asks why something was skipped, and they are not state —
the PR itself is the only source of truth for what has already been said.
