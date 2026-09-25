#!/usr/bin/env python3
"""Post pr-review findings to a GitHub PR as one batched inline review.

Every comment carries a hidden fingerprint marker, and any finding whose
fingerprint is already on the PR is skipped here as well as by the agent -- so
re-running the skill on an unchanged PR posts nothing.

Deduplication has two independent layers, both enforced here (not just trusted from
the model's judgement):
  1. `duplicate_of_id` -- a finding can name an existing comment's id (rc:/ic:/rv:,
     see pr_fetch.py) that it duplicates. If that id is really on the PR, the finding
     is skipped, full stop.
  2. The fingerprint marker -- sha1(path|anchor|issue_class), embedded in every
     comment this tool posts. Catches "this skill already said this" even when the
     model doesn't (or can't) name the specific human comment id.

Usage:
    python pr_post.py <owner/repo> <pr_number> --findings findings.json \
        [--bundle bundle.json] [--dry-run] [--min-severity warning] [--event COMMENT]

findings.json:
    {
      "summary": "optional markdown for the review body",
      "findings": [
        {
          "path": "src/foo.ts",
          "line": 42,                  # line in the PR head (RIGHT) or base (LEFT)
          "side": "RIGHT",             # optional, default RIGHT
          "start_line": 39,            # optional, for a multi-line comment
          "severity": "blocker",       # blocker | warning | suggestion | question
          "issue_class": "null-deref", # stable tag from REVIEW.md's vocabulary
          "anchor": "handleEvent",     # enclosing symbol; keeps the fingerprint
                                       # stable when line numbers shift
          "title": "Possible null dereference",
          "body": "markdown explanation",
          "suggestion": "optional replacement code for a ```suggestion block",
          "duplicate_of_id": "rc:123456789"   # optional: skip, this is the same
                                               # issue as an existing comment/review
        }
      ]
    }
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pr_fetch import GhError, gh_json, paginate, parse_patch, fingerprints_in  # noqa: E402

MARKER_VERSION = 1
DISCLAIMER = "> \U0001F916 *AI-assisted review, posted via the [pr-review skill](https://github.com/pr-review-skill) under this account's own login.*"
SEVERITY_ICON = {
    "blocker": "\U0001F534",
    "warning": "\U0001F7E1",
    "suggestion": "\U0001F535",
    "question": "⚪",
}
SEVERITY_RANK = {"question": 0, "suggestion": 1, "warning": 2, "blocker": 3}


def gh_with_input(args: list, payload: str) -> str:
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", input=payload,
        )
    except FileNotFoundError:
        raise GhError("`gh` is not installed or not on PATH. Run: winget install --id GitHub.cli") from None
    if proc.returncode != 0:
        raise GhError((proc.stderr or proc.stdout or "").strip())
    return proc.stdout


def fingerprint(f: dict) -> str:
    """Stable across runs and across pushes: path + enclosing symbol + issue class.

    Deliberately excludes the line number and the wording of the title, both of
    which move between runs for what is the same issue. This is a best-effort,
    secondary signal -- `duplicate_of_id` (checked against real comment ids, in
    main()) is the primary, code-enforced defense against reposting.
    """
    anchor = (f.get("anchor") or "").strip().lower()
    if not anchor:
        anchor = "L" + str(f.get("line"))
    issue = (f.get("issue_class") or "").strip().lower()
    if not issue:
        issue = re.sub(r"[^a-z0-9]+", "-", (f.get("title") or "").lower()).strip("-")
    key = "|".join([(f.get("path") or "").strip(), anchor, issue])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def compose_body(f: dict, fp: str) -> str:
    icon = SEVERITY_ICON.get((f.get("severity") or "").lower(), "")
    head = (icon + " " if icon else "") + "**" + (f.get("title") or "Review finding").strip() + "**"
    parts = [DISCLAIMER, "", head, "", (f.get("body") or "").strip()]
    if f.get("suggestion"):
        parts += ["", "```suggestion", f["suggestion"].rstrip("\n"), "```"]
    parts += ["", f"<!-- pr-review-skill:v{MARKER_VERSION} fp={fp} -->"]
    return "\n".join(parts).strip() + "\n"


def render_unanchored(u: dict) -> str:
    """Full detail for a finding that couldn't be placed inline -- never just the
    title. The whole point of a finding is the reasoning; losing it here would
    silently throw away the one thing the review was for."""
    lines = [f"<details><summary><b>{u['path']}:{u['line']}</b> — {u['title']} ({u['reason']})</summary>", ""]
    if u.get("body"):
        lines.append(u["body"])
    if u.get("suggestion"):
        lines += ["", "```suggestion", u["suggestion"].rstrip("\n"), "```"]
    lines += ["", "</details>"]
    return "\n".join(lines)


def existing_ids_from_bundle(bundle: dict) -> set:
    ex = bundle.get("existing") or {}
    ids = {c["id"] for c in ex.get("review_comments") or [] if c.get("id")}
    ids |= {c["id"] for c in ex.get("issue_comments") or [] if c.get("id")}
    ids |= {r["id"] for r in ex.get("reviews") or [] if r.get("id")}
    return ids


def load_context(repo: str, number: int, bundle_path):
    """Commentable lines per file, the head sha, fingerprints already on the PR, and
    the set of existing comment/review ids a finding may cite as `duplicate_of_id`."""
    if bundle_path:
        with open(bundle_path, encoding="utf-8") as fh:
            b = json.load(fh)
        commentable = {f["path"]: f["commentable"] for f in b["files"]}
        return commentable, b["head_sha"], set(b.get("posted_fingerprints") or []), existing_ids_from_bundle(b)

    pr = gh_json("api", f"repos/{repo}/pulls/{number}")
    commentable = {}
    for f in paginate(f"repos/{repo}/pulls/{number}/files"):
        _, c = parse_patch(f.get("patch"))
        commentable[f["filename"]] = c
    review_comments = paginate(f"repos/{repo}/pulls/{number}/comments")
    issue_comments = paginate(f"repos/{repo}/issues/{number}/comments")
    reviews = paginate(f"repos/{repo}/pulls/{number}/reviews")
    bodies = (
        [c.get("body") for c in review_comments]
        + [c.get("body") for c in issue_comments]
        + [r.get("body") for r in reviews]
    )
    posted = {fp for body in bodies for fp in fingerprints_in(body)}
    existing_ids = (
        {f"rc:{c['id']}" for c in review_comments}
        | {f"ic:{c['id']}" for c in issue_comments}
        | {f"rv:{r['id']}" for r in reviews}
    )
    return commentable, pr["head"]["sha"], posted, existing_ids


def main() -> int:
    ap = argparse.ArgumentParser(description="Post pr-review findings as inline PR comments.")
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("number", type=int)
    ap.add_argument("--findings", required=True, help="findings JSON file")
    ap.add_argument("--bundle", help="bundle.json from pr_fetch.py (avoids refetching)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be posted")
    ap.add_argument(
        "--min-severity", choices=list(SEVERITY_RANK), default="question",
        help="findings below this severity are summarised only, not posted inline (default: question, i.e. no filtering)",
    )
    ap.add_argument(
        "--event", choices=["COMMENT", "REQUEST_CHANGES", "APPROVE"], default="COMMENT",
        help="review event to submit (default: COMMENT -- never auto-blocks or auto-approves a merge)",
    )
    args = ap.parse_args()

    with open(args.findings, encoding="utf-8") as fh:
        payload = json.load(fh)
    findings = payload.get("findings") or []

    commentable, head_sha, already_posted, existing_ids = load_context(args.repo, args.number, args.bundle)
    min_rank = SEVERITY_RANK[args.min_severity]

    comments = []
    skipped_duplicate = []
    unverified_duplicate_claims = []
    unanchored = []
    below_severity = []
    seen = set()

    for f in findings:
        claimed_dup = f.get("duplicate_of_id")
        if claimed_dup:
            if claimed_dup in existing_ids:
                skipped_duplicate.append({
                    "title": f.get("title"), "path": f.get("path"),
                    "matched_by": "duplicate_of_id", "duplicate_of_id": claimed_dup,
                })
                continue
            # The model named an id that isn't actually on this PR -- don't trust an
            # unverifiable claim to silence a possibly-real finding. Surface it and
            # keep processing the finding normally.
            unverified_duplicate_claims.append({
                "title": f.get("title"), "path": f.get("path"), "claimed_id": claimed_dup,
            })

        fp = f.get("fingerprint") or fingerprint(f)
        if fp in already_posted or fp in seen:
            skipped_duplicate.append({
                "title": f.get("title"), "path": f.get("path"),
                "matched_by": "fingerprint", "fingerprint": fp,
            })
            continue
        seen.add(fp)

        severity = (f.get("severity") or "question").lower()
        if SEVERITY_RANK.get(severity, 0) < min_rank:
            below_severity.append({"title": f.get("title"), "path": f.get("path"), "line": f.get("line")})
            continue

        body = compose_body(f, fp)
        path = f.get("path")
        side = (f.get("side") or "RIGHT").upper()
        line = f.get("line")
        allowed = (commentable.get(path) or {}).get(side) or []

        if line is None or line not in allowed:
            unanchored.append({
                "title": f.get("title"), "path": path, "line": line, "side": side,
                "reason": "file not in the diff" if path not in commentable
                          else "line is outside the diff hunks",
                "body": f.get("body"), "suggestion": f.get("suggestion"),
            })
            continue

        c = {"path": path, "line": line, "side": side, "body": body}
        start = f.get("start_line")
        if start and start != line and start in allowed:
            c["start_line"] = start
            c["start_side"] = side
        comments.append(c)

    body_parts = []
    if payload.get("summary"):
        body_parts.append(payload["summary"].strip())
    if unanchored:
        body_parts.append(
            "**Findings that could not be anchored to the diff**\n\n"
            + "\n\n".join(render_unanchored(u) for u in unanchored)
        )
    if below_severity:
        body_parts.append(
            f"**Findings below --min-severity {args.min_severity} (not posted inline)**\n\n"
            + "\n".join(f"- `{b['path']}:{b['line']}` — {b['title']}" for b in below_severity)
        )
    review_body = "\n\n---\n\n".join(body_parts)
    if review_body:
        review_body = DISCLAIMER + "\n\n" + review_body

    review = {"commit_id": head_sha, "event": args.event, "comments": comments}
    if review_body:
        review["body"] = review_body

    result = {
        "repo": args.repo,
        "number": args.number,
        "head_sha": head_sha,
        "posted": 0,
        "comments_attempted": len(comments),
        "skipped_duplicate": skipped_duplicate,
        "unverified_duplicate_claims": unverified_duplicate_claims,
        "below_severity": below_severity,
        "unanchored": [{k: v for k, v in u.items() if k not in ("body", "suggestion")} for u in unanchored],
        "failed": [],
    }

    if args.dry_run:
        result["dry_run"] = True
        result["preview"] = comments
        result["review_body"] = review_body
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if not comments and not review_body:
        result["note"] = "nothing new to post"
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    try:
        gh_with_input(
            ["api", "--method", "POST", f"repos/{args.repo}/pulls/{args.number}/reviews", "--input", "-"],
            json.dumps(review),
        )
        result["posted"] = len(comments)
        result["mode"] = "batched review"
    except GhError as exc:
        # Review creation is atomic, so nothing landed. Retry comment by comment so
        # one bad anchor cannot sink the whole review.
        result["batch_error"] = str(exc)[:600]
        result["mode"] = "per-comment fallback"
        for c in comments:
            single = dict(c)
            single["commit_id"] = head_sha
            try:
                gh_with_input(
                    ["api", "--method", "POST", f"repos/{args.repo}/pulls/{args.number}/comments", "--input", "-"],
                    json.dumps(single),
                )
                result["posted"] += 1
            except GhError as inner:
                result["failed"].append({
                    "path": c["path"], "line": c["line"], "error": str(inner)[:300],
                })
        if review_body:
            try:
                gh_with_input(
                    ["api", "--method", "POST", f"repos/{args.repo}/issues/{args.number}/comments", "--input", "-"],
                    json.dumps({"body": review_body}),
                )
            except GhError as inner:
                result["failed"].append({"path": "<review body>", "error": str(inner)[:300]})

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
