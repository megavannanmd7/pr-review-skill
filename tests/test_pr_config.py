"""pr_config.py: config parsing and precedence, guidance discovery, learned suppressions.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import os
import tempfile
import unittest

import helpers  # noqa: F401  (puts core/scripts on sys.path)
import pr_config


class ParseTest(unittest.TestCase):
    def test_subset(self):
        cfg = pr_config.parse_simple_yaml("""
# team rules
max_findings: 5
description: false
min_severity: "blocker"   # only the worst
focus:
  - memory allocations in hot paths
  - "unbounded caches # not a comment"
ignore_paths: [docs/**, "*.snap"]
guidelines: |
  First line.
    Indented line.
  Last line.
""")
        self.assertEqual(cfg["max_findings"], 5)
        self.assertIs(cfg["description"], False)
        self.assertEqual(cfg["min_severity"], "blocker")
        self.assertEqual(cfg["focus"], ["memory allocations in hot paths", "unbounded caches # not a comment"])
        self.assertEqual(cfg["ignore_paths"], ["docs/**", "*.snap"])
        self.assertEqual(cfg["guidelines"], "First line.\n  Indented line.\nLast line.")

    def test_rejects_what_it_cannot_read(self):
        for bad in ("  indented: 1", "no colon here", "k: [a, b"):
            with self.assertRaises(pr_config.ConfigError):
                pr_config.parse_simple_yaml(bad)

    def test_validate_drops_unknown_and_invalid(self):
        clean, warnings = pr_config.validate(
            {"max_findings": "ten", "colour": "red", "min_severity": "critical", "focus": "one thing"}, "repo")
        self.assertEqual(clean, {"focus": ["one thing"]})
        self.assertEqual(len(warnings), 3)

    def test_repo_cannot_enable_local_execution(self):
        clean, warnings = pr_config.validate({"local_checks": True}, "repo")
        self.assertEqual(clean, {})
        self.assertIn("can only be set", warnings[0])
        self.assertEqual(pr_config.validate({"local_checks": True}, "user")[0], {"local_checks": True})


class PrecedenceTest(unittest.TestCase):
    def test_repo_beats_user_and_lists_combine(self):
        eff = pr_config.effective_config(
            repo={"max_findings": 3, "focus": ["redis"]},
            user={"max_findings": 8, "focus": ["memory", "redis"], "description": False},
            repo_guidelines="Use the tenant prefix.",
        )
        s, src = eff["settings"], eff["sources"]
        self.assertEqual((s["max_findings"], src["max_findings"]), (3, "repo"))
        self.assertEqual((s["description"], src["description"]), (False, "user"))
        self.assertEqual(s["focus"], ["memory", "redis"])
        self.assertEqual(src["focus"], "user+repo")
        self.assertEqual(s["guidelines"], "Use the tenant prefix.")
        self.assertEqual(s["min_severity"], "warning")

    def test_repo_config_read_through_callback(self):
        files = {".github/pr-review.yml": "max_findings: 2\nbogus: 1\n", ".github/pr-review.md": "Be kind."}
        settings, guidelines, warnings, found = pr_config.load_repo_config(files.get)
        self.assertEqual(settings, {"max_findings": 2})
        self.assertEqual(guidelines, "Be kind.")
        self.assertEqual(found, [".github/pr-review.yml", ".github/pr-review.md"])
        self.assertEqual(len(warnings), 1)

    def test_broken_user_config_is_a_warning_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.yml")
            with open(path, "w") as fh:
                fh.write("  nope")
            settings, warnings = pr_config.load_user_config(path)
        self.assertEqual(settings, {})
        self.assertIn("ignored", warnings[0])


class GuidanceTest(unittest.TestCase):
    def test_root_and_nested_guidance(self):
        tree = ["CLAUDE.md", "CONTRIBUTING.md", "packages/api/AGENTS.md", "packages/web/CLAUDE.md",
                "packages/api/src/x.ts", "docs/adr/0001-redis.md", "README.md"]
        paths = pr_config.guidance_paths(tree, ["packages/api/src/x.ts"])
        self.assertEqual(paths, ["CLAUDE.md", "CONTRIBUTING.md", "packages/api/AGENTS.md"])
        self.assertEqual(pr_config.adr_paths(tree), ["docs/adr/0001-redis.md"])

    def test_budget(self):
        big = "x" * (pr_config.GUIDANCE_MAX_BYTES + 5)
        out = pr_config.collect_guidance(["A.md"], {"A.md": big}.get)
        self.assertTrue(out[0]["truncated"])
        self.assertEqual(len(out[0]["content"]), pr_config.GUIDANCE_MAX_BYTES)

    def test_matches_any(self):
        self.assertTrue(pr_config.matches_any("docs/a/b.md", ["docs/**"]))
        self.assertTrue(pr_config.matches_any("src/x.snap", ["*.snap"]))
        self.assertFalse(pr_config.matches_any("src/docs.ts", ["docs/**"]))


class LearnedTest(unittest.TestCase):
    def test_dismissal_phrases(self):
        self.assertEqual(pr_config.dismissal_reason("This is intentional, see ADR 4"), "intentional")
        self.assertEqual(pr_config.dismissal_reason("won't fix for now"), "won't fix")
        self.assertIsNone(pr_config.dismissal_reason("This was not intentional, fixing"))
        self.assertIsNone(pr_config.dismissal_reason("good catch, fixed"))

    def test_record_dedups_by_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "learned", "o__r.json")
            pr_config.record_learned("o/r", [{"fingerprint": "f1", "reason": "intentional"}], path)
            entries = pr_config.record_learned("o/r", [{"fingerprint": "f1"}, {"fingerprint": "f2"}], path)
            self.assertEqual([e["fingerprint"] for e in entries], ["f1", "f2"])
            self.assertEqual(len(pr_config.load_learned("o/r", path)), 2)


if __name__ == "__main__":
    unittest.main()
