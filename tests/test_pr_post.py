"""pr_post.py: description section, follow-ups, learned suppressions, redaction and
the reviewed-commit marker.

Run: python -m unittest discover tests
"""
from __future__ import annotations

import tempfile
import unittest

from helpers import SHA1, SHA2, FakeGh, base_bundle, finding, pr_fetch, pr_post, run_post

AUTHOR = "Fixes #12.\r\n\r\nMoves the retry logic into the consumer.\r\n"


class DescriptionMergeTest(unittest.TestCase):
    def test_first_run_puts_section_above_author_text_unchanged(self):
        new, status = pr_post.merge_description(AUTHOR, "- Adds retries", SHA1)
        self.assertEqual(status, "added")
        self.assertTrue(new.startswith("<!-- pr-review-skill:description:start sha=aaaaaaaaaaaa -->"))
        self.assertTrue(new.endswith(AUTHOR.strip()))
        self.assertIn("\n---\n", new)

    def test_empty_description_gets_section_without_separator(self):
        for empty in (None, "", "  \n"):
            new, status = pr_post.merge_description(empty, "- Adds retries", SHA1)
            self.assertEqual(status, "added")
            self.assertNotIn("---", new)

    def test_rerun_replaces_section_and_keeps_author_text_byte_for_byte(self):
        first, _ = pr_post.merge_description(AUTHOR, "- Adds retries", SHA1)
        edited = first + "\r\nAlso bumps the timeout."
        second, status = pr_post.merge_description(edited, "- Adds retries\n- Bumps timeout", SHA2)
        self.assertEqual(status, "updated")
        self.assertEqual(second.count("pr-review-skill:description:start"), 1)
        self.assertIn("sha=bbbbbbbbbbbb", second)
        self.assertEqual(pr_fetch.split_description(second)[2], pr_fetch.split_description(edited)[2])

    def test_same_text_same_sha_is_unchanged(self):
        first, _ = pr_post.merge_description(AUTHOR, "- Adds retries", SHA1)
        self.assertEqual(pr_post.merge_description(first, "- Adds retries", SHA1), (None, "unchanged"))

    def test_damaged_markers_leave_description_alone(self):
        first, _ = pr_post.merge_description(AUTHOR, "- Adds retries", SHA1)
        no_end = first.replace("<!-- pr-review-skill:description:end -->", "")
        for damaged in (no_end, first + "\n" + first):
            self.assertEqual(pr_post.merge_description(damaged, "- x", SHA2), (None, "markers_damaged"))

    def test_author_description_strips_only_the_generated_section(self):
        first, _ = pr_post.merge_description(AUTHOR, "- Adds retries", SHA1)
        self.assertEqual(pr_fetch.author_description(first), AUTHOR.strip())
        self.assertEqual(pr_fetch.author_description(AUTHOR), AUTHOR.strip())


class PostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, gh, payload, bundle=None, *flags):
        return run_post(self.tmp.name, gh, payload, bundle or base_bundle(), *flags)

    # --- description -----------------------------------------------------------

    def test_description_is_patched_even_with_no_findings(self):
        gh = FakeGh(AUTHOR)
        result = self.post(gh, {"findings": [], "description": "- Adds retries"})
        self.assertEqual(result["description"]["status"], "added")
        [(method, path, body)] = gh.writes
        self.assertEqual((method, path), ("PATCH", "repos/o/r/pulls/7"))
        self.assertTrue(body["body"].endswith(AUTHOR.strip()))

    def test_dry_run_writes_nothing(self):
        gh = FakeGh(AUTHOR)
        result = self.post(gh, {"findings": [finding()], "description": "- x"}, None, "--dry-run")
        self.assertEqual(gh.writes, [])
        self.assertIn("- x", result["description"]["preview"])

    def test_no_description_flag(self):
        gh = FakeGh(AUTHOR)
        result = self.post(gh, {"findings": [], "description": "- x"}, None, "--no-description")
        self.assertEqual((result["description"]["status"], gh.writes), ("disabled", []))

    def test_patch_failure_still_posts_review(self):
        gh = FakeGh(AUTHOR, fail_patch=True)
        result = self.post(gh, {"findings": [finding()], "description": "- x"})
        self.assertEqual(result["posted"], 1)
        self.assertEqual(result["description"]["status"], "failed")
        self.assertEqual([w[0] for w in gh.writes], ["POST"])

    # --- markers ---------------------------------------------------------------

    def test_review_records_reviewed_head_and_marker_attrs(self):
        gh = FakeGh()
        self.post(gh, {"findings": [finding(anchor="User Service.find -->x")]})
        [(_, _, review)] = gh.writes
        self.assertIn(f"<!-- pr-review-skill:reviewed sha={SHA1} -->", review["body"])
        comment = review["comments"][0]["body"]
        attrs = pr_fetch.marker_attrs(comment)
        self.assertEqual(attrs["cls"], "logic-error")
        self.assertTrue(attrs["anchor"].startswith("User_Service.find"))
        self.assertNotIn("--", attrs["anchor"])
        self.assertNotIn(">", attrs["anchor"])
        self.assertEqual(pr_fetch.fingerprints_in(comment), [attrs["fp"]])

    def test_old_markers_without_attrs_still_parse(self):
        old = "text\n<!-- pr-review-skill:v1 fp=a1b2c3d4e5f6 -->"
        self.assertEqual(pr_fetch.fingerprints_in(old), ["a1b2c3d4e5f6"])
        self.assertEqual(pr_fetch.marker_attrs(old), {"fp": "a1b2c3d4e5f6", "cls": None, "anchor": None})

    def test_nothing_new_posts_nothing(self):
        gh = FakeGh()
        result = self.post(gh, {"findings": []})
        self.assertEqual(gh.writes, [])
        self.assertEqual(result["note"], "no new review comments to post")

    # --- learned suppressions ----------------------------------------------------

    def test_learned_dismissal_skips_finding_unless_overridden(self):
        fp = pr_post.fingerprint(finding())
        bundle = base_bundle(learned_suppressions=[{"fingerprint": fp, "reason": "intentional",
                                                     "url": "https://x/1"}])
        result = self.post(FakeGh(), {"findings": [finding()]}, bundle)
        self.assertEqual(result["posted"], 0)
        self.assertEqual(result["skipped_duplicate"][0]["matched_by"], "learned_suppression")
        result = self.post(FakeGh(), {"findings": [finding()]}, bundle, "--ignore-learned")
        self.assertEqual(result["posted"], 1)

    # --- redaction ---------------------------------------------------------------

    def test_credentials_are_redacted_everywhere(self):
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        token = "ghp_" + "a" * 36
        gh = FakeGh()
        result = self.post(gh, {
            "summary": f"see {token}",
            "description": f"- adds {key}",
            "findings": [finding(body=f"`{key}` is committed", issue_class="secret-leak")],
        })
        written = repr(gh.writes)
        self.assertNotIn(key, written)
        self.assertNotIn(token, written)
        self.assertEqual(result["redacted_credentials"], 3)

    # --- follow-ups --------------------------------------------------------------

    def thread(self, **extra):
        t = {"thread_id": "PRRT_1", "root_comment_id": "rc:55", "resolved": False,
             "last_reply_by_skill": False}
        t.update(extra)
        return t

    def test_reply_and_resolve_own_thread(self):
        gh = FakeGh()
        bundle = base_bundle(skill_threads=[self.thread()])
        result = self.post(gh, {"findings": [], "followups": [
            {"thread_id": "PRRT_1", "action": "reply_and_resolve", "body": "Verified fixed in abc."}]}, bundle)
        self.assertEqual(result["followups"][0]["status"], "done")
        (m1, p1, b1), (m2, p2, a2) = gh.writes
        self.assertEqual((m1, p1), ("POST", "repos/o/r/pulls/7/comments/55/replies"))
        self.assertIn(pr_fetch.FOLLOWUP_MARKER, b1["body"])
        self.assertEqual(m2, "GRAPHQL")
        self.assertIn("id=PRRT_1", a2)

    def test_refuses_threads_the_skill_did_not_start(self):
        gh = FakeGh()
        result = self.post(gh, {"findings": [], "followups": [
            {"thread_id": "PRRT_human", "action": "resolve"}]}, base_bundle(skill_threads=[self.thread()]))
        self.assertEqual(result["followups"][0]["status"], "refused")
        self.assertEqual(gh.writes, [])

    def test_never_replies_twice_in_a_row(self):
        gh = FakeGh()
        bundle = base_bundle(skill_threads=[self.thread(last_reply_by_skill=True)])
        result = self.post(gh, {"findings": [], "followups": [
            {"thread_id": "PRRT_1", "action": "reply", "body": "ping"}]}, bundle)
        self.assertEqual(result["followups"][0]["status"], "refused")
        self.assertEqual(gh.writes, [])

    def test_resolving_an_already_resolved_thread_is_skipped(self):
        gh = FakeGh()
        bundle = base_bundle(skill_threads=[self.thread(resolved=True)])
        result = self.post(gh, {"findings": [], "followups": [
            {"thread_id": "PRRT_1", "action": "resolve"}]}, bundle)
        self.assertEqual(result["followups"][0]["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
