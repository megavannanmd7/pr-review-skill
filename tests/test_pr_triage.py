"""pr_triage.py: risk ranking, depth assignment and partitions.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import unittest

import helpers  # noqa: F401
import pr_triage


def f(path, lines=10, patch="+x = 1", **extra):
    d = {"path": path, "additions": lines, "deletions": 0, "patch": patch, "status": "modified"}
    d.update(extra)
    return d


class TriageTest(unittest.TestCase):
    def test_risk_levels(self):
        auth = pr_triage.score_file(f("src/auth/session.ts"), [])
        svc = pr_triage.score_file(f("src/orders/order.service.ts"), [])
        test = pr_triage.score_file(f("src/orders/order.service.spec.ts"), [])
        docs = pr_triage.score_file(f("docs/setup.md"), [])
        self.assertEqual(auth["risk"], "high")
        self.assertEqual(svc["risk"], "medium")
        self.assertEqual((test["risk"], docs["risk"]), ("low", "low"))

    def test_content_signals_and_config_globs(self):
        e = pr_triage.score_file(f("src/x.py", patch="+with lock:\n+    db.transaction()"), [])
        self.assertIn("touches concurrency, transactions", e["reasons"])
        e = pr_triage.score_file(f("src/plain.py"), ["src/plain.py"])
        self.assertEqual(e["risk"], "high")
        self.assertIn("high_risk_paths in config", e["reasons"])

    def test_small_pr_is_all_deep(self):
        t = pr_triage.triage({"files": [f("docs/a.md"), f("src/auth/x.ts")]})
        self.assertFalse(t["large"])
        self.assertEqual({e["depth"] for e in t["files"]}, {"deep"})
        self.assertEqual(t["partitions"], [])

    def test_large_pr_skims_low_risk_and_partitions_the_rest(self):
        files = [f(f"src/mod{i % 4}/file{i}.ts", lines=100) for i in range(30)]
        files += [f(f"src/mod0/file{i}.spec.ts", lines=100) for i in range(5)]
        t = pr_triage.triage({"files": files, "config": {"settings": {"partition_max_lines": 500}}})
        self.assertTrue(t["large"])
        self.assertEqual(t["coverage"]["skim"], 5)
        partitioned = [p for part in t["partitions"] for p in part["files"]]
        self.assertEqual(len(partitioned), 30)
        self.assertTrue(all(part["lines"] <= 500 for part in t["partitions"]))
        self.assertEqual(t["files"][0]["score"], max(e["score"] for e in t["files"]))

    def test_incremental_rechecks_unchanged_files(self):
        files = [f("src/a.ts", changed_since_last_review=True),
                 f("src/b.ts", changed_since_last_review=False)]
        t = pr_triage.triage({"files": files, "incremental": {"status": "incremental"}})
        depth = {e["path"]: e["depth"] for e in t["files"]}
        self.assertEqual(depth, {"src/a.ts": "deep", "src/b.ts": "recheck"})
        self.assertEqual(t["totals"]["active_files"], 1)


if __name__ == "__main__":
    unittest.main()
