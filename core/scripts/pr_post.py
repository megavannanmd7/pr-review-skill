#!/usr/bin/env python3
"""Post pr-review findings to a GitHub PR as one batched inline review.

Every comment carries a hidden fingerprint marker, and any finding whose
fingerprint is already on the PR is skipped here as well as by the agent -- so
re-running the skill on an unchanged PR posts nothing.

Usage:
    python pr_post.py <owner/repo> <pr_number> --findings findings.json \
        [--bundle bundle.json] [--dry-run]

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
          "issue_class": "null-deref", # stable tag from SKILL.md's vocabulary
          "anchor": "handleEvent",     # enclosing symbol; keeps the fingerprint
                                       # stable when line numbers shift
          "title": "Possible null dereference",
          "body": "markdown explanation",
          "suggestion": "optional replacement code for a ```suggestion block"
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pr_fetch import GhError, gh_json, paginate, parse_patch, fingerprints_in  # noqa: E402

MARKER_VERSION = 1
SEVERITY_ICON = {
    "blocker": "\U0001F534",
    "warning": "\U0001F7E1",
    "suggestion": "\U0001F535",
    "question": "⚪",
}


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
    which move between runs for what is the same issue.
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
    parts = [head, "", (f.get("body") or "").strip()]
    if f.get("suggestion"):
        parts += ["", "```suggestion", f["suggestion"].rstrip("\n"), "```"]
    parts += ["", f"<!-- pr-review-skill:v{MARKER_VERSION} fp={fp} -->"]
    return "\n".join(parts).strip() + "\n"


def load_context(repo: str, number: int, bundle_path):
    """Commentable lines per file, the head sha, and fingerprints already on the PR."""
    if bundle_path:
        with open(bundle_path, encoding="utf-8") as fh:
            b = json.load(fh)
        commentable = {f["path"]: f["commentable"] for f in b["files"]}
        return commentable, b["head_sha"], set(b.get("posted_fingerprints") or [])

    pr = gh_json("api", f"repos/{repo}/pulls/{number}")
    commentable = {}
    for f in paginate(f"repos/{repo}/pulls/{number}/files"):
        _, c = parse_patch(f.get("patch"))
        commentable[f["filename"]] = c
    bodies = (
        [c.get("body") for c in paginate(f"repos/{repo}/pulls/{number}/comments")]
        + [c.get("body") for c in paginate(f"repos/{repo}/issues/{number}/comments")]
        + [r.get("body") for r in paginate(f"repos/{repo}/pulls/{number}/reviews")]
    )
    posted = {fp for body in bodies for fp in fingerprints_in(body)}
    return commentable, pr["head"]["sha"], posted


def main() -> int:
    ap = argparse.ArgumentParser(description="Post pr-review findings as inline PR comments.")
    ap.add_argument("repo", help="owner/repo")
    ap.add_argument("number", type=int)
    ap.add_argument("--findings", required=True, help="findings JSON file")
    ap.add_argument("--bundle", help="bundle.json from pr_fetch.py (avoids refetching)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be posted")
    args = ap.parse_args()

    with open(args.findings, encoding="utf-8") as fh:
        payload = json.load(fh)
    findings = payload.get("findings") or []

    commentable, head_sha, already_posted = load_context(args.repo, args.number, args.bundle)

    comments = []
    skipped_duplicate = []
    unanchored = []
    seen = set()

    for f in findings:
        fp = f.get("fingerprint") or fingerprint(f)
        if fp in already_posted or fp in seen:
            skipped_duplicate.append({"title": f.get("title"), "path": f.get("path"), "fingerprint": fp})
            continue
        seen.add(fp)

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
                "body": body,
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
            + "\n\n".join(
                f"- `{u['path']}:{u['line']}` ({u['reason']}) -- {u['title']}" for u in unanchored
            )
        )
    review_body = "\n\n---\n\n".join(body_parts)

    review = {"commit_id": head_sha, "event": "COMMENT", "comments": comments}
    if review_body:
        review["body"] = review_body

    result = {
        "repo": args.repo,
        "number": args.number,
        "head_sha": head_sha,
        "posted": 0,
        "comments_attempted": len(comments),
        "skipped_duplicate": skipped_duplicate,
        "unanchored": [{k: v for k, v in u.items() if k != "body"} for u in unanchored],
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
