#!/usr/bin/env python3
"""Blast-radius analysis: who calls the things this PR changed?

The most dangerous class of PR bug is invisible in the diff. If a PR changes
`findUser` from returning `User | null` to throwing, the diff looks clean and
correct -- and fifteen callers in files the PR never touches are now broken.

This script finds those callers. It extracts symbols whose *declarations* were
changed or removed by the PR, then greps a local clone for uses of those symbols
outside the PR's own changed files.

It is a heuristic, not a compiler: it reports call sites to look at, and says
nothing about whether each one is actually broken. Treat its output as a list of
places to check, not as findings.

Usage:
    python pr_impact.py --bundle bundle.json --clone /path/to/repo [--ref refs/remotes/pr/842]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Declaration patterns, anchored at line start (after indentation). Anchoring is
# what keeps these from matching ordinary call sites mid-expression.
DECL_PATTERNS = [
    # JavaScript / TypeScript
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)",
    r"^\s*export\s+(?:const|let|var)\s+(\w+)",
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)",
    r"^\s*export\s+(?:interface|type|enum)\s+(\w+)",
    # Python
    r"^\s*(?:async\s+)?def\s+(\w+)",
    r"^\s*class\s+(\w+)",
    # Go
    r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)",
    r"^\s*type\s+(\w+)\s+(?:struct|interface)",
    # Rust
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
    r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)",
    # Java / C#
    r"^\s*(?:public|protected|internal)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],.\s]+?\s+(\w+)\s*\(",
]
COMPILED = [re.compile(p) for p in DECL_PATTERNS]

# Names too generic for a word-grep to say anything useful about.
STOPWORDS = {
    "main", "init", "test", "setup", "teardown", "new", "get", "set", "run", "call",
    "exec", "start", "stop", "close", "open", "read", "write", "load", "save", "parse",
    "format", "build", "render", "update", "create", "delete", "list", "find", "handle",
    "process", "execute", "validate", "serialize", "clone", "copy", "next", "prev",
    "value", "data", "item", "node", "self", "this", "constructor", "default", "index",
    "error", "result", "response", "request", "config", "options", "params", "args",
    "props", "state", "type", "name", "string", "number", "boolean", "object",
}
MIN_SYMBOL_LEN = 4


def declarations_in(line: str) -> list:
    for rx in COMPILED:
        m = rx.match(line)
        if m:
            return [m.group(1)]
    return []


def interesting(symbol: str) -> bool:
    return len(symbol) >= MIN_SYMBOL_LEN and symbol.lower() not in STOPWORDS


def symbols_from_patch(patch: str):
    """(added, removed) declaration names from one file's unified-diff patch."""
    added, removed = set(), set()
    for raw in (patch or "").split("\n"):
        if raw.startswith("+++") or raw.startswith("---") or raw.startswith("@@"):
            continue
        if raw.startswith("+"):
            for s in declarations_in(raw[1:]):
                if interesting(s):
                    added.add(s)
        elif raw.startswith("-"):
            for s in declarations_in(raw[1:]):
                if interesting(s):
                    removed.add(s)
    return added, removed


def git_grep(clone: str, symbol: str, ref, changed_paths: set, limit: int = 10):
    """Word-boundary grep for a symbol, excluding files the PR already changes."""
    cmd = ["git", "-C", clone, "grep", "-n", "-w", "-I", "-e", symbol]
    if ref:
        cmd.append(ref)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError("`git` is not installed or not on PATH")
    if proc.returncode not in (0, 1):  # 1 == no matches, which is fine
        return [], 0

    sites, total = [], 0
    for line in proc.stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split(":", 3)
        # With a ref, git grep prints "ref:path:line:text"; without, "path:line:text".
        if ref and len(parts) >= 4:
            path, lineno, text = parts[1], parts[2], parts[3]
        elif len(parts) >= 3:
            path, lineno, text = parts[0], parts[1], parts[2]
        else:
            continue
        if path in changed_paths:
            continue  # already visible in the diff
        total += 1
        if len(sites) < limit:
            sites.append({"path": path, "line": int(lineno) if lineno.isdigit() else None,
                          "text": text.strip()[:200]})
    return sites, total


def main() -> int:
    ap = argparse.ArgumentParser(description="Find call sites outside the PR for symbols it changed.")
    ap.add_argument("--bundle", required=True, help="bundle.json from pr_fetch.py")
    ap.add_argument("--clone", required=True, help="path to a local clone of the repo")
    ap.add_argument("--ref", help="git ref to search (e.g. refs/remotes/pr/842); default: working tree")
    ap.add_argument("--max-symbols", type=int, default=40)
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()

    with open(args.bundle, encoding="utf-8") as fh:
        bundle = json.load(fh)

    changed_paths = {f["path"] for f in bundle.get("files") or []}
    declared_in: dict = {}
    added_all, removed_all = set(), set()

    for f in bundle.get("files") or []:
        added, removed = symbols_from_patch(f.get("patch"))
        for s in added | removed:
            declared_in.setdefault(s, set()).add(f["path"])
        added_all |= added
        removed_all |= removed

    # A symbol declared only in removed lines is gone or renamed; one declared in both
    # had its signature touched. Symbols only in added lines are new -- nothing can be
    # calling them yet, so they carry no blast radius.
    candidates = []
    for s in sorted(removed_all):
        kind = "changed" if s in added_all else "removed"
        candidates.append((s, kind))
    candidates = candidates[: args.max_symbols]

    result = {
        "repo": bundle.get("repo"),
        "number": bundle.get("number"),
        "clone": args.clone,
        "ref": args.ref,
        "symbols": [],
        "note": (
            "Heuristic word-grep, not a compiler. These are call sites to check, not "
            "findings. A hit may be an unrelated symbol with the same name."
        ),
    }

    def emit(payload: dict) -> None:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text)
        else:
            print(text)

    if not candidates:
        result["note"] = "No changed or removed declarations detected in this PR's diff."
        emit(result)
        if args.out:
            print(f"No changed/removed declarations found -> {args.out}")
        return 0

    for symbol, kind in candidates:
        try:
            sites, total = git_grep(args.clone, symbol, args.ref, changed_paths)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if total == 0:
            continue
        result["symbols"].append({
            "name": symbol,
            "kind": kind,  # "removed" (gone/renamed) or "changed" (signature touched)
            "declared_in": sorted(declared_in.get(symbol, [])),
            "external_call_sites": total,
            "samples": sites,
        })

    result["symbols"].sort(key=lambda s: s["external_call_sites"], reverse=True)
    emit(result)
    if args.out:
        total_sites = sum(s["external_call_sites"] for s in result["symbols"])
        print(
            f"{len(result['symbols'])} changed/removed symbols have {total_sites} "
            f"call sites outside this PR -> {args.out}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
