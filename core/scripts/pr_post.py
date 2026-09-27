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

The PR description is updated too, when findings.json carries a `description`: it is
written between two hidden markers at the top of the description, and the author's own
text is kept exactly as it is below. A rerun rewrites only the text between the
markers, so the section is replaced, never repeated.

Three more things happen here, all enforced in code:
  - Findings a human dismissed on an earlier PR of this repo ("intentional", "won't
    fix", a thumbs-down) are skipped by fingerprint (`learned_suppressions` in the
    bundle), unless --ignore-learned is passed.
  - Anything that looks like a credential is redacted from every body before it
    leaves this machine.
  - `followups` reply to, or resolve, this skill's *own* earlier threads. Threads
    started by anyone else are refused, and the skill never replies twice in a row.

Usage:
    python pr_post.py <owner/repo> <pr_number> --findings findings.json \
        [--bundle bundle.json] [--dry-run] [--min-severity warning] [--event COMMENT] \
        [--no-description] [--ignore-learned]

findings.json:
    {
      "summary": "optional markdown for the review body",
      "description": "optional markdown: what this PR changes, for the PR description",
      "followups": [
        {"thread_id": "PRRT_...", "action": "reply | resolve | reply_and_resolve",
         "body": "markdown reply (for reply actions)"}
      ],
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
from pr_fetch import (  # noqa: E402
    FOLLOWUP_MARKER, GhError, gh, gh_json, paginate, parse_patch, fingerprints_in, resolve_gh,
    split_description,
)

MARKER_VERSION = 1
DISCLAIMER = "> \U0001F916 *AI-assisted review, posted via the [pr-review skill](https://github.com/pr-review-skill) under this account's own login.*"
SEVERITY_ICON = {
    "blocker": "\U0001F534",
    "warning": "\U0001F7E1",
    "suggestion": "\U0001F535",
    "question": "⚪",
}
SEVERITY_RANK = {"question": 0, "suggestion": 1, "warning": 2, "blocker": 3}

# Credential shapes that must never be echoed back into a PR, even inside a finding
# about a leaked secret. Deliberately specific: a false redaction garbles a comment,
# but a broad pattern would mangle ordinary hashes and ids.
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                     # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),                    # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),                 # Slack
    re.compile(r"\bsk-(?:ant-|proj-|live_|test_)?[A-Za-z0-9_-]{20,}\b"),  # OpenAI/Anthropic/Stripe style
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),                         # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
]
GRAPHQL_RESOLVE = "mutation($id:ID!){ resolveReviewThread(input:{threadId:$id}){ thread{ isResolved } } }"


def redact(text, counter: list):
    """Replace credential-shaped strings with a placeholder; counter[0] counts them."""
    if not text:
        return text
    for rx in SECRET_PATTERNS:
        text, n = rx.subn("[redacted credential]", text)
        counter[0] += n
    return text


def marker_value(value) -> str:
    """A marker attribute must stay inside one HTML comment token."""
    cleaned = re.sub(r"[^A-Za-z0-9_.:$#-]+", "_", str(value or ""))
    return re.sub(r"-{2,}", "-", cleaned).strip("_-")[:80]  # "--" is not allowed inside a comment


def gh_with_input(args: list, payload: str) -> str:
    binary = resolve_gh() or "gh"
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", input=payload,
        )
    except FileNotFoundError:
        raise GhError(
            "`gh` is not installed (checked PATH and common install locations). "
            "Run: winget install --id GitHub.cli"
        ) from None
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
    attrs = ""
    if marker_value(f.get("issue_class")):
        attrs += " cls=" + marker_value(f.get("issue_class"))
    if marker_value(f.get("anchor")):
        attrs += " anchor=" + marker_value(f.get("anchor"))
    parts += ["", f"<!-- pr-review-skill:v{MARKER_VERSION} fp={fp}{attrs} -->"]
    return "\n".join(parts).strip() + "\n"


def render_description_block(text: str, head_sha: str, author_text_follows: bool) -> str:
    sha = (head_sha or "")[:12]
    start = "<!-- pr-review-skill:description:start" + (f" sha={sha}" if sha else "") + " -->"
    parts = [
        start,
        "## Summary of changes",
        "",
        f"<sub>\U0001F916 Generated by the pr-review skill from the diff at `{sha[:7]}`. "
        "It is rewritten on every review: edit outside this section, not inside it.</sub>",
        "",
        text.strip(),
    ]
    if author_text_follows:
        parts += ["", "---"]
    parts.append("<!-- pr-review-skill:description:end -->")
    return "\n".join(parts)


def merge_description(current, text: str, head_sha: str):
    """(new description or None, status). Only the skill's own section ever changes.

    status: "added" (first time; the section goes above the author's text), "updated"
    (the section is replaced in place), "unchanged", or "markers_damaged" (one marker
    is missing or repeated, so where the author's text starts can't be trusted --
    leave the description alone).
    """
    current = current or ""
    before, old, after, _, intact = split_description(current)
    if not intact:
        return None, "markers_damaged"
    if old is None:
        author = current.strip()
        block = render_description_block(text, head_sha, bool(author))
        return (block + "\n\n" + author if author else block), "added"
    block = render_description_block(text, head_sha, bool(before.strip() or after.strip()))
    new = before + block + after
    if new == current:
        return None, "unchanged"
    return new, "updated"


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
    ap.add_argument(
        "--no-description", action="store_true",
        help="leave the PR description untouched even if findings.json has a description",
    )
    ap.add_argument(
        "--ignore-learned", action="store_true",
        help="post findings even if a human dismissed the same finding on an earlier PR",
    )
    args = ap.parse_args()

    with open(args.findings, encoding="utf-8") as fh:
        payload = json.load(fh)
    findings = payload.get("findings") or []
    bundle = {}
    if args.bundle:
        with open(args.bundle, encoding="utf-8") as fh:
            bundle = json.load(fh)
    learned = {} if args.ignore_learned else {
        e["fingerprint"]: e for e in bundle.get("learned_suppressions") or [] if e.get("fingerprint")
    }
    redactions = [0]

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

        if fp in learned:
            e = learned[fp]
            skipped_duplicate.append({
                "title": f.get("title"), "path": f.get("path"), "matched_by": "learned_suppression",
                "fingerprint": fp, "reason": e.get("reason"), "dismissed_on": e.get("url"),
            })
            continue

        severity = (f.get("severity") or "question").lower()
        if SEVERITY_RANK.get(severity, 0) < min_rank:
            below_severity.append({"title": f.get("title"), "path": f.get("path"), "line": f.get("line")})
            continue

        body = redact(compose_body(f, fp), redactions)
        path = f.get("path")
        side = (f.get("side") or "RIGHT").upper()
        line = f.get("line")
        allowed = (commentable.get(path) or {}).get(side) or []

        if line is None or line not in allowed:
            unanchored.append({
                "title": f.get("title"), "path": path, "line": line, "side": side,
                "reason": "file not in the diff" if path not in commentable
                          else "line is outside the diff hunks",
                "body": redact(f.get("body"), redactions),
                "suggestion": redact(f.get("suggestion"), redactions),
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
        body_parts.append(redact(payload["summary"].strip(), redactions))
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
    has_content = bool(comments or review_body)
    # Records which head was reviewed, so the next run can review only what's new.
    reviewed_marker = f"<!-- pr-review-skill:reviewed sha={head_sha} -->"
    review_body = (review_body + "\n\n" + reviewed_marker) if review_body else reviewed_marker

    review = {"commit_id": head_sha, "event": args.event, "comments": comments, "body": review_body}

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
    followups, followup_results = plan_followups(payload.get("followups") or [], bundle, redactions)
    result["followups"] = followup_results

    desc_text = (payload.get("description") or "").strip()
    new_description = None
    if args.no_description:
        result["description"] = {"status": "disabled"}
    elif not desc_text:
        result["description"] = {"status": "not_provided"}
    else:
        # Read the description fresh rather than from the bundle: the author may have
        # edited it since the fetch, and writing a stale copy back would erase that.
        try:
            pr = gh_json("api", f"repos/{args.repo}/pulls/{args.number}")
            new_description, status = merge_description(pr.get("body"), redact(desc_text, redactions), head_sha)
            result["description"] = {"status": status}
        except GhError as exc:
            # A description problem must never cost the review itself.
            result["description"] = {"status": "failed", "error": str(exc)[:300]}
            result["failed"].append({"path": "<pr description>", "error": str(exc)[:300]})

    result["redacted_credentials"] = redactions[0]

    if args.dry_run:
        result["dry_run"] = True
        result["preview"] = comments
        result["review_body"] = review_body if has_content else None
        result["followups_preview"] = followups
        if new_description is not None:
            result["description"]["preview"] = new_description
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if not has_content:
        result["note"] = "no new review comments to post"
    else:
        post_review(args, review, comments, review_body, head_sha, result)

    run_followups(args, followups, followup_results, result)

    if new_description is not None:
        try:
            gh_with_input(
                ["api", "--method", "PATCH", f"repos/{args.repo}/pulls/{args.number}", "--input", "-"],
                json.dumps({"body": new_description}),
            )
        except GhError as exc:
            result["description"] = {
                "status": "failed",
                "error": str(exc)[:300],
                "hint": "editing a PR description needs write access to the repo or authorship of the PR",
            }
            result["failed"].append({"path": "<pr description>", "error": str(exc)[:300]})

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def plan_followups(requested: list, bundle: dict, redactions: list):
    """(actions to perform, per-request results). Only this skill's own threads qualify."""
    threads = {t.get("thread_id"): t for t in bundle.get("skill_threads") or [] if t.get("thread_id")}
    planned, results = [], []
    for req in requested:
        tid = req.get("thread_id")
        action = req.get("action")
        entry = {"thread_id": tid, "action": action}
        t = threads.get(tid)
        if t is None:
            entry.update(status="refused", reason="not a thread started by this skill (or no --bundle given)")
        elif action not in ("reply", "resolve", "reply_and_resolve"):
            entry.update(status="refused", reason=f"unknown action {action!r}")
        elif action != "resolve" and not (req.get("body") or "").strip():
            entry.update(status="refused", reason="a reply needs a body")
        elif action != "resolve" and t.get("last_reply_by_skill"):
            entry.update(status="refused", reason="this skill already has the last word in this thread")
        elif action == "resolve" and t.get("resolved"):
            entry.update(status="skipped", reason="already resolved")
        else:
            entry["status"] = "planned"
            body = None
            if action != "resolve":
                body = (DISCLAIMER + "\n\n" + redact(req["body"].strip(), redactions)
                        + "\n\n" + FOLLOWUP_MARKER)
            planned.append({"thread_id": tid, "action": action, "body": body,
                            "root_comment_id": t["root_comment_id"].split(":", 1)[1],
                            "resolve": action != "reply" and not t.get("resolved")})
        results.append(entry)
    return planned, results


def run_followups(args, planned: list, results: list, result: dict) -> None:
    by_tid = {r["thread_id"]: r for r in results if r.get("status") == "planned"}
    for item in planned:
        entry = by_tid[item["thread_id"]]
        try:
            if item["body"]:
                gh_with_input(
                    ["api", "--method", "POST",
                     f"repos/{args.repo}/pulls/{args.number}/comments/{item['root_comment_id']}/replies",
                     "--input", "-"],
                    json.dumps({"body": item["body"]}),
                )
            if item["resolve"]:
                gh("api", "graphql", "-f", "query=" + GRAPHQL_RESOLVE, "-f", "id=" + item["thread_id"])
            entry["status"] = "done"
        except GhError as exc:
            entry.update(status="failed", error=str(exc)[:300])
            result["failed"].append({"path": "<follow-up " + item["thread_id"] + ">", "error": str(exc)[:300]})


def post_review(args, review: dict, comments: list, review_body: str, head_sha: str, result: dict) -> None:
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


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
