"""Native release-consumer boundaries; run on Windows, without os.name mocks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("build_release_native", ROOT / "scripts/build_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@unittest.skipUnless(os.name == "nt", "requires native Windows release consumers")
class WindowsReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="longtask-release-native-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "payload").mkdir()
        self.data = bytes(range(256)) * 8192
        (self.source / "payload/文件.bin").write_bytes(self.data)
        self.manifest = {
            "schema_version": 1, "package": "sample", "version": "1.0.0", "archive_root": "sample",
            "files": ["release-manifest.json"], "directories": ["payload"],
            "exclude_paths": [], "exclude_names": [], "exclude_suffixes": [],
        }
        self.save_manifest()
        self.archive = self.base / "archive.zip"

    def save_manifest(self):
        (self.source / "release-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def junction(self, link, target):
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(link) if link.exists() else None)

    def build(self):
        return release.build(self.source, self.archive)

    def test_binary_trusted_roundtrip_and_rebuild_preserve_archive_and_modes(self):
        built = self.build()
        extracted = release.extract(self.archive, self.base / "staging", built["archive_sha256"])
        installed = Path(extracted["destination"])
        self.assertEqual((installed / "payload/文件.bin").read_bytes(), self.data)
        rebuilt = self.base / "rebuilt.zip"
        self.assertEqual(release.build(installed, rebuilt)["archive_sha256"], built["archive_sha256"])
        release.verify(installed, self.archive, built["archive_sha256"])
        with self.assertRaises(release.ReleaseError):
            release.extract(self.archive, self.base / "wrong", "0" * 64)
        self.assertFalse((self.base / "wrong").exists())

    def test_source_root_and_nested_junctions_fail_closed(self):
        self.junction(self.base / "alias-root", self.source)
        with self.assertRaises((OSError, release.ReleaseError)):
            release.build(self.base / "alias-root", self.archive)
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside")
        self.junction(self.source / "payload/alias", outside)
        with self.assertRaises((OSError, release.ReleaseError)):
            self.build()
        self.assertFalse(self.archive.exists())

    def test_explicit_source_cannot_follow_junction(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"outside")
        self.junction(self.source / "alias", outside)
        self.manifest["directories"] = []
        self.manifest["files"].append("alias/secret")
        self.save_manifest()
        with self.assertRaises(release.ReleaseError):
            self.build()

    def test_archive_output_read_and_destination_reject_junction_ancestors(self):
        built = self.build()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "archive.zip").write_bytes(self.archive.read_bytes())
        alias = self.base / "alias"
        self.junction(alias, outside)
        with self.assertRaises((OSError, release.ReleaseError)):
            release.build(self.source, alias / "new.zip")
        with self.assertRaises(release.ReleaseError):
            release.inspect_archive(alias / "archive.zip", built["archive_sha256"])
        with self.assertRaises((OSError, release.ReleaseError)):
            release.extract(self.archive, alias / "staging", built["archive_sha256"])
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["archive.zip"])

    def test_relative_extraction_and_cleanup_never_follow_junction(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "preserve").write_bytes(b"outside")
        staging = self.base / "staging"
        staging.mkdir()
        self.junction(staging / "alias", outside)
        parent = release.open_directory_path(staging, create=False)
        try:
            with self.assertRaises(release.ReleaseError):
                release.write_file_at(parent, "alias/escape", b"unsafe", 0o644)
            with self.assertRaises(OSError):
                release.remove_tree_at(parent, "alias")
        finally:
            os.close(parent)
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["preserve"])

    def test_failed_extraction_cleans_private_staging_and_preserves_other_entries(self):
        built = self.build()
        staging = self.base / "staging"
        staging.mkdir()
        (staging / "preserve").write_bytes(b"outside")
        write = release.write_file_at
        calls = 0
        def interrupted(*args, **kwargs):
            nonlocal calls
            calls += 1
            write(*args, **kwargs)
            if calls == 1:
                raise OSError("injected extraction failure after first file")
        with mock.patch.object(release, "write_file_at", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "injected"):
                release.extract(self.archive, staging, built["archive_sha256"])
        self.assertEqual(sorted(path.name for path in staging.iterdir()), ["preserve"])
        release.extract(self.archive, staging, built["archive_sha256"])

    def test_output_hardlink_is_preserved_and_not_overwritten(self):
        self.archive.write_bytes(b"preserve")
        os.link(self.archive, self.base / "linked.zip")
        with self.assertRaisesRegex(release.ReleaseError, "non-linked"):
            self.build()
        self.assertEqual(self.archive.read_bytes(), b"preserve")

    def test_existing_target_junction_is_not_replaced(self):
        built = self.build()
        staging = self.base / "staging"
        staging.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        self.junction(staging / "sample", outside)
        with self.assertRaises((OSError, release.ReleaseError)):
            release.extract(self.archive, staging, built["archive_sha256"])
        self.assertTrue((staging / "sample").is_junction())
        self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
