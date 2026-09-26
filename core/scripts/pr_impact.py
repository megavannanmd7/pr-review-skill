#!/usr/bin/env python3
"""Blast-radius analysis: who calls the things this PR changed?

The most dangerous class of PR bug is invisible in the diff. If a PR changes
`findUser` from returning `User | null` to throwing, the diff looks clean and
correct -- and fifteen callers in files the PR never touches are now broken.

This script finds those callers. It collects symbols the PR touched in two ways:

- declarations whose own line was changed or removed (a signature edit, a rename,
  a deletion), read straight from the diff;
- functions whose *body* changed while the declaration line stayed put. A body-only
  change is the common case -- `return null` becoming `throw` never touches the
  `function findUser(` line -- so each changed line is mapped to the function that
  encloses it in the file at the PR head.

It then greps a local clone for uses of those symbols outside the PR's own changed
files.

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
# what keeps these from matching ordinary call sites mid-expression. The flag marks
# containers: declarations that only group other declarations. A change inside a
# class is attributed to the method it sits in, never to the class itself --
# searching for a class name (imported or injected everywhere) would bury the
# callers that matter.
DECL_PATTERNS = [
    # JavaScript / TypeScript
    (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)", False),
    (r"^\s*export\s+(?:const|let|var)\s+(\w+)", False),
    (r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)", True),
    (r"^\s*export\s+(?:interface|type|enum)\s+(\w+)", False),
    # Python
    (r"^\s*(?:async\s+)?def\s+(\w+)", False),
    (r"^\s*class\s+(\w+)", True),
    # Go
    (r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)", False),
    (r"^\s*type\s+(\w+)\s+(?:struct|interface)", False),
    # Rust
    (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)", False),
    (r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait)\s+(\w+)", False),
    # Java / C#
    (r"^\s*(?:public|protected|internal)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\],.\s]+?\s+(\w+)\s*\(", False),
]
COMPILED = [(re.compile(p), container) for p, container in DECL_PATTERNS]

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

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# A changed line that is only whitespace or a comment cannot change behaviour.
COMMENT_PREFIXES = ("#", "//", "/*", "*")

# Words on a changed line that suggest the function's contract moved, not just its
# internals. Used to rank body changes, never to decide whether something is a bug.
CONTRACT_SIGNALS = [
    ("return", re.compile(r"\breturn\b")),
    ("throw", re.compile(r"\bthrow\b")),
    ("raise", re.compile(r"\braise\b")),
    ("panic", re.compile(r"\bpanic\(")),
    ("await", re.compile(r"\bawait\b")),
    ("async", re.compile(r"\basync\b")),
    ("yield", re.compile(r"\byield\b")),
    ("null", re.compile(r"\b(?:null|None|undefined|nil)\b")),
]

KIND_PRIORITY = {"removed": 0, "changed": 1, "body_changed": 2}


def match_declaration(line: str):
    """(name, is_container) if the line declares something, else None."""
    for rx, container in COMPILED:
        m = rx.match(line)
        if m:
            return m.group(1), container
    return None


def declarations_in(line: str) -> list:
    m = match_declaration(line)
    return [m[0]] if m else []


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


def indent_of(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def is_noise(text: str) -> bool:
    s = text.strip()
    return not s or s.startswith(COMMENT_PREFIXES)


def declaration_ranges(lines: list) -> list:
    """Every declaration in a file with the lines its body spans (1-indexed, inclusive).

    The body ends at the first non-blank line indented at or below the declaration
    itself. That one rule covers Python and formatted brace languages without brace
    counting, with three exceptions at the declaration's own indent: a line starting
    with `)` or `]` continues a multi-line signature, a lone `{` opens an Allman-style
    body, and a line starting with `}` (or a bare `end`) closes the body and belongs
    to it.
    """
    ranges = []
    for i, line in enumerate(lines):
        m = match_declaration(line)
        if not m:
            continue
        name, container = m
        own = indent_of(line)
        end = i + 1
        for j in range(i + 1, len(lines)):
            s = lines[j].strip()
            if not s:
                continue
            if indent_of(lines[j]) > own or s[0] in ")]" or s == "{":
                end = j + 1
                continue
            if s[0] == "}" or re.fullmatch(r"end[\s;)]*", s):
                end = j + 1
            break
        ranges.append({"name": name, "container": container, "indent": own,
                       "start": i + 1, "end": end})
    return ranges


def changed_positions(patch: str):
    """Yield (sign, head_line, text) for each changed line that could alter behaviour.

    For an added line, head_line is its line number at the PR head. A deleted line has
    no head line of its own, so head_line is the head line it followed (0 when the
    deletion is at the very top of the file).
    """
    new = 0
    in_hunk = False
    for raw in (patch or "").split("\n"):
        m = HUNK_RE.match(raw)
        if m:
            new = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk or raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            if not is_noise(raw[1:]):
                yield "+", new, raw[1:]
            new += 1
        elif raw.startswith("-"):
            if not is_noise(raw[1:]):
                yield "-", new - 1, raw[1:]
        else:
            new += 1


def enclosing(ranges: list, sign: str, line_no: int, text: str) -> list:
    """Declarations containing a changed line, outermost first."""
    if sign == "+":
        hits = [d for d in ranges if d["start"] < line_no <= d["end"]]
    else:
        # A deletion sits after line_no. It was inside a declaration if the line it
        # followed is, and it was indented as body content -- otherwise deleting code
        # just after a function's last line would be pinned on that function.
        hits = [d for d in ranges
                if d["start"] <= line_no <= d["end"] and indent_of(text) > d["indent"]]
    return sorted(hits, key=lambda d: d["start"])


def run_git(clone: str, *args: str):
    try:
        return subprocess.run(["git", "-C", clone, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise RuntimeError("`git` is not installed or not on PATH")


def resolve_head(clone: str, ref, head_sha):
    """(commit to read changed files at, None) or (None, reason body analysis is skipped).

    Line numbers in the diff are line numbers at the PR head, so the files must be read
    at exactly that commit -- never from whatever the user has checked out.
    """
    target = ref or head_sha
    if not target:
        return None, "no --ref given and the bundle has no head_sha"
    proc = run_git(clone, "rev-parse", "--verify", "--quiet", f"{target}^{{commit}}")
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        return None, (f"{target} is not in the clone; fetch the PR head first "
                      "(git fetch origin pull/<number>/head:refs/remotes/pr/<number>)")
    if head_sha and sha != head_sha:
        return None, (f"{target} is at {sha[:12]} but the PR head is {head_sha[:12]}; "
                      "re-fetch the PR ref")
    return sha, None


def body_changes(bundle: dict, clone: str, commit: str, known: set):
    """Functions whose body the PR changed without touching their declaration line.

    Returns ({name: info}, [paths that could not be read at the PR head]).
    """
    found: dict = {}
    unreadable = []
    for f in bundle.get("files") or []:
        if f.get("status") == "removed" or not f.get("patch"):
            continue
        positions = list(changed_positions(f["patch"]))
        if not positions:
            continue
        proc = run_git(clone, "show", f"{commit}:{f['path']}")
        if proc.returncode != 0:
            unreadable.append(f["path"])
            continue
        ranges = declaration_ranges(proc.stdout.split("\n"))
        for sign, line_no, text in positions:
            chain = enclosing(ranges, sign, line_no, text)
            for depth, d in enumerate(chain):
                name = d["name"]
                if d["container"] or name in known or not interesting(name):
                    continue
                info = found.setdefault(name, {
                    "declared_in": set(), "enclosing": set(), "changed_lines": {},
                    "contract_signals": set(),
                })
                info["declared_in"].add(f["path"])
                info["enclosing"].add(".".join(x["name"] for x in chain[:depth + 1]))
                # A deletion is reported at the head line that took its place, kept
                # inside the function when it was the function's last line.
                at = line_no if sign == "+" else min(line_no + 1, d["end"])
                info["changed_lines"].setdefault(f["path"], set()).add(at)
                for label, rx in CONTRACT_SIGNALS:
                    if rx.search(text):
                        info["contract_signals"].add(label)
    return found, unreadable


def git_grep(clone: str, symbol: str, ref, changed_paths: set, limit: int = 10):
    """Word-boundary grep for a symbol, excluding files the PR already changes."""
    cmd = ["grep", "-n", "-w", "-I", "-e", symbol]
    if ref:
        cmd.append(ref)
    proc = run_git(clone, *cmd)
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
        candidates.append({"name": s, "kind": kind, "declared_in": sorted(declared_in[s])})

    try:
        commit, skipped = resolve_head(args.clone, args.ref, bundle.get("head_sha"))
        unreadable = []
        if commit:
            bodies, unreadable = body_changes(bundle, args.clone, commit, added_all | removed_all)
            for s, info in bodies.items():
                candidates.append({
                    "name": s,
                    "kind": "body_changed",
                    "declared_in": sorted(info["declared_in"]),
                    "enclosing": sorted(info["enclosing"]),
                    "changed_lines": {p: sorted(ls) for p, ls in sorted(info["changed_lines"].items())},
                    "contract_signals": sorted(info["contract_signals"]),
                })
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # Body changes can easily outnumber the cap on a large PR, so rank before cutting:
    # removed and changed declarations always go first, then body changes that touch a
    # return/throw/await-style line, then the ones with the most changed lines.
    def rank(c: dict):
        lines = sum(len(ls) for ls in (c.get("changed_lines") or {}).values())
        return (KIND_PRIORITY[c["kind"]], 0 if c.get("contract_signals") else 1, -lines, c["name"])

    candidates.sort(key=rank)
    over_cap = max(0, len(candidates) - args.max_symbols)
    candidates = candidates[: args.max_symbols]

    result = {
        "repo": bundle.get("repo"),
        "number": bundle.get("number"),
        "clone": args.clone,
        "ref": args.ref,
        "symbols": [],
        "candidates_over_cap": over_cap,
        "body_analysis_skipped": skipped,
        "body_analysis_unreadable_files": unreadable,
        "note": (
            "Heuristic word-grep, not a compiler. These are call sites to check, not "
            "findings. A hit may be an unrelated symbol with the same name. For "
            "body_changed symbols, first decide from the diff whether the function's "
            "contract changed at all; only then check its callers."
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
        result["note"] = "No changed or removed declarations, and no changed function bodies, detected in this PR's diff."
        emit(result)
        if args.out:
            print(f"No changed/removed declarations or function bodies found -> {args.out}")
        return 0

    for c in candidates:
        try:
            sites, total = git_grep(args.clone, c["name"], args.ref, changed_paths)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if total == 0:
            continue
        # kind: "removed" (gone/renamed), "changed" (declaration line touched) or
        # "body_changed" (only the body changed; see enclosing/changed_lines).
        result["symbols"].append({**c, "external_call_sites": total, "samples": sites})

    # Declaration changes first -- a broken signature is certain, a changed body only
    # possibly matters -- then by how many places could be affected.
    result["symbols"].sort(key=lambda s: (s["kind"] == "body_changed", -s["external_call_sites"]))
    emit(result)
    if args.out:
        total_sites = sum(s["external_call_sites"] for s in result["symbols"])
        print(
            f"{len(result['symbols'])} changed/removed symbols have {total_sites} "
            f"call sites outside this PR -> {args.out}"
        )
        if skipped:
            print(f"Body-change analysis skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
