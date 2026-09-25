# pr-review — an AI skill for reviewing GitHub PRs

Reviews a pull request and posts only **new**, actionable findings as inline PR
comments. Run it on the same PR as many times as you like: it reads what is already on
the PR first and stays quiet about anything that has already been raised.

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
gh auth status                                                    # confirms gh is logged in
python ~/.pr-review-skill/core/scripts/pr_fetch.py --help         # confirms the core copied correctly
```

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
```

The repo can be a bare name (resolved from your local clone's remote) or `owner/repo`.

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

Flags: `--dry-run` (summarise, never post), `--max N` (cap findings, default 10).

## What it does

```
repo + PR number
      -> fetch PR diff, existing comments and thread states (gh)
      -> read changed files at the PR head for real context
      -> review
      -> deduplicate against what is already on the PR
      -> show you a summary and wait
      -> post the approved findings as inline comments
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

Reply `post`, `post 1 and 3`, or `no`.

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

## How deduplication works

Three stages, no database and no local state — the PR itself is the source of truth.

1. **Fingerprint match.** Every comment the skill posts ends with a hidden marker,
   `<!-- pr-review-skill:v1 fp=a1b2c3d4e5f6 -->`. The fingerprint is
   `sha1(file | enclosing symbol | issue class)` — deliberately not the line number and
   not the wording, so it survives new pushes and rebases. On a rerun, anything whose
   fingerprint is already on the PR is dropped immediately. `core/scripts/pr_post.py`
   re-checks this at post time, so a duplicate cannot slip through.
2. **Semantic match.** Each remaining finding is compared against existing comments in
   the same file, in the same hunk or within ~15 lines — human comments included. Same
   underlying issue means no new comment, regardless of wording:

   > existing: *"This can fail when project is null."*
   > new: *"`project.id` is accessed without checking whether `project` exists."*
   > → same issue, not posted.

3. **Resolved / outdated threads.** A match on a resolved or outdated thread is
   reported as already handled and never reposted.

Reviewing the same PR twice with no new commits posts nothing.

## Layout

```
core/                       installed once to ~/.pr-review-skill/core/
  REVIEW.md                 the procedure, review rubric and dedup rules
  scripts/pr_fetch.py       PR metadata, diff, commentable lines, existing threads -> bundle.json
  scripts/pr_post.py        findings.json -> one batched inline review (with fallbacks)
adapters/
  antigravity/SKILL.md      each adapter is ~30 lines: frontmatter in that tool's
  claude-code/SKILL.md      format, a pointer to core/REVIEW.md, and the invariants
  cursor/pr-review.md       that must hold even if REVIEW.md cannot be read
  gemini-cli/pr-review.toml
install.ps1 / install.sh
```

The rubric and the scripts exist exactly once per machine. Adapters never contain
review logic, so the tools cannot drift apart — fix a rule in `core/REVIEW.md` and
every tool picks it up on the next run.

`pr_fetch.py` and `pr_post.py` are plain Python 3, standard library only, and shell out
to `gh`. They handle the mechanical parts that are easy to get wrong — pagination,
mapping diff hunks to the lines GitHub will accept a comment on, thread resolution
state, and batched review posting with a per-comment fallback. Judgement (the review
and the semantic dedup) lives in `core/REVIEW.md`.

Both scripts are usable on their own:

```bash
python core/scripts/pr_fetch.py jpteam/paxiai-event-processor 842 --out bundle.json
python core/scripts/pr_post.py  jpteam/paxiai-event-processor 842 \
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

| Symptom | Fix |
| --- | --- |
| `gh is not installed or not on PATH` | `winget install --id GitHub.cli`, then open a new terminal |
| `GitHub CLI is not authenticated` | `gh auth login` |
| `HTTP 404` on a private repo | See the SSO / GitHub Enterprise notes under Install above |
| The agent says it cannot find `REVIEW.md` | Re-run the installer; check `~/.pr-review-skill/core/REVIEW.md` exists |
| Comment lands in the review body instead of inline | The line is outside the diff hunks; GitHub only accepts inline comments on changed lines |
| Skill not offered by the tool | Re-run the installer and restart the tool; check the adapter path in the table above |

## Changing it

Edit `core/REVIEW.md` (procedure, review rubric, dedup rules) or the scripts, commit,
and have everyone re-run the installer. If you change how fingerprints are built, bump
`MARKER_VERSION` in `pr_post.py` — old markers will no longer match and previously
posted findings can be raised again.
