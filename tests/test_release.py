from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
EVALUATION_RESULTS = (
    "evals/host_results.json",
    "evals/invocation_results.json",
    "evals/forward_results.json",
    "evals/memory_results.json",
)
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "scripts" / "build_release.py")
assert SPEC and SPEC.loader
build_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_release)


class ReleaseArchiveTests(unittest.TestCase):
    def test_clean_extraction_self_check_and_tests_do_not_need_evaluation_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "release.zip"
            result = build_release.build(ROOT, archive)
            extraction = build_release.extract(archive, base / "install", result["archive_sha256"])
            installed = Path(extraction["destination"])
            for relative in EVALUATION_RESULTS:
                self.assertFalse((installed / relative).exists(), relative)
            validator = [sys.executable, str(installed / "scripts/validate_longtask.py")]
            checked = subprocess.run([*validator, "--installed"], cwd=installed, capture_output=True,
                                     text=True, timeout=60, check=False)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("host release gate not evaluated", checked.stdout)
            tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                    "-p", "test_validator.py"], cwd=installed, capture_output=True,
                                   text=True, timeout=60, check=False)
            self.assertEqual(tests.returncode, 0, tests.stdout + tests.stderr)
            for relative in EVALUATION_RESULTS:
                self.assertFalse((installed / relative).exists(),
                                 f"self-check must not synthesize evaluation results: {relative}")
            for flags in ([], ["--require-release-pass"]):
                rejected = subprocess.run([*validator, *flags], cwd=installed, capture_output=True,
                                          text=True, timeout=60, check=False)
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn("missing required file: evals/host_results.json", rejected.stderr)
            incompatible = subprocess.run([*validator, "--installed", "--require-release-pass"],
                                          cwd=installed, capture_output=True, text=True, timeout=60, check=False)
            self.assertEqual(incompatible.returncode, 2)
            # Embedded-manifest presence never makes source release checks optional.
            embedded = installed / "RELEASE-MANIFEST.json"
            manifest = json.loads(embedded.read_text())
            manifest["source_tree_sha256"] = "0" * 64
            embedded.write_text(json.dumps(manifest))
            rejected = subprocess.run([*validator, "--installed"], cwd=installed, capture_output=True,
                                      text=True, timeout=60, check=False)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("installed release manifest does not match", rejected.stderr)
            embedded.unlink()
            missing = subprocess.run([*validator, "--installed"], cwd=installed, capture_output=True,
                                     text=True, timeout=60, check=False)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("installed release manifest verification failed", missing.stderr)

    def test_nested_enumeration_failure_blocks_build_and_source_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            nested = root / "payload" / "nested"
            nested.mkdir(parents=True)
            (nested / "required.txt").write_text("required")
            manifest = {
                "schema_version": 1, "package": "sample", "version": "1.0.0", "archive_root": "sample",
                "files": ["release-manifest.json"], "directories": ["payload"],
                "exclude_paths": [], "exclude_names": [], "exclude_suffixes": [],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest))
            archive = Path(temporary) / "release.zip"
            result = build_release.build(root, archive)
            original_scandir = os.scandir
            def fail_nested(path):
                if Path(path) == nested:
                    raise PermissionError("injected unreadable release subtree")
                return original_scandir(path)
            with mock.patch.object(os, "scandir", side_effect=fail_nested):
                with self.assertRaisesRegex(build_release.ReleaseError, "cannot enumerate release directory"):
                    build_release.build(root, Path(temporary) / "broken.zip")
                with self.assertRaisesRegex(build_release.ReleaseError, "cannot enumerate release directory"):
                    build_release.verify(root, archive, result["archive_sha256"])
            self.assertFalse((Path(temporary) / "broken.zip").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFO support")
    def test_fifo_archive_verify_and_extract_fail_without_waiting_for_a_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            pipe = Path(temporary) / "archive.pipe"
            os.mkfifo(pipe)
            for command in ("verify", "extract"):
                args = [sys.executable, str(ROOT / "scripts/build_release.py"), command,
                        "--archive", str(pipe), "--expected-sha256", "0" * 64]
                if command == "extract":
                    args.extend(["--destination", str(Path(temporary) / "staging")])
                result = subprocess.run(args, capture_output=True, text=True, timeout=2, check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("regular file", result.stderr)

    def test_two_builds_are_byte_identical_and_source_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = Path(temporary) / "first.zip"
            second = Path(temporary) / "second.zip"
            first_result = build_release.build(ROOT, first)
            second_result = build_release.build(ROOT, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(first_result["archive_sha256"], second_result["archive_sha256"])
            verified = build_release.verify(ROOT, first, first_result["archive_sha256"])
            self.assertTrue(verified["source_verified"])
            self.assertTrue(verified["trusted_digest_verified"])
            self.assertEqual(verified["archive_sha256"], hashlib.sha256(first.read_bytes()).hexdigest())

    def test_evaluation_runs_do_not_change_release_but_product_edits_do(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            root.mkdir()
            manifest = build_release.load_manifest(ROOT)
            # Snapshot the real release closure so subsequent writes cannot affect
            # the working checkout or hide changes behind mocked build behavior.
            for source in build_release.collect_sources(ROOT, manifest):
                path = root / source.relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(source.data)
                path.chmod(source.mode)
            records = {relative: {"generated_at": "2026-09-07T00:00:00Z", "runs": []}
                       for relative in EVALUATION_RESULTS}
            for relative, record in records.items():
                (root / relative).write_text(json.dumps(record), encoding="utf-8")
            first = base / "first.zip"
            baseline = build_release.build(root, first)
            for relative, record in records.items():
                with self.subTest(result=relative):
                    record["generated_at"] = "2026-09-08T01:02:03Z"
                    record["runs"] = [{"run_id": "new-run", "duration_ms": 42,
                                       "status": "pass"}]
                    (root / relative).write_text(json.dumps(record), encoding="utf-8")
                    repeated = base / "repeated.zip"
                    result = build_release.build(root, repeated)
                    self.assertEqual(repeated.read_bytes(), first.read_bytes())
                    self.assertEqual(result["source_tree_sha256"], baseline["source_tree_sha256"])
                    verified = build_release.verify(root, first, baseline["archive_sha256"])
                    self.assertTrue(verified["source_verified"])
            for relative in ("scripts/run_skill_evals.py", "SKILL.md"):
                with self.subTest(product=relative):
                    path = root / relative
                    original = path.read_bytes()
                    path.write_bytes(original + b"\n# Changed product content\n")
                    changed = base / "changed.zip"
                    result = build_release.build(root, changed)
                    self.assertNotEqual(changed.read_bytes(), first.read_bytes())
                    self.assertNotEqual(result["source_tree_sha256"], baseline["source_tree_sha256"])
                    path.write_bytes(original)

    def test_build_rejects_output_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside.zip"
            outside.write_bytes(b"preserve me")
            output = base / "release.zip"
            output.symlink_to(outside)
            with self.assertRaisesRegex(build_release.ReleaseError, "regular non-linked"):
                build_release.build(ROOT, output)
            self.assertEqual(outside.read_bytes(), b"preserve me")

    def test_build_rejects_output_ancestor_symlink_without_writing_outside(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            (base / "alias").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(build_release.ReleaseError, "non-directory component|safely"):
                build_release.build(ROOT, base / "alias" / "release.zip")
            self.assertFalse((outside / "release.zip").exists())

    def test_excluded_directories_cannot_leak_checkpoint_or_generated_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            manifest = {
                "schema_version": 1, "package": "sample", "version": "1.0.0", "archive_root": "sample",
                "files": ["release-manifest.json"], "directories": ["docs", "payload"],
                "exclude_paths": ["docs/tasks"], "exclude_names": [".longtask", "dist"], "exclude_suffixes": [],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest))
            (root / "docs").mkdir()
            (root / "docs/ARCHITECTURE.md").write_text("Current project content\n")
            (root / "payload").mkdir()
            baseline, _ = build_release.archive_bytes(root)
            for relative in ("docs/tasks/old/state.json", "payload/.longtask/events.jsonl", "payload/dist/release.zip"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("execution details excluded from product\n")
            observed, _ = build_release.archive_bytes(root)
            self.assertEqual(observed, baseline)

    def test_archive_has_exact_allowlisted_closure_without_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "release.zip"
            result = build_release.build(ROOT, archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
            self.assertEqual(names, sorted(names))
            self.assertIn("longtask/RELEASE-MANIFEST.json", names)
            self.assertIn("longtask/release-manifest.json", names)
            self.assertIn("longtask/.codex-plugin/plugin.json", names)
            self.assertIn("longtask/references/personal-marketplace.json", names)
            self.assertFalse(any(".claude" in name for name in names))
            self.assertFalse(any("longtask-1." in name or "longtask-2." in name for name in names))
            self.assertFalse(any("docs/tasks/" in name or "/.longtask/" in name or "/dist/" in name for name in names))
            self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names))
            for relative in EVALUATION_RESULTS:
                self.assertNotIn(f"longtask/{relative}", names)
            for relative in ("evals/invocation_cases.json", "evals/forward_cases.json",
                             "references/评测协议.md", "scripts/run_skill_evals.py",
                             "scripts/run_forward_evals.py", "tests/test_validator.py"):
                self.assertIn(f"longtask/{relative}", names)
            self.assertGreater(result["file_count"], 20)
            with zipfile.ZipFile(archive_path) as archive:
                plugin = json.loads(archive.read("longtask/.codex-plugin/plugin.json"))
            self.assertEqual(plugin["version"], "3.0.0")

    def test_extract_requires_digest_and_reconstructs_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            archive_path = temporary_path / "release.zip"
            result = build_release.build(ROOT, archive_path)
            extraction = build_release.extract(archive_path, temporary_path / "install", result["archive_sha256"])
            installed = Path(extraction["destination"])
            self.assertEqual((installed / "SKILL.md").read_bytes(), (ROOT / "SKILL.md").read_bytes())
            embedded = json.loads((installed / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(embedded["source_tree_sha256"], result["source_tree_sha256"])
            with self.assertRaises(build_release.ReleaseError):
                build_release.extract(archive_path, temporary_path / "other", "0" * 64)

    def test_detached_verify_and_extract_require_a_valid_trusted_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive_path = base / "release.zip"
            build_release.build(ROOT, archive_path)
            for digest in (None, "", "sha256:bad", "A" * 64):
                with self.subTest(operation="verify", digest=digest), self.assertRaisesRegex(
                    build_release.ReleaseError, "trusted SHA-256"
                ):
                    build_release.verify(None, archive_path, digest)
            for digest in ("", "sha256:bad", "A" * 64):
                with self.subTest(operation="extract", digest=digest), self.assertRaisesRegex(
                    build_release.ReleaseError, "trusted SHA-256"
                ):
                    build_release.extract(archive_path, base / f"install-{len(digest)}", digest)

    def test_build_rejects_oversized_source_before_reading_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            payload = root / "payload.bin"
            with payload.open("wb") as stream:
                stream.truncate(build_release.MAX_MEMBER_BYTES + 1)
            manifest = {
                "schema_version": 1, "package": "sample", "version": "1.0.0",
                "archive_root": "sample", "files": ["release-manifest.json", "payload.bin"],
                "directories": [], "exclude_paths": [], "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(build_release.ReleaseError, "per-file size limit"):
                build_release.build(root, base / "release.zip")

    def test_growing_source_read_is_bounded_without_waiting_for_eof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "payload.bin").write_bytes(b"x")
            calls = 0

            def never_eof(_descriptor: int, requested: int) -> bytes:
                nonlocal calls
                calls += 1
                return b"x" * requested

            with (
                mock.patch.object(build_release.os, "read", side_effect=never_eof),
                self.assertRaisesRegex(build_release.ReleaseError, "while reading"),
            ):
                build_release.secure_read_source(root, "payload.bin")
            self.assertLessEqual(calls, build_release.MAX_MEMBER_BYTES // (1024 * 1024) + 1)

    def test_tampered_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "release.zip"
            result = build_release.build(ROOT, archive_path)
            data = bytearray(archive_path.read_bytes())
            data[len(data) // 2] ^= 1
            archive_path.write_bytes(data)
            with self.assertRaises(build_release.ReleaseError):
                build_release.verify(None, archive_path, result["archive_sha256"])

    def test_explicit_file_cannot_escape_through_ancestor_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "payload.txt").write_text("secret", encoding="utf-8")
            (root / "alias").symlink_to(outside, target_is_directory=True)
            manifest = {
                "schema_version": 1,
                "package": "sample",
                "version": "1.0.0",
                "archive_root": "sample",
                "files": ["release-manifest.json", "alias/payload.txt"],
                "directories": [],
                "exclude_paths": [],
                "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(build_release.ReleaseError, "unsafe ancestor"):
                build_release.build(root, base / "release.zip")

    def test_release_source_hardlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            payload = root / "payload.txt"
            payload.write_text("payload", encoding="utf-8")
            os.link(payload, base / "alias.txt")
            manifest = {
                "schema_version": 1,
                "package": "sample",
                "version": "1.0.0",
                "archive_root": "sample",
                "files": ["release-manifest.json", "payload.txt"],
                "directories": [],
                "exclude_paths": [],
                "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(build_release.ReleaseError, "exactly one hard link"):
                build_release.build(root, base / "release.zip")

    def test_manifest_symlink_is_rejected_before_external_bytes_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            outside = base / "outside.json"
            outside.write_text("not-json-secret", encoding="utf-8")
            (root / "release-manifest.json").symlink_to(outside)
            with self.assertRaisesRegex(build_release.ReleaseError, "regular file|securely") as caught:
                build_release.build(root, base / "release.zip")
            self.assertNotIn("not-json-secret", str(caught.exception))

    def test_fifo_source_is_rejected_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            os.mkfifo(root / "payload.pipe")
            manifest = {
                "schema_version": 1,
                "package": "sample",
                "version": "1.0.0",
                "archive_root": "sample",
                "files": ["release-manifest.json", "payload.pipe"],
                "directories": [],
                "exclude_paths": [],
                "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "build_release.py"),
                    "build",
                    "--root",
                    str(root),
                    "--output",
                    str(base / "release.zip"),
                ],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("regular file", result.stderr)

    def test_archive_root_must_be_one_safe_path_component(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            manifest = {
                "schema_version": 1,
                "package": "../escape",
                "version": "1.0.0",
                "archive_root": "../escape",
                "files": ["release-manifest.json"],
                "directories": [],
                "exclude_paths": [],
                "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(build_release.ReleaseError, "archive_root"):
                build_release.build(root, base / "release.zip")

    def test_dot_release_path_is_rejected_without_index_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            root.mkdir()
            manifest = {
                "schema_version": 1,
                "package": "sample",
                "version": "1.0.0",
                "archive_root": "sample",
                "files": ["release-manifest.json"],
                "directories": ["."],
                "exclude_paths": [],
                "exclude_names": ["__pycache__"],
                "exclude_suffixes": [".pyc"],
            }
            (root / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(build_release.ReleaseError, "unsafe or non-canonical"):
                build_release.build(root, base / "release.zip")

    def test_archive_is_parsed_from_the_exact_bytes_that_were_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "release.zip"
            result = build_release.build(ROOT, archive_path)
            trusted = archive_path.read_bytes()
            archive_path.write_bytes(b"not the trusted archive")
            with mock.patch.object(build_release, "read_archive_bytes", return_value=trusted):
                embedded, content = build_release.inspect_archive(
                    archive_path, result["archive_sha256"],
                )
            self.assertEqual(embedded["archive_sha256"], result["archive_sha256"])
            self.assertIn("SKILL.md", content)

    def test_embedded_mode_must_match_the_zip_member_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "release.zip"
            build_release.build(ROOT, archive_path)
            with zipfile.ZipFile(archive_path, "r") as archive:
                members = [(info, archive.read(info)) for info in archive.infolist()]
            rewritten: list[tuple[zipfile.ZipInfo, bytes]] = []
            for info, data in members:
                if info.filename.endswith("/RELEASE-MANIFEST.json"):
                    manifest = json.loads(data)
                    manifest["source_entries"][0]["mode"] = 0o7777
                    manifest["source_tree_sha256"] = build_release.sha256_bytes(
                        build_release.canonical_json(manifest["source_entries"])
                    )
                    data = build_release.canonical_json(manifest)
                rewritten.append((info, data))
            with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                for info, data in rewritten:
                    archive.writestr(info, data)
            with self.assertRaisesRegex(build_release.ReleaseError, "embedded source mode"):
                build_release.inspect_archive(archive_path)

    def test_fd_relative_extraction_rejects_nested_symlink_alias(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.symlink(outside, "nested", target_is_directory=True, dir_fd=descriptor)
                with self.assertRaisesRegex(build_release.ReleaseError, "unsafe extraction ancestor"):
                    build_release.write_file_at(descriptor, "nested/payload.txt", b"unsafe", 0o644)
            finally:
                os.close(descriptor)
            self.assertFalse((outside / "payload.txt").exists())

    def test_windows_ambiguous_release_paths_are_rejected(self) -> None:
        for path in ("C:payload", "CON", "NUL.txt", "trailing.", "trailing "):
            with self.subTest(path=path), self.assertRaises(build_release.ReleaseError):
                build_release.clean_relative_path(path, "test")


if __name__ == "__main__":
    unittest.main()
