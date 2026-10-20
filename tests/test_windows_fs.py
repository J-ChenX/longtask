"""Native boundary tests; these must run on Windows rather than mocked os.name."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if os.name == "nt":
    spec = importlib.util.spec_from_file_location("windows_fs", ROOT / "scripts/windows_fs.py")
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)


@unittest.skipUnless(os.name == "nt", "requires native Windows filesystem handles")
class WindowsFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="longtask-native-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.parent = fs.open_directory_path(self.root)
        self.addCleanup(os.close, self.parent)

    def write(self, name, data=b"original", mode=0o600):
        fd = fs.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL, mode, dir_fd=self.parent)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)

    def junction(self, name, target):
        result = subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(self.root / name),
                                 str(target)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(self.root / name) if (self.root / name).exists() else None)

    def test_binary_reads_preserve_crlf_control_z_and_nul(self):
        content = b"before\r\n\x1a\0after\n" * 1000
        self.write("binary", content)
        fd = fs.open("binary", os.O_RDONLY, dir_fd=self.parent)
        try:
            self.assertEqual(os.read(fd, len(content) + 1), content)
        finally:
            os.close(fd)

    def test_relative_replace_no_clobber_and_flush(self):
        self.write("state", b"old")
        self.write("temporary", b"new")
        with self.assertRaises(FileExistsError):
            fs.rename("temporary", "state", src_dir_fd=self.parent, dst_dir_fd=self.parent)
        self.assertEqual((self.root / "state").read_bytes(), b"old")
        fs.replace("temporary", "state", src_dir_fd=self.parent, dst_dir_fd=self.parent)
        fs.fsync_directory(self.parent)
        self.assertEqual((self.root / "state").read_bytes(), b"new")
        self.assertFalse((self.root / "temporary").exists())

    def test_directory_enumeration_restarts_and_preserves_unicode(self):
        names = {f"entry-{i:04d}" for i in range(850)} | {"中文-é.txt"}
        for name in names:
            self.write(name, b"")
        self.assertEqual(set(fs.listdir(self.parent)), names)
        self.assertEqual(set(fs.listdir(self.parent)), names)

    def test_directory_create_rename_delete_and_missing(self):
        fs.mkdir("staging", 0o700, dir_fd=self.parent)
        fs.rename("staging", "final", src_dir_fd=self.parent, dst_dir_fd=self.parent)
        fd = fs.open("final", fs.O_DIRECTORY, dir_fd=self.parent)
        try:
            fs.fsync_directory(fd)
        finally:
            os.close(fd)
        fs.rmdir("final", dir_fd=self.parent)
        with self.assertRaises(FileNotFoundError):
            fs.stat("final", dir_fd=self.parent)

    def test_private_acl_and_inherited_acl_are_distinguished(self):
        self.write("private")
        self.write("inherited", mode=0o644)
        for name, expected in (("private", True), ("inherited", False)):
            fd = fs.open(name, os.O_RDONLY, dir_fd=self.parent)
            try:
                self.assertEqual(fs.is_private(fd), expected)
            finally:
                os.close(fd)

    def test_retained_staging_handle_can_publish_without_reopening_source(self):
        staging = fs.open("staging", fs.O_DIRECTORY | fs.O_DELETE | os.O_CREAT | os.O_EXCL,
                          0o700, dir_fd=self.parent)
        try:
            fd = fs.open("content", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=staging)
            os.close(fd)
            identity = os.fstat(staging).st_ino
            fs.fsync_directory(staging)
            fs.rename_open(staging, "published", dst_dir_fd=self.parent)
            self.assertEqual(fs.stat("published", dir_fd=self.parent).st_ino, identity)
            self.assertEqual(fs.listdir(staging), ["content"])
            self.assertTrue((self.root / "published" / "content").is_file())
            fs.fsync_directory(self.parent)
        finally:
            os.close(staging)

    def test_hardlink_write_truncate_rename_and_delete_fail_without_mutation(self):
        self.write("original")
        os.link(self.root / "original", self.root / "linked")
        operations = [
            lambda: fs.open("linked", os.O_WRONLY | os.O_TRUNC, dir_fd=self.parent),
            lambda: fs.unlink("linked", dir_fd=self.parent),
            lambda: fs.rename("linked", "moved", src_dir_fd=self.parent, dst_dir_fd=self.parent),
        ]
        for operation in operations:
            with self.assertRaises(OSError):
                operation()
            self.assertEqual((self.root / "original").read_bytes(), b"original")

    def test_junction_traversal_and_deletion_preserve_outside(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "sentinel").write_bytes(b"untouched")
        self.junction("junction", outside)
        for operation in (
            lambda: fs.open_directory_path(self.root / "junction" / "new", create=True),
            lambda: fs.open("junction", fs.O_DIRECTORY, dir_fd=self.parent),
            lambda: fs.rmdir("junction", dir_fd=self.parent),
            lambda: fs.stat("junction", dir_fd=self.parent),
        ):
            with self.assertRaises(OSError):
                operation()
        self.assertEqual(list(outside.iterdir()), [outside / "sentinel"])
        self.assertEqual((outside / "sentinel").read_bytes(), b"untouched")

    def test_pinned_directory_prevents_replacement_and_ancestor_rename(self):
        fs.mkdir("pinned", dir_fd=self.parent)
        child = fs.open("pinned", fs.O_DIRECTORY, dir_fd=self.parent)
        try:
            with self.assertRaises(OSError):
                os.rename(self.root / "pinned", self.root / "moved")
            with self.assertRaises(OSError):
                os.rename(self.root, self.root.with_name(self.root.name + "-moved"))
            fd = fs.open("safe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=child)
            os.close(fd)
            self.assertTrue((self.root / "pinned" / "safe").is_file())
        finally:
            os.close(child)

    def test_deep_handle_pins_ancestor_without_an_extra_ancestor_handle(self):
        (self.root / "ancestor" / "deep").mkdir(parents=True)
        child = fs.open_directory_path(self.root / "ancestor" / "deep")
        try:
            with self.assertRaises(OSError):
                os.rename(self.root / "ancestor", self.root / "moved")
            fd = fs.open("safe", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600, dir_fd=child)
            os.close(fd)
            self.assertTrue((self.root / "ancestor" / "deep" / "safe").is_file())
        finally:
            os.close(child)

    def test_hardlink_inserted_during_open_is_rejected_before_truncation(self):
        self.write("original")
        actual_open = fs._open_handle

        def insert_link(parent, name, **kwargs):
            result = actual_open(parent, name, **kwargs)
            if name == "original":
                os.link(self.root / "original", self.root / "late-link")
                # The open-time metadata must be queried after the race, not reused.
            return result

        with mock.patch.object(fs, "_open_handle", side_effect=insert_link):
            with self.assertRaises(OSError):
                fd = fs.open("original", os.O_RDWR | os.O_TRUNC, dir_fd=self.parent)
                os.close(fd)
        self.assertEqual((self.root / "late-link").read_bytes(), b"original")

    def test_subst_root_cannot_hide_junction_ancestor(self):
        outside = self.root / "outside"
        (outside / "nested").mkdir(parents=True)
        self.junction("junction", outside)
        used = {drive[:2].casefold() for drive in os.listdrives()}
        drive = next((chr(letter) + ":" for letter in range(ord("Z"), ord("D"), -1)
                      if (chr(letter) + ":").casefold() not in used), None)
        if drive is None:
            self.skipTest("no unused drive for isolated SUBST fixture")
        subprocess.run(["subst", drive, str(self.root / "junction" / "nested")], check=True,
                       capture_output=True)
        try:
            with self.assertRaises(OSError):
                fd = fs.open_directory_path(drive + "\\", create=True)
                os.close(fd)
            self.assertEqual(list((outside / "nested").iterdir()), [])
        finally:
            subprocess.run(["subst", drive, "/D"], check=True, capture_output=True)

    def test_open_file_prevents_lock_identity_split_until_process_exit(self):
        self.write("lock", b"0")
        code = """import sys, os, msvcrt
sys.path.insert(0, sys.argv[1])
import windows_fs as fs
fd = fs.open(sys.argv[2], os.O_RDWR)
msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
print('locked', flush=True)
sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-u", "-c", code, str(ROOT / "scripts"),
            str(self.root / "lock")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(OSError):
                fs.unlink("lock", dir_fd=self.parent)
            import msvcrt
            fd = fs.open("lock", os.O_RDWR, dir_fd=self.parent)
            try:
                with self.assertRaises(OSError):
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            finally:
                os.close(fd)
            process.kill()
            process.wait(timeout=10)
            fd = fs.open("lock", os.O_RDWR, dir_fd=self.parent)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(fd)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_unsafe_components_and_remote_roots_are_rejected(self):
        for name in ("../out", "..", "CON", "a:b", "a\\b", "name.", "name ", "a\0b"):
            with self.assertRaises(OSError):
                fs.open(name, os.O_WRONLY | os.O_CREAT, dir_fd=self.parent)
        for path in (r"\\server\share\dir", r"\\?\C:\dir"):
            with self.assertRaises(OSError):
                fs.open_directory_path(path, create=True)

    def test_flush_failure_is_reported(self):
        with mock.patch.object(fs, "_flush", return_value=0):
            with self.assertRaises(OSError):
                fs.fsync_directory(self.parent)


if __name__ == "__main__":
    unittest.main()
