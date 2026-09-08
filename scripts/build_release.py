#!/usr/bin/env python3
"""Build and verify the deterministic longtask release archive."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
EMBEDDED_MANIFEST = "RELEASE-MANIFEST.json"
MANIFEST_NAME = "release-manifest.json"
ALLOWED_MANIFEST_FIELDS = {
    "schema_version",
    "package",
    "version",
    "archive_root",
    "files",
    "directories",
    "exclude_paths",
    "exclude_names",
    "exclude_suffixes",
}
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 512
MAX_MEMBER_BYTES = 8 * 1024 * 1024
WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)


class ReleaseError(RuntimeError):
    """A deterministic, user-facing release validation failure."""


class SourceFile(NamedTuple):
    relative: str
    data: bytes
    mode: int


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def trusted_sha256(value: str | None, *, required: bool) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value) is None:
        raise ReleaseError("a trusted SHA-256 digest is required in canonical hexadecimal form")
    return value.removeprefix("sha256:")


def clean_relative_path(raw: str, field: str) -> str:
    if not isinstance(raw, str) or not raw:
        raise ReleaseError(f"{field} entries must be non-empty strings")
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in raw
        or str(path) != raw
        or raw.endswith("/")
    ):
        raise ReleaseError(f"unsafe or non-canonical {field} path: {raw!r}")
    for part in path.parts:
        if ":" in part or part.rstrip(" .") != part or WINDOWS_RESERVED.fullmatch(part):
            raise ReleaseError(f"non-portable {field} path: {raw!r}")
    return raw


def clean_archive_root(raw: object) -> str:
    if not isinstance(raw, str):
        raise ReleaseError("manifest archive_root must be a safe single path component")
    clean_relative_path(raw, "archive_root")
    if len(PurePosixPath(raw).parts) != 1 or raw in {".", ".."}:
        raise ReleaseError("manifest archive_root must be a safe single path component")
    return raw


def string_list(manifest: dict[str, Any], field: str) -> list[str]:
    value = manifest.get(field)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ReleaseError(f"manifest field {field!r} must be an array of strings")
    if len(value) != len(set(value)):
        raise ReleaseError(f"manifest field {field!r} contains duplicates")
    return value


def load_manifest(root: Path) -> dict[str, Any]:
    try:
        source = secure_read_source(root, MANIFEST_NAME)
        manifest = json.loads(source.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read {MANIFEST_NAME}: {exc}") from exc
    if not isinstance(manifest, dict) or set(manifest) != ALLOWED_MANIFEST_FIELDS:
        raise ReleaseError("release manifest fields do not match the closed schema")
    if manifest.get("schema_version") != 1:
        raise ReleaseError("unsupported release manifest schema_version")
    for field in ("package", "version", "archive_root"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ReleaseError(f"manifest field {field!r} must be a non-empty string")
    clean_archive_root(manifest["archive_root"])
    if manifest["package"] != manifest["archive_root"]:
        raise ReleaseError("package and archive_root must match")
    for field in ("files", "directories", "exclude_paths"):
        for raw in string_list(manifest, field):
            clean_relative_path(raw, field)
    for field in ("exclude_names", "exclude_suffixes"):
        for value in string_list(manifest, field):
            if not value or "/" in value or "\\" in value:
                raise ReleaseError(f"invalid {field} entry: {value!r}")
    if MANIFEST_NAME not in manifest["files"]:
        raise ReleaseError(f"{MANIFEST_NAME} must include itself in the release closure")
    return manifest


def excluded(relative: str, manifest: dict[str, Any]) -> bool:
    path = PurePosixPath(relative)
    if any(relative == item or relative.startswith(item + "/") for item in manifest["exclude_paths"]):
        return True
    if any(part in set(manifest["exclude_names"]) for part in path.parts):
        return True
    return any(relative.endswith(suffix) for suffix in manifest["exclude_suffixes"])


def secure_open(root: Path, relative: str, *, directory: bool = False) -> int:
    """Open a release path component-by-component without following symlinks."""
    if (
        os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK")
    ):
        raise ReleaseError("secure release source traversal is not supported on this platform")
    parts = PurePosixPath(clean_relative_path(relative, "release source")).parts
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        current_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise ReleaseError(f"cannot open release root securely: {exc}") from exc
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(part, directory_flags, dir_fd=current_fd)
            except OSError as exc:
                raise ReleaseError(f"release source has an unsafe ancestor: {relative}: {exc}") from exc
            os.close(current_fd)
            current_fd = next_fd
        try:
            expected = os.stat(parts[-1], dir_fd=current_fd, follow_symlinks=False)
        except OSError as exc:
            raise ReleaseError(f"cannot inspect release source securely: {relative}: {exc}") from exc
        expected_type = stat.S_ISDIR(expected.st_mode) if directory else stat.S_ISREG(expected.st_mode)
        if not expected_type:
            expected_label = "directory" if directory else "regular file"
            raise ReleaseError(f"release source must be a {expected_label}: {relative}")
        flags = directory_flags if directory else os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            descriptor = os.open(parts[-1], flags, dir_fd=current_fd)
        except OSError as exc:
            raise ReleaseError(f"cannot open release source securely: {relative}: {exc}") from exc
        opened = os.fstat(descriptor)
        if (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise ReleaseError(f"release source changed before it was opened: {relative}")
        return descriptor
    finally:
        os.close(current_fd)


def secure_read_source(root: Path, relative: str) -> SourceFile:
    descriptor = secure_open(root, relative)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseError(f"release source must be a regular file: {relative}")
        if before.st_nlink != 1:
            raise ReleaseError(f"release source must have exactly one hard link: {relative}")
        if before.st_size > MAX_MEMBER_BYTES:
            raise ReleaseError(f"release source exceeds the per-file size limit: {relative}")
        chunks: list[bytes] = []
        remaining = MAX_MEMBER_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        if sum(map(len, chunks)) > MAX_MEMBER_BYTES:
            raise ReleaseError(f"release source exceeds the per-file size limit while reading: {relative}")
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if identity_before != identity_after:
            raise ReleaseError(f"release source changed while being read: {relative}")
        data = b"".join(chunks)
        if len(data) != before.st_size:
            raise ReleaseError(f"release source size changed while being read: {relative}")
        mode = 0o755 if before.st_mode & 0o111 else 0o644
        return SourceFile(relative=relative, data=data, mode=mode)
    finally:
        os.close(descriptor)


def secure_check_directory(root: Path, relative: str) -> None:
    descriptor = secure_open(root, relative, directory=True)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ReleaseError(f"release directory must be a real directory: {relative}")
    finally:
        os.close(descriptor)


def collect_sources(root: Path, manifest: dict[str, Any]) -> list[SourceFile]:
    selected: set[str] = set()
    for relative in manifest["files"]:
        if excluded(relative, manifest):
            raise ReleaseError(f"explicit release file is excluded: {relative}")
        selected.add(relative)
    if len(selected) + 1 > MAX_ARCHIVE_MEMBERS:
        raise ReleaseError("release closure has too many files")
    for directory in manifest["directories"]:
        secure_check_directory(root, directory)
        directory_path = root / directory
        try:
            def enumeration_error(error: OSError) -> None:
                raise error

            for current, directory_names, file_names in os.walk(
                directory_path, followlinks=False, onerror=enumeration_error,
            ):
                current_path = Path(current)
                directory_names[:] = [
                    name for name in directory_names
                    if not excluded((current_path / name).relative_to(root).as_posix(), manifest)
                ]
                for name in sorted(directory_names):
                    child = current_path / name
                    if child.is_symlink():
                        raise ReleaseError(f"release closure contains a directory symlink: {child.relative_to(root)}")
                for name in sorted(file_names):
                    path = current_path / name
                    relative = path.relative_to(root).as_posix()
                    if excluded(relative, manifest):
                        continue
                    if relative in selected:
                        raise ReleaseError(f"release source selected more than once: {relative}")
                    selected.add(relative)
                    if len(selected) + 1 > MAX_ARCHIVE_MEMBERS:
                        raise ReleaseError("release closure has too many files")
        except OSError as exc:
            raise ReleaseError(f"cannot enumerate release directory {directory}: {exc}") from exc
    sources: list[SourceFile] = []
    total_size = 0
    for relative in sorted(selected):
        source = secure_read_source(root, relative)
        total_size += len(source.data)
        if total_size > MAX_ARCHIVE_BYTES:
            raise ReleaseError("release source closure exceeds the total size limit")
        sources.append(source)
    release_manifest = next(source for source in sources if source.relative == MANIFEST_NAME)
    try:
        snapshotted_manifest = json.loads(release_manifest.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"snapshotted {MANIFEST_NAME} is invalid: {exc}") from exc
    if snapshotted_manifest != manifest:
        raise ReleaseError(f"{MANIFEST_NAME} changed while the release closure was collected")
    return sources


def source_entries(sources: list[SourceFile]) -> list[dict[str, Any]]:
    return [
        {
            "mode": source.mode,
            "path": source.relative,
            "sha256": sha256_bytes(source.data),
            "size": len(source.data),
        }
        for source in sources
    ]


def embedded_manifest(manifest: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "archive_root": manifest["archive_root"],
        "format": "longtask-release-v1",
        "package": manifest["package"],
        "source_entries": entries,
        "source_tree_sha256": sha256_bytes(canonical_json(entries)),
        "version": manifest["version"],
    }


def zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    info.flag_bits = 0x800
    return info


def archive_bytes(root: Path) -> tuple[bytes, dict[str, Any]]:
    root = root.resolve(strict=True)
    manifest = load_manifest(root)
    sources = collect_sources(root, manifest)
    entries = source_entries(sources)
    embedded = embedded_manifest(manifest, entries)
    with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as stream:
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, strict_timestamps=True) as archive:
            prefix = manifest["archive_root"]
            members = [
                (f"{prefix}/{source.relative}", source.mode, source.data)
                for source in sources
            ]
            members.append((f"{prefix}/{EMBEDDED_MANIFEST}", 0o644, canonical_json(embedded)))
            for name, mode, data in sorted(members):
                archive.writestr(zip_info(name, mode), data)
        stream.seek(0)
        return stream.read(), embedded


def build(root: Path, output: Path) -> dict[str, Any]:
    data, embedded = archive_bytes(root)
    write_archive(output, data)
    return {
        "archive": str(output),
        "archive_sha256": sha256_bytes(data),
        "file_count": len(embedded["source_entries"]),
        "source_tree_sha256": embedded["source_tree_sha256"],
    }


def write_archive(output: Path, data: bytes) -> None:
    """Atomically write an archive without resolving output links or link ancestors."""
    if not output.name or output.name in {".", ".."}:
        raise ReleaseError("release output must name a file")
    parent_fd = open_directory_path(output.parent, create=True)
    temporary_name = f".{output.name}.build-{secrets.token_hex(12)}"
    try:
        try:
            existing = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise ReleaseError("release output must be a regular non-linked file")
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ReleaseError("short write while building release archive")
                view = view[written:]
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.rename(temporary_name, output.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_name = ""
        os.fsync(parent_fd)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def safe_archive_member(name: str, root_name: str) -> str:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or path.parts[0] != root_name:
        raise ReleaseError(f"unsafe archive member: {name!r}")
    relative = PurePosixPath(*path.parts[1:]).as_posix()
    if relative == EMBEDDED_MANIFEST:
        return relative
    return clean_relative_path(relative, "archive member")


def read_archive_bytes(archive_path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(archive_path, flags)
    except OSError as exc:
        raise ReleaseError(f"cannot open archive safely: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ReleaseError("archive must be a single-link regular file")
        if before.st_size > MAX_ARCHIVE_BYTES:
            raise ReleaseError("archive exceeds the configured size limit")
        chunks: list[bytes] = []
        remaining = MAX_ARCHIVE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        )
        if identity(before) != identity(after) or len(data) != before.st_size:
            raise ReleaseError("archive changed while being read")
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ReleaseError("archive exceeds the configured size limit")
        return data
    finally:
        os.close(descriptor)


def inspect_archive(archive_path: Path, expected_sha256: str | None = None) -> tuple[dict[str, Any], dict[str, bytes]]:
    archive_data = read_archive_bytes(archive_path)
    archive_digest = sha256_bytes(archive_data)
    expected_digest = trusted_sha256(expected_sha256, required=False)
    if expected_digest is not None and archive_digest != expected_digest:
        raise ReleaseError("archive SHA-256 does not match the trusted expected value")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_data), "r") as archive:
            infos = archive.infolist()
            if not infos:
                raise ReleaseError("release archive is empty")
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ReleaseError("release archive has too many members")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or names != sorted(names):
                raise ReleaseError("archive members must be unique and sorted")
            root_names = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
            if len(root_names) != 1:
                raise ReleaseError("archive must have exactly one root directory")
            root_name = next(iter(root_names))
            clean_archive_root(root_name)
            content: dict[str, bytes] = {}
            member_modes: dict[str, int] = {}
            total_size = 0
            for info in infos:
                relative = safe_archive_member(info.filename, root_name)
                if info.is_dir() or info.date_time != FIXED_TIMESTAMP or info.compress_type != zipfile.ZIP_STORED:
                    raise ReleaseError(f"non-deterministic archive metadata: {info.filename}")
                mode = (info.external_attr >> 16) & 0o777
                if mode not in {0o644, 0o755}:
                    raise ReleaseError(f"unexpected archive mode for {info.filename}: {mode:o}")
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ReleaseError(f"archive member exceeds the size limit: {info.filename}")
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise ReleaseError("release archive expands beyond the configured size limit")
                content[relative] = archive.read(info)
                member_modes[relative] = mode
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"cannot inspect release archive: {exc}") from exc
    manifest_data = content.pop(EMBEDDED_MANIFEST, None)
    member_modes.pop(EMBEDDED_MANIFEST, None)
    if manifest_data is None:
        raise ReleaseError(f"archive lacks {EMBEDDED_MANIFEST}")
    try:
        embedded = json.loads(manifest_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"invalid embedded manifest: {exc}") from exc
    expected_manifest_keys = {
        "archive_root", "format", "package", "source_entries", "source_tree_sha256", "version",
    }
    if (
        not isinstance(embedded, dict)
        or set(embedded) != expected_manifest_keys
        or embedded.get("format") != "longtask-release-v1"
    ):
        raise ReleaseError("unsupported embedded manifest")
    if embedded.get("archive_root") != root_name or embedded.get("package") != root_name:
        raise ReleaseError("embedded manifest root does not match archive root")
    entries = embedded.get("source_entries")
    if not isinstance(entries, list) or entries != sorted(entries, key=lambda item: item.get("path", "") if isinstance(item, dict) else ""):
        raise ReleaseError("embedded source entries must be a sorted array")
    expected_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"mode", "path", "sha256", "size"}:
            raise ReleaseError("invalid embedded source entry")
        if not isinstance(entry.get("path"), str):
            raise ReleaseError("embedded source path must be a string")
        relative = clean_relative_path(entry["path"], "embedded source")
        if type(entry.get("mode")) is not int or entry["mode"] not in {0o644, 0o755}:
            raise ReleaseError(f"invalid embedded source mode: {relative}")
        if type(entry.get("size")) is not int or entry["size"] < 0:
            raise ReleaseError(f"invalid embedded source size: {relative}")
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
            raise ReleaseError(f"invalid embedded source SHA-256: {relative}")
        if relative in expected_paths:
            raise ReleaseError(f"duplicate embedded source path: {relative}")
        expected_paths.add(relative)
        data = content.get(relative)
        if (
            data is None
            or entry["size"] != len(data)
            or entry["sha256"] != sha256_bytes(data)
            or entry["mode"] != member_modes.get(relative)
        ):
            raise ReleaseError(f"embedded source metadata mismatch: {relative}")
    if set(content) != expected_paths:
        raise ReleaseError("archive members do not match the embedded source closure")
    if embedded.get("source_tree_sha256") != sha256_bytes(canonical_json(entries)):
        raise ReleaseError("embedded source tree digest mismatch")
    embedded["archive_sha256"] = archive_digest
    return embedded, content


def verify(root: Path | None, archive_path: Path, expected_sha256: str | None) -> dict[str, Any]:
    expected_digest = trusted_sha256(expected_sha256, required=root is None)
    embedded, content = inspect_archive(archive_path, expected_digest)
    if root is not None:
        root = root.resolve(strict=True)
        manifest = load_manifest(root)
        sources = collect_sources(root, manifest)
        expected_entries = source_entries(sources)
        expected_embedded = embedded_manifest(manifest, expected_entries)
        comparable = {key: value for key, value in embedded.items() if key != "archive_sha256"}
        if comparable != expected_embedded:
            raise ReleaseError("archive manifest does not match the current source closure")
        for source in sources:
            if content[source.relative] != source.data:
                raise ReleaseError(f"archive bytes differ from source: {source.relative}")
        expected_bytes, _ = archive_bytes(root)
        if sha256_bytes(expected_bytes) != embedded["archive_sha256"]:
            raise ReleaseError("archive is not the deterministic build of the current source closure")
    return {
        "archive": str(archive_path),
        "archive_sha256": embedded["archive_sha256"],
        "file_count": len(embedded["source_entries"]),
        "source_tree_sha256": embedded["source_tree_sha256"],
        "source_verified": root is not None,
        "trusted_digest_verified": expected_digest is not None,
    }


def open_directory_path(path: Path, *, create: bool) -> int:
    """Open an absolute directory component-by-component without following links."""
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ReleaseError("secure release extraction is not supported on this platform")
    absolute = path.absolute()
    parts = absolute.parts
    if not parts or parts[0] != os.path.sep:
        raise ReleaseError("extraction destination must resolve from an absolute filesystem root")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(os.path.sep, flags)
    try:
        for part in parts[1:]:
            try:
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise ReleaseError(f"extraction destination is missing: {absolute}")
                os.mkdir(part, 0o755, dir_fd=descriptor)
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise ReleaseError(f"extraction destination contains a non-directory component: {absolute}")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise ReleaseError(f"cannot open extraction destination safely: {absolute}: {exc}") from exc
            opened = os.fstat(child)
            if (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(child)
                raise ReleaseError(f"extraction destination changed while opening: {absolute}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def create_directory_at(parent_fd: int, name: str, mode: int = 0o700) -> int:
    os.mkdir(name, mode, dir_fd=parent_fd)
    expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode) or (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(descriptor)
        raise ReleaseError(f"extraction directory changed while opening: {name}")
    return descriptor


def write_file_at(root_fd: int, relative: str, data: bytes, mode: int) -> None:
    parts = PurePosixPath(clean_relative_path(relative, "extraction member")).parts
    directory_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            try:
                child_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd,
                )
            except FileNotFoundError:
                child_fd = create_directory_at(directory_fd, part)
            except OSError as exc:
                raise ReleaseError(f"unsafe extraction ancestor: {relative}: {exc}") from exc
            opened = os.fstat(child_fd)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(child_fd)
                raise ReleaseError(f"extraction ancestor is not a directory: {relative}")
            os.close(directory_fd)
            directory_fd = child_fd
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(parts[-1], flags, mode, dir_fd=directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReleaseError(f"extraction target is not a regular file: {relative}")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ReleaseError(f"short write while extracting: {relative}")
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def remove_tree_at(parent_fd: int, name: str) -> None:
    """Best-effort no-follow cleanup for a private failed extraction tree."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for child in os.listdir(descriptor):
            metadata = os.stat(child, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                remove_tree_at(descriptor, child)
            else:
                os.unlink(child, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(name, dir_fd=parent_fd)


def extract(archive_path: Path, destination: Path, expected_sha256: str) -> dict[str, Any]:
    expected_digest = trusted_sha256(expected_sha256, required=True)
    embedded, content = inspect_archive(archive_path, expected_digest)
    root_name = embedded.get("archive_root")
    if not isinstance(root_name, str) or not root_name:
        raise ReleaseError("embedded archive_root is invalid")
    clean_archive_root(root_name)
    destination_fd = open_directory_path(destination, create=True)
    temporary_name = f".{root_name}.extract-{secrets.token_hex(12)}"
    temporary_fd: int | None = None
    modes = {entry["path"]: entry["mode"] for entry in embedded["source_entries"]}
    try:
        try:
            os.stat(root_name, dir_fd=destination_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ReleaseError(f"extraction target already exists: {destination / root_name}")
        temporary_fd = create_directory_at(destination_fd, temporary_name)
        for relative in sorted(content):
            write_file_at(temporary_fd, relative, content[relative], modes[relative])
        write_file_at(temporary_fd, EMBEDDED_MANIFEST, canonical_json({
            key: value for key, value in embedded.items() if key != "archive_sha256"
        }), 0o644)
        os.fsync(temporary_fd)
        os.rename(temporary_name, root_name, src_dir_fd=destination_fd, dst_dir_fd=destination_fd)
        os.fsync(destination_fd)
        temporary_name = ""
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name:
            remove_tree_at(destination_fd, temporary_name)
        os.close(destination_fd)
    target_root = destination.absolute() / root_name
    return {
        "archive_sha256": embedded["archive_sha256"],
        "destination": str(target_root),
        "file_count": len(content),
        "source_tree_sha256": embedded["source_tree_sha256"],
        "trusted_digest_verified": True,
    }


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    subcommands = command.add_subparsers(dest="command", required=True)
    build_parser = subcommands.add_parser("build")
    build_parser.add_argument("--root", type=Path, default=Path.cwd())
    build_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subcommands.add_parser("verify")
    verify_parser.add_argument("--root", type=Path)
    verify_parser.add_argument("--archive", type=Path, required=True)
    verify_parser.add_argument("--expected-sha256")
    extract_parser = subcommands.add_parser("extract")
    extract_parser.add_argument("--archive", type=Path, required=True)
    extract_parser.add_argument("--destination", type=Path, required=True)
    extract_parser.add_argument("--expected-sha256", required=True)
    return command


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "build":
            result = build(args.root.resolve(), args.output.absolute())
        elif args.command == "verify":
            result = verify(args.root.resolve() if args.root else None, args.archive.resolve(), args.expected_sha256)
        else:
            result = extract(args.archive.resolve(), args.destination.absolute(), args.expected_sha256)
    except (ReleaseError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "ok": False}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
