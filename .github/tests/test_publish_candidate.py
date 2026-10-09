from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/publish_candidate.py"
SPEC = importlib.util.spec_from_file_location("publish_candidate", SCRIPT)
assert SPEC and SPEC.loader
RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RELEASE)
SHA = "a" * 40
OTHER_SHA = "b" * 40


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "release-manifest.json").write_text(json.dumps({"version": "4.1.0"}))
        self.outputs = self.root / "outputs"
        environment = mock.patch.dict(os.environ, {
            "GITHUB_SHA": SHA, "GITHUB_REF": "refs/heads/main", "GITHUB_OUTPUT": str(self.outputs),
            "GH_TOKEN": "synthetic-token", "GH_REPO": "owner/repo",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.tag = None
        self.release = None
        self.release_pages = {1: []}
        self.created_tags = []
        self.publisher = mock.patch.object(RELEASE.subprocess, "run")
        self.gh = self.publisher.start()
        self.addCleanup(self.publisher.stop)
        self.api_patch = mock.patch.object(RELEASE, "api", side_effect=self.fake_api)
        self.api_mock = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.stdout = redirect_stdout(io.StringIO())
        self.stdout.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)

    def fake_api(self, method, path, payload=None, **kwargs):
        if method == "GET" and path == "releases/tags/v4.1.0":
            return self.release
        if method == "GET" and path.startswith("releases?per_page=100&page="):
            return self.release_pages[int(path.rsplit("=", 1)[1])]
        if method == "GET" and path == "commits/v4.1.0":
            return {"sha": self.tag} if self.tag else None
        if method == "POST" and path == "git/refs":
            self.created_tags.append(payload)
            self.tag = payload["sha"]
            return {"ref": payload["ref"]}
        self.fail(f"Unexpected API operation: {method} {path}")

    def artifacts(self):
        (self.root / "dist").mkdir()
        for name in ("longtask-4.1.0.zip", "longtask-4.1.0.sha256", "release-notes.txt"):
            (self.root / "dist" / name).write_text("synthetic verified fixture")

    def test_unpublished_main_version_plans_without_remote_mutation(self):
        RELEASE.plan(self.root)
        self.assertIn("publish=true\n", self.outputs.read_text())
        self.assertIn("tag=v4.1.0\n", self.outputs.read_text())
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_existing_published_version_skips_later_commits_and_does_not_overwrite(self):
        self.release = {"tag_name": "v4.1.0", "draft": False}
        self.tag = OTHER_SHA
        RELEASE.plan(self.root)
        RELEASE.publish(self.root)
        self.assertIn("publish=false\n", self.outputs.read_text())
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_matching_tag_can_resume_missing_release(self):
        self.tag = SHA
        self.artifacts()
        RELEASE.plan(self.root)
        RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_called_once()

    def test_new_tag_is_bound_to_checked_commit_and_candidate_is_not_latest(self):
        self.artifacts()
        RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [{"ref": "refs/tags/v4.1.0", "sha": SHA}])
        command = self.gh.call_args.args[0]
        self.assertIn("--verify-tag", command)
        self.assertIn("--prerelease", command)
        self.assertIn("--latest=false", command)
        self.assertEqual(command[3], "v4.1.0")

    def test_conflicting_tag_fails_without_mutation(self):
        self.tag = OTHER_SHA
        self.artifacts()
        for action in (RELEASE.plan, RELEASE.publish):
            with self.subTest(action=action.__name__), self.assertRaises(RELEASE.ReleaseError):
                action(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_incomplete_draft_fails_without_overwriting_assets(self):
        # The published-by-tag endpoint returns 404 for a draft; the write job sees it in the list.
        self.release_pages[1] = [{"tag_name": "v4.1.0", "draft": True}]
        self.artifacts()
        RELEASE.plan(self.root)
        self.assertIn("publish=true\n", self.outputs.read_text())
        with self.assertRaisesRegex(RELEASE.ReleaseError, "incomplete draft"):
            RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_draft_on_later_page_prevents_remote_mutation(self):
        self.release_pages[1] = [{"tag_name": f"v0.0.{n}", "draft": False} for n in range(100)]
        self.release_pages[2] = [{"tag_name": "v4.1.0", "draft": True}]
        self.artifacts()
        with self.assertRaisesRegex(RELEASE.ReleaseError, "incomplete draft"):
            RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_unknown_release_listing_fails_before_remote_mutation(self):
        self.artifacts()
        for record in (None, {}, [{"tag_name": "v4.1.0"}]):
            self.release_pages[1] = record
            with self.subTest(record=record), self.assertRaises(RELEASE.ReleaseError):
                RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_published_release_appearing_after_plan_is_skipped(self):
        RELEASE.plan(self.root)
        self.release_pages[1] = [{"tag_name": "v4.1.0", "draft": False}]
        RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_api_failure_is_not_treated_as_absent_release(self):
        self.api_mock.side_effect = RELEASE.ReleaseError("HTTP 403")
        with self.assertRaises(RELEASE.ReleaseError):
            RELEASE.plan(self.root)
        self.assertFalse(self.outputs.exists())
        self.gh.assert_not_called()

    def test_missing_verified_assets_prevents_tag_creation(self):
        with self.assertRaises(RELEASE.ReleaseError):
            RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_tag_drift_before_publication_prevents_release(self):
        self.artifacts()
        original = self.fake_api
        def drift(method, path, payload=None, **kwargs):
            record = original(method, path, payload, **kwargs)
            if method == "POST":
                self.tag = OTHER_SHA
            return record
        self.api_mock.side_effect = drift
        with self.assertRaises(RELEASE.ReleaseError):
            RELEASE.publish(self.root)
        self.gh.assert_not_called()

    def test_invalid_version_or_event_cannot_publish(self):
        for ref in ("refs/heads/feature", "refs/tags/v4.0.0"):
            with self.subTest(ref=ref), mock.patch.dict(os.environ, {"GITHUB_REF": ref}):
                with self.assertRaises(RELEASE.ReleaseError):
                    RELEASE.plan(self.root)
        (self.root / "release-manifest.json").write_text(json.dumps({"version": "4.1.0; unsafe"}))
        with self.assertRaises(RELEASE.ReleaseError):
            RELEASE.plan(self.root)
        self.api_mock.assert_not_called()

    def test_http_transport_only_allows_explicit_404_absence(self):
        self.api_patch.stop()
        for status in (404, 403, 500):
            with self.subTest(status=status), mock.patch.object(RELEASE.urllib.request, "urlopen", side_effect=
                    urllib.error.HTTPError("https://example.invalid", status, "failure", {}, None)):
                if status == 404:
                    self.assertIsNone(RELEASE.api("GET", "releases/tags/v4.1.0", missing_ok=True))
                else:
                    with self.assertRaises(RELEASE.ReleaseError):
                        RELEASE.api("GET", "releases/tags/v4.1.0", missing_ok=True)
                with self.assertRaises(RELEASE.ReleaseError):
                    RELEASE.api("POST", "git/refs", {"ref": "refs/tags/v4.1.0", "sha": SHA})


if __name__ == "__main__":
    unittest.main()
