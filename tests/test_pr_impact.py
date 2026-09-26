"""End-to-end tests for core/scripts/pr_impact.py.

Each test builds a throwaway git repo with a base and a head commit, turns
`git diff base head` into a bundle shaped like pr_fetch.py's output, runs the
script, and checks what it reports.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "core", "scripts", "pr_impact.py")


class Repo:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", self.root, *args], check=True,
                              capture_output=True, text=True).stdout

    def commit(self, files: dict) -> str:
        for path, body in files.items():
            full = os.path.join(self.root, path)
            if body is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(body).lstrip("\n"))
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c", "--allow-empty")
        return self.git("rev-parse", "HEAD").strip()

    def bundle(self, base: str, head: str, head_sha=None) -> dict:
        files = []
        for path in self.git("diff", "--name-only", base, head).split():
            diff = self.git("diff", base, head, "--", path)
            # GitHub's files[].patch starts at the first hunk, without file headers.
            patch = diff[diff.index("@@"):] if "@@" in diff else ""
            gone = not os.path.exists(os.path.join(self.root, path))
            files.append({"path": path, "status": "removed" if gone else "modified",
                          "patch": patch.rstrip("\n")})
        return {"repo": "o/r", "number": 1, "head_sha": head_sha or head, "files": files}


class PrImpactTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(os.path.join(self.tmp.name, "repo"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_impact(self, base: str, head: str, *extra: str, head_sha=None) -> dict:
        bundle_path = os.path.join(self.tmp.name, "bundle.json")
        out_path = os.path.join(self.tmp.name, "impact.json")
        with open(bundle_path, "w", encoding="utf-8") as fh:
            json.dump(self.repo.bundle(base, head, head_sha), fh)
        subprocess.run([sys.executable, SCRIPT, "--bundle", bundle_path,
                        "--clone", self.repo.root, "--out", out_path, *extra],
                       check=True, capture_output=True, text=True)
        with open(out_path, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def by_name(result: dict) -> dict:
        return {s["name"]: s for s in result["symbols"]}

    def test_python_method_body_change_is_reported_with_signals(self):
        base = self.repo.commit({
            "svc.py": """
                class UserService:
                    def find_user_by_id(self, uid):
                        row = lookup(uid)
                        if not row:
                            return None
                        return row
            """,
            "callers.py": "u = svc.find_user_by_id(1)\nif not u: pass\n",
        })
        head = self.repo.commit({"svc.py": """
            class UserService:
                def find_user_by_id(self, uid):
                    row = lookup(uid)
                    if not row:
                        raise KeyError(uid)
                    return row
        """})
        sym = self.by_name(self.run_impact(base, head))["find_user_by_id"]
        self.assertEqual(sym["kind"], "body_changed")
        self.assertEqual(sym["enclosing"], ["UserService.find_user_by_id"])
        self.assertIn("raise", sym["contract_signals"])
        self.assertIn("return", sym["contract_signals"])
        self.assertEqual(sym["changed_lines"], {"svc.py": [5]})
        self.assertEqual(sym["external_call_sites"], 1)

    def test_comment_only_edit_reports_nothing(self):
        base = self.repo.commit({
            "svc.py": "def compute_total(xs):\n    # sum them\n    return sum(xs)\n",
            "callers.py": "compute_total([1])\n",
        })
        head = self.repo.commit({"svc.py": "def compute_total(xs):\n    # add them up\n    return sum(xs)\n"})
        self.assertEqual(self.run_impact(base, head)["symbols"], [])

    def test_module_level_change_between_functions_reports_nothing(self):
        src = """
            def alpha_worker(x):
                return x

            TIMEOUT = {t}

            def beta_worker(x):
                return x
        """
        base = self.repo.commit({"svc.py": src.format(t=5),
                                 "callers.py": "alpha_worker(1)\nbeta_worker(2)\n"})
        head = self.repo.commit({"svc.py": src.format(t=10)})
        self.assertEqual(self.run_impact(base, head)["symbols"], [])

    def test_deleting_last_statement_is_pinned_on_that_function_only(self):
        base = self.repo.commit({
            "svc.py": """
                def alpha_worker(x):
                    y = x + 1
                    return y

                def beta_worker(x):
                    return x
            """,
            "callers.py": "alpha_worker(1)\nbeta_worker(2)\n",
        })
        head = self.repo.commit({"svc.py": """
            def alpha_worker(x):
                y = x + 1

            def beta_worker(x):
                return x
        """})
        names = self.by_name(self.run_impact(base, head))
        self.assertIn("alpha_worker", names)
        self.assertNotIn("beta_worker", names)

    def test_go_func_body_change(self):
        base = self.repo.commit({
            "svc.go": "package svc\n\nfunc LoadAccount(id int) (*Account, error) {\n\treturn nil, nil\n}\n",
            "main.go": "package main\n\nfunc main() { svc.LoadAccount(1) }\n",
        })
        head = self.repo.commit({
            "svc.go": "package svc\n\nfunc LoadAccount(id int) (*Account, error) {\n\treturn nil, ErrNotFound\n}\n",
        })
        sym = self.by_name(self.run_impact(base, head))["LoadAccount"]
        self.assertEqual(sym["kind"], "body_changed")

    def test_ts_multiline_signature_return_type_change(self):
        base = self.repo.commit({
            "users.ts": """
                export async function fetchProfile(
                  id: string,
                ): Promise<Profile> {
                  return db.get(id);
                }
            """,
            "page.ts": "const p = await fetchProfile(id);\n",
        })
        head = self.repo.commit({"users.ts": """
            export async function fetchProfile(
              id: string,
            ): Promise<Profile | null> {
              return db.get(id);
            }
        """})
        sym = self.by_name(self.run_impact(base, head))["fetchProfile"]
        self.assertEqual(sym["kind"], "body_changed")
        self.assertIn("null", sym["contract_signals"])

    def test_nested_closure_reports_inner_and_outer(self):
        base = self.repo.commit({
            "svc.py": """
                def build_handler(cfg):
                    def on_event(evt):
                        return evt
                    return on_event
            """,
            "callers.py": "h = build_handler(c)\nh.on_event(e)\n",
        })
        head = self.repo.commit({"svc.py": """
            def build_handler(cfg):
                def on_event(evt):
                    return evt.payload
                return on_event
        """})
        names = self.by_name(self.run_impact(base, head))
        self.assertEqual(names["build_handler"]["kind"], "body_changed")
        self.assertEqual(names["on_event"]["enclosing"], ["build_handler.on_event"])

    def test_declaration_line_change_stays_changed_not_body_changed(self):
        base = self.repo.commit({
            "svc.py": "def charge_card(amount):\n    return gateway(amount)\n",
            "callers.py": "charge_card(5)\n",
        })
        head = self.repo.commit({
            "svc.py": "def charge_card(amount, currency):\n    return gateway(amount, currency)\n",
        })
        result = self.run_impact(base, head)
        self.assertEqual([(s["name"], s["kind"]) for s in result["symbols"]],
                         [("charge_card", "changed")])

    def test_private_helper_with_only_same_file_callers_is_dropped(self):
        base = self.repo.commit({
            "svc.py": """
                def _normalise_email(e):
                    return e.lower()

                def register_user(e):
                    return _normalise_email(e)
            """,
            "callers.py": "register_user('x')\n",
        })
        head = self.repo.commit({"svc.py": """
            def _normalise_email(e):
                return e.strip().lower()

            def register_user(e):
                return _normalise_email(e)
        """})
        self.assertNotIn("_normalise_email", self.by_name(self.run_impact(base, head)))

    def test_ts_class_method_without_modifier_is_not_attributed_to_class(self):
        base = self.repo.commit({
            "user.service.ts": """
                export class UserService {
                  findOne(id: string) {
                    return this.repo.get(id);
                  }
                }
            """,
            "user.controller.ts": "constructor(private users: UserService) {}\nthis.users.findOne(id);\n",
        })
        head = self.repo.commit({"user.service.ts": """
            export class UserService {
              findOne(id: string) {
                return this.repo.get(id) ?? null;
              }
            }
        """})
        # Method declarations without an access modifier aren't matched yet, and the
        # class must never stand in for them.
        self.assertEqual(self.run_impact(base, head)["symbols"], [])

    def test_missing_head_skips_body_analysis_but_keeps_declarations(self):
        base = self.repo.commit({
            "svc.py": "def legacy_export(x):\n    return x\n\ndef keep_going(x):\n    return x\n",
            "callers.py": "legacy_export(1)\nkeep_going(2)\n",
        })
        head = self.repo.commit({"svc.py": "def keep_going(x):\n    return x + 1\n"})
        result = self.run_impact(base, head, head_sha="0" * 40)
        self.assertIn("not in the clone", result["body_analysis_skipped"])
        self.assertEqual([(s["name"], s["kind"]) for s in result["symbols"]],
                         [("legacy_export", "removed")])

    def test_stale_ref_skips_body_analysis(self):
        base = self.repo.commit({"svc.py": "def keep_going(x):\n    return x\n",
                                 "callers.py": "keep_going(2)\n"})
        head = self.repo.commit({"svc.py": "def keep_going(x):\n    return x + 1\n"})
        result = self.run_impact(base, head, "--ref", base)
        self.assertIn("re-fetch", result["body_analysis_skipped"])

    def test_cap_keeps_every_declaration_change(self):
        n_body, n_removed = 60, 5
        before = "".join(f"def body_fn_{i:02d}(x):\n    return x\n\n" for i in range(n_body))
        before += "".join(f"def gone_fn_{i}(x):\n    return x\n\n" for i in range(n_removed))
        after = "".join(f"def body_fn_{i:02d}(x):\n    return x + 1\n\n" for i in range(n_body))
        callers = "".join(f"body_fn_{i:02d}(1)\n" for i in range(n_body))
        callers += "".join(f"gone_fn_{i}(1)\n" for i in range(n_removed))
        base = self.repo.commit({"svc.py": before, "callers.py": callers})
        head = self.repo.commit({"svc.py": after})
        result = self.run_impact(base, head, "--max-symbols", "40")
        kinds = [s["kind"] for s in result["symbols"]]
        self.assertEqual(kinds.count("removed"), n_removed)
        self.assertEqual(kinds.count("body_changed"), 40 - n_removed)
        self.assertEqual(result["candidates_over_cap"], n_body + n_removed - 40)


if __name__ == "__main__":
    unittest.main()
