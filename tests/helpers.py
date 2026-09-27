"""Shared helpers for tests that run pr_post.main() against a fake gh."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core", "scripts"))
import pr_fetch  # noqa: E402,F401
import pr_post  # noqa: E402

SHA1 = "a" * 40
SHA2 = "b" * 40


class FakeGh:
    """Stands in for gh: serves the PR, records every write."""

    def __init__(self, body="", fail_patch=False):
        self.body = body
        self.fail_patch = fail_patch
        self.writes = []

    def gh_json(self, *args):
        return {"body": self.body, "head": {"sha": SHA1}}

    def gh_with_input(self, args, payload):
        method, path = args[2], args[3]
        if method == "PATCH" and self.fail_patch:
            raise pr_fetch.GhError("HTTP 403: Resource not accessible by integration")
        self.writes.append((method, path, json.loads(payload)))
        return "{}"

    def gh(self, *args):
        self.writes.append(("GRAPHQL", args[1], list(args[2:])))
        return "{}"


def run_post(tmp: str, gh: FakeGh, payload: dict, bundle: dict, *flags: str) -> dict:
    bundle_path = os.path.join(tmp, "bundle.json")
    findings = os.path.join(tmp, "findings.json")
    with open(bundle_path, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh)
    with open(findings, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    argv = ["pr_post.py", "o/r", "7", "--findings", findings, "--bundle", bundle_path, *flags]
    out = io.StringIO()
    with mock.patch.object(pr_post, "gh_json", gh.gh_json), \
            mock.patch.object(pr_post, "gh_with_input", gh.gh_with_input), \
            mock.patch.object(pr_post, "gh", gh.gh), \
            mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
        assert pr_post.main() == 0
    return json.loads(out.getvalue())


def base_bundle(**extra) -> dict:
    b = {"head_sha": SHA1, "posted_fingerprints": [], "existing": {},
         "files": [{"path": "a.py", "commentable": {"RIGHT": [3, 4], "LEFT": []}}]}
    b.update(extra)
    return b


def finding(**extra) -> dict:
    f = {"path": "a.py", "line": 3, "severity": "warning", "issue_class": "logic-error",
         "anchor": "handle_event", "title": "Wrong total", "body": "b"}
    f.update(extra)
    return f
