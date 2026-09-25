# pr-review — an Antigravity skill for reviewing GitHub PRs

Reviews a pull request and posts only **new**, actionable findings as inline PR
comments. Run it on the same PR as many times as you like: it reads what is already on
the PR first and stays quiet about anything that has already been raised.

No Personal Access Token. Authentication is your own `gh` CLI login.

## Install

Prerequisites, once per machine:

```bash
winget install --id GitHub.cli        # macOS: brew install gh
gh auth login                         # GitHub.com -> HTTPS -> login with a browser
python --version                      # 3.9 or newer, on PATH
```

Then, from a clone of this repo:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1     # Windows
```

```bash
./install.sh                                                # macOS / Linux
```

That copies `skills/pr-review/` into both Antigravity skill locations:

| Target | Path |
| --- | --- |
| Antigravity IDE | `~/.gemini/config/skills/pr-review/` |
| Antigravity CLI | `~/.gemini/antigravity-cli/skills/pr-review/` |

Restart Antigravity afterwards. Re-run the installer whenever you pull changes to this
repo.

## Use

From the Antigravity chat or the CLI:

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

## How deduplication works

Three stages, no database and no local state — the PR itself is the source of truth.

1. **Fingerprint match.** Every comment the skill posts ends with a hidden marker,
   `<!-- pr-review-skill:v1 fp=a1b2c3d4e5f6 -->`. The fingerprint is
   `sha1(file | enclosing symbol | issue class)` — deliberately not the line number and
   not the wording, so it survives new pushes and rebases. On a rerun, anything whose
   fingerprint is already on the PR is dropped immediately. `scripts/pr_post.py`
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
skills/pr-review/
  SKILL.md              the procedure the agent follows
  scripts/pr_fetch.py   PR metadata, diff, commentable lines, existing threads -> bundle.json
  scripts/pr_post.py    findings.json -> one batched inline review (with fallbacks)
install.ps1 / install.sh
```

`pr_fetch.py` and `pr_post.py` are plain Python 3, standard library only, and shell out
to `gh`. They handle the mechanical parts that are easy to get wrong — pagination,
mapping diff hunks to the lines GitHub will accept a comment on, thread resolution
state, and batched review posting with a per-comment fallback. Judgement (the review
and the semantic dedup) lives in `SKILL.md`.

Both scripts are usable on their own:

```bash
python skills/pr-review/scripts/pr_fetch.py jpteam/paxiai-event-processor 842 --out bundle.json
python skills/pr-review/scripts/pr_post.py  jpteam/paxiai-event-processor 842 \
    --findings findings.json --bundle bundle.json --dry-run
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `gh is not installed or not on PATH` | `winget install --id GitHub.cli`, then open a new terminal |
| `GitHub CLI is not authenticated` | `gh auth login` |
| `HTTP 404` on a private repo | Your `gh` account lacks access, or `gh auth refresh -s repo` is needed |
| Comment lands in the review body instead of inline | The line is outside the diff hunks; GitHub only accepts inline comments on changed lines |
| Skill not offered in Antigravity | Re-run the installer and restart Antigravity; check `~/.gemini/config/skills/pr-review/SKILL.md` exists |

## Changing it

Edit `skills/pr-review/SKILL.md` (procedure, review rubric, dedup rules) or the scripts,
commit, and have everyone re-run the installer. If you change how fingerprints are
built, bump `MARKER_VERSION` in `pr_post.py` — old markers will no longer match and
previously posted findings can be raised again.
