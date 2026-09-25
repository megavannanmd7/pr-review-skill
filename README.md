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
`--include-lockfiles` (lockfiles and generated/vendored files are skipped by default).

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
mapping diff hunks to the lines GitHub will accept a comment on, a fallback to the
whole-PR unified diff when GitHub omits a file's patch from the files endpoint (common
on large diffs), thread resolution state, permission preflight, lockfile filtering,
and batched review posting with a per-comment fallback. Judgement (the review itself,
and picking which existing comment a finding duplicates) lives in `core/REVIEW.md`;
`pr_post.py` enforces that judgement rather than trusting it blindly — see "How
deduplication works" above.

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
| `HTTP 403` when posting | Check `viewer_permission` in `bundle.json` — you need at least `write` on the repo |
| The agent says it cannot find `REVIEW.md` | Re-run the installer; check `~/.pr-review-skill/core/REVIEW.md` exists |
| Comment lands in the review body instead of inline | The line is outside the diff hunks; GitHub only accepts inline comments on changed lines. Full detail is still included, not just the title |
| A finding you expected to see is missing | Check `below_severity` in the result — it may have been held back by `--min-severity`; pass `--min-severity question` to see everything |
| Skill not offered by the tool | Re-run the installer and restart the tool; check the adapter path in the table above |

## Changing it

Edit `core/REVIEW.md` (procedure, review rubric, dedup rules) or the scripts, commit,
and have everyone re-run the installer. If you change how fingerprints are built, bump
`MARKER_VERSION` in `pr_post.py` — old markers will no longer match and previously
posted findings can be raised again.
