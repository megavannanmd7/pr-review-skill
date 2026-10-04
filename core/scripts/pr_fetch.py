#!/usr/bin/env python3
"""Fetch everything the pr-review skill needs about a GitHub PR, as one JSON bundle.

Authentication comes entirely from the local `gh` CLI. No tokens are read, written
or printed by this script.

Usage:
    python pr_fetch.py <owner/repo> <pr_number> [--out bundle.json]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys

# Windows consoles default to a legacy codepage (cp1252); PR titles, comment bodies
# and diffs routinely contain unicode (arrows, smart quotes, emoji), so force UTF-8
# on our own stdout/stderr rather than let a print() crash mid-run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

MARKER_RE = re.compile(r"<!--\s*pr-review-skill:v(\d+)\s+fp=([0-9a-f]{6,64})\s*-->")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")

GRAPHQL_THREADS = """
query($owner:String!,$name:String!,$number:Int!,$after:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:100, after:$after){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id
          isResolved
          isOutdated
          path
          comments(first:100){ nodes{ databaseId } }
        }
      }
    }
  }
}
"""

# Files that are almost never worth reviewing line-by-line: they are machine
# generated, and reading their diffs burns context without producing findings.
# Skipped, not deleted -- the path still shows up in `skipped_files` so the model
# (and the human) can see what was left out and why.
DEFAULT_SKIP_GLOBS = [
    "*.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
    "go.sum", "composer.lock", "poetry.lock", "Gemfile.lock", "Pipfile.lock",
    "*.min.js", "*.min.css", "*.map", "*.generated.*",
]
# Directory names skipped at any depth (monorepo-safe -- "packages/x/dist/y.js" is
# still caught, not just a top-level "dist/*").
DEFAULT_SKIP_DIR_NAMES = {"dist", "build", "vendor", "node_modules"}


class GhError(RuntimeError):
    pass


# Well-known install locations, checked when a bare `gh` lookup fails. This exists
# because PATH is a snapshot taken when a shell/process started: installing `gh` with
# winget/brew *after* that happens (the single most common case -- install it, then
# immediately try to use it in the same already-open terminal or agent session)
# updates the registry/profile but not that process's already-inherited PATH. A shell
# built into the same session genuinely cannot see it without a restart; a filesystem
# check here can, because it never consults PATH at all.
_KNOWN_GH_PATHS = [
    # Windows
    r"C:\Program Files\GitHub CLI\gh.exe",
    r"C:\Program Files (x86)\GitHub CLI\gh.exe",
    os.path.expandvars(r"%LocalAppData%\Programs\GitHub CLI\gh.exe"),
    os.path.expandvars(r"%LocalAppData%\Microsoft\WinGet\Links\gh.exe"),
    os.path.expanduser(r"~\scoop\shims\gh.exe"),
    os.path.expandvars(r"%ChocolateyInstall%\bin\gh.exe"),
    # macOS
    "/opt/homebrew/bin/gh", "/usr/local/bin/gh",
    # Linux
    "/usr/bin/gh", "/usr/local/bin/gh", "/snap/bin/gh", os.path.expanduser("~/.local/bin/gh"),
]

_resolved_gh = None  # cached for the life of this process


def resolve_gh() -> str | None:
    """Absolute path to `gh`, or None if it truly can't be found anywhere.

    Checks PATH first (the normal case), then known install locations (the stale-PATH
    case). Never trust a bare "not found" from PATH alone to mean "not installed".
    """
    global _resolved_gh
    if _resolved_gh:
        return _resolved_gh
    found = shutil.which("gh")
    if not found:
        found = next((p for p in _KNOWN_GH_PATHS if p and os.path.isfile(p)), None)
    _resolved_gh = found
    return found


def gh(*args: str) -> str:
    binary = resolve_gh() or "gh"
    try:
        proc = subprocess.run(
            [binary, *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        raise GhError(
            "`gh` is not installed (checked PATH and common install locations).\n"
            "  Install:  winget install --id GitHub.cli\n"
            "  Sign in:  gh auth login\n"
            "  If you just installed it, this may still be a stale PATH in *this*\n"
            "  process -- opening a new terminal / restarting the tool usually fixes it."
        ) from None
    if proc.returncode != 0:
        raise GhError("gh " + " ".join(args) + " failed:\n" + (proc.stderr or "").strip())
    return proc.stdout


def gh_json(*args: str):
    out = gh(*args).strip()
    return json.loads(out) if out else None


def check_auth() -> None:
    try:
        gh("auth", "status")
    except GhError as exc:
        raise GhError(
            "GitHub CLI is not authenticated.\n"
            "  Run:  gh auth login   (GitHub.com -> HTTPS -> login with a browser)\n\n"
            + str(exc)
        ) from None


def check_permission(owner: str, name: str) -> str:
    """The authenticated user's permission level on the repo (admin/write/read/none).

    Checked once up front so a lack of write access surfaces before the model spends
    time reviewing a PR it will fail to comment on.
    """
    try:
        me = gh_json("api", "user")
        login = me.get("login") if me else None
        if not login:
            return "unknown"
        perm = gh_json("api", f"repos/{owner}/{name}/collaborators/{login}/permission")
        return (perm or {}).get("permission", "unknown")
    except GhError:
        return "unknown"  # non-fatal: some hosts/orgs restrict this endpoint even with valid access


def paginate(path: str, per_page: int = 100, max_pages: int = 20) -> list:
    """Page through a REST list endpoint explicitly (portable across gh versions)."""
    items: list = []
    for page in range(1, max_pages + 1):
        sep = "&" if "?" in path else "?"
        batch = gh_json("api", f"{path}{sep}per_page={per_page}&page={page}")
        if not batch:
            break
        items.extend(batch)
        if len(batch) < per_page:
            break
    return items


def parse_patch(patch):
    """Map a unified-diff patch to its hunks and the lines GitHub accepts comments on.

    GitHub rejects inline comments anchored outside a diff hunk, so the commentable
    sets computed here are what make posting reliable.
    """
    hunks: list = []
    right: list = []
    left: list = []
    if not patch:
        return hunks, {"RIGHT": right, "LEFT": left}

    old = new = 0
    cur = None
    for raw in patch.split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(3))
            cur = {
                "header": raw.strip(),
                "new_start": new,
                "new_end": new,
                "old_start": old,
                "old_end": old,
            }
            hunks.append(cur)
            continue
        if cur is None:
            continue
        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            right.append(new)
            cur["new_end"] = new
            new += 1
        elif raw.startswith("-"):
            left.append(old)
            cur["old_end"] = old
            old += 1
        else:  # context line (leading space, or a bare empty trailing line)
            right.append(new)
            left.append(old)
            cur["new_end"] = new
            cur["old_end"] = old
            new += 1
            old += 1
    return hunks, {"RIGHT": right, "LEFT": left}


def fetch_unified_diff_patches(repo: str, number: int) -> dict:
    """Fallback source of per-file patches when the files-list endpoint omits one.

    GitHub's `/pulls/{n}/files` leaves `patch` null for very large diffs. The
    whole-PR unified diff (`Accept: application/vnd.github.v3.diff`) doesn't have
    that limit, so we fetch it once and slice out the hunks for whichever files
    need them -- in the same "hunks-only" shape `files[].patch` normally has, so
    parse_patch() doesn't need to know the difference.
    """
    try:
        text = gh(
            "api", f"repos/{repo}/pulls/{number}",
            "-H", "Accept: application/vnd.github.v3.diff",
        )
    except GhError:
        return {}

    patches: dict = {}
    path = None
    lines: list = []
    in_hunks = False

    def flush():
        if path and lines:
            patches[path] = "\n".join(lines)

    for raw in text.split("\n"):
        m = DIFF_GIT_RE.match(raw)
        if m:
            flush()
            path = m.group(2)
            lines = []
            in_hunks = False
            continue
        if raw.startswith("@@"):
            in_hunks = True
        if in_hunks:
            lines.append(raw)
    flush()
    return patches


def is_skippable(path: str, extra_globs) -> bool:
    if not extra_globs:
        return False
    parts = path.split("/")
    if DEFAULT_SKIP_DIR_NAMES.intersection(parts):
        return True
    # Bare-filename patterns like "pnpm-lock.yaml" should also catch that file inside
    # a subdirectory in a monorepo (frontend/pnpm-lock.yaml), not just at the repo
    # root, so match on the basename too.
    basename = parts[-1]
    for pattern in extra_globs:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern):
            return True
    return False


def fetch_checks(repo: str, head_sha: str, max_annotations: int = 100) -> dict:
    """CI results for the PR head, including per-file annotations.

    A typechecker or linter has usually already run on this commit, and its real
    output is far better evidence than an LLM imagining what a compiler would say.
    Fetching it here is free and, unlike running the project's own build locally,
    involves executing none of the PR's code -- which matters when the PR comes from
    a fork.
    """
    out = {"state": "unknown", "runs": [], "annotations": [], "annotations_truncated": False}
    try:
        data = gh_json("api", f"repos/{repo}/commits/{head_sha}/check-runs?per_page=100")
    except GhError:
        return out

    runs = (data or {}).get("check_runs") or []
    failing_ids = []
    for r in runs:
        conclusion = r.get("conclusion")
        out["runs"].append({
            "name": r.get("name"),
            "status": r.get("status"),
            "conclusion": conclusion,
            "title": ((r.get("output") or {}).get("title") or "")[:200],
            "summary_excerpt": ((r.get("output") or {}).get("summary") or "")[:500],
        })
        if conclusion in ("failure", "action_required", "timed_out") and (r.get("output") or {}).get("annotations_count"):
            failing_ids.append((r["id"], r.get("name")))

    conclusions = {r.get("conclusion") for r in runs}
    if not runs:
        out["state"] = "none"
    elif conclusions & {"failure", "action_required", "timed_out"}:
        out["state"] = "failure"
    elif any(r.get("status") != "completed" for r in runs):
        out["state"] = "pending"
    else:
        out["state"] = "success"

    for run_id, run_name in failing_ids:
        if len(out["annotations"]) >= max_annotations:
            out["annotations_truncated"] = True
            break
        try:
            anns = gh_json("api", f"repos/{repo}/check-runs/{run_id}/annotations?per_page=100") or []
        except GhError:
            continue
        for a in anns:
            if len(out["annotations"]) >= max_annotations:
                out["annotations_truncated"] = True
                break
            out["annotations"].append({
                "check_name": run_name,
                "path": a.get("path"),
                "start_line": a.get("start_line"),
                "end_line": a.get("end_line"),
                "level": a.get("annotation_level"),
                "title": a.get("title"),
                "message": (a.get("message") or "")[:1000],
            })

    # Older CI (Jenkins and friends) reports commit statuses rather than check runs.
    if out["state"] in ("none", "unknown"):
        try:
            status = gh_json("api", f"repos/{repo}/commits/{head_sha}/status")
            if status and status.get("state"):
                out["state"] = status["state"]
                for s in status.get("statuses") or []:
                    out["runs"].append({
                        "name": s.get("context"),
                        "status": "completed",
                        "conclusion": s.get("state"),
                        "title": (s.get("description") or "")[:200],
                        "summary_excerpt": "",
                    })
        except GhError:
            pass

    return out


def thread_state(owner: str, name: str, number: int) -> dict:
    """databaseId of each review comment -> {resolved, outdated, thread_id} of its
    thread. `thread_id` is the thread's own GraphQL node id -- the only thing GitHub's
    `resolveReviewThread` mutation accepts, so pr_post.py needs it to ever mark one of
    this skill's own threads resolved."""
    state: dict = {}
    after = None
    for _ in range(20):
        args = [
            "api", "graphql",
            "-f", "query=" + GRAPHQL_THREADS,
            "-f", "owner=" + owner,
            "-f", "name=" + name,
            "-F", "number=" + str(number),
        ]
        if after:
            args += ["-f", "after=" + after]
        try:
            data = gh_json(*args)
        except GhError:
            return state  # thread resolution is a nicety; never fail the fetch over it
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
        for node in threads["nodes"]:
            for c in node["comments"]["nodes"]:
                state[c["databaseId"]] = {
                    "resolved": bool(node["isResolved"]),
                    "outdated": bool(node["isOutdated"]),
                    "thread_id": node.get("id"),
                }
        if not threads["pageInfo"]["hasNextPage"]:
            break
        after = threads["pageInfo"]["endCursor"]
    return state


def fingerprints_in(body) -> list:
    return [m.group(2) for m in MARKER_RE.finditer(body or "")]


# The skill's own section of the PR description sits between these two markers. Only
# the text between them is ever rewritten; everything outside is the author's and is
# preserved byte for byte.
DESC_START_RE = re.compile(r"<!--\s*pr-review-skill:description:start(?:\s+sha=([0-9a-f]{7,40}))?\s*-->")
DESC_END_RE = re.compile(r"<!--\s*pr-review-skill:description:end\s*-->")


def split_description(body):
    """Split a PR description into (before, generated, after, sha, intact).

    `generated` is None when the skill has never written to this description. `intact`
    is False when the markers are damaged (one missing, out of order, or repeated) --
    the caller must then leave the description alone rather than guess where the
    author's text starts.
    """
    body = body or ""
    starts = list(DESC_START_RE.finditer(body))
    ends = list(DESC_END_RE.finditer(body))
    if not starts and not ends:
        return body, None, "", None, True
    if len(starts) != 1 or len(ends) != 1 or ends[0].start() < starts[0].end():
        return body, None, "", None, False
    st, en = starts[0], ends[0]
    return body[: st.start()], body[st.end(): en.start()], body[en.end():], st.group(1), True


def author_description(body) -> str:
    """The PR description with the skill's own section removed: the author's words only."""
    before, generated, after, _, intact = split_description(body)
    if generated is None or not intact:
        return (body or "").strip()
    return (before.rstrip() + "\n\n" + after.lstrip()).strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch a GitHub PR as one JSON bundle.")
    ap.add_argument("repo", nargs="?", help="owner/repo")
    ap.add_argument("number", type=int, nargs="?", help="pull request number")
    ap.add_argument("--out", help="write the bundle here instead of stdout")
    ap.add_argument(
        "--max-patch-bytes", type=int, default=60000,
        help="truncate any single file patch longer than this (default 60000)",
    )
    ap.add_argument(
        "--include-lockfiles", action="store_true",
        help="do not skip lockfiles/generated/vendored files (skipped by default)",
    )
    ap.add_argument(
        "--check-auth", action="store_true",
        help="just verify gh is found and authenticated, then exit (no repo/number needed)",
    )
    args = ap.parse_args()

    if args.check_auth:
        binary = resolve_gh()
        if not binary:
            print(
                "gh: not found on PATH or in any known install location.\n"
                "  Install:  winget install --id GitHub.cli   # macOS: brew install gh\n"
                "  Sign in:  gh auth login",
                file=sys.stderr,
            )
            return 1
        try:
            check_auth()
        except GhError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"gh: {binary}")
        print("auth: OK")
        return 0

    if not args.repo or args.number is None:
        print("repo and number are required unless --check-auth is passed", file=sys.stderr)
        return 2
    if "/" not in args.repo:
        print("repo must be owner/repo, got " + repr(args.repo), file=sys.stderr)
        return 2
    owner, name = args.repo.split("/", 1)
    n = args.number

    check_auth()
    permission = check_permission(owner, name)

    pr = gh_json("api", f"repos/{args.repo}/pulls/{n}")
    files_raw = paginate(f"repos/{args.repo}/pulls/{n}/files")
    review_comments = paginate(f"repos/{args.repo}/pulls/{n}/comments")
    issue_comments = paginate(f"repos/{args.repo}/issues/{n}/comments")
    reviews = paginate(f"repos/{args.repo}/pulls/{n}/reviews")
    commits_raw = paginate(f"repos/{args.repo}/pulls/{n}/commits")
    threads = thread_state(owner, name, n)
    checks = fetch_checks(args.repo, pr["head"]["sha"])

    skip_globs = [] if args.include_lockfiles else DEFAULT_SKIP_GLOBS
    needs_fallback = any(
        f.get("patch") is None and f["status"] != "removed" and not is_skippable(f["filename"], skip_globs)
        for f in files_raw
    )
    fallback_patches = fetch_unified_diff_patches(args.repo, n) if needs_fallback else {}

    files = []
    skipped_files = []
    for f in files_raw:
        path = f["filename"]
        if is_skippable(path, skip_globs):
            skipped_files.append({"path": path, "reason": "lockfile/generated/vendored (default filter)"})
            continue

        full_patch = f.get("patch")
        used_fallback = False
        if full_patch is None and path in fallback_patches:
            full_patch = fallback_patches[path]
            used_fallback = True

        patch = full_patch
        truncated = False
        if patch and len(patch) > args.max_patch_bytes:
            patch = patch[: args.max_patch_bytes]
            truncated = True
        hunks, commentable = parse_patch(full_patch)
        files.append({
            "path": path,
            "previous_path": f.get("previous_filename"),
            "status": f["status"],
            "additions": f["additions"],
            "deletions": f["deletions"],
            "patch": patch,
            "patch_truncated": truncated,
            "patch_omitted": full_patch is None,  # binary, or unavailable from either source
            "patch_fallback_used": used_fallback,
            "hunks": hunks,
            "commentable": commentable,
        })

    def source_id(prefix: str, raw_id) -> str:
        return f"{prefix}:{raw_id}"

    existing = []
    for c in review_comments:
        state = threads.get(c["id"], {})
        line = c.get("line")
        if line is None:
            line = c.get("original_line")
        existing.append({
            "id": source_id("rc", c["id"]),
            "path": c.get("path"),
            "line": line,
            "start_line": c.get("start_line") or c.get("original_start_line"),
            "side": c.get("side") or "RIGHT",
            "anchored": c.get("position") is not None,
            "outdated": state.get("outdated", c.get("position") is None),
            "resolved": state.get("resolved", False),
            "thread_id": state.get("thread_id"),
            "user": (c.get("user") or {}).get("login"),
            "in_reply_to_id": c.get("in_reply_to_id"),
            "created_at": c.get("created_at"),
            "body": c.get("body"),
            "fingerprints": fingerprints_in(c.get("body")),
        })

    all_bodies = (
        [c.get("body") for c in review_comments]
        + [c.get("body") for c in issue_comments]
        + [r.get("body") for r in reviews]
    )
    posted = sorted({fp for body in all_bodies for fp in fingerprints_in(body)})

    _, generated_desc, _, generated_sha, _ = split_description(pr.get("body"))
    bundle = {
        "repo": args.repo,
        "number": n,
        "url": pr.get("html_url"),
        "title": pr.get("title"),
        # The author's own description. The skill's generated section (if an earlier
        # run added one) is split out so it is never mistaken for the author's intent.
        "body": author_description(pr.get("body")),
        "generated_description": generated_desc,
        "generated_description_sha": generated_sha,
        "author": (pr.get("user") or {}).get("login"),
        "state": pr.get("state"),
        "draft": pr.get("draft"),
        "merged": pr.get("merged"),
        "head_sha": pr["head"]["sha"],
        "head_ref": pr["head"]["ref"],
        "base_ref": pr["base"]["ref"],
        "changed_files": pr.get("changed_files"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "viewer_permission": permission,  # admin/write/read/none/unknown
        # Commit messages carry the author's stated intent; a deliberate, documented
        # behaviour change is not a bug, and knowing that avoids flagging it as one.
        "commits": [
            {
                "sha": (c.get("sha") or "")[:12],
                "message": ((c.get("commit") or {}).get("message") or "").strip()[:1000],
                "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
            }
            for c in commits_raw
        ],
        "checks": checks,
        "files": files,
        "skipped_files": skipped_files,
        "existing": {
            # A single flat list, each entry tagged with a stable "id" the model can
            # reference verbatim in a finding's `duplicate_of_id` -- rc:/ic:/rv: are
            # separate id namespaces (review comment / issue comment / review body),
            # since GitHub's own numeric ids are not unique across them.
            "review_comments": existing,
            "issue_comments": [
                {
                    "id": source_id("ic", c["id"]),
                    "user": (c.get("user") or {}).get("login"),
                    "created_at": c.get("created_at"),
                    "body": c.get("body"),
                }
                for c in issue_comments
            ],
            "reviews": [
                {
                    "id": source_id("rv", r["id"]),
                    "user": (r.get("user") or {}).get("login"),
                    "state": r.get("state"),
                    "submitted_at": r.get("submitted_at"),
                    "body": r.get("body"),
                }
                for r in reviews
                if (r.get("body") or "").strip()
            ],
        },
        "posted_fingerprints": posted,
    }

    text = json.dumps(bundle, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(
            f"PR #{n} {args.repo}: {len(files)} files ({len(skipped_files)} skipped), "
            f"{len(existing)} existing inline comments, "
            f"{len(posted)} previously posted by this skill, "
            f"CI: {checks['state']} ({len(checks['annotations'])} annotations), "
            f"your permission: {permission} -> {args.out}"
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GhError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
