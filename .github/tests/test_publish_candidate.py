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
        self.tag_objects = {}
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
        if method == "GET" and path == "git/ref/tags/v4.1.0":
            if not self.tag:
                return None
            return {"ref": "refs/tags/v4.1.0", "object": {
                "sha": self.tag, "type": "tag" if self.tag in self.tag_objects else "commit"}}
        if method == "GET" and path.startswith("git/tags/"):
            return self.tag_objects[path.rsplit("/", 1)[1]]
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

    def test_missing_exact_ref_404_plans_even_when_commit_lookup_would_return_422(self):
        self.api_patch.stop()
        paths = []
        def missing(request, **kwargs):
            paths.append(request.full_url)
            status = 422 if "/commits/" in request.full_url else 404
            raise urllib.error.HTTPError(request.full_url, status, "missing", {}, None)
        with mock.patch.object(RELEASE.urllib.request, "urlopen", side_effect=missing):
            RELEASE.plan(self.root)
        self.assertEqual(paths, [
            "https://api.github.com/repos/owner/repo/releases/tags/v4.1.0",
            "https://api.github.com/repos/owner/repo/git/ref/tags/v4.1.0",
        ])
        self.assertIn("publish=true\n", self.outputs.read_text())
        self.gh.assert_not_called()

    def test_successful_null_response_never_authorizes_publication(self):
        self.artifacts()
        self.api_patch.stop()
        for null_path in ("releases/tags/v4.1.0", "git/ref/tags/v4.1.0"):
            calls = []
            def fetch(request, **kwargs):
                calls.append((request.method, request.full_url))
                self.assertEqual(request.method, "GET")
                path = request.full_url.split("/owner/repo/", 1)[1]
                if path == null_path:
                    return io.BytesIO(b"null")
                if path.startswith("releases/tags/"):
                    raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
                if path.startswith("releases?"):
                    return io.BytesIO(b"[]")
                self.fail(f"Unexpected API operation: {path}")
            with mock.patch.object(RELEASE.urllib.request, "urlopen", side_effect=fetch):
                for action in (RELEASE.plan, RELEASE.publish):
                    with self.subTest(path=null_path, action=action.__name__), self.assertRaisesRegex(
                            RELEASE.ReleaseError, "invalid null record"):
                        action(self.root)
            self.assertFalse(self.outputs.exists())
            self.assertTrue(all(method == "GET" for method, _ in calls))
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def annotated_chain(self, commit, depth=1):
        objects = [f"{n:040x}" for n in range(1, depth + 1)]
        self.tag = objects[0]
        self.tag_objects = {
            sha: {"sha": sha, "object": {"sha": objects[i + 1] if i + 1 < depth else commit,
                                       "type": "tag" if i + 1 < depth else "commit"}}
            for i, sha in enumerate(objects)
        }

    def test_annotated_tags_are_bound_to_peeled_commit(self):
        self.artifacts()
        for depth in (1, 2, 8):
            with self.subTest(depth=depth):
                self.annotated_chain(SHA, depth)
                self.gh.reset_mock()
                RELEASE.plan(self.root)
                RELEASE.publish(self.root)
                self.assertEqual(self.created_tags, [])
                self.gh.assert_called_once()

    def test_annotated_tag_conflict_is_not_overwritten(self):
        self.annotated_chain(OTHER_SHA)
        self.artifacts()
        for action in (RELEASE.plan, RELEASE.publish):
            with self.subTest(action=action.__name__), self.assertRaisesRegex(RELEASE.ReleaseError, "different commit"):
                action(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_malformed_exact_reference_fails_before_mutation(self):
        target = {"sha": SHA, "type": "commit"}
        self.artifacts()
        records = [[], {}, {"ref": "refs/heads/v4.1.0", "object": target},
                   {"ref": "refs/tags/v4.1.0-extra", "object": target}]
        records += [{"ref": "refs/tags/v4.1.0", "object": value} for value in
                    (None, [], {"sha": SHA, "type": "tree"}, {"sha": SHA, "type": []},
                     {"sha": "invalid", "type": "commit"})]
        original = self.fake_api
        for record in records:
            def invalid(method, path, payload=None, **kwargs):
                if path == "git/ref/tags/v4.1.0":
                    return record
                return original(method, path, payload, **kwargs)
            self.api_mock.side_effect = invalid
            for action in (RELEASE.plan, RELEASE.publish):
                with self.subTest(record=record, action=action.__name__), self.assertRaises(RELEASE.ReleaseError):
                    action(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_corrupt_annotated_objects_do_not_become_missing_tags(self):
        self.artifacts()
        for value in (None, [], {}, {"sha": OTHER_SHA, "object": {"sha": SHA, "type": "commit"}},
                      {"sha": "1".zfill(40), "object": {"sha": SHA, "type": "blob"}}):
            self.annotated_chain(SHA)
            self.tag_objects[self.tag] = value
            with self.subTest(value=value), self.assertRaises(RELEASE.ReleaseError):
                RELEASE.publish(self.root)
        self.assertEqual(self.created_tags, [])
        self.gh.assert_not_called()

    def test_missing_annotated_object_404_is_fatal(self):
        self.annotated_chain(SHA)
        original = self.fake_api
        self.api_patch.stop()
        def fetch(request, **kwargs):
            path = request.full_url.split("/owner/repo/", 1)[1]
            if path.startswith("releases/tags/") or path.startswith("git/tags/"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing object", {}, None)
            data = original("GET", path)
            return io.BytesIO(json.dumps(data).encode())
        with mock.patch.object(RELEASE.urllib.request, "urlopen", side_effect=fetch):
            with self.assertRaisesRegex(RELEASE.ReleaseError, "HTTP 404"):
                RELEASE.plan(self.root)
        self.assertFalse(self.outputs.exists())
        self.gh.assert_not_called()

    def test_cyclic_and_excessive_annotated_chains_are_bounded(self):
        for depth, cycle in ((1, True), (9, False)):
            self.annotated_chain(SHA, depth)
            if cycle:
                self.tag_objects[self.tag]["object"] = {"sha": self.tag, "type": "tag"}
            self.api_mock.reset_mock()
            with self.subTest(depth=depth), self.assertRaisesRegex(RELEASE.ReleaseError, "cyclic or excessive"):
                RELEASE.plan(self.root)
            self.assertLessEqual(self.api_mock.call_count, 10)
        self.assertFalse(self.outputs.exists())
        self.gh.assert_not_called()

    def test_manifest_upgrade_selects_new_release_and_tag(self):
        (self.root / "release-manifest.json").write_text(json.dumps({"version": "4.2.0"}))
        def new_version(method, path, **kwargs):
            self.assertEqual(method, "GET")
            self.assertIn(path, ("releases/tags/v4.2.0", "git/ref/tags/v4.2.0"))
            return None
        self.api_mock.side_effect = new_version
        RELEASE.plan(self.root)
        self.assertEqual(self.outputs.read_text(), "publish=true\nversion=4.2.0\ntag=v4.2.0\n")
        self.gh.assert_not_called()

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
        for status in (404, 403, 409, 422, 500):
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
