# PR Review (inline, deduplicated)

Review a GitHub pull request and post only genuinely new, actionable findings as
inline PR comments.

**Step 0 — read the procedure.** Read `~/.pr-review-skill/core/REVIEW.md`
(on Windows `%USERPROFILE%\.pr-review-skill\core\REVIEW.md`) and follow it exactly.
It holds the full workflow, the review rubric, and the deduplication rules. Do not
work from this file alone.

Everything typed after `/pr-review` is the request, for example
`842 paxiai-event-processor`, plus any extra context (focus areas, or
`Additional context: <file>.md`). If nothing was typed after the command, ask which
PR to review.

Helper scripts live at `~/.pr-review-skill/core/scripts/`:

- `pr_fetch.py <owner/repo> <number> --out <workdir>/bundle.json`
- `pr_post.py <owner/repo> <number> --findings <workdir>/findings.json --bundle <workdir>/bundle.json`

Run them with the terminal tool. If `~/.pr-review-skill/core/REVIEW.md` does not
exist, tell the user to re-run the installer from their clone of the pr-review-skill
repo, and stop.

## Invariants (these hold even if you cannot read REVIEW.md)

- Authenticate only through the local `gh` CLI. Never ask for, read, or create a
  Personal Access Token, and never print credentials.
- Read the PR's existing comments and deduplicate against them before posting
  anything. Reviewing the same PR twice must not repeat a comment.
- Show the findings summary and **wait for the user's approval** before posting.
- Anchor inline comments only to lines that appear in the diff.
- No style or formatting nits, and never restate what the code does.
- Never `checkout`, `stash`, or `reset` in the user's working tree.
