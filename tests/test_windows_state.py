"""Native state-consumer boundaries; these are not filesystem-backend mocks."""
from __future__ import annotations

import importlib.util
import ctypes
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
SPEC = importlib.util.spec_from_file_location("longtask_state_native", ROOT / "scripts/longtask_state.py")
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)


@unittest.skipUnless(os.name == "nt", "requires native Windows state consumers")
class WindowsStateTests(unittest.TestCase):
    def test_cli_root_junction_and_reparse_ancestor_fail_without_target_writes(self):
        with tempfile.TemporaryDirectory(prefix="longtask-root-alias-") as temp:
            base = Path(temp)
            target = base / "target"
            target.mkdir()
            (target / "child").mkdir()
            alias = base / "alias"
            self.junction(alias, target)
            for root in (alias, alias / "child"):
                result = subprocess.run([sys.executable, str(ROOT / "scripts/longtask_state.py"),
                                         "init", "--root", str(root), "--mode", "continue",
                                         "--goal", "unsafe root must fail"], capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((target / ".longtask").exists())
                self.assertFalse((target / "child/.longtask").exists())
            alias.rmdir()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="longtask-state-native-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workspace"
        self.root.mkdir()

    def junction(self, path, target):
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(path), str(target)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(path) if path.exists() else None)

    def runtime_file(self, name, data=b"{}", mode=0o600):
        runtime = self.root / ".longtask"
        runtime.mkdir(exist_ok=True)
        parent = state.open_directory_path(runtime, create=False)
        try:
            fd = state.fs.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=parent)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        return runtime / name

    def cli(self, command, *args, ok=True):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/longtask_state.py"),
                                 command, "--root", str(self.root), *args],
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
        return json.loads(result.stdout if ok else result.stderr)

    def init(self):
        return self.cli("init", "--task-id", "native", "--mode", "continue", "--goal", "Native persistence")

    def aliases(self, *targets):
        return state.validate_write_aliases([
            {"id": str(i), "status": "active", "write_set": [target]}
            for i, target in enumerate(targets)
        ], self.root)

    def test_short_ancestor_path_supports_init_context_and_checkpoint(self):
        ancestor = self.base / "LongAncestorForNativeCheckpoint"
        ancestor.mkdir()
        long_root = ancestor / "workspace"
        long_root.mkdir()
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(ancestor), buffer, len(buffer))
        self.assertTrue(0 < length < len(buffer))
        short_ancestor = Path(buffer.value)
        self.assertNotEqual(short_ancestor.name.casefold(), ancestor.name.casefold(),
                            "native alias fixture requires DOS 8.3 creation on TEMP volume")
        self.root = short_ancestor / "workspace"
        self.assertNotEqual(self.root.absolute(), self.root.resolve())
        current = self.init()
        self.assertTrue((long_root / ".longtask/state.json").is_file())
        restored = self.cli("context", "--resume-choice", "resume")
        self.assertEqual(restored["binding"]["task_id"], current["task_id"])
        saved = self.cli("checkpoint", "--expected-task-id", current["task_id"],
                         "--expected-revision", str(current["revision"]))
        self.assertEqual(saved["revision"], current["revision"] + 1)
        self.assertTrue(self.cli("validate")["valid"])

    def test_active_write_sets_include_missing_paths_and_case_aliases(self):
        (self.root / "safe").mkdir()
        (self.root / "safe/file").write_bytes(b"data")
        self.assertEqual(self.aliases("safe", "future/child"), [])
        self.assertTrue(any("alias conflict" in e for e in self.aliases("safe/file", "SAFE/FILE")))
        self.assertTrue(any("alias conflict" in e for e in self.aliases("future/child", "FUTURE/CHILD")))

    def test_active_case_alias_parent_child_conflicts_and_attribution(self):
        (self.root / "OwnedDir").mkdir()
        for parent in ("FutureDir", "OwnedDir"):
            for targets in ((parent, parent.lower() + "/new.py"),
                            (parent.lower() + "/new.py", parent)):
                self.assertTrue(state.write_targets_overlap(*targets))
                self.assertTrue(any("active write-set conflict" in e for e in self.aliases(*targets)))
        self.assertTrue(state.write_set_covers_path(["OWNEDDIR"], "OwnedDir/new.py"))
        self.assertFalse(state.write_set_covers_path(["OWNEDDIR"], "OwnedDirectory/new.py"))
        self.assertEqual(self.aliases("OwnedDir/first.py", "owneddir/second.py"), [])

    def test_active_dos_short_name_aliases_are_rejected_even_for_one_package(self):
        directory = self.root / "LongDirectoryName"
        directory.mkdir()
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(str(directory), buffer, len(buffer))
        self.assertGreater(length, 0)
        self.assertLess(length, len(buffer))
        short = Path(buffer.value).name
        self.assertNotEqual(short.casefold(), directory.name.casefold(),
                            "native alias fixture requires DOS 8.3 creation on TEMP volume")
        for targets in ((short,), (short + "/new.py",),
                        (directory.name, short + "/new.py"),
                        (short + "/new.py", directory.name)):
            self.assertTrue(any("alias conflict" in e for e in self.aliases(*targets)))
        self.assertEqual(self.aliases(directory.name + "/new.py"), [])

    def test_active_normalized_name_query_failure_is_closed(self):
        (self.root / "file").write_bytes(b"data")
        with mock.patch.object(state.fs, "canonical_component", side_effect=OSError("name unavailable")):
            self.assertTrue(any("cannot inspect active directory write-set safely" in e
                                for e in self.aliases("file")))

    def test_active_write_sets_reject_junction_leaf_and_ancestor_without_enumerating(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"never inspect")
        (self.root / "safe").mkdir()
        self.junction(self.root / "safe/link", outside)
        original = state.fs.listdir
        enumerated = []
        def names(fd):
            enumerated.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return original(fd)
        with mock.patch.object(state.fs, "listdir", side_effect=names):
            for target in ("safe", "safe/link/secret"):
                self.assertTrue(any("symlink alias" in e for e in self.aliases(target)))
        info = outside.stat()
        self.assertNotIn((info.st_dev, info.st_ino), enumerated)

    def test_active_write_sets_reject_symlinks_and_descendant_hardlinks(self):
        directory = self.root / "safe"
        directory.mkdir()
        for target in ("missing", "../outside"):
            link = directory / "link"
            link.symlink_to(target)
            self.assertTrue(any("symlink alias" in e for e in self.aliases("safe")))
            link.unlink()
        outside = self.base / "external"
        outside.mkdir()
        (directory / "link").symlink_to(outside, target_is_directory=True)
        self.assertTrue(any("escapes workspace through symlink" in e for e in self.aliases("safe")))
        (directory / "link").unlink()
        original = directory / "file"
        original.write_bytes(b"data")
        (self.base / "alias").hardlink_to(original)
        self.assertTrue(any("hardlink conflict" in e for e in self.aliases("safe")))

    def test_active_write_set_replacement_between_stat_and_open_fails(self):
        target = self.root / "file"
        target.write_bytes(b"before")
        original = state.fs.open
        changed = False
        def opening(name, flags, *args, **kwargs):
            nonlocal changed
            if name == "file" and not changed:
                changed = True
                target.rename(self.root / "old")
                target.write_bytes(b"after")
            return original(name, flags, *args, **kwargs)
        with mock.patch.object(state.fs, "open", side_effect=opening):
            self.assertTrue(any("cannot inspect active directory write-set safely" in e
                                for e in self.aliases("file")))

    def test_active_write_set_directory_mutation_fails(self):
        directory = self.root / "safe"
        directory.mkdir()
        original = state.fs.listdir
        changed = False
        def names(fd):
            nonlocal changed
            result = original(fd)
            if not changed:
                changed = True
                (directory / "new").write_bytes(b"data")
            return result
        with mock.patch.object(state.fs, "listdir", side_effect=names):
            self.assertTrue(any("cannot inspect active directory write-set safely" in e
                                for e in self.aliases("safe")))

    def test_late_write_set_hardlink_fails_query_and_cas_without_mutation(self):
        from test_longtask_state import LongtaskStateTests
        directory = self.root / "safe"
        directory.mkdir()
        target = directory / "file"
        target.write_bytes(b"data")
        initial = self.init()
        package = LongtaskStateTests.package("active", status="active", write_set=["safe"],
                                            owner="worker", base_revision=initial["artifact_digest"])
        active = self.cli("package", "--expected-task-id", initial["task_id"],
                          "--expected-revision", str(initial["revision"]),
                          "--data", json.dumps(package), "--actor", "worker")
        (self.base / "alias").hardlink_to(target)
        before = (self.root / ".longtask/state.json").read_bytes()
        query = self.cli("context")
        self.assertEqual(query["route"]["entry"], "error")
        self.assertIn("hardlink conflict", query["route"]["reason"])
        self.assertIn("hardlink conflict", self.checkpoint(active, ok=False)["error"])
        self.assertEqual((self.root / ".longtask/state.json").read_bytes(), before)

    def test_active_write_set_hardlink_inserted_in_open_window_fails(self):
        target = self.root / "file"
        target.write_bytes(b"data")
        original = state.fs.open
        changed = False
        def opening(name, flags, *args, **kwargs):
            nonlocal changed
            if name == "file" and not changed:
                changed = True
                (self.base / "alias").hardlink_to(target)
            return original(name, flags, *args, **kwargs)
        with mock.patch.object(state.fs, "open", side_effect=opening):
            self.assertTrue(any("cannot inspect active directory write-set safely" in e
                                for e in self.aliases("file")))
        self.assertEqual(target.read_bytes(), b"data")

    def checkpoint(self, initial, ok=True):
        return self.cli("checkpoint", "--expected-task-id", initial["task_id"],
                        "--expected-revision", str(initial["revision"]), ok=ok)

    def child(self, code, *args):
        return subprocess.run([sys.executable, "-c", code, str(ROOT / "scripts"),
                               str(self.root), *args], capture_output=True, text=True, encoding="utf-8")

    def test_snapshot_binary_unicode_and_runtime_exclusion(self):
        directory = self.root / "中文"
        directory.mkdir()
        product = directory / "数据.bin"
        product.write_bytes(b"\r\n\x1a\0\xff" * 1000)
        first = state.workspace_manifest(self.root)
        self.assertEqual(set(first["files"]), {"中文/数据.bin"})
        self.runtime_file("state.json")
        self.assertEqual(state.workspace_manifest(self.root), first)
        product.write_bytes(b"changed\r\n\x1a\0")
        self.assertNotEqual(state.workspace_manifest(self.root), first)

    def test_fallback_runtime_exclusion_accepts_native_directory_case(self):
        (self.root / ".LONGTASK").mkdir()
        (self.root / ".LONGTASK/state.json").write_bytes(b"not a workspace artifact")
        self.assertEqual(state.workspace_manifest(self.root)["files"], {})

    def test_snapshot_symlink_identity_never_reads_external_target(self):
        outside = self.base / "outside"
        outside.write_bytes(b"first")
        link = self.root / "link"
        link.symlink_to(outside)
        first = state.artifact_digest(self.root)
        outside.write_bytes(b"second")
        self.assertEqual(state.artifact_digest(self.root), first)
        link.unlink()
        link.symlink_to("missing-target")
        self.assertNotEqual(state.artifact_digest(self.root), first)
        # The permissive observation entrypoint must not affect runtime stat.
        parent = state.open_directory_path(self.root, create=False)
        try:
            with self.assertRaises(OSError):
                state.fs.stat("link", dir_fd=parent)
        finally:
            os.close(parent)

    def test_directory_symlink_is_identity_and_never_recursed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret").write_bytes(b"first")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        first = state.workspace_manifest(self.root)
        self.assertEqual(set(first["files"]), {"link"})
        (outside / "secret").write_bytes(b"second")
        self.assertEqual(state.workspace_manifest(self.root), first)

    def test_fallback_snapshot_rejects_junction_without_following(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "secret"
        sentinel.write_bytes(b"untouched")
        self.junction(self.root / "junction", outside)
        with self.assertRaises((OSError, state.StateError)):
            state.workspace_manifest(self.root)
        self.assertEqual(sentinel.read_bytes(), b"untouched")

    def test_cached_descendant_below_junction_is_rejected(self):
        directory = self.root / "tracked"
        directory.mkdir()
        (directory / "product").write_bytes(b"inside")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True, capture_output=True)
        (directory / "product").unlink()
        directory.rmdir()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "product").write_bytes(b"outside")
        self.junction(directory, outside)
        with self.assertRaisesRegex(state.StateError, "unsafe workspace artifact ancestor"):
            state.artifact_digest(self.root)
        directory.rmdir()
        state.artifact_digest(self.root)  # A cached ordinary deletion is allowed.

    def test_artifact_parent_pins_directory_during_observation(self):
        directory = self.root / "nested"
        directory.mkdir()
        with state.artifact_parent(self.root, directory / "product") as parent:
            with self.assertRaises(OSError):
                directory.rename(self.root / "moved")
            self.assertEqual(state.fs.listdir(parent), [])

    def test_runtime_rejects_junction_and_preserves_external_content(self):
        outside = self.base / "outside"
        outside.mkdir()
        sentinel = outside / "state.json"
        sentinel.write_bytes(b"external")
        self.junction(self.root / ".longtask", outside)
        with self.assertRaises((OSError, state.StateError)):
            state.active_state_path(self.root)
        self.assertEqual(sentinel.read_bytes(), b"external")

    def test_runtime_hardlink_and_unknown_files_are_preserved(self):
        path = self.runtime_file("state.json")
        os.link(path, self.base / "alias")
        with self.assertRaisesRegex(state.StateError, "hardlink"):
            state.check_runtime_directory(path.parent)
        (self.base / "alias").unlink()
        unknown = path.parent / "user-data"
        unknown.write_bytes(b"preserve")
        with self.assertRaisesRegex(state.StateError, "unknown checkpoint file"):
            state.check_runtime_directory(path.parent)
        self.assertEqual(unknown.read_bytes(), b"preserve")

    def test_runtime_temporary_recovery_requires_private_acl(self):
        name = ".state.json.write-" + "a" * 24
        temporary = self.runtime_file(name)
        with self.assertRaisesRegex(state.StateError, "interrupted checkpoint atomic write"):
            state.check_runtime_directory(temporary.parent)
        self.assertTrue(temporary.exists())
        self.assertEqual(state.check_runtime_directory(temporary.parent, reclaim_temporary=True), set())
        self.assertFalse(temporary.exists())
        inherited = self.runtime_file(name, mode=0o644)
        with self.assertRaisesRegex(state.StateError, "unexpected ownership or permissions"):
            state.check_runtime_directory(inherited.parent, reclaim_temporary=True)
        self.assertTrue(inherited.exists())

    def test_runtime_markers_and_flush_errors_remain_fail_closed(self):
        self.assertEqual(state.active_state_path(self.root), self.root / ".longtask/state.json")
        marker = self.runtime_file(".initializing.json")
        with self.assertRaisesRegex(state.StateError, "initialization interrupted"):
            state.active_state_path(self.root)
        with mock.patch.object(state.fs, "fsync_directory", side_effect=OSError("flush failed")):
            with self.assertRaisesRegex(OSError, "flush failed"):
                state.fsync_directory(marker.parent)

    def test_init_context_checkpoint_cas_and_same_major_recovery(self):
        initial = self.init()
        context = self.cli("context", "--view", "default", "--resume-choice", "resume")
        self.assertEqual(context["binding"]["task_id"], initial["task_id"])
        self.assertTrue(context["recovery"]["selection_available"])
        current = self.checkpoint(initial)
        self.assertEqual(current["revision"], 1)
        before = (self.root / ".longtask/state.json").read_bytes()
        self.assertIn("stale revision", self.checkpoint(initial, ok=False)["error"])
        self.assertEqual((self.root / ".longtask/state.json").read_bytes(), before)
        current["skill_version"] = "4.0.0"
        state.atomic_write(self.root / ".longtask/state.json", json.dumps(current))
        self.assertEqual(self.cli("context")["binding"]["revision"], 1)
        self.assertEqual(self.checkpoint(current)["skill_version"], "4.0.0")

    def test_cli_json_is_utf8_with_ansi_python_defaults(self):
        goal = "中文 é 🐾"
        env = {key: value for key, value in os.environ.items()
               if key not in {"PYTHONUTF8", "PYTHONIOENCODING"}}
        command = [sys.executable, "-X", "utf8=0", str(ROOT / "scripts/longtask_state.py"),
                   "init", "--root", str(self.root), "--task-id", "unicode",
                   "--mode", "continue", "--goal", goal]
        result = subprocess.run(command, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.decode("utf-8"))["goal"], goal)
        stored = (self.root / ".longtask/state.json").read_bytes()
        self.assertEqual(json.loads(stored.decode("utf-8"))["goal"], goal)

    def test_lock_contention_identity_split_and_process_exit_release(self):
        initial = self.init()
        code = """import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import longtask_state as s
with s.state_lock(Path(sys.argv[2])/'.longtask'):
 print('locked',flush=True)
 sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(ROOT / "scripts"), str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8")
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            lock = self.root / ".longtask/.state.lock"
            with self.assertRaises(OSError):
                lock.rename(lock.with_name("split"))
            self.assertIn("busy", self.checkpoint(initial, ok=False)["error"])
            process.kill()
            process.communicate(timeout=15)
            self.assertEqual(self.checkpoint(initial)["revision"], 1)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)

    def test_killed_atomic_publish_reclaims_only_private_temporary_under_lock(self):
        initial = self.init()
        path = self.root / ".longtask/state.json"
        before = path.read_bytes()
        code = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import longtask_state as s
s.fs.rename_open=lambda *a,**k: os._exit(71)
s.atomic_write(Path(sys.argv[2])/'.longtask/state.json','unpublished')
"""
        crashed = self.child(code)
        self.assertEqual(crashed.returncode, 71, crashed.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(list(path.parent.glob(".state.json.write-*"))), 1)
        self.assertEqual(self.cli("context")["route"]["entry"], "error")
        self.assertEqual(self.checkpoint(initial)["revision"], 1)
        self.assertEqual(list(path.parent.glob(".state.json.write-*")), [])

    def test_event_crash_windows_reconcile_without_duplicate_or_truncation(self):
        for window in ("before-event", "after-event"):
            with self.subTest(window=window):
                root = self.root / window
                root.mkdir()
                original_root = self.root
                self.root = root
                try:
                    initial = self.init()
                    code = """import os,sys
sys.path.insert(0,sys.argv[1])
import longtask_state as s
if sys.argv[4]=='before-event':
 s.append_event=lambda *a,**k: os._exit(72)
else:
 original=s.atomic_write
 calls=0
 def write(*a,**k):
  global calls
  calls+=1
  if calls==2: os._exit(72)
  return original(*a,**k)
 s.atomic_write=write
args=s.parser().parse_args(['checkpoint','--root',sys.argv[2],
 '--expected-task-id',sys.argv[3],'--expected-revision','0'])
args.function(args)
"""
                    crashed = self.child(code, initial["task_id"], window)
                    self.assertEqual(crashed.returncode, 72, crashed.stderr)
                    path = self.root / ".longtask/state.json"
                    current = json.loads(path.read_text(encoding="utf-8"))
                    self.assertEqual((current["revision"], current["event_revision"]), (1, 0))
                    self.assertEqual(self.cli("context")["route"]["entry"], "error")
                    reconciled = self.cli("reconcile-events", "--expected-task-id", initial["task_id"],
                                          "--expected-revision", "1", "--reason", "Crash recovery")
                    self.assertEqual(reconciled["event_revision"], 1)
                    events = [json.loads(line) for line in (path.parent / "events.jsonl").read_text(encoding="utf-8").splitlines()]
                    self.assertEqual([item["revision"] for item in events], [0, 1])
                    self.assertEqual(events[-1]["event"], "event_gap_reconciled" if window == "before-event" else "checkpointed")
                    (path.parent / "events.jsonl").write_bytes(b'{"partial":')
                    self.assertEqual(self.cli("context")["route"]["entry"], "error")
                    self.assertEqual((path.parent / "events.jsonl").read_bytes(), b'{"partial":')
                finally:
                    self.root = original_root

    def test_atomic_write_and_read_reject_hardlink_aliases(self):
        path = self.runtime_file("state.json", b"private")
        os.link(path, self.base / "alias")
        with self.assertRaises((state.StateError, OSError)):
            state.atomic_write(path, "forbidden")
        with self.assertRaises((state.StateError, OSError)):
            state.runtime_read(path)
        self.assertEqual((self.base / "alias").read_bytes(), b"private")

    def test_atomic_write_collision_preserves_preexisting_temporary(self):
        name = ".state.json.write-" + "a" * 24
        existing = self.runtime_file(name, b"preserve")
        with mock.patch.object(state.secrets, "token_hex", return_value="a" * 24):
            with self.assertRaises(FileExistsError):
                state.atomic_write(existing.parent / "state.json", "forbidden")
        self.assertEqual(existing.read_bytes(), b"preserve")

    def test_runtime_read_rejects_hardlink_inserted_between_inspection_and_open(self):
        path = self.runtime_file("state.json", b"private")
        original = state.fs.open

        def insert_alias(name, flags, *args, **kwargs):
            if name == "state.json":
                os.link(path, self.base / "late-alias")
            return original(name, flags, *args, **kwargs)

        with mock.patch.object(state.fs, "open", side_effect=insert_alias):
            with self.assertRaisesRegex(state.StateError, "changed identity"):
                state.runtime_read(path)
        self.assertEqual((self.base / "late-alias").read_bytes(), b"private")

    def test_finish_revalidates_both_cleanup_windows_and_keeps_lock_identity(self):
        import test_longtask_state as fixtures

        for name in ("test_finish_removes_execution_history_and_next_init_has_no_archive",
                     "test_finish_interrupted_before_journal_delete_revalidates_and_retries",
                     "test_finish_interrupted_after_journal_delete_revalidates_and_retries"):
            with self.subTest(scenario=name):
                case = fixtures.LongtaskStateTests(name)
                result = unittest.TestResult()
                case.run(result)
                self.assertFalse(result.errors or result.failures, result.errors + result.failures)


if __name__ == "__main__":
    unittest.main()
