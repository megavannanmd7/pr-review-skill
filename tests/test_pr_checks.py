"""pr_checks.py: the execution guards, and mapping output to diff lines.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import helpers  # noqa: F401
import pr_checks


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = self.tmp.name
        run = lambda *a: subprocess.run(["git", "-C", self.wt, *a], check=True, capture_output=True, text=True)
        run("init", "-q")
        run("config", "user.email", "t@example.com")
        run("config", "user.name", "t")
        os.makedirs(os.path.join(self.wt, "src"))
        with open(os.path.join(self.wt, "src", "app.py"), "w") as fh:
            fh.write("x = 1\n")
        run("add", "-A")
        run("commit", "-q", "-m", "c")
        self.head = run("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.tmp.cleanup()

    def bundle(self, commands, **extra):
        b = {"head_sha": self.head, "is_fork_pr": False,
             "config": {"settings": {"checks": commands, "checks_timeout": 20}},
             "files": [{"path": "src/app.py", "commentable": {"RIGHT": [1, 2], "LEFT": []}}]}
        b.update(extra)
        return b

    def test_guards(self):
        py = sys.executable
        self.assertEqual(pr_checks.run(self.bundle([f"{py} -c 1"]), self.wt, False)["status"], "refused_not_enabled")
        self.assertEqual(pr_checks.run(self.bundle(["x"], is_fork_pr=True), self.wt, True)["status"], "refused_fork")
        self.assertEqual(pr_checks.run(self.bundle(["x"], is_fork_pr=None), self.wt, True)["status"], "refused_fork")
        self.assertEqual(pr_checks.run(self.bundle([]), self.wt, True)["status"], "no_checks_configured")
        wrong = self.bundle(["x"], head_sha="0" * 40)
        self.assertEqual(pr_checks.run(wrong, self.wt, True)["status"], "refused_wrong_checkout")

    def test_runs_and_maps_diagnostics(self):
        cmd = f'"{sys.executable}" -c "print(\'src/app.py:1:5: error: bad\'); print(\'other.py:3: e\'); raise SystemExit(2)"'
        result = pr_checks.run(self.bundle([cmd]), self.wt, True)
        self.assertEqual(result["status"], "failures")
        self.assertEqual(result["runs"][0]["exit_code"], 2)
        [d] = result["diagnostics"]
        self.assertEqual((d["path"], d["line"], d["in_diff"]), ("src/app.py", 1, True))

    def test_tokens_are_not_passed_to_commands(self):
        cmd = f'"{sys.executable}" -c "import os; print(sorted(k for k in os.environ if \'TOKEN\' in k))"'
        with mock.patch.dict(os.environ, {"GH_TOKEN": "s3cret", "MY_API_KEY": "k"}):
            out = pr_checks.run(self.bundle([cmd]), self.wt, True)["runs"][0]["output_tail"]
        self.assertIn("[]", out)
        self.assertNotIn("s3cret", out)

    def test_timeout(self):
        cmd = f'"{sys.executable}" -c "import time; time.sleep(5)"'
        result = pr_checks.run(self.bundle([cmd]), self.wt, True, timeout=1)
        self.assertTrue(result["runs"][0]["timed_out"])
        self.assertEqual(result["status"], "failures")


if __name__ == "__main__":
    unittest.main()
