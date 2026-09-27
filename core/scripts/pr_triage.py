#!/usr/bin/env python3
"""Decide how much attention each file in a PR gets.

A 200-file PR can't be reviewed with the same depth everywhere, and pretending
otherwise just means the files that matter get the same skim as the snapshots. This
script ranks every file by risk and assigns a depth:

- deep     read in full and review properly
- skim     look for anything glaring; only on large PRs, and only for low-risk files
           (tests, docs, assets) -- ordinary code is never skimmed
- recheck  unchanged since the skill last reviewed this PR: only confirm earlier
           findings still hold, don't re-review from scratch

For large PRs it also packs the deep files into partitions of related files, so a
host that can run subagents can review them in parallel.

It is a heuristic. The scores decide the order of attention, never whether something
is a bug.

Usage:
    python pr_triage.py --bundle bundle.json [--out triage.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_config  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Path words, matched against whole path segments or file-name words.
HIGH_RISK_WORDS = {
    "auth", "authn", "authz", "login", "logout", "session", "sessions", "token", "tokens",
    "oauth", "jwt", "sso", "saml", "permission", "permissions", "rbac", "acl", "security",
    "crypto", "secret", "secrets", "password", "passwords", "payment", "payments",
    "billing", "invoice", "invoices", "charge", "wallet", "migration", "migrations",
    "schema", "infra", "deploy", "terraform", "helm", "k8s", "kubernetes",
}
MEDIUM_RISK_WORDS = {
    "api", "controller", "controllers", "route", "routes", "router", "handler", "handlers",
    "middleware", "resolver", "resolvers", "model", "models", "repository", "repositories",
    "dao", "queue", "consumer", "consumers", "producer", "worker", "workers", "job", "jobs",
    "cron", "cache", "redis", "db", "database", "config", "settings", "env", "core",
    "common", "shared", "lib", "utils", "service", "services", "gateway",
}
HIGH_RISK_EXTS = (".sql", ".tf")
LOW_RISK_PATTERNS = [
    re.compile(p) for p in (
        r"(^|/)(test|tests|__tests__|spec|specs|testdata|fixtures?|__snapshots__|e2e)/",
        r"[._-](test|spec)\.[a-z]+$", r"_test\.go$", r"^test_[^/]+\.py$", r"/test_[^/]+\.py$",
        r"\.snap$", r"(^|/)docs?/", r"\.(md|rst|txt|adoc)$", r"(^|/)CHANGELOG",
        r"\.(svg|png|jpe?g|gif|ico|webp)$", r"(^|/)(i18n|locales?|translations)/",
    )
]
# Signals on changed lines. Each distinct one adds a point, capped.
CONTENT_SIGNALS = [
    ("concurrency", re.compile(r"\b(mutex|lock|unlock|synchronized|atomic|semaphore|goroutine|go func|Promise\.all|asyncio\.gather|thread|Thread)\b")),
    ("transactions", re.compile(r"\b(transaction|rollback|commit\(|@Transactional|BEGIN|SAVEPOINT)\b")),
    ("execution", re.compile(r"\b(eval|exec|subprocess|child_process|spawn|os\.system|shell=True|Runtime\.getRuntime)\b")),
    ("raw sql", re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE)\b.+\b(FROM|INTO|SET|WHERE)\b")),
    ("credentials", re.compile(r"(?i)\b(password|secret|api[_-]?key|access[_-]?key|private[_-]?key|token)\b")),
    ("destructive", re.compile(r"(?i)\b(drop table|truncate|rm -rf|delete from|drop column)\b")),
    ("resilience", re.compile(r"(?i)\b(retry|retries|timeout|backoff|circuit)\b")),
]


def changed_lines(patch: str) -> list:
    return [raw[1:] for raw in (patch or "").split("\n")
            if raw[:1] in "+-" and not raw.startswith(("+++", "---"))]


def path_words(path: str) -> set:
    words = set()
    for part in path.lower().split("/"):
        words.add(part)
        words.update(w for w in re.split(r"[^a-z0-9]+", part) if w)
    return words


def score_file(f: dict, high_risk_globs) -> dict:
    path = f["path"]
    low = path.lower()
    score, reasons = 0, []
    words = path_words(path)

    if pr_config.matches_any(path, high_risk_globs):
        score += 3
        reasons.append("high_risk_paths in config")
    hits = sorted(words & HIGH_RISK_WORDS)
    if hits or low.endswith(HIGH_RISK_EXTS) or "dockerfile" in low or low.startswith(".github/workflows/"):
        score += 3
        reasons.append("sensitive area: " + (", ".join(hits) if hits else low.rsplit("/", 1)[-1]))
    else:
        mid = sorted(words & MEDIUM_RISK_WORDS)
        if mid:
            score += 2
            reasons.append("central code: " + ", ".join(mid[:3]))

    lines = changed_lines(f.get("patch"))
    text = "\n".join(lines)
    signals = [name for name, rx in CONTENT_SIGNALS if rx.search(text)]
    if signals:
        score += min(len(signals), 3)
        reasons.append("touches " + ", ".join(signals))

    size = (f.get("additions") or 0) + (f.get("deletions") or 0)
    if size > 400:
        score += 2
        reasons.append(f"large change ({size} lines)")
    elif size > 100:
        score += 1
        reasons.append(f"sizeable change ({size} lines)")

    if f.get("status") == "removed" and not any(p.search(path) for p in LOW_RISK_PATTERNS):
        score += 1
        reasons.append("file deleted")

    if any(p.search(path) for p in LOW_RISK_PATTERNS):
        score -= 3
        reasons.append("tests/docs/assets")

    risk = "high" if score >= 3 else "medium" if score >= 1 else "normal" if score == 0 else "low"
    return {"path": path, "risk": risk, "score": score, "reasons": reasons, "lines": size}


def module_of(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if len(parts) > 1 else ".")


def partition(entries: list, max_lines: int, max_files: int = 12) -> list:
    """Pack files into groups of related modules, each small enough for one reviewer."""
    by_module: dict = {}
    for e in entries:
        by_module.setdefault(module_of(e["path"]), []).append(e)
    groups, cur, cur_lines = [], [], 0
    # Riskiest modules first, so if anything is cut short it's the least important part.
    ordered = sorted(by_module.items(), key=lambda kv: (-max(e["score"] for e in kv[1]), kv[0]))
    for _, files in ordered:
        for e in sorted(files, key=lambda e: -e["score"]):
            if cur and (cur_lines + e["lines"] > max_lines or len(cur) >= max_files):
                groups.append(cur)
                cur, cur_lines = [], 0
            cur.append(e)
            cur_lines += e["lines"]
    if cur:
        groups.append(cur)
    return [{
        "id": i + 1,
        "files": [e["path"] for e in g],
        "lines": sum(e["lines"] for e in g),
        "modules": sorted({module_of(e["path"]) for e in g}),
        "highest_risk": next((r for r in ("high", "medium", "normal", "low")
                              if any(e["risk"] == r for e in g)), "low"),
    } for i, g in enumerate(groups)]


def triage(bundle: dict) -> dict:
    settings = ((bundle.get("config") or {}).get("settings")) or {}
    max_files = settings.get("large_pr_files", 25)
    max_lines = settings.get("large_pr_lines", 1500)
    part_lines = settings.get("partition_max_lines", 800)
    files = bundle.get("files") or []
    incremental = bundle.get("incremental") or {}
    is_incremental = incremental.get("status") == "incremental"

    entries = [score_file(f, settings.get("high_risk_paths") or []) for f in files]
    by_path = {f["path"]: f for f in files}
    active = [e for e in entries
              if not is_incremental or by_path[e["path"]].get("changed_since_last_review", True)]
    total_lines = sum(e["lines"] for e in active)
    large = len(active) > max_files or total_lines > max_lines

    for e in entries:
        f = by_path[e["path"]]
        if is_incremental and not f.get("changed_since_last_review", True):
            e["depth"] = "recheck"
        elif large and e["risk"] == "low":
            e["depth"] = "skim"
        else:
            e["depth"] = "deep"

    entries.sort(key=lambda e: (-e["score"], e["path"]))
    deep = [e for e in entries if e["depth"] == "deep"]
    counts = {d: sum(1 for e in entries if e["depth"] == d) for d in ("deep", "skim", "recheck")}
    return {
        "large": large,
        "thresholds": {"files": max_files, "lines": max_lines, "partition_max_lines": part_lines},
        "totals": {"files": len(entries), "lines": sum(e["lines"] for e in entries),
                   "active_files": len(active), "active_lines": total_lines},
        "incremental_status": incremental.get("status"),
        "coverage": {
            **counts,
            "skipped": len(bundle.get("skipped_files") or []),
            "files_truncated": bool(bundle.get("files_truncated")),
            "patches_truncated": sum(1 for f in files if f.get("patch_truncated")),
        },
        "files": entries,
        "partitions": partition(deep, part_lines) if large else [],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Rank a PR's files by risk and plan review depth.")
    ap.add_argument("--bundle", required=True, help="bundle.json from pr_fetch.py")
    ap.add_argument("--out", help="write JSON here instead of stdout")
    args = ap.parse_args()
    with open(args.bundle, encoding="utf-8") as fh:
        result = triage(json.load(fh))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        c = result["coverage"]
        print(f"{'large' if result['large'] else 'normal'} PR: {c['deep']} deep, {c['skim']} skim, "
              f"{c['recheck']} recheck, {c['skipped']} skipped, "
              f"{len(result['partitions'])} partitions -> {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
