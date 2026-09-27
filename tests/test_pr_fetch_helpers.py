"""pr_fetch.py helpers: skill threads, dismissals, incremental state, stacking.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import unittest
from unittest import mock

from helpers import SHA1, SHA2, pr_fetch

SKILL = "> AI\n\n**Null deref in handler**\n\nbody\n<!-- pr-review-skill:v1 fp=abc123abc123 cls=null-deref anchor=handle -->"


def rc(id_, body, reply_to=None, user="dev", when="2026-01-01T00:00:00Z", **extra):
    c = {"id": id_, "body": body, "in_reply_to_id": reply_to, "user": {"login": user},
         "created_at": when, "path": "a.ts", "line": 4, "position": 1,
         "html_url": f"https://x/{id_}"}
    c.update(extra)
    return c


class ThreadsTest(unittest.TestCase):
    def test_skill_threads_and_dismissals(self):
        comments = [
            rc(1, SKILL),
            rc(2, "This is intentional, the caller validates it.", reply_to=1, when="2026-01-02T00:00:00Z"),
            rc(3, "Human comment, not ours"),
            rc(4, SKILL.replace("abc123abc123", "def456def456"), reactions={"-1": 1}),
            rc(5, "ok " + pr_fetch.FOLLOWUP_MARKER, reply_to=4, user="me"),
        ]
        threads = {1: {"thread_id": "T1", "resolved": False, "outdated": False},
                   4: {"thread_id": "T4", "resolved": False, "outdated": True}}
        out = pr_fetch.build_skill_threads(comments, threads)
        self.assertEqual([t["thread_id"] for t in out], ["T1", "T4"])
        t1, t4 = out
        self.assertEqual((t1["title"], t1["issue_class"], t1["anchor"]), ("Null deref in handler", "null-deref", "handle"))
        self.assertFalse(t1["last_reply_by_skill"])
        self.assertTrue(t4["last_reply_by_skill"])
        learned = pr_fetch.dismissals("o/r", 7, out)
        self.assertEqual([(d["fingerprint"], d["reason"]) for d in learned],
                         [("abc123abc123", "intentional"), ("def456def456", "thumbs-down reaction")])


class IncrementalTest(unittest.TestCase):
    def test_last_reviewed_sha_prefers_newest_review_marker(self):
        reviews = [{"submitted_at": "2026-01-01", "body": f"<!-- pr-review-skill:reviewed sha={SHA1} -->"},
                   {"submitted_at": "2026-01-03", "body": f"<!-- pr-review-skill:reviewed sha={SHA2} -->"}]
        self.assertEqual(pr_fetch.last_reviewed_sha(reviews, [], "cccccccccccc"), SHA2)
        self.assertEqual(pr_fetch.last_reviewed_sha([], [], "cccccccccccc"), "cccccccccccc")

    def test_states(self):
        pr_paths = {"a.ts", "b.ts"}
        self.assertEqual(pr_fetch.incremental_state("o/r", None, SHA2, pr_paths, True)["status"], "first_review")
        self.assertEqual(pr_fetch.incremental_state("o/r", SHA1, SHA2, pr_paths, False)["status"], "disabled")
        self.assertEqual(pr_fetch.incremental_state("o/r", SHA2[:12], SHA2, pr_paths, True)["status"], "no_new_commits")
        with mock.patch.object(pr_fetch, "gh_json", return_value={"status": "ahead", "files": [
                {"filename": "a.ts"}, {"filename": "from-main.ts"}]}):
            st = pr_fetch.incremental_state("o/r", SHA1, SHA2, pr_paths, True)
        self.assertEqual((st["status"], st["files_changed_since"]), ("incremental", ["a.ts"]))
        with mock.patch.object(pr_fetch, "gh_json", return_value={"status": "diverged"}):
            self.assertEqual(pr_fetch.incremental_state("o/r", SHA1, SHA2, pr_paths, True)["status"], "rebased")
        with mock.patch.object(pr_fetch, "gh_json", side_effect=pr_fetch.GhError("404")):
            self.assertEqual(pr_fetch.incremental_state("o/r", SHA1, SHA2, pr_paths, True)["status"], "unreachable")


class StackTest(unittest.TestCase):
    def test_detects_parent_pr(self):
        pr = {"base": {"ref": "feature/a", "repo": {"default_branch": "main"}}}
        with mock.patch.object(pr_fetch, "gh_json", return_value=[{"number": 41, "title": "A", "html_url": "u"}]):
            st = pr_fetch.detect_stack("o/r", pr)
        self.assertTrue(st["is_stacked"])
        self.assertEqual(st["parent_pr"]["number"], 41)
        self.assertFalse(pr_fetch.detect_stack("o/r", {"base": {"ref": "main", "repo": {"default_branch": "main"}}})["is_stacked"])


if __name__ == "__main__":
    unittest.main()
