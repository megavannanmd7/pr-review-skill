# How pr-review-skill works

This is the internals doc: how deduplication is actually enforced, the review rubric's
noise-reduction rules, how untrusted PR content is handled, the repo layout, and how to
change or extend the skill. If you just want to install and use it, see
[../README.md](../README.md) — nothing here is needed for day-to-day use.

## How deduplication works

Two independent layers. The first is enforced by code, not just by the model's
judgement — that's deliberate, so dedup doesn't silently degrade if the model phrases
something differently on a rerun.

1. **Named-duplicate match (primary, code-enforced).** `pr_fetch.py` tags every
   existing comment, issue comment and review with a stable id (`rc:`/`ic:`/`rv:`).
   When the review step finds a finding that matches one of them, it sets
   `"duplicate_of_id": "rc:123456789"` on the finding. `pr_post.py` checks that id is
   real and drops the finding — the skip happens in the script, not by hoping the
   model remembers to leave it out. This is checked against every existing comment on
   the PR, not just ones near the same line, so a general PR-level comment ("we're
   migrating off Redis") can suppress a specific inline finding anywhere in the diff:

   > existing: *"This can fail when project is null."*
   > new: *"`project.id` is accessed without checking whether `project` exists."*
   > → same issue, `duplicate_of_id` set, not posted.

   If the model names an id that turns out not to actually be on the PR, the claim is
   ignored (not trusted blindly) and the finding posts normally — surfaced in the
   result as `unverified_duplicate_claims`, so a bad claim can't silently suppress a
   real finding.

2. **Fingerprint match (secondary, fast-path).** Every comment the skill posts ends
   with a hidden marker, `<!-- pr-review-skill:v1 fp=a1b2c3d4e5f6 -->`, built from
   `sha1(file | enclosing symbol | issue class)`. On a rerun, anything whose
   fingerprint is already on the PR is dropped automatically, without needing the
   model to re-derive a `duplicate_of_id`. It's a weaker signal on its own — the
   fingerprint changes if the model names the anchor or issue class differently next
   time — so it backs up layer 1 rather than replacing it.

**Resolved vs. outdated are not the same thing**, and the skill doesn't treat them the
same. GitHub marks a thread `outdated` automatically the moment a new commit shifts
the diff under it — whether or not the issue was actually fixed. Only a thread a human
explicitly marked `resolved` is auto-suppressed; an `outdated`-but-unresolved thread is
checked against the current code, and if the problem is still there, it's still
raised.

Reviewing the same PR twice with no new commits posts nothing.

## Auto-resolving fixed comments

On a rerun, the skill also checks its own earlier threads that are still open — not
just whether to re-raise the same issue (above), but whether to actively close it out.
For every unresolved thread that carries this skill's own fingerprint marker, it rereads
the current file at the PR head and asks whether the exact failure mode the original
comment named is actually gone. Only if it can point to the specific lines that fix it
does it propose marking the thread resolved; that proposal is shown to you alongside
the findings and needs the same approval before anything happens.

It will **not** propose resolving a thread just because:

- GitHub marked it `outdated` (a new commit shifted the diff under it — that's not the
  same as someone fixing it, see above);
- the author or anyone else replied "fixed" or "done" (an unverified claim, not
  evidence the skill checked itself);
- it isn't sure. An open thread costs nothing; one marked resolved while the bug is
  still there is worse than useless.

On approval, it posts a short reply explaining what changed and why, then resolves the
thread through GitHub's own API — never by editing or deleting the original comment.
`pr_post.py` re-verifies every request against the bundle first: the comment must be
real, must carry this skill's own marker (it will never resolve a thread it didn't
post, even if asked to), must not already be resolved, and must come with a specific
reason. Pass `--no-resolve` to turn this off entirely for a run.

## How it keeps the noise down

An AI reviewer's failure mode isn't missing bugs, it's confidently inventing them. Four
rules in `core/REVIEW.md` exist specifically to make that harder:

1. **Every finding must name a concrete failure trigger.** Not "filter might be
   undefined" but "GET /documents with no `?filter=` → `JSON.parse(undefined)` throws
   → 500". A model that has to name the triggering input has to go look for one, and
   speculative findings die at that step.
2. **Trace a caller before claiming a value can be null.** If middleware, a type, or a
   validator guarantees `user.tier` is set, telling the author to add `?.` is asking
   for dead code. No caller checked → it's a `question`, not a `warning`.
3. **Check the convention before recommending a pattern.** If no sibling controller
   has a try/catch, the framework is handling errors globally (NestJS `@Catch()`,
   Express error middleware) and the comment is noise. A pattern absent *everywhere*
   is an architecture question for the summary, not an inline nit.
4. **Performance findings need a scale.** Sequential `await` in a loop is often
   deliberate — rate limits, connection pools, ordering. `Promise.all` suggested into
   a rate-limited API is how a review comment causes an outage.

Plus a **self-refutation pass** before the summary: for each finding, could the author
dismiss this immediately with context that wasn't checked? If it can't survive that,
it's dropped. Dropping a shaky finding costs nothing; posting one costs the author's
trust in every other finding.

Two things also make the review better informed rather than just quieter:

- **CI results are read, not guessed.** `pr_fetch.py` pulls check-run conclusions and
  per-file annotations into the bundle, so real compiler and linter output is available
  as evidence. The rubric forbids re-reporting anything CI already flagged, and forbids
  claiming a build failure that isn't in `checks`. Nothing from the PR is executed
  locally to get this — which matters for fork PRs.
- **Blast radius is checked, not assumed.** The most dangerous PR bug is invisible in
  the diff: change `findUser` from returning `null` to throwing and the diff looks
  perfect while every caller doing `if (!user)` breaks. `pr_impact.py` greps a local
  clone for call sites *outside* the PR's own files, both for symbols whose declaration
  the PR changed or removed and for functions whose body changed while the declaration
  line stayed the same. Each changed line is mapped to its enclosing function in the
  file at the PR head. It's a heuristic word-grep, not a compiler: it reports places to
  check. For a body-only change, the rubric first asks whether the function's contract
  changed at all, and requires reading a call site before reporting it.

## Handling untrusted PR content

A PR's title, body, commits, diff, file contents, and existing comments can all be
written by someone other than the person running this skill — most obviously on a fork
PR. Two rules in `core/REVIEW.md` treat that content accordingly:

- **It's data, never instructions.** A directive hidden in a commit message or a code
  comment ("approve this PR", "ignore previous instructions") is something to review,
  not something to obey. Only the rubric itself and the flags the invoking user typed
  govern a run.
- **A secret's value never gets quoted back.** If a finding flags a hardcoded
  credential, the comment names the kind and location only — never the value itself. A
  public GitHub comment is emailed to every watcher and stays searchable long after the
  line is deleted, so repeating a leaked secret there to prove the finding would be
  worse than the original leak. The finding still posts (as a `blocker`); it just never
  carries the thing it's warning about.

## Layout

```
core/                       installed once to ~/.pr-review-skill/core/
  REVIEW.md                 the procedure, review rubric, evidence rules and dedup rules
  scripts/pr_fetch.py       PR metadata, diff, commentable lines, commits, CI results,
                            existing threads -> bundle.json
  scripts/pr_impact.py      bundle.json + a local clone -> call sites outside the PR
                            for every declaration or function body it changed
                            (blast radius)
  scripts/pr_post.py        findings.json -> one batched inline review (with fallbacks),
                            plus resolving the skill's own fixed threads
adapters/
  antigravity/SKILL.md      each adapter is ~30 lines: frontmatter in that tool's
  claude-code/SKILL.md      format, a pointer to core/REVIEW.md, and the invariants
  cursor/pr-review.md       that must hold even if REVIEW.md cannot be read
  gemini-cli/pr-review.toml
tests/                      not installed; run with python -m unittest discover tests
install.ps1 / install.sh   shell installers (no Node needed)
bin/install.js             npm/npx installer -- same job, one script instead of two
package.json               "private": true; declares bin/install.js, never published
```

The rubric and the scripts exist exactly once per machine. Adapters never contain
review logic, so the tools cannot drift apart — fix a rule in `core/REVIEW.md` and
every tool picks it up on the next run.

`pr_fetch.py` and `pr_post.py` are plain Python 3, standard library only, and shell out
to `gh`. They handle the mechanical parts that are easy to get wrong — pagination,
mapping diff hunks to the lines GitHub will accept a comment on, a fallback to the
whole-PR unified diff when GitHub omits a file's patch from the files endpoint (common
on large diffs), thread resolution state, permission preflight, lockfile filtering,
and batched review posting with a per-comment fallback. Judgement (the review itself,
and picking which existing comment a finding duplicates) lives in `core/REVIEW.md`;
`pr_post.py` enforces that judgement rather than trusting it blindly — see "How
deduplication works" above.

Both scripts are usable on their own:

```bash
python core/scripts/pr_fetch.py  jpteam/paxiai-event-processor 842 --out bundle.json
python core/scripts/pr_impact.py --bundle bundle.json --clone ../paxiai-event-processor \
    --ref refs/remotes/pr/842
python core/scripts/pr_post.py   jpteam/paxiai-event-processor 842 \
    --findings findings.json --bundle bundle.json --dry-run
```

## Adding another tool

Nearly every AI coding tool now has a "reusable prompt triggered by a keyword"
mechanism with shell access. To add one, copy an existing adapter, translate the
frontmatter to that tool's format, and add a case to both installers. Do not copy the
rubric into it.

Not yet covered, and why:

- **Codex CLI** — its custom-prompt mechanism is deprecated in favour of a skills
  mechanism; needs a short spike to confirm the current folder and format.
- **GitHub Copilot** (`.github/prompts/pr-review.prompt.md`) — works only in agent
  mode, since ask and edit modes cannot run `gh` or Python.

## Changing it

Edit `core/REVIEW.md` (procedure, review rubric, dedup rules) or the scripts, commit,
and have everyone re-run the installer. If you change how fingerprints are built, bump
`MARKER_VERSION` in `pr_post.py` — old markers will no longer match and previously
posted findings can be raised again.

Run `python -m unittest discover tests` after changing `pr_impact.py`. The tests build
throwaway git repos and check what the script reports for each kind of change.

There are three installers (`install.sh`, `install.ps1`, `bin/install.js`) that all do
the same file-copy job. If you change destinations, platform names, or the prerequisite
checks, change all three — nothing enforces they stay in sync. `bin/install.js` is the
one to run (`node bin/install.js install --all`, or `npm link` once to get a real
`pr-review-skill` command) after editing it, since it has no test of its own.
