#!/usr/bin/env python3
"""Run the repo's own checks (typecheck, lint, tests) against the PR head, locally.

This executes the PR's code on your machine, so it is locked down:

- Off unless you turn it on: `local_checks: true` in your own
  ~/.pr-review-skill/config.yml, or asking for it in the request. The agent passes
  --allow-exec only then, and this script refuses to run without it.
- Never for a fork PR. Its code comes from outside the repo.
- Only in a worktree checked out at exactly the PR head, never your own checkout.
- Only the commands listed under `checks:` in config. Nothing is guessed or
  auto-detected, since even `npm install` runs arbitrary scripts.
- Tokens are removed from the environment the commands see. Code that runs can
  still reach anything else your user account can, which is why this is opt-in.

Diagnostics that point at a file and line in the diff are pulled out of the output,
so a real compiler error can be cited instead of guessed at.

Usage:
    python pr_checks.py --bundle bundle.json --worktree <dir> --allow-exec [--out checks.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

OUTPUT_TAIL = 20000
SECRET_ENV_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CREDENTIAL)", re.I)
# path:line, path:line:col, path(line,col) -- covers tsc, eslint (unix format), mypy,
# flake8, ruff, go vet, rustc, javac, pytest tracebacks' "File "x", line N".
DIAG_RES = [
    re.compile(r"(?P<path>[\w./\\@+-]+\.[A-Za-z0-9]+)[:(](?P<line>\d+)(?:[:,](?P<col>\d+))?\)?"),
    re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)'),
]


def scrubbed_env() -> dict:
    return {k: v for k, v in os.environ.items() if not SECRET_ENV_RE.search(k)}


def normalise(path: str, worktree: str) -> str:
    path = path.replace("\\", "/")
    root = os.path.realpath(worktree).replace("\\", "/").rstrip("/") + "/"
    if path.startswith(root):
        path = path[len(root):]
    return path[2:] if path.startswith("./") else path


def diagnostics(output: str, worktree: str, files: dict, command: str, limit: int = 200) -> list:
    """Lines of output that point into a file the PR changes."""
    out, seen = [], set()
    for text in output.split("\n"):
        for rx in DIAG_RES:
            for m in rx.finditer(text):
                path = normalise(m.group("path"), worktree)
                if path not in files:
                    continue
                line = int(m.group("line"))
                key = (path, line, text.strip())
                if key in seen:
                    continue
                seen.add(key)
                right = ((files[path].get("commentable") or {}).get("RIGHT")) or []
                out.append({"command": command, "path": path, "line": line,
                            "in_diff": line in right, "text": text.strip()[:500]})
                if len(out) >= limit:
                    return out
    return out


def git_head(worktree: str):
    try:
        proc = subprocess.run(["git", "-C", worktree, "rev-parse", "HEAD"],
                              capture_output=True, text=True)
    except FileNotFoundError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def run(bundle: dict, worktree: str, allow_exec: bool, timeout=None) -> dict:
    settings = ((bundle.get("config") or {}).get("settings")) or {}
    commands = settings.get("checks") or []
    result = {"status": None, "runs": [], "diagnostics": []}
    if not allow_exec:
        result["status"] = "refused_not_enabled"
        result["reason"] = "local checks execute the PR's code; pass --allow-exec only when the user enabled them"
        return result
    # Only a PR positively known to come from this repo qualifies; unknown means no.
    if bundle.get("is_fork_pr") is not False:
        result["status"] = "refused_fork"
        result["reason"] = "this PR comes from a fork (or its origin is unknown); its code is never executed"
        return result
    if not commands:
        result["status"] = "no_checks_configured"
        result["reason"] = "add commands under `checks:` in .github/pr-review.yml or your own config"
        return result
    head = git_head(worktree)
    if not head or head != bundle.get("head_sha"):
        result["status"] = "refused_wrong_checkout"
        result["reason"] = f"{worktree} is at {head or 'no commit'}, not the PR head {bundle.get('head_sha')}"
        return result

    files = {f["path"]: f for f in bundle.get("files") or []}
    limit = timeout or settings.get("checks_timeout", 600)
    env = scrubbed_env()
    for command in commands:
        started = time.time()
        entry = {"command": command, "exit_code": None, "timed_out": False}
        try:
            proc = subprocess.run(command, shell=True, cwd=worktree, env=env, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=limit)
            output = (proc.stdout or "") + (proc.stderr or "")
            entry["exit_code"] = proc.returncode
        except subprocess.TimeoutExpired as exc:
            raw = (exc.stdout or b"") + (exc.stderr or b"")
            output = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            entry["timed_out"] = True
        entry["duration_s"] = round(time.time() - started, 1)
        entry["output_tail"] = output[-OUTPUT_TAIL:]
        result["runs"].append(entry)
        result["diagnostics"] += diagnostics(output, worktree, files, command)
    failed = [r for r in result["runs"] if r["timed_out"] or r["exit_code"] != 0]
    result["status"] = "failures" if failed else "passed"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Run configured checks against the PR head in a worktree.")
    ap.add_argument("--bundle", required=True)
    ap.add_argument("--worktree", required=True, help="a worktree checked out at the PR head")
    ap.add_argument("--allow-exec", action="store_true",
                    help="required: confirms the user enabled local execution of the PR's code")
    ap.add_argument("--timeout", type=int, help="per-command timeout in seconds (default: checks_timeout)")
    ap.add_argument("--out")
    args = ap.parse_args()
    with open(args.bundle, encoding="utf-8") as fh:
        bundle = json.load(fh)
    result = run(bundle, args.worktree, args.allow_exec, args.timeout)
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        in_diff = sum(1 for d in result["diagnostics"] if d["in_diff"])
        print(f"local checks: {result['status']}, {len(result['runs'])} commands, "
              f"{in_diff} diagnostics on diff lines -> {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
