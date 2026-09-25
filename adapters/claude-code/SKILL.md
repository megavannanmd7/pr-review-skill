---
name: pr-review
description: Reviews a GitHub pull request and posts only genuinely new, actionable findings as inline PR comments. Use when the user asks to review a pull request, for example "Review PR 842 in paxiai-event-processor", optionally with extra focus areas or a context .md file. Deduplicates against comments already on the PR, so the same PR can be reviewed repeatedly without repeating comments.
---

# PR Review (inline, deduplicated)

**Step 0 — read the procedure.** Read `~/.pr-review-skill/core/REVIEW.md`
(on Windows `%USERPROFILE%\.pr-review-skill\core\REVIEW.md`) and follow it exactly.
It holds the full workflow, the review rubric, and the deduplication rules. Do not
work from this file alone.

Pass the user's arguments through as-is, for example `842 paxiai-event-processor`,
plus any extra context they gave (focus areas, or `Additional context: <file>.md`).

Helper scripts live at `~/.pr-review-skill/core/scripts/`:

- `pr_fetch.py <owner/repo> <number> --out <workdir>/bundle.json`
- `pr_post.py <owner/repo> <number> --findings <workdir>/findings.json --bundle <workdir>/bundle.json`

If `~/.pr-review-skill/core/REVIEW.md` does not exist, tell the user to re-run the
installer from their clone of the pr-review-skill repo, and stop.

## Invariants (these hold even if you cannot read REVIEW.md)

- Authenticate only through the local `gh` CLI. Never ask for, read, or create a
  Personal Access Token, and never print credentials.
- Read the PR's existing comments and deduplicate against them before posting
  anything. Reviewing the same PR twice must not repeat a comment.
- Show the findings summary and **wait for the user's approval** before posting.
- Anchor inline comments only to lines that appear in the diff.
- No style or formatting nits, and never restate what the code does.
- Never `checkout`, `stash`, or `reset` in the user's working tree.
