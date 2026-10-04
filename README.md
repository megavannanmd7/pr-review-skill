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
npm i -g github:megavannanmd7/pr-review-skill
pr-review-skill install --all
```

No npm/Node? Clone and use the shell installer instead — see [Install](#install).

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

Then install with **npm** (needs Node 18+; nothing from this repo stays on disk outside
`~/.pr-review-skill/` and each tool's own command directory):

```bash
npm i -g github:megavannanmd7/pr-review-skill   # or: npx github:megavannanmd7/pr-review-skill install --all
pr-review-skill install --all
pr-review-skill install --platform claude-code,cursor
pr-review-skill doctor                          # re-check gh/python without reinstalling
```

This repo isn't published to any npm registry — `github:...` installs straight from the
git repo, so whoever runs it needs the same access to it as a `git clone` would (nothing
new). Re-running `npm i -g github:...` always pulls whatever is on `main` right now, so
"update" is the same command as "install".

No Node, or you'd rather not install globally? Use the shell installer instead, from a
clone of this repo:

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

Either installer puts the shared core in `~/.pr-review-skill/core/` and a small adapter
in each tool's own directory:

| Tool | Adapter lands at | Invoke with |
| --- | --- | --- |
| Antigravity IDE | `~/.gemini/config/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Claude Code | `~/.claude/skills/pr-review/SKILL.md` | `/pr-review` or plain English |
| Cursor | `~/.cursor/commands/pr-review.md` | `/pr-review` |
| Gemini CLI | `~/.gemini/commands/pr-review.toml` | `/pr-review` |

Restart the tool afterwards. Re-run `npm i -g github:...` (or `git pull` + the shell
installer) whenever this repo changes.

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

Flags: `--dry-run` (summarise, never post), `--max N` (cap findings, default 10),
`--min-severity warning|suggestion|question|blocker` (default behaviour posts
`blocker`/`warning` inline and puts `suggestion`/`question` in the summary only — say
"show me everything" or pass `--min-severity question` to see all four inline),
`--event REQUEST_CHANGES|APPROVE` (default `COMMENT`; only used if you ask for it),
`--include-lockfiles` (lockfiles and generated/vendored files are skipped by default),
`--no-description` (leave the PR description alone), `--no-resolve` (leave this
skill's own prior threads alone, even ones a rerun would otherwise confirm as fixed).

## What it does

```
repo + PR number
      -> fetch PR diff, existing comments, commit messages and CI results (gh)
      -> read changed files at the PR head for real context
      -> check who calls what this PR changed (blast radius)
      -> review, with a concrete failure trigger required per finding
      -> deduplicate against what is already on the PR
      -> self-check: try to refute each finding before reporting it
      -> check its own earlier, still-open threads against the current code
      -> draft a "Summary of changes" for the PR description
      -> show you the findings, any threads it can now confirm are fixed, and the
         description draft, and wait
      -> post the approved findings as inline comments, mark the approved threads
         resolved, and write the summary above the author's description
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

Looks fixed (will mark resolved on approval):
A. src/qux.ts:91 — null check added at line 88, the original crash path is gone
```

Below that it shows the drafted PR-description section in full, and any threads it can
now confirm are fixed (see [below](#closing-out-fixed-comments)). Reply `post`
(everything shown), `post 1 and 3`, `skip description`, `description only`,
`skip resolve A`, `resolve only`, or `no`.

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
       2. src/consumers/eventConsumer.ts:142 — awaited call inside a loop
          that could be batched with Promise.all.
       Skipped as duplicate:
       - "project can be null" already raised in existing review comment
         on src/consumers/eventConsumer.ts:96 (unresolved) — this finding
         (project.id read without a null check) is the same issue.

you>  post

skill> Posted 2 inline comments to https://github.com/jpteam/paxiai-event-processor/pull/842
```

Run `/pr-review 842 paxiai-event-processor` again with no new commits, and step 1
(the summary) reports 0 new comments — nothing gets reposted.

## What happens to your PR description

On by default. After you approve, the top of the PR description looks like this, with
your own text kept exactly as it was, underneath:

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

Only that section ever changes — it's rewritten from the current diff on every run, and
your text above and below it is never touched. If a marker ever gets damaged (deleted,
or pasted twice), it leaves the description alone entirely rather than guess where your
text starts. Turn this off with `--no-description`. Editing the description needs write
access to the repo or authorship of the PR; without either, that part fails and the
rest of the review still posts.

It also never re-raises a fixed issue or double-posts on a rerun with no new commits —
see [docs/how-it-works.md](docs/how-it-works.md) for exactly how that's enforced in
code, not just trusted to the model.

## Closing out fixed comments

On a rerun, it also checks its own earlier comments that are still open. If it can read
the current code and confirm the exact issue it originally raised is actually gone, it
proposes marking that thread resolved — shown to you for approval the same as any
finding, never done silently. It will **not** do this just because GitHub marked a
thread `outdated`, or because someone replied "fixed" without the skill verifying it
itself. Turn this off with `--no-resolve`; see
[docs/how-it-works.md](docs/how-it-works.md) for the exact verification rules.

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
| The agent says it cannot find `REVIEW.md` | Re-run the installer; check `~/.pr-review-skill/core/REVIEW.md` exists |
| Comment lands in the review body instead of inline | The line is outside the diff hunks; GitHub only accepts inline comments on changed lines. Full detail is still included, not just the title |
| A finding you expected to see is missing | Check `below_severity` in the result — it may have been held back by `--min-severity`; pass `--min-severity question` to see everything |
| Blast-radius step was skipped | It needs a local clone; the summary says so when there wasn't one. Clone the repo and re-run |
| `pr_impact.py` reports an unrelated file | Expected — it's a word-grep, not a compiler. The rubric requires reading a call site before reporting it as a finding |
| Skill not offered by the tool | Re-run the installer and restart the tool; check the adapter path in the table above |

## More

This covers day-to-day use. For how deduplication and auto-resolve are enforced in
code, the review rubric's noise-reduction rules, how untrusted PR content and secrets
are handled, the repo layout, adding another tool, and how to change the skill, see
[docs/how-it-works.md](docs/how-it-works.md).
