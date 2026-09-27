#!/usr/bin/env python3
"""Configuration, repo guidance and learned suppressions for the pr-review skill.

Three sources shape a review, highest precedence first:

1. What the user says in this invocation (flags, focus areas) -- applied by the agent.
2. The repo's `.github/pr-review.yml` (plus free-form `.github/pr-review.md`), read
   from the PR's *base* commit so a PR can never rewrite the rules it is reviewed by.
3. The user's own `~/.pr-review-skill/config.yml`.

List settings (focus, suppress, ignore_paths, high_risk_paths) are combined across
sources rather than overridden. A few settings only count when they come from the
user's own config, because they decide what runs on the user's machine.

The config format is a deliberately small subset of YAML, so there is nothing to
install:

    # comment
    max_findings: 5
    description: true
    focus:
      - memory allocations in hot paths
      - unbounded caches
    ignore_paths: [docs/**, "*.snap"]
    guidelines: |
      Free text, indented under the key,
      kept as written.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import time

SEVERITIES = ["question", "suggestion", "warning", "blocker"]
DESCRIPTION_SECTIONS = ["overview", "changes", "contract", "tests", "risk", "reviewer_guide"]

# key -> (type, default)
SCHEMA = {
    "focus": ("list", []),
    "suppress": ("list", []),
    "guidelines": ("str", ""),
    "ignore_paths": ("list", []),
    "high_risk_paths": ("list", []),
    "max_findings": ("int", 10),
    "min_severity": ("severity", "warning"),
    "description": ("bool", True),
    "description_sections": ("sections", ["overview", "changes", "contract", "tests"]),
    "follow_up": ("bool", True),
    "incremental": ("bool", True),
    "learn_from_dismissals": ("bool", True),
    "large_pr_files": ("int", 25),
    "large_pr_lines": ("int", 1500),
    "partition_max_lines": ("int", 800),
    "checks": ("list", []),
    "checks_timeout": ("int", 600),
    "local_checks": ("bool", False),
}
LIST_MERGE_KEYS = {"focus", "suppress", "ignore_paths", "high_risk_paths"}
# Settings that decide what executes on the user's machine. A repo can suggest check
# commands, but only the user can turn local execution on.
USER_ONLY_KEYS = {"local_checks"}

REPO_CONFIG_PATHS = [".github/pr-review.yml", ".github/pr-review.yaml"]
REPO_GUIDELINES_PATH = ".github/pr-review.md"
USER_HOME = os.path.join(os.path.expanduser("~"), ".pr-review-skill")
USER_CONFIG_PATH = os.path.join(USER_HOME, "config.yml")
LEARNED_DIR = os.path.join(USER_HOME, "learned")

# Files many repos already keep for humans or other AI tools. Read at the base commit.
ROOT_GUIDANCE = [
    "CLAUDE.md", "AGENTS.md", "GEMINI.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md", ".cursorrules", ".github/copilot-instructions.md",
    ".github/pull_request_template.md", ".github/PULL_REQUEST_TEMPLATE.md",
    "PULL_REQUEST_TEMPLATE.md", "docs/pull_request_template.md",
]
# Also looked for in every directory above a changed file (monorepo packages).
NESTED_GUIDANCE_NAMES = {"CLAUDE.md", "AGENTS.md"}
ADR_DIRS = ("docs/adr/", "docs/adrs/", "adr/", "docs/decisions/", "doc/adr/")
GUIDANCE_MAX_FILES = 12
GUIDANCE_MAX_BYTES = 20000
GUIDANCE_TOTAL_BYTES = 80000


class ConfigError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Parsing


def _scalar(raw: str):
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s


def _strip_comment(line: str) -> str:
    """Drop a trailing `# comment`, but not a `#` inside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def parse_simple_yaml(text: str) -> dict:
    """Parse the small YAML subset documented at the top of this file.

    Raises ConfigError with a line number on anything outside that subset, rather
    than guessing -- a silently misread config is worse than a loud one.
    """
    out: dict = {}
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = _strip_comment(raw)
        if not line.strip():
            i += 1
            continue
        if raw[0].isspace():
            raise ConfigError(f"line {i + 1}: unexpected indentation")
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if not m:
            raise ConfigError(f"line {i + 1}: expected `key: value`")
        key, rest = m.group(1), m.group(2).strip()
        i += 1
        if rest in ("|", ">"):
            block = []
            while i < len(lines) and (not lines[i].strip() or lines[i][0].isspace()):
                block.append(lines[i])
                i += 1
            indent = min((len(b) - len(b.lstrip()) for b in block if b.strip()), default=0)
            joined = [b[indent:] for b in block]
            out[key] = ("\n" if rest == "|" else " ").join(joined).strip()
        elif rest.startswith("["):
            if not rest.endswith("]"):
                raise ConfigError(f"line {i}: inline list must close on the same line")
            inner = rest[1:-1].strip()
            out[key] = [_scalar(x) for x in re.findall(r'"[^"]*"|\'[^\']*\'|[^,]+', inner)] if inner else []
        elif rest:
            out[key] = _scalar(rest)
        else:
            items = []
            while i < len(lines):
                item_line = _strip_comment(lines[i])
                if not item_line.strip():
                    i += 1
                    continue
                im = re.match(r"^\s+-\s+(.*)$", item_line)
                if not im:
                    break
                items.append(_scalar(im.group(1)))
                i += 1
            out[key] = items
    return out


def validate(raw: dict, source: str):
    """(clean settings, warnings). Unknown keys and bad values are dropped with a warning."""
    clean, warnings = {}, []
    for key, value in raw.items():
        if key not in SCHEMA:
            warnings.append(f"{source}: unknown setting `{key}` ignored")
            continue
        kind, _ = SCHEMA[key]
        ok = True
        if kind == "list":
            if isinstance(value, str):
                value = [value]
            ok = isinstance(value, list) and all(isinstance(v, (str, int)) for v in value)
            value = [str(v) for v in value] if ok else value
        elif kind == "str":
            ok = isinstance(value, str)
        elif kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        elif kind == "bool":
            ok = isinstance(value, bool)
        elif kind == "severity":
            ok = value in SEVERITIES
        elif kind == "sections":
            if isinstance(value, str):
                value = [value]
            ok = isinstance(value, list) and all(v in DESCRIPTION_SECTIONS for v in value)
        if not ok:
            warnings.append(f"{source}: invalid value for `{key}`: {value!r} (ignored)")
            continue
        if key in USER_ONLY_KEYS and source != "user":
            warnings.append(f"{source}: `{key}` can only be set in your own {USER_CONFIG_PATH}; ignored")
            continue
        clean[key] = value
    return clean, warnings


def effective_config(repo: dict, user: dict, repo_guidelines: str = "") -> dict:
    """Merge defaults <- user <- repo. Returns {"settings", "sources"}."""
    settings = {k: (list(d) if isinstance(d, list) else d) for k, (_, d) in SCHEMA.items()}
    sources = {k: "default" for k in SCHEMA}
    for name, layer in (("user", user), ("repo", repo)):
        for key, value in layer.items():
            if key in LIST_MERGE_KEYS:
                merged = settings[key] + [v for v in value if v not in settings[key]]
                settings[key] = merged
                sources[key] = sources[key] + "+" + name if sources[key] != "default" else name
            else:
                settings[key] = value
                sources[key] = name
    if repo_guidelines.strip():
        parts = [settings["guidelines"], repo_guidelines.strip()]
        settings["guidelines"] = "\n\n".join(p for p in parts if p)
        sources["guidelines"] = "repo" if sources["guidelines"] == "default" else sources["guidelines"] + "+repo"
    return {"settings": settings, "sources": sources}


def load_user_config(path: str = USER_CONFIG_PATH):
    """(settings, warnings) from the user's own config file; empty if there is none."""
    if not os.path.isfile(path):
        return {}, []
    try:
        with open(path, encoding="utf-8") as fh:
            return validate(parse_simple_yaml(fh.read()), "user")
    except (OSError, ConfigError) as exc:
        return {}, [f"user config {path}: {exc} (ignored)"]


def load_repo_config(read_file):
    """(settings, guidelines text, warnings, paths found). `read_file(path)` -> text or None."""
    settings, warnings, found = {}, [], []
    for path in REPO_CONFIG_PATHS:
        text = read_file(path)
        if text is None:
            continue
        found.append(path)
        try:
            settings, w = validate(parse_simple_yaml(text), "repo")
            warnings += w
        except ConfigError as exc:
            warnings.append(f"repo config {path}: {exc} (ignored)")
        break
    guidelines = read_file(REPO_GUIDELINES_PATH) or ""
    if guidelines:
        found.append(REPO_GUIDELINES_PATH)
    return settings, guidelines, warnings, found


# ---------------------------------------------------------------------------
# Repo guidance


def guidance_paths(tree_paths, changed_paths) -> list:
    """Which existing guidance files apply to this PR: root ones, plus CLAUDE.md /
    AGENTS.md in any directory above a changed file, nearest-to-root first."""
    present = set(tree_paths)
    chosen = [p for p in ROOT_GUIDANCE if p in present]
    dirs = set()
    for path in changed_paths:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:depth]))
    for d in sorted(dirs, key=lambda x: (x.count("/"), x)):
        for name in sorted(NESTED_GUIDANCE_NAMES):
            candidate = f"{d}/{name}"
            if candidate in present and candidate not in chosen:
                chosen.append(candidate)
    return chosen


def adr_paths(tree_paths) -> list:
    return sorted(p for p in tree_paths if p.startswith(ADR_DIRS) and p.endswith(".md"))


def collect_guidance(paths, read_file) -> list:
    """[{path, content, truncated}] within the per-file and total size budgets."""
    out, total = [], 0
    for path in paths[:GUIDANCE_MAX_FILES]:
        text = read_file(path)
        if not text:
            continue
        truncated = len(text) > GUIDANCE_MAX_BYTES
        text = text[:GUIDANCE_MAX_BYTES]
        if total + len(text) > GUIDANCE_TOTAL_BYTES:
            out.append({"path": path, "content": None, "truncated": True,
                        "note": "over the total guidance budget; fetch it if relevant"})
            continue
        total += len(text)
        out.append({"path": path, "content": text, "truncated": truncated})
    return out


def matches_any(path: str, globs) -> bool:
    base = path.rsplit("/", 1)[-1]
    for g in globs or []:
        if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(base, g):
            return True
        if g.endswith("/**") and (path + "/").startswith(g[:-2]):
            return True
    return False


# ---------------------------------------------------------------------------
# Learned suppressions

DISMISS_RE = re.compile(
    r"\b(intentional(?:ly)?|by design|won'?t fix|wontfix|not a bug|false positive|"
    r"expected behaviou?r|as intended|working as intended|not applicable|ignore this)\b",
    re.IGNORECASE,
)
NEGATED_RE = re.compile(r"\bnot\s+(?:intentional|by design|expected|as intended)\b", re.IGNORECASE)


def dismissal_reason(reply: str):
    """The dismissive phrase in an author's reply, or None. Negations don't count."""
    text = reply or ""
    if NEGATED_RE.search(text):
        return None
    m = DISMISS_RE.search(text)
    return m.group(1) if m else None


def learned_path(repo: str) -> str:
    return os.path.join(LEARNED_DIR, repo.replace("/", "__") + ".json")


def load_learned(repo: str, path=None) -> list:
    path = path or learned_path(repo)
    try:
        with open(path, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("entries") or []
    except (OSError, ValueError):
        return []


def record_learned(repo: str, new_entries: list, path=None) -> list:
    """Add dismissals to the repo's learned file (deduplicated by fingerprint).
    Returns the full, updated list. Never raises: learning is best-effort."""
    path = path or learned_path(repo)
    entries = load_learned(repo, path)
    known = {e.get("fingerprint") for e in entries}
    added = False
    for e in new_entries:
        if e.get("fingerprint") and e["fingerprint"] not in known:
            entries.append({**e, "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            known.add(e["fingerprint"])
            added = True
    if added:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"version": 1, "repo": repo, "entries": entries}, fh, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return entries
