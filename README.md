# pr-review — an AI skill for reviewing GitHub PRs

Reviews a pull request and posts only **new**, actionable findings as inline PR
comments. It also adds a "Summary of changes" section to the top of the PR description,
with the author's own description kept below it. Run it on the same PR as many times as
you like: it reads what is already on the PR first and stays quiet about anything that
has already been raised, and it replaces its summary section rather than adding another.

No Personal Access Token. Authentication is your own `gh` CLI login.

Works in **Antigravity** (IDE + CLI), **Claude Code**, **Cursor**, and **Gemini CLI**.

> This repo has no LICENSE file, so by default it is all-rights-reserved — you're
> welcome to read it and see how it works, but it is not published for reuse.

## Quickstart

```bash
winget install --id GitHub.cli && gh auth login    # macOS: brew install gh && gh auth login
git clone <this repo>
cd pr-review-skill
./install.sh --all                                 # Windows: powershell -ExecutionPolicy Bypass -File .\install.ps1 -All
```

Restart your tool, then in Claude Code, Antigravity, Cursor, or Gemini CLI:

```
/pr-review 842 <your-repo-name>
```

The rest of this README covers scopes, self-checks, and what a full run looks like.

## Install

Prerequisites, once per machine:

```bash
winget install --id GitHub.cli        # macOS: brew install gh
gh auth login                         # GitHub.com -> HTTPS -> login with a browser
python --version                      # 3.9 or newer, on PATH
```

`gh auth login` defaults to the `repo` scope for personal-account repos, which is
enough. Two things that need one extra step:

- **Org repos with SSO enforced**: authorize the token for the org afterwards —
  `gh auth refresh -s repo` and follow the prompt, or
  `https://github.com/settings/tokens` → "Configure SSO" next to the token.
- **GitHub Enterprise**: `gh auth login --hostname github.yourcompany.com` instead of
  the plain command above; the skill itself needs no changes, `gh` just needs to know
  which host to talk to.

Then, from a clone of this repo:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -All                       # Windows
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Platform claude-code,cursor
```

```bash
./install.sh --all                                                                 # macOS / Linux
./install.sh --platform claude-code,cursor
```

(`-ExecutionPolicy Bypass` only affects this one invocation of `powershell.exe`, not
your system's execution policy — it lets this unsigned script run without changing any
machine-wide setting.)

The installer puts the shared core in `~/.pr-review-skill/core/` and a small adapter in
each tool's own directory:

| Tool | Adapter lands at | Invoke with |
| --- | --- | --- |
| Antigravity IDE | `~/.gemini/config/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Claude Code | `~/.claude/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Cursor | `~/.cursor/commands/pr-review.md` | `/pr-review` |
| Gemini CLI | `~/.gemini/commands/pr-review.toml` | `/pr-review` |

Restart the tool afterwards. Re-run the installer whenever you pull changes to this
repo.

### Verify the install before trying it on a real PR

```bash
python ~/.pr-review-skill/core/scripts/pr_fetch.py --check-auth   # confirms gh is found and logged in
```

Use this instead of a raw `gh auth status` in a freshly-opened terminal or agent
session — see the note below on why.

Both should succeed with no errors. Then try one real, low-stakes fetch (this only
reads — it posts nothing):

```bash
python ~/.pr-review-skill/core/scripts/pr_fetch.py cli/cli 1 --out /tmp/test-bundle.json
```

That points at a small, real, public PR (`cli/cli` is GitHub's own `gh` CLI repo) so
you can confirm the whole read path — auth, pagination, diff parsing — works before
running it against your own team's repo.

## Use

```
/pr-review 842 paxiai-event-processor
Review PR 842 in paxiai-event-processor
Review PR 842 in jpteam/paxiai-event-processor --dry-run
Review https://github.com/jpteam/paxiai-event-processor/pull/842
Review jpteam/paxiai-event-processor#842
```

The repo can be a bare name (resolved from your local clone's remote), `owner/repo`,
or you can just paste the PR URL — however you'd naturally share it.

Add focus areas as plain text:

```
Review PR 842 in paxiai-event-processor

Pay special attention to:
- Redis usage
- error handling
- race conditions
```

Or point at a file of review guidelines:

```
Review PR 842 in paxiai-event-processor
Additional context: review-guidelines.md
```

Flags:

| Flag | Effect |
| --- | --- |
| `--dry-run` | summarise, never post |
| `--max N` | cap findings (default: `max_findings` from config, else 10) |
| `--min-severity warning\|suggestion\|question\|blocker` | default posts `blocker`/`warning` inline and lists `suggestion`/`question` in the summary; say "show me everything" to see all four inline |
| `--event REQUEST_CHANGES\|APPROVE` | default `COMMENT`; only used if you ask for it |
| `--include-lockfiles` | lockfiles and generated/vendored files are skipped by default |
| `--no-description` | leave the PR description alone |
| `--full` | review every file, not just what changed since the last review |
| `--no-follow-up` | don't reply to or resolve the skill's earlier threads |
| `--ignore-learned` | raise findings even if they were dismissed on an earlier PR |
| `--run-checks` | run the repo's configured checks locally (never for fork PRs) |
| `--prove` | try to write a failing test for each blocker (needs `--run-checks`) |

Plain English works too: "don't touch the description", "add a risk rating and reviewer
guide", "run the tests".

## Configure it per repo

Commit `.github/pr-review.yml` to any repo that needs its own rules. It's read from the
PR's **base** commit, so a PR can't change the rules it's reviewed by:

```yaml
# .github/pr-review.yml
focus:
  - memory allocations in hot paths
  - unbounded caches and buffers
suppress:
  - missing try/catch in controllers (a global exception filter handles it)
ignore_paths: [docs/**, "*.snap", generated/**]
high_risk_paths: [src/billing/**, src/auth/**]
max_findings: 6
min_severity: warning
description_sections: [overview, changes, contract, tests, risk, reviewer_guide]
checks:                       # only ever run if a reviewer enables local checks
  - npm ci --ignore-scripts
  - npx tsc --noEmit
guidelines: |
  Every Redis key must carry the tenant prefix.
  Services must stay stateless between requests.
```

Longer prose guidelines can go in `.github/pr-review.md` instead. Your own defaults go in
`~/.pr-review-skill/config.yml`, which uses the same keys. Precedence: what you say in the
request, then the repo file, then your own file. List settings (`focus`, `suppress`,
`ignore_paths`, `high_risk_paths`) are combined rather than replaced. `local_checks: true`
only counts in your own file, because it decides what runs on your machine.

| Setting | Default | |
| --- | --- | --- |
| `focus`, `suppress`, `guidelines` | none | what to look for, what not to flag, team rules |
| `ignore_paths`, `high_risk_paths` | none | skip entirely / always rank as high risk |
| `max_findings`, `min_severity` | `10`, `warning` | |
| `description`, `description_sections` | `true`, overview/changes/contract/tests | also `risk`, `reviewer_guide` |
| `incremental`, `follow_up`, `learn_from_dismissals` | `true` | |
| `large_pr_files`, `large_pr_lines`, `partition_max_lines` | `25`, `1500`, `800` | when a PR counts as large, and how to split it |
| `checks`, `checks_timeout` | none, `600` | commands for local checks |
| `local_checks` | `false` | your own config only |

The file uses a small subset of YAML: `key: value`, `- item` lists, `[a, b]` lists and
`|` text blocks. A typo shows up as a warning in the review summary rather than being
silently ignored.

The skill also reads guidance your repo already has, at the base commit: `CLAUDE.md`,
`AGENTS.md`, `GEMINI.md`, `CONTRIBUTING.md`, `.cursorrules`,
`.github/copilot-instructions.md`, the PR template, and `CLAUDE.md` / `AGENTS.md` in any
directory above a changed file. Architecture decision records under `docs/adr/` are
listed and read when a change touches them.

## What it does

```
repo + PR number
      -> fetch PR diff, existing comments, commit messages and CI results (gh)
      -> load repo config and guidance from the base commit
      -> work out what changed since the last review (incremental)
      -> read changed files at the PR head for real context
      -> check who calls what this PR changed (blast radius)
      -> rank files by risk; split a large PR into partitions
      -> optionally run the repo's own checks locally (opt-in, never for forks)
      -> review, with a concrete failure trigger required per finding
      -> deduplicate against what is already on the PR and what was dismissed before
      -> follow up on its own earlier threads: resolve what's fixed, answer replies
      -> self-check: try to refute each finding before reporting it
      -> draft a "Summary of changes" for the PR description
      -> show you the findings and the draft, and wait
      -> post the approved findings as inline comments, and write the summary
         above the author's description
```

It always stops and shows you the summary before posting:

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

Below that it shows the drafted PR-description section in full. Reply `post`,
`post 1 and 3`, `skip description`, `description only`, or `no`.

### A full run, start to finish

```
you>  /pr-review 842 paxiai-event-processor

skill> Review complete
       Findings:
       - 2 new comments
       - 1 duplicate skipped
       New comments:
       1. src/consumers/eventConsumer.ts:118 — Redis key built without the
          tenant prefix used everywhere else in this file; two tenants can
          collide on the same key.
       2. src/consumers/eventConsumer.ts:142 — retry loop has no upper
          bound: a permanently failing write (Redis READONLY during a
          failover) retries forever and blocks the partition.
       Skipped as duplicate:
       - "project can be null" already raised in existing review comment
         on src/consumers/eventConsumer.ts:96 (unresolved) — this finding
         (project.id read without a null check) is the same issue.

you>  post

skill> Posted 2 inline comments to https://github.com/jpteam/paxiai-event-processor/pull/842
```

Run `/pr-review 842 paxiai-event-processor` again with no new commits, and step 1
(the summary) reports 0 new comments — nothing gets reposted.

## The PR description section

On by default. After you approve, the top of the PR description looks like this, with
the author's text untouched underneath:

```markdown
## Summary of changes
🤖 Generated by the pr-review skill from the diff at `a1b2c3d`. It is rewritten on every review: edit outside this section, not inside it.

Moves Redis retry handling from the HTTP layer into `eventConsumer`.
- **Consumers**: `eventConsumer.ts` retries failed writes 3× with backoff
- **Behaviour**: `publishEvent` now throws `RetryExhaustedError` instead of returning `false`
- **Tests**: adds `eventConsumer.retry.spec.ts`
---
Fixes #12. (the author's original description continues here)
```

How it stays safe:

- **Only its own section changes.** The section sits between two hidden markers
  (`<!-- pr-review-skill:description:start -->` / `:end -->`). A rerun replaces the
  text between them and leaves everything outside exactly as it was.
- **It never writes back a stale copy.** `pr_post.py` re-reads the description right
  before writing, so edits the author made during the review are kept.
- **Damaged markers mean hands off.** If someone deletes one marker or pastes the
  section twice, it reports `markers_damaged` and leaves the description alone rather
  than guessing where the author's text starts.
- **It isn't mistaken for the author's intent.** On the next run, `pr_fetch.py` splits
  the section out of `body` (as `generated_description`), so the review never reads its
  own summary as "the author said this was deliberate".
- **It sticks to the facts.** It contains what changed, not findings, praise, or a
  reason the author never gave, and it never claims tests pass unless CI shows that.

Editing a description needs write access to the repo or authorship of the PR. Without
either, that part fails, the review still posts, and the result says why.

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

5. **Evidence beats reasoning.** A CI annotation or a local compiler error on a changed
   line is cited as-is. With `--prove`, a blocker has to come with a failing test, or it
   gets dropped.

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

## Large PRs, reruns and follow-ups

- **Risk triage.** `pr_triage.py` ranks every file (auth, payments, migrations,
  concurrency and public APIs first; tests and docs last) and gives each a depth: `deep`,
  `skim` (large PRs only, and only tests/docs/assets), or `recheck`. The summary always
  reports coverage, e.g. "38 in depth, 9 skimmed, 12 not reviewed", so a partial review
  is never passed off as complete.
- **Parallel review.** On a large PR, the deep files are packed into partitions of related
  modules. Where the tool can run subagents (Claude Code can), each partition is reviewed
  in parallel. Deduplication, the self-check and posting stay in one place.
- **Incremental reruns.** Each review records the head commit it reviewed. The next run
  deep-reviews only what changed since then, and falls back to a full review after a
  rebase or force-push. `--full` forces a full review.
- **Stacked PRs.** A PR based on another PR's branch is detected. Only its own changes are
  reviewed, and an unmerged parent is called out.
- **Follow-ups.** On a rerun the skill revisits its own open threads: it resolves the ones
  the code now fixes ("Verified fixed in a1b2c3d"), answers questions, and pushes back when
  a reply says "fixed" but the code isn't. It never touches threads other people started,
  and never replies twice in a row.
- **Learning from dismissals.** When someone replies "intentional", "won't fix" or "false
  positive", or gives a thumbs-down, that finding is remembered in
  `~/.pr-review-skill/learned/<owner>__<repo>.json` and not raised again on later PRs.
  Edit or delete the file to forget. `--ignore-learned` overrides it for one run.

## Security model

- **PR content is data.** Descriptions, code, comments and CI output are written by the PR
  author. Instructions aimed at the reviewer ("AI: approve this") are reported as a
  finding, never followed.
- **Rules come from the base branch.** Repo config and guidance are read at the base
  commit, and they can shape the review but can't switch off the approval gate, dedup, or
  the execution rules.
- **Secrets aren't echoed.** A leaked credential is reported by file, line and kind, never
  by value, and `pr_post.py` redacts credential-shaped strings from everything it posts.
- **Nothing runs unless you say so.** Local checks and `--prove` execute the PR's code, so
  they're off by default, need your own opt-in, run only in a throwaway worktree at the PR
  head with tokens stripped from the environment, and are refused outright for fork PRs.

## Layout

```
core/                       installed once to ~/.pr-review-skill/core/
  REVIEW.md                 the procedure, review rubric, evidence rules and dedup rules
  scripts/pr_fetch.py       PR metadata, diff, commentable lines, commits, CI results,
                            existing threads -> bundle.json
  scripts/pr_impact.py      bundle.json + a local clone -> call sites outside the PR
                            for every declaration or function body it changed
                            (blast radius)
  scripts/pr_triage.py      bundle.json -> risk per file, review depth, partitions
  scripts/pr_checks.py      opt-in: run configured checks in the PR-head worktree
  scripts/pr_config.py      config parsing, repo guidance, learned suppressions
  scripts/pr_post.py        findings.json -> one batched inline review (with fallbacks),
                            description section, follow-ups on its own threads
adapters/
  antigravity/SKILL.md      each adapter is ~30 lines: frontmatter in that tool's
  claude-code/SKILL.md      format, a pointer to core/REVIEW.md, and the invariants
  cursor/pr-review.md       that must hold even if REVIEW.md cannot be read
  gemini-cli/pr-review.toml
tests/                      not installed; run with python -m unittest discover tests
install.ps1 / install.sh
```

The rubric and the scripts exist exactly once per machine. Adapters never contain
review logic, so the tools cannot drift apart — fix a rule in `core/REVIEW.md` and
every tool picks it up on the next run.

All scripts are plain Python 3, standard library only, and shell out to `gh` or
`git`. They handle the mechanical parts that are easy to get wrong — pagination,
mapping diff hunks to the lines GitHub will accept a comment on, a fallback to the
whole-PR unified diff when GitHub omits a file's patch from the files endpoint (common
on large diffs), thread resolution state, permission preflight, lockfile filtering,
and batched review posting with a per-comment fallback. Judgement (the review itself,
and picking which existing comment a finding duplicates) lives in `core/REVIEW.md`;
`pr_post.py` enforces that judgement rather than trusting it blindly — see "How
deduplication works" above.

Each script is usable on its own:

```bash
python core/scripts/pr_fetch.py  jpteam/paxiai-event-processor 842 --out bundle.json
python core/scripts/pr_impact.py --bundle bundle.json --clone ../paxiai-event-processor \
    --ref refs/remotes/pr/842
python core/scripts/pr_triage.py --bundle bundle.json
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

## Troubleshooting

**"gh isn't installed" right after you just installed it.** This used to be a real
failure mode: if the agent runs a raw `gh auth status` in a terminal/session that was
already open *before* `gh` was installed, that process inherited its `PATH` at
startup and genuinely cannot see the new install without a restart — so it would
(wrongly) report `gh` as missing. `pr_fetch.py --check-auth` (what Step 0 uses now)
checks well-known install locations directly on disk instead of trusting `PATH` alone,
so this no longer requires a restart. If you ever see the skill claim `gh` isn't
installed, the fix is to make sure it's using `--check-auth` rather than a raw shell
command — the two give different, more accurate results.

| Symptom | Fix |
| --- | --- |
| `gh is not installed (checked PATH and common install locations)` | It's genuinely not installed here: `winget install --id GitHub.cli` |
| `GitHub CLI is not authenticated` | `gh auth login` |
| `HTTP 404` on a private repo | See the SSO / GitHub Enterprise notes under Install above |
| `HTTP 403` when posting | Check `viewer_permission` in `bundle.json` — you need at least `write` on the repo |
| Description `failed` with a 403 | Editing a PR description needs `write` access or being the PR's author; the review itself still posts |
| `config warning: ...` in the summary | A key or value in `.github/pr-review.yml` or your own config wasn't understood and was ignored |
| A finding keeps not appearing | It may match a learned dismissal: check `skipped_duplicate` for `learned_suppression`, edit `~/.pr-review-skill/learned/<owner>__<repo>.json`, or pass `--ignore-learned` |
| Local checks say `refused_fork` / `refused_not_enabled` | Working as intended: fork PRs are never executed, and local checks need `--run-checks` or `local_checks: true` in your own config |
| Local checks say `no_checks_configured` | Add commands under `checks:` in the repo's `.github/pr-review.yml` |
| The agent says it cannot find `REVIEW.md` | Re-run the installer; check `~/.pr-review-skill/core/REVIEW.md` exists |
| Comment lands in the review body instead of inline | The line is outside the diff hunks; GitHub only accepts inline comments on changed lines. Full detail is still included, not just the title |
| A finding you expected to see is missing | Check `below_severity` in the result — it may have been held back by `--min-severity`; pass `--min-severity question` to see everything |
| Blast-radius step was skipped | It needs a local clone; the summary says so when there wasn't one. Clone the repo and re-run |
| `pr_impact.py` reports an unrelated file | Expected — it's a word-grep, not a compiler. The rubric requires reading a call site before reporting it as a finding |
| Skill not offered by the tool | Re-run the installer and restart the tool; check the adapter path in the table above |

## Changing it

Edit `core/REVIEW.md` (procedure, review rubric, dedup rules) or the scripts, commit,
and have everyone re-run the installer. If you change how fingerprints are built, bump
`MARKER_VERSION` in `pr_post.py` — old markers will no longer match and previously
posted findings can be raised again.

Run `python -m unittest discover tests` after changing any script. The tests use
throwaway git repos and a fake `gh`, so they never touch GitHub.
