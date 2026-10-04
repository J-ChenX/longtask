#!/usr/bin/env python3
"""Deterministic state, routing, approval, and checkpoint operations for longtask."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
SKILL_VERSION = "3.0.0"
TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PHASES = ("discovery", "architecture", "documentation", "execution", "review", "complete")
STATUSES = ("planned", "active", "awaiting_approval", "blocked", "failed", "complete", "superseded")
MODES = ("setup", "continue", "review", "modify", "retrofit")
APPROVAL_POLICIES = ("interactive", "guarded", "autonomous")
REVIEW_STATUSES = ("not_started", "in_progress", "failed", "conditionally_approved", "approved", "superseded")
RUNTIME_NAMES = {"state.json", "events.jsonl", ".state.lock", ".initializing.json", ".finishing.json"}
IGNORED_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
HASH_CHUNK_BYTES = 1024 * 1024
MAX_WORKSPACE_FILE_BYTES = 64 * 1024 * 1024
MAX_WORKSPACE_BYTES = 512 * 1024 * 1024
STATE_REQUIRED_KEYS = {
    "schema_version", "skill_version", "task_id", "mode", "approval_policy", "phase", "status",
    "revision", "event_revision", "evidence_epoch", "goal", "base_commit", "head_commit", "artifact_digest", "approved_digest",
    "approval", "review", "work_packages", "validation_evidence", "blockers", "resolved_blockers",
    "handoff", "next_action", "updated_at",
}
STATE_KEYS = STATE_REQUIRED_KEYS
APPROVAL_KEYS = {"scope", "digest", "head_commit", "actor", "approved_at"}
REVIEW_KEYS = {"status", "reviewed_digest", "unresolved_blockers"}
EVIDENCE_KEYS = {
    "kind", "summary", "result", "command", "check_id", "details", "artifact_digest", "evidence_epoch", "actor", "recorded_at",
}
BLOCKER_KEYS = {"id", "summary", "actor", "created_at", "opened_digest", "opened_revision"}
RESOLVED_BLOCKER_KEYS = BLOCKER_KEYS | {
    "resolved_by", "resolved_at", "resolution_digest", "verification_actor",
}
REVIEW_DETAIL_REQUIRED_KEYS = {
    "reviewer", "role", "scope", "base_revision", "artifact_digest", "status", "findings", "evidence",
    "coverage_gaps", "assumptions", "task_id", "evidence_epoch", "head_commit",
}
REVIEW_DETAIL_KEYS = REVIEW_DETAIL_REQUIRED_KEYS | {"covered_package_ids"}
REVIEW_VERDICTS = {"approved", "changes_required", "unable_to_verify"}
BLOCKING_SEVERITIES = {"blocker", "stale_documentation", "unable_to_verify"}
FINDING_KEYS = {
    "id", "severity", "claim", "impact", "evidence", "affected_requirement",
    "recommended_correction", "reproducible",
}
PACKAGE_CONTRACT_KEYS = {
    "objective", "dependencies", "affected_modules", "write_set", "acceptance_checks",
    "risk", "risk_level", "required_review_roles", "rollback", "stopping_condition",
}
PACKAGE_REQUIRED_KEYS = {
    "id", "objective", "status", "dependencies", "affected_modules", "write_set",
    "acceptance_checks", "base_revision", "risk", "risk_level", "required_review_roles",
    "rollback", "stopping_condition", "owner", "lease_expires", "contributors",
}
PACKAGE_RUNTIME_KEYS = {
    "evidence", "baseline_manifest", "stale_base", "lease_expired", "stale_evidence", "scope_drift",
    "dependency_failed", "execution_group", "settlement_manifest", "acceptance_evidence_start",
}
RISK_LEVELS = {"low", "medium", "high", "critical"}
HANDOFF_INTENTS = {"plan", "document", "execute", "review", "remediate", "verify", "decide", "complete"}
HANDOFF_TARGET_KINDS = {"task", "work_package", "artifact", "review_findings", "decision"}
HANDOFF_INPUT_KEYS = {
    "intent", "objective", "reason", "target", "required_inputs", "acceptance_checks",
    "next_if_pass", "next_if_fail",
}
HANDOFF_KEYS = HANDOFF_INPUT_KEYS | {
    "artifact_digest", "head_commit", "evidence_epoch", "created_revision", "created_by", "created_at",
}
HANDOFF_TARGET_KEYS = {"kind", "ref"}
HANDOFF_BRANCH_KEYS = {"intent", "objective"}


class StateError(RuntimeError):
    """Raised when longtask state cannot be trusted."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def task_id_valid(value: str) -> bool:
    return bool(TASK_ID.fullmatch(value)) and ".." not in value


def actor_valid(value: Any) -> bool:
    """Accept only stable cooperative identity labels, never visual aliases."""
    return (
        isinstance(value, str)
        and ACTOR.fullmatch(value) is not None
        and value == unicodedata.normalize("NFC", value)
    )


def require_actor(value: Any) -> str:
    if not actor_valid(value):
        raise StateError("actor must be a canonical NFC ASCII identifier")
    return value


def git_head(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _runtime_file(relative: Path) -> bool:
    parts = relative.parts
    if any(part in IGNORED_PARTS for part in parts):
        return True
    return bool(parts) and parts[0] == ".longtask"


@contextmanager
def artifact_parent(root: Path, path: Path) -> Iterator[int | None]:
    """Pin every ancestor; a missing cached path is a deletion, a link is an error."""
    descriptor = open_directory_path(root, create=False)
    try:
        for part in path.relative_to(root).parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            except FileNotFoundError:
                yield None
                return
            except OSError as exc:
                raise StateError(f"unsafe workspace artifact ancestor: {path.relative_to(root)}: {exc}") from exc
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def workspace_files(root: Path, *, workspace_root: Path | None = None) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            check=False,
            capture_output=True,
        )
    except OSError:
        result = None
    if result is not None and result.returncode == 0:
        candidates = [root / os.fsdecode(item) for item in result.stdout.split(b"\0") if item]
    else:
        try:
            candidates = list(root.rglob("*"))
        except (OSError, RuntimeError) as exc:
            raise StateError(f"cannot enumerate workspace without Git: {exc}") from exc

    files: list[Path] = []
    for path in candidates:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if _runtime_file(path.relative_to(workspace_root or root)):
            continue
        with artifact_parent(root, path) as parent:
            if parent is None:
                continue
            try:
                metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                try:
                    try:
                        os.stat(".git", dir_fd=child, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                finally:
                    os.close(child)
            elif not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                raise StateError(f"workspace artifact is not a regular file or link: {relative}")
        files.append(path)
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def _workspace_snapshot(root: Path, *, workspace_root: Path | None = None, byte_budget: list[int] | None = None) -> tuple[str, dict[str, str]]:
    """Stream each artifact once and return an aggregate digest plus path fingerprints."""
    digest = hashlib.sha256()
    files: dict[str, str] = {}
    workspace_root = workspace_root or root
    byte_budget = byte_budget if byte_budget is not None else [0]
    for path in workspace_files(root, workspace_root=workspace_root):
        relative_text = path.relative_to(root).as_posix()
        relative = relative_text.encode("utf-8")
        with artifact_parent(root, path) as parent:
            if parent is None:
                raise StateError(f"workspace artifact disappeared while scanning: {relative_text}")
            try:
                metadata = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except OSError as exc:
                raise StateError(f"cannot inspect workspace artifact {relative_text}: {exc}") from exc
            mode = stat.S_IMODE(metadata.st_mode)
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(mode.to_bytes(4, "big"))
            if stat.S_ISREG(metadata.st_mode):
                if metadata.st_size > MAX_WORKSPACE_FILE_BYTES:
                    raise StateError(f"workspace artifact exceeds per-file digest limit: {relative_text}")
                byte_budget[0] += metadata.st_size
                if byte_budget[0] > MAX_WORKSPACE_BYTES:
                    raise StateError("workspace artifacts exceed aggregate digest limit")
                prefix = b"file\0"
                digest.update((len(prefix) + metadata.st_size).to_bytes(8, "big"))
                digest.update(prefix)
                file_digest = hashlib.sha256(mode.to_bytes(4, "big") + prefix)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                try:
                    descriptor = os.open(path.name, flags, dir_fd=parent)
                except OSError as exc:
                    raise StateError(f"cannot open workspace artifact safely: {relative_text}: {exc}") from exc
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                        or opened.st_size != metadata.st_size
                    ):
                        raise StateError(f"workspace artifact changed while opening: {relative_text}")
                    remaining = opened.st_size
                    while remaining:
                        chunk = os.read(descriptor, min(HASH_CHUNK_BYTES, remaining))
                        if not chunk:
                            raise StateError(f"workspace artifact shrank while hashing: {relative_text}")
                        remaining -= len(chunk)
                        digest.update(chunk)
                        file_digest.update(chunk)
                    if os.read(descriptor, 1):
                        raise StateError(f"workspace artifact grew while hashing: {relative_text}")
                    final = os.fstat(descriptor)
                    if (
                        (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns)
                        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                    ):
                        raise StateError(f"workspace artifact changed while hashing: {relative_text}")
                finally:
                    os.close(descriptor)
                files[relative_text] = file_digest.hexdigest()
                continue
            if stat.S_ISLNK(metadata.st_mode):
                try:
                    content = b"symlink\0" + os.fsencode(os.readlink(path.name, dir_fd=parent))
                except OSError as exc:
                    raise StateError(f"cannot read workspace link {relative_text}: {exc}") from exc
            elif stat.S_ISDIR(metadata.st_mode):
                content = b"gitlink\0" + _workspace_snapshot(path, workspace_root=workspace_root, byte_budget=byte_budget)[0].encode("ascii")
            else:
                raise StateError(f"workspace artifact is not a regular file or link: {relative_text}")
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
            files[relative_text] = hashlib.sha256(mode.to_bytes(4, "big") + content).hexdigest()
    return f"sha256:{digest.hexdigest()}", files


def artifact_digest(root: Path) -> str:
    return _workspace_snapshot(root)[0]


def workspace_manifest(root: Path) -> dict[str, Any]:
    """Capture path-level fingerprints for later write-set attribution."""
    digest, files = _workspace_snapshot(root)
    return {"artifact_digest": digest, "files": files}


def changed_manifest_paths(baseline: dict[str, Any], current: dict[str, Any]) -> list[str]:
    baseline_files = baseline.get("files", {})
    current_files = current.get("files", {})
    if not isinstance(baseline_files, dict) or not isinstance(current_files, dict):
        return []
    return sorted(
        path for path in set(baseline_files) | set(current_files)
        if baseline_files.get(path) != current_files.get(path)
    )


def write_set_covers_path(write_set: Any, changed_path: str) -> bool:
    if not isinstance(write_set, list):
        return False
    for target in write_set:
        normalized = normalize_write_target(target) if isinstance(target, str) else None
        if normalized is not None and (changed_path == normalized or changed_path.startswith(normalized + "/")):
            return True
    return False


def task_dir(root: Path, task_id: str) -> Path:
    if not task_id_valid(task_id):
        raise StateError(f"invalid task_id: {task_id!r}")
    return _inside(root, root / ".longtask")


def _inside(root: Path, target: Path) -> Path:
    resolved = target.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise StateError(f"state path escapes workspace: {target}") from exc
    if resolved != target.absolute():
        raise StateError(f"state path contains a symlink alias: {target}")
    return resolved


def check_runtime_directory(directory: Path, *, reclaim_temporary: bool = False) -> None:
    """Validate all entries first; reclaim runtime write debris only while holding its lock."""
    _inside(directory.parent, directory)
    if not directory.exists():
        return
    descriptor = open_directory_path(directory, create=False)
    temporary: list[tuple[str, os.stat_result]] = []
    # These are the only destinations written by atomic_write inside a checkpoint.
    temporary_pattern = re.compile(r"^\.(?:state\.json|events\.jsonl|\.initializing\.json)\.write-[0-9a-f]{24}$")
    try:
        for name in os.listdir(descriptor):
            is_temporary = temporary_pattern.fullmatch(name) is not None
            if name not in RUNTIME_NAMES and not is_temporary:
                raise StateError(f"unknown checkpoint file; preserve and reconcile it: {name}")
            value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode):
                raise StateError(f"checkpoint file must be a regular non-symlink file: {name}")
            if value.st_nlink != 1:
                raise StateError(f"checkpoint file must not be a hardlink: {name}")
            if is_temporary:
                if stat.S_IMODE(value.st_mode) != 0o600 or (hasattr(os, "getuid") and value.st_uid != os.getuid()):
                    raise StateError(f"checkpoint temporary file has unexpected ownership or permissions; preserve it: {name}")
                temporary.append((name, value))
        if temporary and not reclaim_temporary:
            raise StateError("interrupted checkpoint atomic write; retry its command to reclaim runtime temporary files under lock")
        for name, expected in temporary:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_mode, current.st_nlink, current.st_uid) != (
                expected.st_dev, expected.st_ino, expected.st_mode, expected.st_nlink, expected.st_uid
            ):
                raise StateError(f"checkpoint temporary file changed identity; preserve it: {name}")
        for name, _ in temporary:
            os.unlink(name, dir_fd=descriptor)
        if temporary:
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def active_state_path(root: Path) -> Path:
    directory = _inside(root, root / ".longtask")
    check_runtime_directory(directory)
    if (directory / ".initializing.json").exists():
        raise StateError("checkpoint initialization interrupted; retry the identical init request")
    if (directory / ".finishing.json").exists():
        raise StateError("checkpoint cleanup interrupted; retry finish with the observed identity")
    return directory / "state.json"


def load_state(root: Path) -> tuple[Path, dict[str, Any]]:
    path = active_state_path(root)
    if not path.is_file():
        raise StateError(".longtask/state.json is missing; no unfinished checkpoint")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot parse {path.relative_to(root)}: {exc}") from exc
    if not isinstance(data, dict):
        raise StateError("state.json must contain an object")
    return path, data


def open_directory_path(path: Path, *, create: bool) -> int:
    """Open a directory from the filesystem root without following link ancestors."""
    absolute = path.absolute()
    if os.name == "nt" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        if create:
            absolute.mkdir(parents=True, exist_ok=True)
        resolved = absolute.resolve()
        if resolved != absolute:
            raise StateError(f"state directory contains a symlink alias: {absolute}")
        return os.open(absolute, os.O_RDONLY)
    parts = absolute.parts
    if not parts or parts[0] != os.path.sep:
        raise StateError("state directory must be absolute")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(os.path.sep, flags)
    try:
        for part in parts[1:]:
            try:
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise StateError(f"state directory is missing: {absolute}")
                os.mkdir(part, 0o755, dir_fd=descriptor)
                expected = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(expected.st_mode):
                raise StateError(f"state directory contains a non-directory component: {absolute}")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except OSError as exc:
                raise StateError(f"cannot open state directory safely: {absolute}: {exc}") from exc
            opened = os.fstat(child)
            if (expected.st_dev, expected.st_ino) != (opened.st_dev, opened.st_ino):
                os.close(child)
                raise StateError(f"state directory changed while opening: {absolute}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def atomic_write(path: Path, text: str) -> None:
    parent_fd = open_directory_path(path.parent, create=True)
    temporary_name = f".{path.name}.write-{secrets.token_hex(12)}"
    try:
        try:
            existing = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1
        ):
            raise StateError(f"state target must be a regular non-linked file: {path}")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_fd)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary_name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        temporary_name = ""
        os.fsync(parent_fd)
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)


def fsync_directory(directory: Path) -> None:
    """Persist directory entries where the host supports directory fsync."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = open_directory_path(directory, create=False)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(descriptor)


@contextmanager
def state_lock(directory: Path) -> Iterator[None]:
    lock = directory / ".state.lock"
    directory_fd = open_directory_path(directory, create=False)
    try:
        expected = os.stat(lock.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        expected = None
    except OSError as exc:
        os.close(directory_fd)
        raise StateError(f"cannot inspect task lock safely: {lock}: {exc}") from exc
    if expected is not None:
        if not stat.S_ISREG(expected.st_mode):
            os.close(directory_fd)
            raise StateError(f"task lock must be a regular non-symlink file: {lock}")
        if expected.st_nlink != 1:
            os.close(directory_fd)
            raise StateError(f"task lock must not be a hardlink: {lock}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock.name, flags, 0o600, dir_fd=directory_fd)
    except OSError as exc:
        os.close(directory_fd)
        raise StateError(f"cannot open task lock safely: {lock}: {exc}") from exc
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
            raise StateError(f"task lock changed to an unsafe file during open: {lock}")
        if expected is not None and (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise StateError(f"task lock changed identity during open: {lock}")
        try:
            current = os.stat(lock.name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise StateError(f"cannot verify task lock identity after open: {lock}: {exc}") from exc
        if (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino):
            raise StateError(f"task lock changed identity during open: {lock}")
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise StateError(f"task state is busy: {lock}") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)
        os.close(directory_fd)


def check_event_target(directory: Path) -> Path:
    path = directory / "events.jsonl"
    directory_fd = open_directory_path(directory, create=False)
    try:
        metadata = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return path
    finally:
        os.close(directory_fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise StateError(f"event log must be a regular non-symlink file: {path}")
    if metadata.st_nlink != 1:
        raise StateError(f"event log must not be a hardlink: {path}")
    return path


def append_event(directory: Path, event: str, actor: str, revision: int, details: dict[str, Any]) -> None:
    require_actor(actor)
    record = {
        "event": event,
        "actor": actor,
        "revision": revision,
        "at": now(),
        "details": details,
    }
    path = directory / "events.jsonl"
    directory_fd = open_directory_path(directory, create=False)
    try:
        expected = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        expected = None
    if expected is not None:
        if not stat.S_ISREG(expected.st_mode):
            os.close(directory_fd)
            raise StateError(f"event log must be a regular non-symlink file: {path}")
        if expected.st_nlink != 1:
            os.close(directory_fd)
            raise StateError(f"event log must not be a hardlink: {path}")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path.name, flags, 0o600, dir_fd=directory_fd)
    except Exception:
        os.close(directory_fd)
        raise
    try:
        try:
            actual = os.fstat(descriptor)
            if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                raise StateError(f"event log changed to an unsafe file during open: {path}")
            if expected is not None and (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise StateError(f"event log changed identity during open: {path}")
        except Exception:
            os.close(descriptor)
            raise
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if expected is None:
            os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def validate_event_log(
    directory: Path, expected_revision: Any, *, allow_ahead_to: int | None = None,
) -> list[str]:
    """Validate the recoverable audit stream against the state commitment."""
    errors: list[str] = []
    try:
        path = check_event_target(directory)
    except StateError as exc:
        return [str(exc)]
    if expected_revision == -1:
        if path.exists() and path.stat().st_size:
            errors.append("event log must be empty while event_revision is -1")
        return errors
    if not path.is_file():
        return ["event log is missing"]
    previous: int | None = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return [f"cannot read event log: {exc}"]
    if not lines:
        return ["event log is empty"]
    for index, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append(f"event log line {index} is invalid JSON: {exc}")
            continue
        if not isinstance(record, dict):
            errors.append(f"event log line {index} must be an object")
            continue
        if set(record) != {"event", "actor", "revision", "at", "details"}:
            errors.append(f"event log line {index} has an invalid record shape")
        revision = record.get("revision")
        if type(revision) is not int or revision < 0:
            errors.append(f"event log line {index} has an invalid revision")
            continue
        if index == 1:
            if revision != 0:
                errors.append("event log must start at revision 0")
            if record.get("event") != "initialized":
                errors.append("event log must start with the initialized event")
        if previous is not None:
            if revision <= previous:
                errors.append(f"event log line {index} revision is not strictly increasing")
            elif revision != previous + 1 and record.get("event") != "event_gap_reconciled":
                errors.append(f"event log line {index} skips revisions without reconciliation")
        if not isinstance(record.get("event"), str) or not record.get("event"):
            errors.append(f"event log line {index} has an invalid event")
        if not actor_valid(record.get("actor")):
            errors.append(f"event log line {index} has an invalid actor")
        if not timestamp_valid(record.get("at")):
            errors.append(f"event log line {index} has an invalid timestamp")
        if not isinstance(record.get("details"), dict):
            errors.append(f"event log line {index} details must be an object")
        previous = revision
    accepted_revisions = {expected_revision}
    if type(allow_ahead_to) is int and allow_ahead_to > expected_revision:
        accepted_revisions.add(allow_ahead_to)
    if previous not in accepted_revisions:
        errors.append(f"event log ends at revision {previous}, expected {expected_revision}")
    return errors


def _require_keys(data: dict[str, Any], keys: set[str]) -> list[str]:
    return [f"missing key: {key}" for key in sorted(keys - data.keys())]


def timestamp_valid(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value,
    ):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate_blockers(blockers: list[Any], resolved: bool) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    required = RESOLVED_BLOCKER_KEYS if resolved else BLOCKER_KEYS
    collection = "resolved_blockers" if resolved else "blockers"
    for index, blocker in enumerate(blockers):
        label = f"{collection}[{index}]"
        if not isinstance(blocker, dict):
            errors.append(f"{label} must be an object")
            continue
        errors.extend(f"{label} {message}" for message in _require_keys(blocker, required))
        unexpected = set(blocker) - required
        if unexpected:
            errors.append(f"{label} has unexpected keys: {', '.join(sorted(unexpected))}")
        for key in ("id", "summary"):
            if not isinstance(blocker.get(key), str) or not blocker.get(key):
                errors.append(f"{label}.{key} must be non-empty")
        if not actor_valid(blocker.get("actor")):
            errors.append(f"{label}.actor must be canonical")
        blocker_id = blocker.get("id")
        if isinstance(blocker_id, str):
            if blocker_id in ids:
                errors.append(f"duplicate blocker id: {blocker_id}")
            ids.add(blocker_id)
        if not timestamp_valid(blocker.get("created_at")):
            errors.append(f"{label}.created_at must be a timezone-aware ISO 8601 timestamp")
        opened_digest = blocker.get("opened_digest")
        if not isinstance(opened_digest, str) or not DIGEST.fullmatch(opened_digest):
            errors.append(f"{label}.opened_digest is invalid")
        if type(blocker.get("opened_revision")) is not int or blocker.get("opened_revision", -1) < 0:
            errors.append(f"{label}.opened_revision must be a non-negative integer")
        if resolved:
            for key in ("resolved_by", "verification_actor"):
                if not actor_valid(blocker.get(key)):
                    errors.append(f"{label}.{key} must be canonical")
            if not timestamp_valid(blocker.get("resolved_at")):
                errors.append(f"{label}.resolved_at must be a timezone-aware ISO 8601 timestamp")
            digest = blocker.get("resolution_digest")
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                errors.append(f"{label}.resolution_digest is invalid")
    return errors


def validate_handoff(value: Any, state: dict[str, Any] | None = None) -> list[str]:
    """Validate a version-bound, non-recursive cross-window handoff frame."""
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["handoff must be an object"]
    missing = HANDOFF_KEYS - value.keys()
    if missing:
        errors.append("handoff missing: " + ", ".join(sorted(missing)))
        return errors
    unexpected = set(value) - HANDOFF_KEYS
    if unexpected:
        errors.append("unexpected handoff keys: " + ", ".join(sorted(unexpected)))
    if not isinstance(value.get("intent"), str) or value.get("intent") not in HANDOFF_INTENTS:
        errors.append(f"invalid handoff intent: {value.get('intent')!r}")
    for key in ("objective", "reason", "created_at"):
        if not isinstance(value.get(key), str) or not value.get(key):
            errors.append(f"handoff.{key} must be a non-empty string")
    if not actor_valid(value.get("created_by")):
        errors.append("handoff.created_by must be canonical")
    if not timestamp_valid(value.get("created_at")):
        errors.append("handoff.created_at must be a timezone-aware ISO 8601 timestamp")
    if not isinstance(value.get("artifact_digest"), str) or not DIGEST.fullmatch(value["artifact_digest"]):
        errors.append("handoff.artifact_digest must be an artifact digest")
    if value.get("head_commit") is not None and (
        not isinstance(value.get("head_commit"), str) or not value.get("head_commit")
    ):
        errors.append("handoff.head_commit must be a non-empty string or null")
    if type(value.get("evidence_epoch")) is not int or value.get("evidence_epoch", -1) < 0:
        errors.append("handoff.evidence_epoch must be a non-negative integer")
    if type(value.get("created_revision")) is not int or value.get("created_revision", -1) < 0:
        errors.append("handoff.created_revision must be a non-negative integer")
    elif state is not None and type(state.get("revision")) is int and value["created_revision"] > state["revision"]:
        errors.append("handoff.created_revision cannot exceed state revision")

    target = value.get("target")
    if not isinstance(target, dict) or set(target) != HANDOFF_TARGET_KEYS:
        errors.append("handoff.target must contain exactly kind and ref")
    else:
        if not isinstance(target.get("kind"), str) or target.get("kind") not in HANDOFF_TARGET_KINDS:
            errors.append(f"invalid handoff target kind: {target.get('kind')!r}")
        if not isinstance(target.get("ref"), str) or not target.get("ref"):
            errors.append("handoff.target.ref must be a non-empty string")
        packages = state.get("work_packages") if state is not None else None
        evidence = state.get("validation_evidence") if state is not None else None
        if state is not None and target.get("kind") == "work_package" and isinstance(packages, list) and not any(
            isinstance(item, dict) and item.get("id") == target.get("ref")
            for item in packages
        ):
            errors.append(f"handoff targets unknown work package: {target.get('ref')}")
        if state is not None and target.get("kind") == "task" and target.get("ref") != state.get("task_id"):
            errors.append("handoff task target must match task_id")
        if state is not None and target.get("kind") == "review_findings" and isinstance(evidence, list) and not any(
            isinstance(item, dict)
            and item.get("kind") == "review"
            and item.get("result") == "fail"
            and item.get("check_id") == target.get("ref")
            and item.get("artifact_digest") == value.get("artifact_digest")
            and item.get("evidence_epoch") == value.get("evidence_epoch")
            for item in evidence
        ):
            errors.append(f"handoff targets unknown current failing review evidence: {target.get('ref')}")

    for key in ("required_inputs", "acceptance_checks"):
        items = value.get(key)
        if not isinstance(items, list) or not items or not all(isinstance(item, str) and item for item in items):
            errors.append(f"handoff.{key} must contain non-empty strings")
        elif len(set(items)) != len(items):
            errors.append(f"handoff.{key} must be unique")
    for key in ("next_if_pass", "next_if_fail"):
        branch = value.get(key)
        if not isinstance(branch, dict) or set(branch) != HANDOFF_BRANCH_KEYS:
            errors.append(f"handoff.{key} must contain exactly intent and objective")
            continue
        if not isinstance(branch.get("intent"), str) or branch.get("intent") not in HANDOFF_INTENTS:
            errors.append(f"invalid handoff.{key} intent: {branch.get('intent')!r}")
        if not isinstance(branch.get("objective"), str) or not branch.get("objective"):
            errors.append(f"handoff.{key}.objective must be a non-empty string")
    return errors


def validate_state(
    data: dict[str, Any], root: Path | None = None, allow_event_gap: bool = False,
    allow_workspace_drift: bool = False, allow_event_log_ahead: bool = False,
) -> list[str]:
    errors = _require_keys(data, STATE_REQUIRED_KEYS)
    if errors:
        return errors
    unexpected = set(data) - STATE_KEYS
    if unexpected:
        errors.append(f"unexpected state keys: {', '.join(sorted(unexpected))}")

    if data["schema_version"] != SCHEMA_VERSION:
        errors.append(f"unsupported schema_version: {data['schema_version']!r}")
    if data["skill_version"] != SKILL_VERSION:
        errors.append(f"unsupported skill_version: {data['skill_version']!r}")
    if not isinstance(data["task_id"], str) or not task_id_valid(data["task_id"]):
        errors.append("invalid task_id")
    for key, allowed in (("mode", MODES), ("approval_policy", APPROVAL_POLICIES), ("phase", PHASES), ("status", STATUSES)):
        if data[key] not in allowed:
            errors.append(f"invalid {key}: {data[key]!r}")
    if type(data["revision"]) is not int or data["revision"] < 0:
        errors.append("revision must be a non-negative integer")
    if type(data["event_revision"]) is not int or data["event_revision"] < -1:
        errors.append("event_revision must be an integer from -1 through revision")
    elif type(data["revision"]) is int:
        if data["event_revision"] > data["revision"]:
            errors.append("event_revision must be an integer from -1 through revision")
        elif not allow_event_gap and data["event_revision"] != data["revision"]:
            errors.append("event log revision does not match state revision")
    if type(data["evidence_epoch"]) is not int or data["evidence_epoch"] < 0:
        errors.append("evidence_epoch must be a non-negative integer")
    for key in ("goal", "next_action", "updated_at"):
        if not isinstance(data[key], str):
            errors.append(f"{key} must be a string")
    if not isinstance(data["goal"], str) or not data["goal"].strip():
        errors.append("goal must be non-empty")
    if not timestamp_valid(data["updated_at"]):
        errors.append("updated_at must be a timezone-aware ISO 8601 timestamp")
    for key in ("base_commit", "head_commit"):
        if data[key] is not None and not isinstance(data[key], str):
            errors.append(f"{key} must be a string or null")
    for key in ("artifact_digest", "approved_digest"):
        if data[key] is not None and (not isinstance(data[key], str) or not DIGEST.fullmatch(data[key])):
            errors.append(f"invalid {key}")

    approval = data["approval"]
    if approval is not None:
        if not isinstance(approval, dict):
            errors.append("approval must be an object or null")
        else:
            errors.extend(f"approval {message}" for message in _require_keys(approval, APPROVAL_KEYS))
            unexpected = set(approval) - APPROVAL_KEYS
            if unexpected:
                errors.append(f"unexpected approval keys: {', '.join(sorted(unexpected))}")
            if approval.get("digest") != data["approved_digest"]:
                errors.append("approval digest must equal approved_digest")
            if approval.get("head_commit") is not None and not isinstance(approval.get("head_commit"), str):
                errors.append("approval.head_commit must be a string or null")
            for key in ("scope", "approved_at"):
                if not isinstance(approval.get(key), str) or not approval.get(key):
                    errors.append(f"approval.{key} must be a non-empty string")
            if not actor_valid(approval.get("actor")):
                errors.append("approval.actor must be canonical")
            if not timestamp_valid(approval.get("approved_at")):
                errors.append("approval.approved_at must be a timezone-aware ISO 8601 timestamp")
    elif data["approved_digest"] is not None:
        errors.append("approved_digest requires approval metadata")

    review = data["review"]
    if not isinstance(review, dict):
        errors.append("review must be an object")
    else:
        errors.extend(_require_keys(review, REVIEW_KEYS))
        unexpected = set(review) - REVIEW_KEYS
        if unexpected:
            errors.append(f"unexpected review keys: {', '.join(sorted(unexpected))}")
        if review.get("status") not in REVIEW_STATUSES:
            errors.append(f"invalid review status: {review.get('status')!r}")
        reviewed = review.get("reviewed_digest")
        if reviewed is not None and (not isinstance(reviewed, str) or not DIGEST.fullmatch(reviewed)):
            errors.append("invalid reviewed_digest")
        if type(review.get("unresolved_blockers")) is not int or review.get("unresolved_blockers", -1) < 0:
            errors.append("unresolved_blockers must be a non-negative integer")

    if not isinstance(data["work_packages"], list):
        errors.append("work_packages must be an array")
    else:
        errors.extend(validate_packages(data["work_packages"]))
        if root is not None:
            errors.extend(validate_write_aliases(data["work_packages"], root))
    if not isinstance(data["validation_evidence"], list):
        errors.append("validation_evidence must be an array")
    else:
        for index, evidence in enumerate(data["validation_evidence"]):
            errors.extend(validate_evidence(evidence, f"validation_evidence[{index}]"))
            if (
                isinstance(evidence, dict)
                and evidence.get("kind") == "review"
                and evidence.get("result") == "pass"
                and evidence.get("artifact_digest") == data.get("artifact_digest")
                and evidence.get("evidence_epoch") == data.get("evidence_epoch")
                and (
                    not isinstance(evidence.get("details"), dict)
                    or "covered_package_ids" not in evidence["details"]
                )
            ):
                errors.append(
                    f"validation_evidence[{index}].details.covered_package_ids "
                    "is required for current passing review evidence"
                )
            if (
                isinstance(evidence, dict) and evidence.get("kind") == "review"
                and evidence.get("artifact_digest") == data.get("artifact_digest")
                and evidence.get("evidence_epoch") == data.get("evidence_epoch")
                and isinstance(evidence.get("details"), dict)
            ):
                for key in ("task_id", "head_commit"):
                    if evidence["details"].get(key) != data.get(key):
                        errors.append(f"validation_evidence[{index}].details.{key} must match the current task binding")
        if isinstance(data["work_packages"], list):
            errors.extend(global_check_contract_errors(data["validation_evidence"], data["work_packages"]))
    if not isinstance(data["blockers"], list):
        errors.append("blockers must be an array")
    else:
        errors.extend(validate_blockers(data["blockers"], resolved=False))
    if not isinstance(data["resolved_blockers"], list):
        errors.append("resolved_blockers must be an array")
    else:
        errors.extend(validate_blockers(data["resolved_blockers"], resolved=True))
    if isinstance(data["blockers"], list) and isinstance(data["resolved_blockers"], list):
        active_ids = {
            item.get("id") for item in data["blockers"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        resolved_ids = {
            item.get("id") for item in data["resolved_blockers"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        reused = sorted(item for item in active_ids & resolved_ids if isinstance(item, str))
        if reused:
            errors.append("blocker ids cannot be reused after resolution: " + ", ".join(reused))

    errors.extend(validate_handoff(data["handoff"], data))

    if root is not None and isinstance(data.get("task_id"), str) and task_id_valid(data["task_id"]):
        ahead_to = data.get("revision") if allow_event_log_ahead else None
        errors.extend(validate_event_log(
            task_dir(root, data["task_id"]), data.get("event_revision"), allow_ahead_to=ahead_to,
        ))

    if (data["status"] == "complete" or data["phase"] == "complete") and isinstance(data["review"], dict) and isinstance(data["work_packages"], list) and isinstance(data["blockers"], list):
        errors.extend(completion_errors(data, None if allow_workspace_drift else root))
    return errors


def package_acceptance_evidence(package: dict[str, Any]) -> list[Any]:
    """Return only evidence collected after the package's latest invalidation boundary."""
    evidence = package.get("evidence", [])
    start = package.get("acceptance_evidence_start", 0)
    if not isinstance(evidence, list) or type(start) is not int or not 0 <= start <= len(evidence):
        return []
    return evidence[start:]


def invalidate_package_acceptance(package: dict[str, Any]) -> None:
    """Retain history without allowing pre-failure passes to prove repaired acceptance."""
    package["acceptance_evidence_start"] = len(package.get("evidence", []))
    package["stale_evidence"] = True


def validate_packages(packages: list[Any]) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    active_writes: dict[str, str] = {}
    statuses: dict[str, Any] = {}
    active_packages: list[dict[str, Any]] = []
    for index, package in enumerate(packages):
        if not isinstance(package, dict):
            errors.append(f"work_packages[{index}] must be an object")
            continue
        missing = PACKAGE_REQUIRED_KEYS - package.keys()
        if missing:
            errors.append(f"work_packages[{index}] missing: {', '.join(sorted(missing))}")
            continue
        unexpected = set(package) - PACKAGE_REQUIRED_KEYS - PACKAGE_RUNTIME_KEYS
        if unexpected:
            errors.append(f"work_packages[{index}] has unexpected keys: {', '.join(sorted(unexpected))}")
        package_id = package["id"]
        if not isinstance(package_id, str) or not package_id:
            errors.append(f"work_packages[{index}].id must be non-empty")
            continue
        if package_id in ids:
            errors.append(f"duplicate work package id: {package_id}")
        ids.add(package_id)
        statuses[package_id] = package["status"]
        if not isinstance(package["objective"], str) or not package["objective"]:
            errors.append(f"work_packages[{index}].objective must be non-empty")
        if not isinstance(package["affected_modules"], list) or not all(isinstance(item, str) and item for item in package["affected_modules"]):
            errors.append(f"work_packages[{index}].affected_modules must contain non-empty strings")
        for key in ("risk", "rollback", "stopping_condition"):
            if not isinstance(package[key], str) or not package[key]:
                errors.append(f"work_packages[{index}].{key} must be non-empty")
        if not isinstance(package["risk_level"], str) or package["risk_level"] not in RISK_LEVELS:
            errors.append(f"work_packages[{index}].risk_level is invalid")
        roles = package["required_review_roles"]
        if not isinstance(roles, list) or not all(isinstance(item, str) and item for item in roles):
            errors.append(f"work_packages[{index}].required_review_roles must contain non-empty strings")
        elif len(set(roles)) != len(roles):
            errors.append(f"work_packages[{index}].required_review_roles must be unique")
        elif package["risk_level"] in {"high", "critical"} and len(roles) < 2:
            errors.append(f"work_packages[{index}] high-risk work requires at least two review roles")
        if package["base_revision"] is not None and (
            not isinstance(package["base_revision"], str) or not DIGEST.fullmatch(package["base_revision"])
        ):
            errors.append(f"work_packages[{index}].base_revision must be an artifact digest or null")
        baseline_manifest = package.get("baseline_manifest")
        if baseline_manifest is not None:
            if not isinstance(baseline_manifest, dict) or set(baseline_manifest) != {"artifact_digest", "files"}:
                errors.append(f"work_packages[{index}].baseline_manifest must contain artifact_digest and files")
            else:
                manifest_digest = baseline_manifest.get("artifact_digest")
                manifest_files = baseline_manifest.get("files")
                if manifest_digest != package.get("base_revision"):
                    errors.append(f"work_packages[{index}].baseline_manifest must match base_revision")
                if not isinstance(manifest_files, dict) or not all(
                    isinstance(path, str) and normalize_write_target(path) == path
                    and isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                    for path, fingerprint in (manifest_files.items() if isinstance(manifest_files, dict) else [])
                ):
                    errors.append(f"work_packages[{index}].baseline_manifest.files is invalid")
        if "execution_group" in package and (
            not isinstance(package["execution_group"], str)
            or not re.fullmatch(r"[0-9a-f]{32}", package["execution_group"])
        ):
            errors.append(f"work_packages[{index}].execution_group is invalid")
        if "settlement_manifest" in package:
            settlement = package["settlement_manifest"]
            if not package.get("execution_group") or not isinstance(baseline_manifest, dict):
                errors.append(f"work_packages[{index}].settlement_manifest requires execution_group and baseline_manifest")
            if (not isinstance(settlement, dict) or set(settlement) != {"artifact_digest", "files"}
                or not isinstance(settlement.get("artifact_digest"), str)
                or not DIGEST.fullmatch(settlement["artifact_digest"])
                or not isinstance(settlement.get("files"), dict)
                or not all(isinstance(path, str) and normalize_write_target(path) == path
                           and isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint)
                           for path, fingerprint in settlement.get("files", {}).items())):
                errors.append(f"work_packages[{index}].settlement_manifest is invalid")
        if package.get("status") == "active" and baseline_manifest is None:
            errors.append(f"active work package requires a runtime baseline_manifest: {package_id}")
        for flag in ("stale_base", "lease_expired", "stale_evidence", "scope_drift"):
            if flag in package and not isinstance(package[flag], bool):
                errors.append(f"work_packages[{index}].{flag} must be boolean")
        dependencies[package_id] = package["dependencies"] if isinstance(package["dependencies"], list) else []
        if not isinstance(package["dependencies"], list) or not all(isinstance(item, str) for item in package["dependencies"]):
            errors.append(f"work_packages[{index}].dependencies must contain strings")
        elif len(set(package["dependencies"])) != len(package["dependencies"]):
            errors.append(f"work_packages[{index}].dependencies must be unique")
        if not isinstance(package["write_set"], list) or not all(isinstance(item, str) and item for item in package["write_set"]):
            errors.append(f"work_packages[{index}].write_set must contain non-empty strings")
        elif len(set(package["write_set"])) != len(package["write_set"]):
            errors.append(f"work_packages[{index}].write_set must be unique")
        if not isinstance(package["acceptance_checks"], list) or not package["acceptance_checks"] or not all(
            isinstance(item, str) and item for item in package["acceptance_checks"]
        ):
            errors.append(f"work_packages[{index}].acceptance_checks must contain at least one non-empty string")
        elif len(set(package["acceptance_checks"])) != len(package["acceptance_checks"]):
            errors.append(f"work_packages[{index}].acceptance_checks must be unique")
        if package["owner"] is not None and not actor_valid(package["owner"]):
            errors.append(f"work_packages[{index}].owner must be a canonical actor or null")
        if package["lease_expires"] is not None and (
            not isinstance(package["lease_expires"], str) or not package["lease_expires"]
        ):
            errors.append(f"work_packages[{index}].lease_expires must be a non-empty string or null")
        if package["lease_expires"] is not None and not timestamp_valid(package["lease_expires"]):
            errors.append(f"work_packages[{index}].lease_expires must be a timezone-aware ISO 8601 timestamp")
        if not isinstance(package["contributors"], list) or not package["contributors"] or not all(
            actor_valid(item) for item in package["contributors"]
        ):
            errors.append(f"work_packages[{index}].contributors must contain canonical actors")
        elif len(set(package["contributors"])) != len(package["contributors"]):
            errors.append(f"work_packages[{index}].contributors must be unique")
        package_status = package["status"]
        if not isinstance(package_status, str) or package_status not in {"planned", "ready", "active", "blocked", "failed", "complete", "superseded"}:
            errors.append(f"invalid work package status for {package_id}: {package['status']!r}")
        for flag in ("stale_base", "lease_expired", "scope_drift", "dependency_failed"):
            if package.get(flag) is True and package_status != "blocked":
                errors.append(f"work package {package_id} with {flag}=true must be blocked")
        package_evidence = package.get("evidence")
        acceptance_start = package.get("acceptance_evidence_start", 0)
        if (type(acceptance_start) is not int or acceptance_start < 0
            or acceptance_start > (len(package_evidence) if isinstance(package_evidence, list) else 0)):
            errors.append(f"work package {package_id}.acceptance_evidence_start must be an evidence array boundary")
        if "evidence" in package:
            if not isinstance(package_evidence, list):
                errors.append(f"work package {package_id}.evidence must be an array")
            else:
                for evidence_index, item in enumerate(package_evidence):
                    errors.extend(validate_evidence(item, f"work package {package_id} evidence[{evidence_index}]"))
        if package_status == "complete":
            evidence = package_acceptance_evidence(package)
            if not isinstance(evidence, list) or not evidence:
                errors.append(f"completed work package lacks structured evidence: {package_id}")
            else:
                latest_by_check: dict[str, dict[str, Any]] = {}
                for item in evidence:
                    if isinstance(item, dict) and isinstance(item.get("check_id"), str):
                        latest_by_check[item["check_id"]] = item
                for check in package["acceptance_checks"] if isinstance(package["acceptance_checks"], list) else []:
                    if latest_by_check.get(check, {}).get("result") != "pass":
                        errors.append(f"completed work package {package_id} lacks passing evidence for check: {check}")
        normalized_targets: list[str] = []
        if isinstance(package["write_set"], list):
            for target in package["write_set"]:
                normalized = normalize_write_target(target)
                if normalized is None:
                    errors.append(f"invalid write-set target for {package_id}: {target!r}")
                    continue
                normalized_targets.append(normalized)
        if package_status == "active":
            active_packages.append(package)
            for normalized in normalized_targets:
                for prior_target, prior_package in active_writes.items():
                    if write_targets_overlap(prior_target, normalized):
                        errors.append(f"active write-set conflict: {normalized} ({prior_package}, {package_id})")
                active_writes[normalized] = package_id

    if len(active_packages) > 1:
        for package in active_packages:
            package_id = package.get("id", "?")
            if not package.get("owner") or not package.get("lease_expires"):
                errors.append(f"parallel active package requires owner and lease_expires: {package_id}")
            # Lease expiry is recoverable runtime state. checkpoint marks it blocked.

    for package_id, deps in dependencies.items():
        for dependency in deps:
            if not isinstance(dependency, str):
                continue
            if dependency not in ids:
                errors.append(f"unknown dependency for {package_id}: {dependency}")
            elif statuses.get(package_id) in ("ready", "active") and statuses.get(dependency) != "complete":
                errors.append(f"{statuses.get(package_id)} package {package_id} has unfinished dependency: {dependency}")
        if statuses.get(package_id) in ("ready", "active", "complete"):
            package = next((item for item in packages if isinstance(item, dict) and item.get("id") == package_id), {})
            if not package.get("base_revision"):
                errors.append(f"{statuses.get(package_id)} package {package_id} requires base_revision")

    indegree = {
        package_id: sum(1 for dependency in deps if isinstance(dependency, str) and dependency in dependencies)
        for package_id, deps in dependencies.items()
    }
    dependents: dict[str, list[str]] = {package_id: [] for package_id in dependencies}
    for package_id, deps in dependencies.items():
        for dependency in deps:
            if isinstance(dependency, str) and dependency in dependents:
                dependents[dependency].append(package_id)
    ready = [package_id for package_id, degree in indegree.items() if degree == 0]
    processed = 0
    while ready:
        package_id = ready.pop()
        processed += 1
        for dependent in dependents[package_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if processed != len(indegree):
        cycle_members = sorted(package_id for package_id, degree in indegree.items() if degree > 0)
        errors.append("dependency cycle contains: " + ", ".join(cycle_members))
    return errors


def current_package_evidence_errors(
    packages: list[Any], artifact: Any, evidence_epoch: Any,
) -> list[str]:
    """Validate complete package claims at the state's current content/intent boundary."""
    errors: list[str] = []
    for package in packages:
        if not isinstance(package, dict) or package.get("status") != "complete":
            continue
        latest_by_check: dict[str, dict[str, Any]] = {}
        evidence = package_acceptance_evidence(package)
        for item in evidence:
            if (
                isinstance(item, dict)
                and isinstance(item.get("check_id"), str)
                and item.get("artifact_digest") == artifact
                and item.get("evidence_epoch") == evidence_epoch
            ):
                latest_by_check[item["check_id"]] = item
        checks = package.get("acceptance_checks") if isinstance(package.get("acceptance_checks"), list) else []
        if not checks:
            errors.append(
                f"completed work package {package.get('id', '?')} has no acceptance checks"
            )
        for check in checks:
            if not isinstance(check, str) or not check:
                continue
            if latest_by_check.get(check, {}).get("result") != "pass":
                errors.append(
                    f"completed work package {package.get('id', '?')} lacks passing evidence "
                    f"at the current digest for check: {check}"
                )
    return errors


def validate_evidence(evidence: Any, label: str) -> list[str]:
    if not isinstance(evidence, dict):
        return [f"{label} must be an object"]
    errors = [f"{label} {message}" for message in _require_keys(evidence, EVIDENCE_KEYS)]
    unexpected = set(evidence) - EVIDENCE_KEYS
    if unexpected:
        errors.append(f"{label} has unexpected keys: {', '.join(sorted(unexpected))}")
    for key in ("kind", "summary", "recorded_at"):
        if not isinstance(evidence.get(key), str) or not evidence.get(key):
            errors.append(f"{label}.{key} must be non-empty")
    if not actor_valid(evidence.get("actor")):
        errors.append(f"{label}.actor must be canonical")
    if not isinstance(evidence.get("result"), str) or evidence.get("result") not in {"pass", "fail"}:
        errors.append(f"{label}.result must be pass or fail")
    if evidence.get("command") is not None and not isinstance(evidence.get("command"), str):
        errors.append(f"{label}.command must be a string or null")
    if evidence.get("check_id") is not None and (not isinstance(evidence.get("check_id"), str) or not evidence.get("check_id")):
        errors.append(f"{label}.check_id must be a non-empty string or null")
    if type(evidence.get("evidence_epoch")) is not int or evidence.get("evidence_epoch", -1) < 0:
        errors.append(f"{label}.evidence_epoch must be a non-negative integer")
    details = evidence.get("details")
    if details is not None and not isinstance(details, dict):
        errors.append(f"{label}.details must be an object or null")
    if isinstance(details, dict) and "recovery_observation" in details and (
        not isinstance(details["recovery_observation"], str) or not details["recovery_observation"].strip()
    ):
        errors.append(f"{label}.details.recovery_observation must be a non-empty observation")
    digest = evidence.get("artifact_digest")
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        errors.append(f"{label}.artifact_digest is invalid")
    if not timestamp_valid(evidence.get("recorded_at")):
        errors.append(f"{label}.recorded_at must be a timezone-aware ISO 8601 timestamp")
    if evidence.get("kind") == "review":
        if not isinstance(details, dict):
            errors.append(f"{label} review evidence requires a reviewer envelope")
        else:
            errors.extend(
                f"{label}.details {message}" for message in _require_keys(details, REVIEW_DETAIL_REQUIRED_KEYS)
            )
            unexpected_details = set(details) - REVIEW_DETAIL_KEYS
            if unexpected_details:
                errors.append(f"{label}.details has unexpected keys: {', '.join(sorted(unexpected_details))}")
            for key in ("role", "base_revision", "artifact_digest", "status"):
                if not isinstance(details.get(key), str) or not details.get(key):
                    errors.append(f"{label}.details.{key} must be non-empty")
            if not actor_valid(details.get("reviewer")):
                errors.append(f"{label}.details.reviewer must be canonical")
            for key in ("scope", "findings", "evidence", "coverage_gaps", "assumptions"):
                if not isinstance(details.get(key), list):
                    errors.append(f"{label}.details.{key} must be an array")
            if isinstance(details.get("scope"), list) and not details["scope"]:
                errors.append(f"{label}.details.scope must not be empty")
            elif isinstance(details.get("scope"), list) and not all(
                isinstance(item, str) and item for item in details["scope"]
            ):
                errors.append(f"{label}.details.scope must contain non-empty strings")
            covered_package_ids = details.get("covered_package_ids")
            if covered_package_ids is not None and (
                not isinstance(covered_package_ids, list)
                or not all(isinstance(item, str) and item for item in covered_package_ids)
                or len(set(covered_package_ids)) != len(covered_package_ids)
            ):
                errors.append(f"{label}.details.covered_package_ids must contain unique non-empty strings")
            for key in ("evidence", "coverage_gaps", "assumptions"):
                if isinstance(details.get(key), list) and not all(
                    isinstance(item, str) and item for item in details[key]
                ):
                    errors.append(f"{label}.details.{key} must contain non-empty strings")
            if not isinstance(details.get("task_id"), str) or not task_id_valid(details["task_id"]):
                errors.append(f"{label}.details.task_id must be a valid task generation")
            if type(details.get("evidence_epoch")) is not int or details.get("evidence_epoch", -1) < 0:
                errors.append(f"{label}.details.evidence_epoch must be a non-negative integer")
            elif details["evidence_epoch"] != evidence.get("evidence_epoch"):
                errors.append(f"{label}.details.evidence_epoch must match evidence epoch")
            if details.get("head_commit") is not None and (
                not isinstance(details.get("head_commit"), str) or not details.get("head_commit")
            ):
                errors.append(f"{label}.details.head_commit must be a non-empty string or null")
            if details.get("artifact_digest") != evidence.get("artifact_digest"):
                errors.append(f"{label}.details.artifact_digest must match evidence digest")
            if details.get("reviewer") != evidence.get("actor"):
                errors.append(f"{label}.details.reviewer must match evidence actor")
            if details.get("base_revision") != evidence.get("artifact_digest"):
                errors.append(f"{label}.details.base_revision must match reviewed artifact digest")
            if not isinstance(details.get("status"), str) or details.get("status") not in REVIEW_VERDICTS:
                errors.append(f"{label}.details.status is invalid")
            if evidence.get("result") == "pass" and details.get("status") != "approved":
                errors.append(f"{label}.details.status must be approved for passing review evidence")
            if evidence.get("result") == "pass" and details.get("coverage_gaps"):
                errors.append(f"{label}.details.coverage_gaps must be empty for passing review evidence")
            if evidence.get("result") == "fail" and details.get("status") == "approved":
                errors.append(f"{label}.details.status cannot be approved for failing review evidence")
            if isinstance(details.get("findings"), list):
                for index, finding in enumerate(details["findings"]):
                    if not isinstance(finding, dict):
                        errors.append(f"{label}.details.findings[{index}] must be an object")
                        continue
                    finding_label = f"{label}.details.findings[{index}]"
                    errors.extend(f"{finding_label} {message}" for message in _require_keys(finding, FINDING_KEYS))
                    unexpected_finding = set(finding) - FINDING_KEYS
                    if unexpected_finding:
                        errors.append(
                            f"{finding_label} has unexpected keys: {', '.join(sorted(unexpected_finding))}"
                        )
                    if not isinstance(finding.get("severity"), str) or finding.get("severity") not in {
                        "blocker", "stale_documentation", "warning", "suggestion", "unable_to_verify",
                    }:
                        errors.append(f"{finding_label}.severity is invalid")
                    for key in (
                        "id", "claim", "impact", "affected_requirement", "recommended_correction",
                    ):
                        if not isinstance(finding.get(key), str) or not finding.get(key):
                            errors.append(f"{finding_label}.{key} must be non-empty")
                    if not isinstance(finding.get("evidence"), list) or not finding.get("evidence") or not all(
                        isinstance(item, str) and item for item in finding.get("evidence", [])
                    ):
                        errors.append(f"{finding_label}.evidence must contain at least one non-empty string")
                    if not isinstance(finding.get("reproducible"), bool):
                        errors.append(f"{finding_label}.reproducible must be boolean")
                if evidence.get("result") == "pass" and any(
                    isinstance(finding, dict) and isinstance(finding.get("severity"), str)
                    and finding.get("severity") in BLOCKING_SEVERITIES
                    for finding in details["findings"]
                ):
                    errors.append(f"{label} passing review evidence cannot contain blocking findings")
                if evidence.get("result") == "fail" and not details["findings"]:
                    errors.append(f"{label} failing review evidence requires at least one finding")
    return errors


def normalize_write_target(target: Any) -> str | None:
    if not isinstance(target, str) or not target or "\\" in target or "\0" in target:
        return None
    if target.startswith("/") or any(part in {"", ".", ".."} for part in target.split("/")):
        return None
    return target.rstrip("/")


def write_targets_overlap(left: str, right: str) -> bool:
    left_parts = left.split("/")
    right_parts = right.split("/")
    common = min(len(left_parts), len(right_parts))
    return left_parts[:common] == right_parts[:common]


def validate_write_aliases(packages: list[Any], root: Path) -> list[str]:
    errors: list[str] = []
    active_targets: dict[Path, str] = {}
    active_inodes: dict[tuple[int, int], str] = {}
    root_resolved = root.resolve()
    for package in packages:
        if not isinstance(package, dict) or package.get("status") != "active":
            continue
        package_id = str(package.get("id", "?"))
        write_set = package.get("write_set")
        if not isinstance(write_set, list):
            continue
        for raw in write_set:
            normalized = normalize_write_target(raw)
            if normalized is None:
                continue
            target = root_resolved / normalized
            members = [target]
            walk_errors: list[OSError] = []
            try:
                inspect_descendants = target.is_dir() and not target.is_symlink()
            except (OSError, RuntimeError) as exc:
                errors.append(
                    f"cannot inspect active directory write-set safely: {package_id}:{normalized}: {exc}"
                )
                inspect_descendants = False
            if inspect_descendants:
                for directory, child_directories, filenames in os.walk(
                    target, topdown=True, followlinks=False, onerror=walk_errors.append,
                ):
                    members.extend(
                        Path(directory) / name for name in sorted(child_directories + filenames)
                    )
            for walk_error in walk_errors:
                errors.append(
                    f"cannot inspect active directory write-set safely: {package_id}:{normalized}: {walk_error}"
                )
            for member in members:
                display = member.relative_to(root_resolved).as_posix()
                try:
                    resolved = member.resolve()
                    resolved.relative_to(root_resolved)
                except ValueError:
                    errors.append(
                        f"active write-set target escapes workspace through symlink: {package_id}:{display}"
                    )
                    continue
                except (OSError, RuntimeError) as exc:
                    errors.append(
                        f"cannot inspect active directory write-set safely: {package_id}:{display}: {exc}"
                    )
                    continue
                if resolved != member.absolute():
                    errors.append(
                        f"active directory write-set contains a symlink alias: {package_id}:{display}"
                    )
                    continue
                try:
                    if member.is_symlink():
                        errors.append(
                            f"active directory write-set contains a symlink alias: {package_id}:{display}"
                        )
                        continue
                    prior = active_targets.get(resolved)
                    if prior is not None and prior != package_id:
                        errors.append(f"active write-set alias conflict: {resolved} ({prior}, {package_id})")
                    active_targets[resolved] = package_id
                    if resolved.exists() and resolved.is_file():
                        metadata = resolved.stat()
                        if metadata.st_nlink != 1:
                            errors.append(
                                f"active write-set hardlink conflict: {resolved} has link count {metadata.st_nlink}"
                            )
                        inode = (metadata.st_dev, metadata.st_ino)
                        prior_inode = active_inodes.get(inode)
                        if prior_inode is not None and prior_inode != package_id:
                            errors.append(f"active write-set hardlink conflict: {resolved} ({prior_inode}, {package_id})")
                        active_inodes[inode] = package_id
                except (OSError, RuntimeError) as exc:
                    errors.append(
                        f"cannot inspect active directory write-set safely: {package_id}:{display}: {exc}"
                    )
    return errors


def package_contributor_actors(packages: list[Any]) -> set[str]:
    actors: set[str] = set()
    for package in packages:
        if not isinstance(package, dict):
            continue
        owner = package.get("owner")
        if isinstance(owner, str) and owner:
            actors.add(owner)
        contributors = package.get("contributors")
        if isinstance(contributors, list):
            actors.update(item for item in contributors if isinstance(item, str) and item)
        evidence = package.get("evidence")
        if isinstance(evidence, list):
            actors.update(
                item.get("actor") for item in evidence
                if isinstance(item, dict) and isinstance(item.get("actor"), str) and item.get("actor")
            )
    return actors


def evidence_record_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        value if isinstance(value, str) or value is None else None
        for value in (item.get(key) for key in ("kind", "check_id", "actor", "recorded_at"))
    )


def global_evidence_records(
    evidence: list[Any], packages: list[Any],
) -> list[dict[str, Any]]:
    package_records = {
        evidence_record_identity(item)
        for package in packages if isinstance(package, dict)
        for item in package.get("evidence", []) if isinstance(item, dict)
    }
    return [
        item for item in evidence
        if isinstance(item, dict) and evidence_record_identity(item) not in package_records
    ]


def global_check_contract_errors(evidence: list[Any], packages: list[Any]) -> list[str]:
    """Keep an explicit global check ID bound to its first declared meaning."""
    contracts: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for item in global_evidence_records(evidence, packages):
        kind, check_id, summary = item.get("kind"), item.get("check_id"), item.get("summary")
        if not all(isinstance(value, str) and value for value in (kind, check_id, summary)):
            continue
        key = (kind, check_id)
        prior = contracts.setdefault(key, summary)
        if prior != summary:
            errors.append(
                f"global check contract changed for {kind}/{check_id}: {prior!r} != {summary!r}"
            )
    return errors


def latest_current_checks(state: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    """Reduce global evidence by check identity before inspecting its verdict."""
    latest: dict[str, dict[str, Any]] = {}
    for item in global_evidence_records(state["validation_evidence"], state["work_packages"]):
        if (item.get("kind") == kind
                and item.get("artifact_digest") == state["artifact_digest"]
                and item.get("evidence_epoch") == state["evidence_epoch"]):
            latest[item["check_id"]] = item
    return list(latest.values())


def review_coverage_errors(
    packages: list[Any], reviews: list[dict[str, Any]], approval_actor: Any,
) -> list[str]:
    """Check independent review coverage against each concrete work package."""
    errors: list[str] = []
    contributors = package_contributor_actors(packages)
    independent = [
        item for item in reviews
        if item.get("actor") != approval_actor and item.get("actor") not in contributors
    ]
    by_actor = {
        item.get("actor"): item for item in independent
        if isinstance(item.get("actor"), str) and item.get("actor")
    }
    if not by_actor:
        return ["review requires current passing evidence from reviewers independent of contributors and approver"]

    live_packages = [
        package for package in packages
        if isinstance(package, dict) and package.get("status") != "superseded"
    ]
    if not live_packages and len(by_actor) < 1:
        return ["review requires at least one independent reviewer"]
    for package in live_packages:
        package_id = package.get("id")
        scoped_reviews = [
            item for item in independent
            if isinstance(item.get("details"), dict)
            and isinstance(item["details"].get("covered_package_ids"), list)
            and package_id in item["details"]["covered_package_ids"]
        ]
        observed_roles = {
            item["details"]["role"] for item in scoped_reviews
            if isinstance(item["details"].get("role"), str)
        }
        required_roles = {
            role for role in package.get("required_review_roles", [])
            if isinstance(role, str) and role
        }
        missing_roles = sorted(required_roles - observed_roles)
        if missing_roles:
            errors.append(
                f"review coverage for package {package_id} is missing required roles: "
                + ", ".join(missing_roles)
            )
        scoped_actors = {
            item.get("actor") for item in scoped_reviews if actor_valid(item.get("actor"))
        }
        minimum_reviewers = 2 if package.get("risk_level") in {"high", "critical"} else 1
        if len(scoped_actors) < minimum_reviewers:
            errors.append(
                f"review coverage for package {package_id} requires at least "
                f"{minimum_reviewers} independent reviewers"
            )
    return errors


def verification_recovery(state: dict[str, Any], approver: Any) -> tuple[list[str], int]:
    """A same-artifact retry requires independent fresh observations, then a new review."""
    records = global_evidence_records(state.get("validation_evidence", []), state.get("work_packages", []))
    failures: dict[tuple[str, str], dict[str, Any]] = {}
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for item in records:
        if (item.get("artifact_digest") != state.get("artifact_digest")
                or item.get("evidence_epoch") != state.get("evidence_epoch")
                or item.get("kind") == "review"):
            continue
        if not isinstance(item.get("kind"), str) or not isinstance(item.get("check_id"), str):
            continue
        key = (item["kind"], item["check_id"])
        latest[key] = item
        if item.get("result") == "fail":
            failures[key] = item
    errors: list[str] = []
    boundary = -1
    contributors = package_contributor_actors(state.get("work_packages", []))
    for key, failed in failures.items():
        retry = latest[key]
        if retry.get("result") != "pass":
            errors.append(f"verification retry still failing: {key[1]}")
        elif retry.get("actor") in contributors | {approver, failed.get("actor")}:
            errors.append(f"verification retry requires an independent actor: {key[1]}")
        elif not isinstance(retry.get("details"), dict) or not isinstance(retry["details"].get("recovery_observation"), str) or not retry["details"]["recovery_observation"].strip():
            errors.append(f"verification retry requires details.recovery_observation: {key[1]}")
        for index, item in enumerate(state.get("validation_evidence", [])):
            if item is retry:
                boundary = max(boundary, index)
    return errors, boundary


def completion_errors(data: dict[str, Any], root: Path | None) -> list[str]:
    errors: list[str] = []
    if data["phase"] != "complete" or data["status"] != "complete":
        errors.append("phase and status must both be complete")
    if data["blockers"]:
        errors.append("completion has unresolved blockers")
    if data.get("mode") != "review" and not any(
        isinstance(package, dict) and package.get("status") == "complete" for package in data["work_packages"]
    ):
        errors.append("non-review completion requires at least one completed work package for the current goal")
    incomplete = [
        package.get("id", "?") if isinstance(package, dict) else "?"
        for package in data["work_packages"]
        if not isinstance(package, dict) or package.get("status") not in {"complete", "superseded"}
    ]
    if incomplete:
        errors.append(f"incomplete work packages: {', '.join(incomplete)}")
    passing_evidence = [
        item for item in data.get("validation_evidence", [])
        if isinstance(item, dict) and item.get("result") == "pass"
        and item.get("artifact_digest") == data.get("artifact_digest")
        and item.get("evidence_epoch") == data.get("evidence_epoch")
    ]
    if not passing_evidence:
        errors.append("completion requires validation evidence")
    phase_review = [item for item in passing_evidence if item.get("kind") == "phase:review"]
    if not phase_review:
        errors.append("completion requires current passing evidence kind=phase:review")
    errors.extend(current_package_evidence_errors(
        data["work_packages"], data.get("artifact_digest"), data.get("evidence_epoch")
    ))
    latest_global: dict[tuple[Any, Any], dict[str, Any]] = {}
    for item in global_evidence_records(data.get("validation_evidence", []), data.get("work_packages", [])):
        if (
            item.get("artifact_digest") == data.get("artifact_digest")
            and item.get("evidence_epoch") == data.get("evidence_epoch")
        ):
            kind = item.get("kind") if isinstance(item.get("kind"), str) else None
            check_id = item.get("check_id") if isinstance(item.get("check_id"), str) else None
            latest_global[(kind, check_id)] = item
    if any(item.get("result") == "fail" for item in latest_global.values()):
        errors.append("completion has latest failing validation evidence on the current digest and epoch")
    approval_actor = data.get("approval", {}).get("actor") if isinstance(data.get("approval"), dict) else None
    recovery_errors, recovery_boundary = verification_recovery(data, approval_actor)
    errors.extend(recovery_errors)
    passing_reviews = [
        item for index, item in enumerate(data.get("validation_evidence", []))
        if index > recovery_boundary and item in passing_evidence
        and item.get("kind") == "review" and item.get("actor") != approval_actor
    ]
    errors.extend(review_coverage_errors(data["work_packages"], passing_reviews, approval_actor))
    if any(
        isinstance(item, dict) and item.get("kind") == "review" and item.get("result") == "fail"
        and item.get("artifact_digest") == data.get("artifact_digest")
        and item.get("evidence_epoch") == data.get("evidence_epoch")
        for item in data.get("validation_evidence", [])
    ):
        errors.append("completion has unresolved failing review evidence on the current digest")
    review = data["review"]
    if review.get("status") != "approved" or review.get("unresolved_blockers") != 0:
        errors.append("completion requires an approved review with zero blockers")
    digests = (data["artifact_digest"], data["approved_digest"], review.get("reviewed_digest"))
    if None in digests or len(set(digests)) != 1:
        errors.append("completion digests do not match")
    if not isinstance(data.get("approval"), dict) or data["approval"].get("scope") != "review":
        errors.append("completion requires the authoritative approval scope to be review")
    elif data["approval"].get("head_commit") != data.get("head_commit"):
        errors.append("Git HEAD differs from the final approval version")
    if root is not None and data["artifact_digest"] != artifact_digest(root):
        errors.append("workspace changed after completion evidence was recorded")
    if root is not None and data.get("head_commit") != git_head(root):
        errors.append("Git HEAD changed after the recorded completion revision")
    return errors


def save_mutation(
    root: Path,
    expected_task_id: str,
    expected_revision: int,
    event: str,
    actor: str,
    mutate: Any,
    details: dict[str, Any],
) -> dict[str, Any]:
    require_actor(actor)
    if not task_id_valid(expected_task_id):
        raise StateError(f"invalid expected task_id: {expected_task_id!r}")
    tasks_directory = root / ".longtask"
    with state_lock(tasks_directory):
        check_runtime_directory(tasks_directory, reclaim_temporary=True)
        path, pointed = load_state(root)
        if pointed.get("task_id") != expected_task_id:
            raise StateError(
                f"stale task: expected {expected_task_id}, found {pointed.get('task_id')}"
            )
        check_event_target(path.parent)
        current = json.loads(path.read_text(encoding="utf-8"))
        current_errors = validate_state(current, root, allow_workspace_drift=True)
        if current_errors:
            raise StateError("cannot mutate invalid state: " + "; ".join(current_errors))
        if current.get("task_id") != expected_task_id:
            raise StateError(
                f"stale task: expected {expected_task_id}, found {current.get('task_id')}"
            )
        if current.get("revision") != expected_revision:
            raise StateError(f"stale revision: expected {expected_revision}, found {current.get('revision')}")
        head = git_head(root)
        if current["head_commit"] != head:
            # Every mutation observes this boundary, including commands that refresh HEAD.
            current["head_commit"] = head
            current["evidence_epoch"] += 1
            current["approval"] = None
            current["approved_digest"] = None
            current["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
            if current["phase"] == "complete":
                current["phase"], current["status"] = "review", "active"
        mutate(current)
        current["revision"] = expected_revision + 1
        current["updated_at"] = now()
        errors = validate_state(current, root, allow_event_gap=True)
        if errors:
            raise StateError("invalid state mutation: " + "; ".join(errors))
        atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        try:
            append_event(path.parent, event, actor, current["revision"], details)
            current["event_revision"] = current["revision"]
            atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        except (OSError, StateError) as exc:
            current = json.loads(path.read_text(encoding="utf-8"))
            current["_runtime_warnings"] = [f"state persisted but event synchronization failed: {exc}"]
        return current


def build_handoff(
    payload: dict[str, Any], state: dict[str, Any], digest: str, head: str | None,
    actor: str, created_revision: int,
) -> dict[str, Any]:
    missing = HANDOFF_INPUT_KEYS - payload.keys()
    if missing:
        raise StateError("handoff payload missing: " + ", ".join(sorted(missing)))
    unexpected = set(payload) - HANDOFF_INPUT_KEYS
    if unexpected:
        raise StateError("unexpected handoff payload keys: " + ", ".join(sorted(unexpected)))
    handoff = {
        **payload,
        "artifact_digest": digest,
        "head_commit": head,
        "evidence_epoch": state["evidence_epoch"],
        "created_revision": created_revision,
        "created_by": actor,
        "created_at": now(),
    }
    validation_state = {**state, "revision": created_revision}
    errors = validate_handoff(handoff, validation_state)
    if errors:
        raise StateError("invalid handoff: " + "; ".join(errors))
    return handoff


def initial_handoff(state: dict[str, Any], actor: str) -> dict[str, Any]:
    if state["mode"] == "review":
        payload = {
            "intent": "review",
            "objective": "Independently review the current artifact against the recorded goal and evidence.",
            "reason": "The task was initialized through the review entry.",
            "target": {"kind": "artifact", "ref": "current-workspace"},
            "required_inputs": ["state:goal", "review:current", "validation_evidence:current"],
            "acceptance_checks": ["Review findings are recorded against the frozen artifact digest."],
            "next_if_pass": {"intent": "verify", "objective": "Verify review coverage and completion gates."},
            "next_if_fail": {"intent": "remediate", "objective": "Correct the recorded review findings."},
        }
    else:
        payload = {
            "intent": "plan",
            "objective": "Record the task acceptance criteria and work-package boundaries.",
            "reason": "The task has been initialized but its executable contract is not yet recorded.",
            "target": {"kind": "task", "ref": state["task_id"]},
            "required_inputs": ["state:goal", "docs/ARCHITECTURE.md"],
            "acceptance_checks": ["Acceptance criteria and recoverable work packages are recorded."],
            "next_if_pass": {"intent": "document", "objective": "Synchronize the approved design into persistent documentation."},
            "next_if_fail": {"intent": "decide", "objective": "Resolve missing or disputed task intent with the user."},
        }
    return build_handoff(payload, state, state["artifact_digest"], state["head_commit"], actor, 0)


def validate_replacement_target(root: Path, args: argparse.Namespace) -> None:
    """Compare opaque old identity only; never restore or migrate historical state."""
    if not (root / ".longtask/state.json").exists():
        raise StateError("--replace-active requires an existing active task")
    expected = (args.expected_task_id, args.expected_revision, args.expected_artifact_digest)
    if None in expected:
        raise StateError("--replace-active requires --expected-task-id, --expected-revision, and --expected-artifact-digest")
    _, active = load_state(root)
    if expected != (active.get("task_id"), active.get("revision"), active.get("artifact_digest")):
        raise StateError("active task changed before --replace-active; prior active task CAS does not match")
    current_version = (active.get("schema_version"), active.get("skill_version")) == (SCHEMA_VERSION, SKILL_VERSION)
    if current_version and active.get("artifact_digest") != artifact_digest(root):
        raise StateError("active workspace changed before --replace-active; checkpoint and reconcile it first")


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    require_actor(args.actor)
    root = Path(args.root).resolve()
    directory = task_dir(root, args.task_id)
    descriptor = open_directory_path(directory, create=True)
    os.close(descriptor)
    path = directory / "state.json"
    pending_path = directory / ".initializing.json"
    request = {
        "task_id": args.task_id, "mode": args.mode, "approval_policy": args.approval_policy,
        "goal": args.goal, "actor": args.actor, "replace_active": args.replace_active,
        "expected_task_id": args.expected_task_id, "expected_revision": args.expected_revision,
        "expected_artifact_digest": args.expected_artifact_digest,
    }
    with state_lock(directory):
        check_runtime_directory(directory, reclaim_temporary=True)
        if (directory / ".finishing.json").exists():
            raise StateError("checkpoint cleanup interrupted; retry finish before init")
        if pending_path.exists():
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            if not isinstance(pending, dict) or pending.get("request") != request:
                raise StateError("interrupted initialization requires the identical request and prior active task CAS")
            state = pending.get("state")
            errors = validate_state(state, allow_event_gap=True)
            if errors:
                raise StateError("invalid pending initialization: " + "; ".join(errors))
            if state["artifact_digest"] != artifact_digest(root) or state["head_commit"] != git_head(root):
                raise StateError("pending initialization workspace has changed; reconcile before retry")
            return publish_initialization(directory, state, args.actor)
        if path.exists() and not args.replace_active:
            raise StateError("an unfinished checkpoint already exists; finish it or explicitly --replace-active")
        if args.replace_active:
            validate_replacement_target(root, args)
        elif (directory / "events.jsonl").exists():
            raise StateError("checkpoint journal exists without state; reconcile before initialization")
        head = git_head(root)
        phase = {
            "setup": "discovery",
            "retrofit": "discovery",
            "continue": "architecture",
            "modify": "architecture",
            "review": "review",
        }[args.mode]
        state = {
            "schema_version": SCHEMA_VERSION,
            "skill_version": SKILL_VERSION,
            "task_id": args.task_id[:31] + "-" + secrets.token_hex(16),
            "mode": args.mode,
            "approval_policy": args.approval_policy,
            "phase": phase,
            "status": "active",
            "revision": 0,
            "event_revision": -1,
            "evidence_epoch": 0,
            "goal": args.goal,
            "base_commit": head,
            "head_commit": head,
            "artifact_digest": artifact_digest(root),
            "approved_digest": None,
            "approval": None,
            "review": {"status": "not_started", "reviewed_digest": None, "unresolved_blockers": 0},
            "work_packages": [],
            "validation_evidence": [],
            "blockers": [],
            "resolved_blockers": [],
            "next_action": "Record the task acceptance criteria and work-package boundaries.",
            "updated_at": now(),
        }
        state["handoff"] = initial_handoff(state, args.actor)
        state["next_action"] = state["handoff"]["objective"]
        errors = validate_state(state, allow_event_gap=True)
        if errors:
            raise StateError("cannot initialize state: " + "; ".join(errors))
        atomic_write(pending_path, json.dumps({"request": request, "state": state}, ensure_ascii=False, sort_keys=True) + "\n")
        return publish_initialization(directory, state, args.actor)


def publish_initialization(directory: Path, state: dict[str, Any], actor: str) -> dict[str, Any]:
    """A transient intent marker makes singleton replacement retryable without an archive."""
    path = directory / "state.json"
    try:
        # Retrying always recreates revision zero from its immutable initialization intent.
        atomic_write(directory / "events.jsonl", "")
        atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        append_event(directory, "initialized", actor, 0, {"mode": state["mode"]})
        state["event_revision"] = 0
        atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        (directory / ".initializing.json").unlink()
        fsync_directory(directory)
    except (OSError, StateError) as exc:
        raise StateError("initialization synchronization is incomplete; retry the identical init request") from exc
    return state


def cmd_finish(args: argparse.Namespace) -> dict[str, Any]:
    """Forget execution history only after its current result has converged into project content."""
    require_actor(args.actor)
    root = Path(args.root).resolve()
    directory = task_dir(root, args.expected_task_id)
    with state_lock(directory):
        check_runtime_directory(directory, reclaim_temporary=True)
        marker = directory / ".finishing.json"
        if (directory / ".initializing.json").exists():
            raise StateError("checkpoint initialization interrupted; retry init before finish")
        recovering = marker.exists()
        if recovering:
            if (directory / "state.json").exists():
                raise StateError("cleanup marker conflicts with active state; reconcile without deleting either")
            state = json.loads(marker.read_text(encoding="utf-8"))
        else:
            _, state = load_state(root)
        if not isinstance(state, dict):
            raise StateError("checkpoint must contain an object")
        if state.get("task_id") != args.expected_task_id:
            raise StateError(f"stale task: expected {args.expected_task_id}, found {state.get('task_id')}")
        if state.get("revision") != args.expected_revision:
            raise StateError(f"stale revision: expected {args.expected_revision}, found {state.get('revision')}")
        # A cleanup marker is the same already validated state, not a weaker completion receipt.
        errors = validate_state(state, None if recovering else root)
        if errors:
            raise StateError("cannot finish checkpoint: " + "; ".join(errors))
        errors.extend(completion_errors(state, root))
        knowledge = latest_current_checks(state, "knowledge")
        if not knowledge or any(item.get("result") != "pass" for item in knowledge):
            errors.append("finish requires current passing kind=knowledge evidence that project content is converged")
        if errors:
            raise StateError("cannot finish checkpoint: " + "; ".join(errors))
        result = {"finished": True, "task_id": state["task_id"], "revision": state["revision"],
                  "artifact_digest": state["artifact_digest"], "retained": [".longtask/.state.lock"]}
        # Keep the lock inode stable across generations. Unlinking it permits two independent locks.
        descriptor = open_directory_path(directory, create=False)
        try:
            if not recovering:
                os.replace("state.json", ".finishing.json", src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.fsync(descriptor)
            try:
                os.unlink("events.jsonl", dir_fd=descriptor)
            except FileNotFoundError:
                if not recovering:
                    raise
            os.fsync(descriptor)
            os.unlink(".finishing.json", dir_fd=descriptor)
            os.fsync(descriptor)
        except OSError as exc:
            raise StateError("checkpoint cleanup incomplete; retry finish with the same identity: " + str(exc)) from exc
        finally:
            os.close(descriptor)
        return result


def execution_scope_drift(packages: list[dict[str, Any]], current: dict[str, Any]) -> bool:
    """Attribute active changes, retaining only frozen peers from the same execution group."""
    active = [package for package in packages if package.get("status") == "active"]
    if not active:
        return False
    baseline = active[0].get("baseline_manifest")
    group = active[0].get("execution_group")
    if not isinstance(baseline, dict) or any(
        package.get("baseline_manifest") != baseline or package.get("execution_group") != group
        for package in active
    ):
        return True
    settled = [
        package for package in packages
        if group and package.get("execution_group") == group
        and package.get("baseline_manifest") == baseline
        and package.get("status") not in {"active", "superseded"}
        and isinstance(package.get("settlement_manifest"), dict)
    ]
    # Frozen peers cannot acquire new changes, including additions/deletions under directory writes.
    for package in settled:
        if any(write_set_covers_path(package.get("write_set"), path)
               for path in changed_manifest_paths(package["settlement_manifest"], current)):
            return True
    return any(
        sum(write_set_covers_path(package.get("write_set"), path) for package in active + settled) != 1
        for path in changed_manifest_paths(baseline, current)
    )


def invalidate_stale_packages(
    state: dict[str, Any], digest: str, head: str | None, root: Path,
) -> list[str]:
    stale: list[str] = []
    current_manifest = workspace_manifest(root)
    active_scope_drift = execution_scope_drift(state["work_packages"], current_manifest)
    for package in state["work_packages"]:
        status = package.get("status")
        if status not in {"ready", "active"}:
            continue
        lease_expired = package_lease_expired(package)
        stale_base = status == "ready" and package.get("base_revision") != digest
        scope_drift = status == "active" and active_scope_drift
        if stale_base or scope_drift or lease_expired:
            package["status"] = "blocked"
            if stale_base:
                package["stale_base"] = True
            if scope_drift:
                package["scope_drift"] = True
            if lease_expired:
                package["lease_expired"] = True
                if not scope_drift:
                    package["settlement_manifest"] = current_manifest
            stale.append(package.get("id", "?"))
    return stale


def package_lease_expired(package: dict[str, Any]) -> bool:
    if package.get("status") != "active" or not timestamp_valid(package.get("lease_expires")):
        return False
    expiry = datetime.fromisoformat(package["lease_expires"].replace("Z", "+00:00"))
    return expiry <= datetime.now(timezone.utc)


def cmd_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        state["artifact_digest"] = digest
        state["head_commit"] = head
        stale_packages = invalidate_stale_packages(state, digest, head, root)
        if state["approved_digest"] not in (None, digest):
            state["approved_digest"] = None
            state["approval"] = None
        if state["review"]["reviewed_digest"] not in (None, digest):
            state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
            if state["phase"] == "complete":
                state["phase"] = "review"
                state["status"] = "active"
        if args.next_action is not None:
            state["next_action"] = args.next_action
        elif stale_packages:
            state["next_action"] = "Rebase or reconcile stale packages: " + ", ".join(stale_packages)

    return save_mutation(root, args.expected_task_id, args.expected_revision, "checkpointed", args.actor, mutate, {"digest": digest})


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        state["artifact_digest"] = digest
        state["head_commit"] = head
        invalidate_stale_packages(state, digest, head, root)
        state["approved_digest"] = digest
        state["approval"] = {
            "scope": args.scope, "digest": digest, "head_commit": head,
            "actor": args.actor, "approved_at": now(),
        }
        if args.scope == "review":
            if state["phase"] != "review":
                raise StateError("review approval requires phase=review")
            if state["blockers"] or any(p.get("status") not in {"complete", "superseded"} for p in state["work_packages"]):
                raise StateError("review cannot be approved with blockers or incomplete work packages")
            if state["review"].get("unresolved_blockers") != 0:
                raise StateError("review cannot be approved while review blockers remain")
            package_evidence_errors = current_package_evidence_errors(
                state["work_packages"], digest, state["evidence_epoch"]
            )
            if package_evidence_errors:
                raise StateError("review approval failed: " + "; ".join(package_evidence_errors))
            recovery_errors, recovery_boundary = verification_recovery(state, args.actor)
            if recovery_errors:
                raise StateError("review approval failed: " + "; ".join(recovery_errors))
            passing_reviews = [
                item for index, item in enumerate(state["validation_evidence"])
                if index > recovery_boundary and item.get("kind") == "review" and item.get("result") == "pass"
                and item.get("artifact_digest") == digest and item.get("evidence_epoch") == state["evidence_epoch"]
                and item.get("actor") != args.actor
            ]
            coverage_errors = review_coverage_errors(state["work_packages"], passing_reviews, args.actor)
            if coverage_errors:
                raise StateError("review approval failed: " + "; ".join(coverage_errors))
            if any(
                item.get("kind") == "review" and item.get("result") == "fail"
                and item.get("artifact_digest") == digest and item.get("evidence_epoch") == state["evidence_epoch"]
                for item in state["validation_evidence"]
            ):
                raise StateError("review approval is blocked by unresolved failing review evidence on the current digest")
            state["review"] = {"status": "approved", "reviewed_digest": digest, "unresolved_blockers": 0}

    return save_mutation(root, args.expected_task_id, args.expected_revision, "approved", args.actor, mutate, {"scope": args.scope, "digest": digest})


def cmd_transition(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        old_index = PHASES.index(state["phase"])
        new_index = PHASES.index(args.phase)
        if args.phase == "complete" and state["phase"] != "review":
            raise StateError("completion transition requires phase=review")
        if new_index < old_index and (state["mode"] != "modify" or args.phase not in {"architecture", "documentation", "execution"}):
            raise StateError("backward transitions require modify mode and target architecture/documentation/execution")
        if new_index > old_index + 1:
            raise StateError("cannot skip an unproven phase transition")
        state["artifact_digest"] = digest
        if args.phase != "complete":
            state["head_commit"] = head
        stale_packages = invalidate_stale_packages(state, digest, head, root)
        if new_index == old_index + 1:
            required_kind = f"phase:{state['phase']}"
            evidence = latest_current_checks(state, required_kind)
            if not evidence or any(item["result"] != "pass" for item in evidence):
                raise StateError(f"transition requires current passing evidence kind={required_kind}")
        if args.phase == "review" and any(
            package.get("status") not in {"complete", "superseded"} for package in state["work_packages"]
        ):
            raise StateError("review transition requires all work packages complete or superseded")
        state["phase"] = args.phase
        state["status"] = args.status
        if args.next_action is not None:
            state["next_action"] = args.next_action
        elif stale_packages:
            state["next_action"] = "Rebase or reconcile stale packages: " + ", ".join(stale_packages)

    details = {"phase": args.phase, "status": args.status}
    return save_mutation(root, args.expected_task_id, args.expected_revision, "transitioned", args.actor, mutate, details)


def _parse_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise StateError(f"{label} must be a JSON object")
    return value


def cmd_handoff(args: argparse.Namespace) -> dict[str, Any]:
    """Atomically freeze the next window's role, target, exit checks, and branches."""
    root = Path(args.root).resolve()
    payload = _parse_object(args.data, "handoff")
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        state["artifact_digest"] = digest
        state["head_commit"] = head
        stale_packages = invalidate_stale_packages(state, digest, head, root)
        if state["approved_digest"] not in (None, digest):
            state["approved_digest"] = None
            state["approval"] = None
        if state["review"]["reviewed_digest"] not in (None, digest):
            state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
            if state["phase"] == "complete":
                state["phase"] = "review"
                state["status"] = "active"
        if stale_packages:
            raise StateError("cannot create handoff while packages require reconciliation: " + ", ".join(stale_packages))
        state["handoff"] = build_handoff(
            payload, state, digest, head, args.actor, state["revision"] + 1,
        )
        state["next_action"] = state["handoff"]["objective"]

    event_details = {
        "intent": payload.get("intent"),
        "target": payload.get("target"),
        "digest": digest,
    }
    return save_mutation(root, args.expected_task_id, args.expected_revision, "handoff_recorded", args.actor, mutate, event_details)


def cmd_package(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    package = _parse_object(args.data, "package")
    package_id = package.get("id")
    if not isinstance(package_id, str) or not package_id:
        raise StateError("package.id must be a non-empty string")
    if "evidence" in package:
        raise StateError("package payload must not contain evidence")
    forbidden_runtime = (set(package) & PACKAGE_RUNTIME_KEYS) - {"baseline_manifest"}
    if forbidden_runtime:
        raise StateError(
            "package payload must not contain runtime fields: " + ", ".join(sorted(forbidden_runtime))
        )
    # A caller-provided snapshot is never trusted; active transitions capture it from the workspace.
    package.pop("baseline_manifest", None)
    package.pop("contributors", None)
    current_digest = artifact_digest(root)
    current_head = git_head(root)
    package_status = package.get("status")
    if isinstance(package_status, str) and package_status == "active" and package_lease_expired(package):
        raise StateError("an active package lease must expire in the future")

    def mutate(state: dict[str, Any]) -> None:
        state["artifact_digest"] = current_digest
        state["head_commit"] = current_head
        invalidate_stale_packages(state, current_digest, current_head, root)
        packages = state["work_packages"]
        existing_package = next((item for item in packages if item.get("id") == package_id), None)
        started = isinstance(existing_package, dict) and isinstance(existing_package.get("baseline_manifest"), dict)
        if package_status in ("ready", "active") and not started and package.get("base_revision") != current_digest:
            raise StateError("ready/active package base_revision must match the current artifact digest")
        if started and package.get("status") != "superseded" and package.get("write_set") != existing_package.get("write_set"):
            raise StateError("started work-package write_set is immutable; supersede and replace the package")
        if (
            isinstance(existing_package, dict)
            and existing_package.get("status") in {"ready", "active", "blocked", "failed", "complete"}
            and package.get("base_revision") != existing_package.get("base_revision")
        ):
            raise StateError("work-package base_revision is immutable after work starts")
        if (
            isinstance(existing_package, dict)
            and existing_package.get("status") == "blocked"
            and (existing_package.get("scope_drift") or existing_package.get("stale_base"))
            and package.get("status") != "superseded"
        ):
            raise StateError("scope/stale-base blocked packages must be superseded and replaced with a new package ID")
        if package.get("status") == "active" and any(
            item.get("status") == "blocked" and item.get("scope_drift")
            for item in packages if item.get("id") != package_id
        ):
            raise StateError("supersede scope-drift blocked packages before starting replacement work")
        other_active = [
            item for item in packages
            if item.get("status") == "active" and item.get("id") != package_id
        ]
        current_manifest = workspace_manifest(root)
        if package.get("status") == "active" and not started and other_active and (
            any(item.get("base_revision") != current_digest for item in other_active)
            or any(item.get("baseline_manifest") != current_manifest for item in other_active)
        ):
            raise StateError("cannot start parallel work after an existing active package has workspace drift")
        if (
            isinstance(existing_package, dict)
            and existing_package.get("status") == "active"
            and not package_lease_expired(existing_package)
            and package.get("owner") != existing_package.get("owner")
            and existing_package.get("owner") != args.actor
        ):
            raise StateError("an unexpired active package owner can only be transferred by its current owner")
        contract_changed = existing_package is None or any(
            existing_package.get(key) != package.get(key) for key in PACKAGE_CONTRACT_KEYS
        )
        if contract_changed:
            state["evidence_epoch"] += 1
            for prior_package in packages:
                if prior_package.get("status") == "complete":
                    prior_package["stale_evidence"] = True
            if package.get("status") == "complete":
                package["status"] = "blocked"
                package["stale_evidence"] = True
        contributors = set(existing_package.get("contributors", [])) if isinstance(existing_package, dict) else set()
        if isinstance(existing_package, dict) and isinstance(existing_package.get("owner"), str):
            contributors.add(existing_package["owner"])
        if isinstance(package.get("owner"), str):
            contributors.add(package["owner"])
        contributors.add(args.actor)
        package["contributors"] = sorted(contributors)
        if isinstance(existing_package, dict) and isinstance(existing_package.get("evidence"), list):
            package["evidence"] = existing_package["evidence"]
        if isinstance(existing_package, dict) and "acceptance_evidence_start" in existing_package:
            package["acceptance_evidence_start"] = existing_package["acceptance_evidence_start"]
        if isinstance(existing_package, dict) and isinstance(existing_package.get("baseline_manifest"), dict):
            package["baseline_manifest"] = existing_package["baseline_manifest"]
        elif package.get("status") == "active":
            package["baseline_manifest"] = workspace_manifest(root)
        if started:
            if existing_package.get("execution_group"):
                package["execution_group"] = existing_package["execution_group"]
            if existing_package.get("settlement_manifest"):
                package["settlement_manifest"] = existing_package["settlement_manifest"]
        elif package.get("status") == "active":
            package["execution_group"] = other_active[0]["execution_group"] if other_active else secrets.token_hex(16)
        if started and package.get("status") in ("active", "complete"):
            if (existing_package.get("status") == "complete" and existing_package.get("settlement_manifest")
                and any(write_set_covers_path(existing_package.get("write_set"), path)
                        for path in changed_manifest_paths(existing_package["settlement_manifest"], current_manifest))):
                raise StateError("completed package has changes outside its frozen settlement; supersede and replace it")
            candidate = dict(package, status="active")
            if execution_scope_drift([candidate, *[p for p in packages if p.get("id") != package_id]], current_manifest):
                raise StateError("cannot resume or complete package with scope drift from its execution baseline")
        if package.get("status") == "complete" and not contract_changed:
            evidence_errors = current_package_evidence_errors(
                [package], current_digest, state["evidence_epoch"]
            )
            if evidence_errors:
                raise StateError("cannot complete work package: " + "; ".join(evidence_errors))
            package.pop("stale_base", None)
            package.pop("lease_expired", None)
            package.pop("stale_evidence", None)
        if package.get("baseline_manifest") and package.get("status") in ("complete", "failed", "blocked"):
            # Preserve an existing completion seal; re-completion must not bless changed frozen files.
            if not (existing_package and existing_package.get("status") == "complete"):
                package["settlement_manifest"] = current_manifest
        elif package.get("status") == "active":
            package.pop("settlement_manifest", None)
        for index, existing in enumerate(packages):
            if existing.get("id") == package_id:
                packages[index] = package
                break
        else:
            packages.append(package)
        if package.get("status") == "failed":
            invalidate_package_acceptance(package)
            blocked_dependents = block_dependency_closure(packages, package_id)
            for stopped in packages:
                if stopped.get("id") in blocked_dependents and stopped.get("baseline_manifest"):
                    stopped["settlement_manifest"] = current_manifest
        state["approved_digest"] = None
        state["approval"] = None
        if state["review"]["status"] != "not_started":
            state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
        if state["phase"] == "complete":
            state["phase"] = "review"
            state["status"] = "active"

    return save_mutation(root, args.expected_task_id, args.expected_revision, "package_upserted", args.actor, mutate, {"package_id": package_id})


def block_dependency_closure(packages: list[dict[str, Any]], failed_id: str) -> list[str]:
    """Atomically block every transitive consumer of a failed package."""
    blocked: list[str] = []
    frontier = {failed_id}
    while frontier:
        next_frontier: set[str] = set()
        for package in packages:
            package_id = package.get("id")
            if (
                isinstance(package_id, str)
                and package_id not in blocked
                and package.get("status") != "superseded"
                and any(dependency in frontier for dependency in package.get("dependencies", []))
            ):
                package["status"] = "blocked"
                package["dependency_failed"] = True
                invalidate_package_acceptance(package)
                blocked.append(package_id)
                next_frontier.add(package_id)
        frontier = next_frontier
    return blocked


def cmd_evidence(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    if args.package_id and not args.check_id:
        raise StateError("package evidence requires --check-id")
    digest = artifact_digest(root)
    head = git_head(root)
    details = _parse_object(args.details, "evidence details") if args.details is not None else None
    check_id = args.check_id or "global:" + hashlib.sha256(
        f"{args.kind}\0{args.summary}".encode()
    ).hexdigest()[:16]
    evidence = {
        "kind": args.kind,
        "summary": args.summary,
        "result": args.result,
        "command": args.command,
        "check_id": check_id,
        "details": details,
        "artifact_digest": digest,
        "evidence_epoch": None,
        "actor": args.actor,
        "recorded_at": now(),
    }

    def mutate(state: dict[str, Any]) -> None:
        if evidence["kind"] == "review":
            if not isinstance(details, dict):
                raise StateError("review evidence requires a reviewer envelope")
            # A collector's current CAS identity must never upgrade a delayed review.
            # HEAD changes have already advanced the epoch inside save_mutation.
            for key in ("task_id", "evidence_epoch", "head_commit"):
                if key not in details or details[key] != state[key]:
                    raise StateError(f"review envelope {key} does not match the current task binding")
            covered = details.get("covered_package_ids")
            if evidence["result"] == "pass" and not isinstance(covered, list):
                raise StateError("passing review evidence requires details.covered_package_ids")
            if isinstance(covered, list):
                eligible = {
                    item.get("id") for item in state["work_packages"]
                    if item.get("status") != "superseded"
                }
                unknown = sorted(item for item in covered if item not in eligible)
                if unknown:
                    raise StateError("review covers unknown or superseded work packages: " + ", ".join(unknown))
        evidence["evidence_epoch"] = state["evidence_epoch"]
        state["artifact_digest"] = digest
        state["head_commit"] = head
        invalidate_stale_packages(state, digest, head, root)
        if state["approved_digest"] not in (None, digest):
            state["approved_digest"] = None
            state["approval"] = None
        if state["review"]["reviewed_digest"] not in (None, digest):
            state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
            if state["phase"] == "complete":
                state["phase"] = "review"
                state["status"] = "active"
        state["validation_evidence"].append(evidence)
        package = None
        if args.package_id:
            package = next((item for item in state["work_packages"] if item.get("id") == args.package_id), None)
            if package is None:
                raise StateError(f"unknown work package: {args.package_id}")
            if package.get("status") not in {"active", "complete"}:
                raise StateError("package evidence requires an active or complete work package without scope drift")
            if check_id not in package.get("acceptance_checks", []):
                raise StateError("package evidence requires --check-id matching an acceptance check")
            package.setdefault("evidence", []).append(evidence)
        if evidence["result"] == "fail":
            state["approved_digest"] = None
            state["approval"] = None
            if package is not None:
                package["status"] = "failed"
                invalidate_package_acceptance(package)
                blocked_dependents = block_dependency_closure(state["work_packages"], args.package_id)
                failure_manifest = workspace_manifest(root)
                for stopped in state["work_packages"]:
                    if stopped.get("id") in [args.package_id, *blocked_dependents] and stopped.get("baseline_manifest"):
                        stopped["settlement_manifest"] = failure_manifest
                state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
                if state["phase"] in {"review", "complete"}:
                    state["phase"] = "review" if state["mode"] == "review" else "execution"
                state["status"] = "active"
                state["next_action"] = f"Repair and re-verify failed package {args.package_id}."
                if blocked_dependents:
                    state["next_action"] += " Reconcile blocked dependents: " + ", ".join(blocked_dependents) + "."
                state["handoff"] = build_handoff({
                    "intent": "remediate",
                    "objective": state["next_action"],
                    "reason": evidence["summary"],
                    "target": {"kind": "work_package", "ref": args.package_id},
                    "required_inputs": [f"work_package:{args.package_id}", f"validation_evidence:{check_id}"],
                    "acceptance_checks": package.get("acceptance_checks") or ["Re-verify the failed package."],
                    "next_if_pass": {
                        "intent": "review",
                        "objective": "Independently review the repaired package on a newly frozen digest.",
                    },
                    "next_if_fail": {
                        "intent": "decide",
                        "objective": "Escalate the repeatedly failing package or revise its contract.",
                    },
                }, state, digest, head, args.actor, state["revision"] + 1)
            elif evidence["kind"] == "review" or state["phase"] == "complete" or state["review"]["status"] == "approved":
                review_failure = evidence["kind"] == "review"
                if review_failure:
                    state["review"] = {"status": "failed", "reviewed_digest": digest,
                                       "unresolved_blockers": max(1, state["review"]["unresolved_blockers"])}
                elif state["review"]["unresolved_blockers"] == 0:
                    state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
                if state["phase"] == "complete":
                    state["phase"] = "review"
                state["status"] = "active"
                state["next_action"] = "Resolve the latest failing evidence and independently re-review the result."
                review_failure = evidence["kind"] == "review"
                finding_ids = [
                    item.get("id") for item in (details or {}).get("findings", [])
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("id")
                ] if review_failure else []
                checks = [f"Resolve review finding {finding_id}." for finding_id in finding_ids]
                if not checks:
                    checks = (["Resolve the failing review evidence and record a changed artifact digest."]
                              if review_failure else ["Independently retry the same check with recovery_observation, then obtain a new review."])
                state["handoff"] = build_handoff({
                    "intent": "remediate",
                    "objective": state["next_action"],
                    "reason": evidence["summary"],
                    "target": {
                        "kind": "review_findings" if review_failure else "artifact",
                        "ref": check_id,
                    },
                    "required_inputs": (
                        [f"validation_evidence:{check_id}", "review:current"]
                        if review_failure else [f"validation_evidence:{check_id}", "state:current"]
                    ),
                    "acceptance_checks": checks,
                    "next_if_pass": {
                        "intent": "review",
                        "objective": "Independently re-review the corrected artifact on a newly frozen digest.",
                    },
                    "next_if_fail": {
                        "intent": "decide",
                        "objective": "Escalate unresolved or disputed review findings for a scoped decision.",
                    },
                }, state, digest, head, args.actor, state["revision"] + 1)

    event_details = {"kind": args.kind, "result": args.result, "package_id": args.package_id, "check_id": check_id}
    return save_mutation(root, args.expected_task_id, args.expected_revision, "evidence_recorded", args.actor, mutate, event_details)


def cmd_blocker(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    digest = artifact_digest(root)
    head = git_head(root)
    if not task_id_valid(args.blocker_id):
        raise StateError(f"invalid blocker id: {args.blocker_id!r}")
    if args.action == "add" and not args.summary:
        raise StateError("adding a blocker requires --summary")

    def mutate(state: dict[str, Any]) -> None:
        state["artifact_digest"] = digest
        state["head_commit"] = head
        invalidate_stale_packages(state, digest, head, root)
        blockers = state["blockers"]
        existing = next((item for item in blockers if item.get("id") == args.blocker_id), None)
        if args.action == "add":
            if existing is not None:
                raise StateError(f"blocker already exists: {args.blocker_id}")
            if any(item.get("id") == args.blocker_id for item in state["resolved_blockers"]):
                raise StateError("resolved blocker ids cannot be reused; create a new occurrence id")
            blockers.append({
                "id": args.blocker_id,
                "summary": args.summary,
                "actor": args.actor,
                "created_at": now(),
                "opened_digest": digest,
                "opened_revision": state["revision"] + 1,
            })
            if isinstance(state.get("approval"), dict) and state["approval"].get("scope") == "review":
                state["approved_digest"] = None
                state["approval"] = None
            if state["review"]["status"] == "approved":
                state["review"] = {
                    "status": "failed",
                    "reviewed_digest": state["artifact_digest"],
                    "unresolved_blockers": max(1, state["review"]["unresolved_blockers"]),
                }
            if state["phase"] == "complete":
                state["phase"] = "review"
                state["status"] = "active"
        else:
            if existing is None:
                raise StateError(f"unknown blocker: {args.blocker_id}")
            checks = latest_current_checks(state, f"blocker:{args.blocker_id}")
            verification = next((item for item in checks if item["actor"] != args.actor), None)
            if any(item["result"] != "pass" for item in checks):
                verification = None
            if verification is None:
                raise StateError("resolving a blocker requires current independent evidence kind=blocker:{id}")
            if existing["opened_digest"] == digest:
                raise StateError("resolving a blocker requires a changed artifact digest for the fix revision")
            blockers.remove(existing)
            state["resolved_blockers"].append({
                **existing,
                "resolved_by": args.actor,
                "resolved_at": now(),
                "resolution_digest": digest,
                "verification_actor": verification["actor"],
            })

    details = {"action": args.action, "blocker_id": args.blocker_id}
    return save_mutation(root, args.expected_task_id, args.expected_revision, f"blocker_{args.action}", args.actor, mutate, details)


def cmd_review(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    if args.status == "approved":
        raise StateError("use approve --scope review for an approved review")
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        current_review = state["review"]
        if args.status == "not_started" and current_review["status"] != "not_started":
            raise StateError("review findings cannot be erased by resetting status to not_started")
        if args.status in {"failed", "conditionally_approved"} and args.unresolved_blockers == 0:
            raise StateError(f"review status {args.status} requires unresolved blockers")
        if args.unresolved_blockers < current_review["unresolved_blockers"]:
            if current_review.get("reviewed_digest") == digest:
                raise StateError("reducing review blockers requires a changed artifact digest")
            current = {**state, "artifact_digest": digest}
            remediation = latest_current_checks(current, "review-remediation")
            if (not remediation or any(item["result"] != "pass" for item in remediation)
                    or not any(item["actor"] != args.actor for item in remediation)):
                raise StateError("reducing review blockers requires current independent review-remediation evidence")
        state["artifact_digest"] = digest
        state["head_commit"] = head
        invalidate_stale_packages(state, digest, head, root)
        reviewed_digest = None if args.status in {"not_started", "superseded"} else digest
        state["review"] = {
            "status": args.status,
            "reviewed_digest": reviewed_digest,
            "unresolved_blockers": args.unresolved_blockers,
        }
        if isinstance(state.get("approval"), dict) and state["approval"].get("scope") == "review":
            state["approved_digest"] = None
            state["approval"] = None
        if state["phase"] == "complete":
            state["phase"] = "review"
            state["status"] = "active"

    details = {"status": args.status, "unresolved_blockers": args.unresolved_blockers, "digest": digest}
    return save_mutation(root, args.expected_task_id, args.expected_revision, "review_recorded", args.actor, mutate, details)


def cmd_modify(args: argparse.Namespace) -> dict[str, Any]:
    """Enter an authorized L4 change without replacing the task or unrelated packages."""
    root = Path(args.root).resolve()
    if not args.reason.strip():
        raise StateError("modify requires a non-empty reason")
    selected = set(args.package_id)
    digest, head = artifact_digest(root), git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        if state["phase"] == "discovery":
            raise StateError("establish architecture through the discovery exit before modify")
        packages = {p["id"]: p for p in state["work_packages"] if p["status"] != "superseded"}
        if selected - packages.keys():
            raise StateError("modify targets unknown or superseded packages: " + ", ".join(sorted(selected - packages.keys())))
        state["artifact_digest"], state["head_commit"] = digest, head
        invalidate_stale_packages(state, digest, head, root)
        affected = set(selected)
        for package_id in sorted(selected):
            affected.update(block_dependency_closure(state["work_packages"], package_id))
        for package_id in affected:
            packages[package_id].update(status="blocked", stale_base=True, stale_evidence=True)
        state["mode"], state["phase"], state["status"] = "modify", "architecture", "active"
        state["evidence_epoch"] += 1
        state["approval"], state["approved_digest"] = None, None
        state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
        state["next_action"] = "Revise the affected architecture and replace frozen packages after approval."
        state["handoff"] = build_handoff({
            "intent": "plan", "objective": state["next_action"], "reason": args.reason,
            "target": {"kind": "task", "ref": state["task_id"]},
            "required_inputs": ["docs/ARCHITECTURE.md", *[f"work_package:{p}" for p in sorted(affected)]],
            "acceptance_checks": ["Changed contracts and affected package replacements are reviewed."],
            "next_if_pass": {"intent": "document", "objective": "Update affected persistent contracts."},
            "next_if_fail": {"intent": "decide", "objective": "Resolve the architectural uncertainty."},
        }, state, digest, head, args.actor, state["revision"] + 1)

    return save_mutation(root, args.expected_task_id, args.expected_revision, "architecture_change_started",
                         args.actor, mutate, {"reason": args.reason, "package_ids": sorted(selected)})


def cmd_goal(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    if not args.goal.strip():
        raise StateError("goal must be non-empty")
    digest = artifact_digest(root)
    head = git_head(root)

    def mutate(state: dict[str, Any]) -> None:
        if state["goal"] == args.goal:
            raise StateError("new goal must differ from the current goal")
        state["goal"] = args.goal
        state["evidence_epoch"] += 1
        state["approved_digest"] = None
        state["approval"] = None
        state["review"] = {"status": "superseded", "reviewed_digest": None, "unresolved_blockers": 0}
        for package in state["work_packages"]:
            if package.get("status") != "superseded":
                package["status"] = "superseded"
            for flag in ("stale_base", "lease_expired", "scope_drift", "dependency_failed"):
                package.pop(flag, None)
        state["phase"] = "discovery"
        state["status"] = "active"
        state["next_action"] = "Re-establish acceptance criteria and work packages for the revised goal."
        state["artifact_digest"] = digest
        state["head_commit"] = head
        state["handoff"] = build_handoff({
            "intent": "plan",
            "objective": state["next_action"],
            "reason": "The task goal changed, so downstream contracts and evidence were superseded.",
            "target": {"kind": "task", "ref": state["task_id"]},
            "required_inputs": ["state:goal", "docs/ARCHITECTURE.md"],
            "acceptance_checks": ["Revised acceptance criteria and work packages are recorded."],
            "next_if_pass": {
                "intent": "document",
                "objective": "Synchronize the revised design into persistent documentation.",
            },
            "next_if_fail": {
                "intent": "decide",
                "objective": "Resolve disputed or incomplete task intent with the user.",
            },
        }, state, digest, head, args.actor, state["revision"] + 1)

    return save_mutation(root, args.expected_task_id, args.expected_revision, "goal_revised", args.actor, mutate, {"goal": args.goal})


def cmd_reconcile_events(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    require_actor(args.actor)
    tasks_directory = root / ".longtask"
    with state_lock(tasks_directory):
        check_runtime_directory(tasks_directory, reclaim_temporary=True)
        path, pointed = load_state(root)
        if pointed.get("task_id") != args.expected_task_id:
            raise StateError(f"stale task: expected {args.expected_task_id}, found {pointed.get('task_id')}")
        check_event_target(path.parent)
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("task_id") != args.expected_task_id:
            raise StateError(f"stale task: expected {args.expected_task_id}, found {state.get('task_id')}")
        if state.get("revision") != args.expected_revision:
            raise StateError(f"stale revision: expected {args.expected_revision}, found {state.get('revision')}")
        errors = validate_state(
            state, root, allow_event_gap=True, allow_event_log_ahead=True,
        )
        if errors:
            raise StateError("cannot reconcile structurally invalid state: " + "; ".join(errors))
        if state["event_revision"] >= state["revision"]:
            raise StateError("state has no event revision gap")
        prior = state["event_revision"]
        event_errors = validate_event_log(path.parent, state["revision"])
        if event_errors:
            append_event(
                path.parent,
                "event_gap_reconciled",
                args.actor,
                state["revision"],
                {"from_event_revision": prior, "reason": args.reason},
            )
        state["event_revision"] = state["revision"]
        state["updated_at"] = now()
        atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return state


def project_has_code(root: Path) -> bool:
    """Inspect the whole safe inventory; unknown artifacts require existing-project inspection."""
    documentation = {".md", ".rst", ".txt", ".adoc"}
    administrative = {"LICENSE", "LICENCE", "COPYING", ".gitignore", ".gitattributes", ".editorconfig"}
    for path in workspace_files(root):
        relative = path.relative_to(root)
        if path.name in administrative or path.suffix.lower() in documentation:
            continue
        if relative.parts[0] in {".github", ".vscode", ".idea"}:
            continue
        # Manifests, source files, scripts, data and unknown product types are all
        # evidence that setup must first inspect an existing implementation.
        return True
    return False


def handoff_phase_conflict(state: dict[str, Any], handoff: Any) -> bool:
    if not isinstance(handoff, dict):
        return False
    intent = handoff.get("intent")
    phase = state.get("phase")
    if intent == "plan":
        return phase not in {"discovery", "architecture"}
    if intent == "document":
        return phase not in {"architecture", "documentation"}
    if intent == "execute":
        return phase != "execution"
    if intent == "remediate":
        review_failed = state.get("review", {}).get("status") in {"failed", "conditionally_approved", "superseded"}
        package_failed = any(
            isinstance(item, dict) and item.get("status") in {"blocked", "failed"}
            for item in state.get("work_packages", [])
        )
        return phase not in {"execution", "review"} or (
            phase == "review" and not review_failed and not package_failed and not state.get("blockers")
        )
    if intent == "complete":
        return phase != "complete" or state.get("status") != "complete"
    return False


def build_resume_options(
    state: dict[str, Any],
    handoff: Any,
    handoff_stale: bool,
    handoff_conflict: bool,
    expired_packages: list[str],
) -> tuple[list[dict[str, Any]], str]:
    fresh_handoff = isinstance(handoff, dict) and not handoff_stale
    intent = handoff.get("intent") if isinstance(handoff, dict) else None
    phase = state.get("phase")
    resumable_review_intents = {"remediate", "verify", "decide"}
    resume_available = (
        fresh_handoff
        and not handoff_conflict
        and not expired_packages
        and intent != "complete"
        and phase != "complete"
        and (phase != "review" or intent in resumable_review_intents)
    )
    review_available = fresh_handoff
    unavailable_reason = (
        "handoff_missing_or_stale" if not fresh_handoff else
        "handoff_phase_conflict" if handoff_conflict else
        "lease_expired" if expired_packages else
        "phase_disallows_execution" if not resume_available else None
    )
    options = [
        {
            "id": "resume",
            "label": "继续执行断点",
            "description": "回到当前任务阶段和工作包断点继续，不启动审查。",
            "available": resume_available,
            "unavailable_reason": unavailable_reason,
        },
        {
            "id": "review",
            "label": "审查冻结产物",
            "description": "只读审查交接帧绑定的冻结目标。",
            "available": review_available,
            "unavailable_reason": None if review_available else "handoff_missing_or_stale",
        },
        {
            "id": "inspect",
            "label": "先查看状态",
            "description": "只读取总目标、断点、证据和风险，不开始执行或审查。",
            "available": True,
            "unavailable_reason": None,
        },
    ]
    if not fresh_handoff or handoff_stale or handoff_conflict or expired_packages:
        recommended = "inspect"
    elif handoff.get("intent") == "review":
        recommended = "review"
    elif not resume_available:
        recommended = "inspect"
    else:
        recommended = "resume"
    return options, recommended


def cmd_route(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    directory = root / ".longtask"
    try:
        check_runtime_directory(directory)
    except StateError as exc:
        return {"entry": "error", "reason": str(exc)}
    if any((directory / name).exists() for name in ("state.json", ".initializing.json", ".finishing.json", "events.jsonl")):
        try:
            path, state = load_state(root)
        except StateError as exc:
            return {"entry": "error", "reason": str(exc)}
        errors = validate_state(state, root, allow_workspace_drift=True)
        if errors:
            return {"entry": "error", "reason": "; ".join(errors), "state": str(path.relative_to(root))}
        current_digest = artifact_digest(root)
        current_head = git_head(root)
        handoff = state["handoff"]
        handoff_stale = bool(handoff) and (
            handoff.get("artifact_digest") != current_digest
            or handoff.get("head_commit") != current_head
            or handoff.get("evidence_epoch") != state["evidence_epoch"]
            or handoff.get("created_revision") != state["revision"]
        )
        handoff_conflict = handoff_phase_conflict(state, handoff)
        expired = [
            package.get("id", "?") for package in state["work_packages"]
            if isinstance(package, dict) and package_lease_expired(package)
        ]
        resume_options, recommended_choice = build_resume_options(
            state, handoff, handoff_stale, handoff_conflict, expired,
        )
        selected_choice = args.resume_choice
        selected_option = next(
            (option for option in resume_options if option["id"] == selected_choice), None,
        )
        if expired:
            entry, reason = "continue", "active package lease expired; checkpoint before resuming writes"
            completion_valid = False
            stale = current_digest != state["artifact_digest"]
            review = state["review"]
        else:
            stale = current_digest != state["artifact_digest"]
            review = state["review"]
            completion_valid = (
                state["phase"] == "complete"
                and state["status"] == "complete"
                and not stale
                and not completion_errors(state, root)
            )
            if completion_valid:
                entry, reason = "complete", "revision-bound completion invariants hold"
            elif isinstance(handoff, dict) and not handoff_stale and handoff_conflict:
                entry = "review" if state["phase"] in {"review", "complete"} else "continue"
                reason = "structured handoff intent conflicts with the task phase; reconcile the phase before acting"
            elif isinstance(handoff, dict) and not handoff_stale and handoff.get("intent") == "review":
                entry, reason = "review", "structured handoff recommends review of the frozen target"
            elif isinstance(handoff, dict) and not handoff_stale and handoff.get("intent") in {
                "plan", "document", "execute", "remediate", "verify", "decide",
            }:
                entry, reason = "continue", f"structured handoff recommends {handoff['intent']} work"
            elif isinstance(handoff, dict) and handoff_stale:
                entry = "review" if handoff.get("intent") in {"review", "complete"} or state["phase"] in {"review", "complete"} else "continue"
                reason = "structured handoff target is stale; reconcile and record a new frozen handoff before acting"
            elif state["phase"] in {"review", "complete"} or review["status"] in {"failed", "conditionally_approved", "approved", "superseded"}:
                entry, reason = "review", "review is required or stale"
            else:
                entry, reason = "continue", "active task state is valid"
                if stale:
                    reason = "workspace changed since the last checkpoint"

        selection_available = selected_option is None or bool(selected_option["available"])
        if selected_choice == "inspect":
            entry, reason = "continue", "resume choice requests read-only state inspection"
        elif selected_choice == "resume" and selection_available:
            entry, reason = "continue", "resume choice requests continuation from the current task checkpoint"
        elif selected_choice == "review" and selection_available:
            entry, reason = "review", "resume choice requests review of the frozen handoff target"
        elif selected_choice is not None and not selection_available:
            entry = "continue"
            reason = f"resume choice {selected_choice} is unavailable; inspect and reconcile the task state"
        return {
            "entry": entry,
            "reason": reason,
            "task_id": state["task_id"],
            "phase": state["phase"],
            "status": state["status"],
            "revision": state["revision"],
            "stale": stale,
            "goal": state["goal"],
            "handoff": handoff,
            "handoff_stale": handoff_stale if handoff is not None else None,
            "handoff_conflict": handoff_conflict if handoff is not None else None,
            "choice_required": selected_choice is None and not completion_valid,
            "selected_choice": selected_choice,
            "selection_available": selection_available,
            "recommended_choice": recommended_choice,
            "resume_options": resume_options,
            "expired_packages": expired,
            "assurance": "cooperative",
            "state": str(path.relative_to(root)),
        }
    if (root / "docs/tasks/_ACTIVE.md").exists():
        return {"entry": "error", "reason": "legacy task-directory layout is unsupported; merge project knowledge explicitly; no migration is performed"}
    if (root / "docs" / "ARCHITECTURE.md").exists():
        return {"entry": "continue", "reason": "project content is available; initialize a temporary checkpoint only for unfinished work"}
    if project_has_code(root):
        return {"entry": "retrofit", "reason": "existing code detected without longtask architecture"}
    return {"entry": "setup", "reason": "no longtask architecture or existing-code signal detected"}


def cmd_doctor(args: argparse.Namespace) -> dict[str, Any]:
    """Read-only diagnostics; action names are generated here, never executable state text."""
    route = cmd_route(argparse.Namespace(root=args.root, resume_choice="inspect"))
    issues: list[dict[str, Any]] = []
    def issue(code: str, message: str, command: str, **context: Any) -> None:
        issues.append({"code": code, "message": message,
                       "next_operation": {"command": command, **context}})
    if route["entry"] == "error":
        issue("invalid_state", route["reason"], "inspect-storage")
    elif "state" in route:
        _, state = load_state(Path(args.root).resolve())
        state = {**state, "artifact_digest": artifact_digest(Path(args.root).resolve())}
        if (state["phase"] == "complete" and state["status"] == "complete"
                and not route.get("stale") and not completion_errors(state, Path(args.root).resolve())):
            return {"read_only": True, "route": route, "issues": [], "healthy": True,
                    "next_operation": {"command": "finish"}, "assurance": "cooperative"}
        if route.get("handoff_stale"):
            issue("stale_handoff", "Coordinate current state, then freeze a new handoff.", "handoff")
        if route.get("handoff_conflict"):
            issue("handoff_phase_conflict", "Reconcile the handoff intent with the current phase.", "handoff")
        for package in state["work_packages"]:
            package_id = package["id"]
            if package_id in route.get("expired_packages", []):
                issue("lease_expired", "Checkpoint and renew ownership before writing.", "checkpoint", package_id=package_id)
            if package.get("scope_drift") or package.get("stale_base"):
                issue("replacement_required", "Supersede this package and create a current contract with a new ID.", "package", package_id=package_id)
            checks = {item["check_id"]: item for item in package_acceptance_evidence(package)
                      if item.get("artifact_digest") == state["artifact_digest"] and item.get("evidence_epoch") == state["evidence_epoch"]}
            missing = [check for check in package["acceptance_checks"] if checks.get(check, {}).get("result") != "pass"]
            if missing and package["status"] != "superseded":
                issue("package_evidence_missing", "Run the declared acceptance checks on the current artifact.", "evidence", package_id=package_id, check_ids=missing)
        for blocker in state["blockers"]:
            issue("open_blocker", blocker["summary"], "blocker", blocker_id=blocker["id"])
        if state["review"]["unresolved_blockers"]:
            issue("review_blockers", "Resolve product findings on a changed artifact and obtain independent verification.", "review")
        recovery_errors, _ = verification_recovery(state, None)
        for message in recovery_errors:
            issue("verification_recovery", message, "evidence")
        if state["phase"] == "complete":
            for message in completion_errors(state, Path(args.root).resolve()):
                issue("completion_invalid", message, "review")
        else:
            kind = f"phase:{state['phase']}"
            checks = latest_current_checks(state, kind)
            if not checks or any(item["result"] != "pass" for item in checks):
                issue("phase_evidence_missing", "Record current phase exit evidence before advancing.", "evidence", kind=kind)
    next_operation = {"command": "finish"} if "state" in route and route.get("phase") == "complete" and not issues else None
    return {"read_only": True, "route": route, "issues": issues,
            "healthy": not issues, "next_operation": next_operation, "assurance": "cooperative"}


def summarize_output(output: dict[str, Any]) -> dict[str, Any]:
    """Keep CAS and warnings visible while omitting growing evidence/history arrays."""
    if "schema_version" not in output or "task_id" not in output:
        return output
    fields = ("task_id", "revision", "event_revision", "artifact_digest", "head_commit",
              "evidence_epoch", "phase", "status", "mode", "next_action", "_runtime_warnings")
    summary = {key: output[key] for key in fields if key in output}
    summary.update(work_packages=len(output.get("work_packages", [])),
                   validation_evidence=len(output.get("validation_evidence", [])),
                   blockers=len(output.get("blockers", [])), review=output.get("review"),
                   state=".longtask/state.json", output="summary")
    return summary


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    path, state = load_state(root)
    errors = validate_state(state, root)
    if errors:
        raise StateError("; ".join(errors))
    return {"valid": True, "task_id": state["task_id"], "revision": state["revision"], "state": str(path.relative_to(root))}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route")
    route.add_argument("--root", required=True)
    route.add_argument("--resume-choice", choices=("resume", "review", "inspect"))
    route.set_defaults(function=cmd_route)

    initialize = subparsers.add_parser("init")
    initialize.add_argument("--root", required=True)
    initialize.add_argument("--task-id", default="checkpoint", help="optional label prefix; task_id returned by init is a fresh runtime-minted CAS identity")
    initialize.add_argument("--mode", choices=MODES, required=True)
    initialize.add_argument("--approval-policy", choices=APPROVAL_POLICIES, default="guarded")
    initialize.add_argument("--goal", required=True)
    initialize.add_argument("--actor", default="agent")
    initialize.add_argument("--replace-active", action="store_true")
    initialize.add_argument("--expected-task-id")
    initialize.add_argument("--expected-revision", type=int)
    initialize.add_argument("--expected-artifact-digest")
    initialize.set_defaults(function=cmd_init)

    for name, function in (("checkpoint", cmd_checkpoint), ("approve", cmd_approve), ("transition", cmd_transition), ("finish", cmd_finish)):
        command = subparsers.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--expected-task-id", required=True)
        command.add_argument("--expected-revision", type=int, required=True)
        command.add_argument("--actor", default="agent")
        if name == "checkpoint":
            command.add_argument("--next-action")
        elif name == "approve":
            command.add_argument("--scope", required=True)
        elif name == "transition":
            command.add_argument("--phase", choices=PHASES, required=True)
            command.add_argument("--status", choices=STATUSES, required=True)
            command.add_argument("--next-action")
        command.set_defaults(function=function)

    package = subparsers.add_parser("package", help="atomically add or replace a work-package object")
    package.add_argument("--root", required=True)
    package.add_argument("--expected-task-id", required=True)
    package.add_argument("--expected-revision", type=int, required=True)
    package.add_argument("--data", required=True, help="complete contract input JSON, not a returned package record; exclude evidence/runtime fields (see references/状态协议.md)")
    package.add_argument("--actor", default="agent")
    package.set_defaults(function=cmd_package)

    handoff = subparsers.add_parser("handoff", help="record a structured, version-bound cross-window continuation frame")
    handoff.add_argument("--root", required=True)
    handoff.add_argument("--expected-task-id", required=True)
    handoff.add_argument("--expected-revision", type=int, required=True)
    handoff.add_argument("--data", required=True, help="handoff JSON without runtime-derived binding fields")
    handoff.add_argument("--actor", default="agent")
    handoff.set_defaults(function=cmd_handoff)

    evidence = subparsers.add_parser("evidence", help="record revision-bound validation evidence")
    evidence.add_argument("--root", required=True)
    evidence.add_argument("--expected-task-id", required=True)
    evidence.add_argument("--expected-revision", type=int, required=True)
    evidence.add_argument("--kind", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--result", choices=("pass", "fail"), required=True)
    evidence.add_argument("--command")
    evidence.add_argument("--check-id")
    evidence.add_argument("--details", help="JSON object; required reviewer envelope for kind=review")
    evidence.add_argument("--package-id")
    evidence.add_argument("--actor", default="agent")
    evidence.set_defaults(function=cmd_evidence)

    blocker = subparsers.add_parser("blocker", help="atomically add or resolve a task blocker")
    blocker.add_argument("--root", required=True)
    blocker.add_argument("--expected-task-id", required=True)
    blocker.add_argument("--expected-revision", type=int, required=True)
    blocker.add_argument("--action", choices=("add", "resolve"), required=True)
    blocker.add_argument("--blocker-id", required=True)
    blocker.add_argument("--summary")
    blocker.add_argument("--actor", default="agent")
    blocker.set_defaults(function=cmd_blocker)

    review = subparsers.add_parser("review", help="record a non-approved review state")
    review.add_argument("--root", required=True)
    review.add_argument("--expected-task-id", required=True)
    review.add_argument("--expected-revision", type=int, required=True)
    review.add_argument("--status", choices=tuple(status for status in REVIEW_STATUSES if status != "approved"), required=True)
    review.add_argument("--unresolved-blockers", type=int, default=0)
    review.add_argument("--actor", default="reviewer")
    review.set_defaults(function=cmd_review)

    modify = subparsers.add_parser("modify", help="enter an authorized architecture change and freeze affected packages")
    modify.add_argument("--root", required=True)
    modify.add_argument("--expected-task-id", required=True)
    modify.add_argument("--expected-revision", type=int, required=True)
    modify.add_argument("--package-id", action="append", default=[], help="affected package; repeat for multiple roots")
    modify.add_argument("--reason", required=True)
    modify.add_argument("--actor", default="agent")
    modify.set_defaults(function=cmd_modify)

    goal = subparsers.add_parser("goal", help="revise task intent and supersede downstream evidence")
    goal.add_argument("--root", required=True)
    goal.add_argument("--expected-task-id", required=True)
    goal.add_argument("--expected-revision", type=int, required=True)
    goal.add_argument("--goal", required=True)
    goal.add_argument("--actor", default="agent")
    goal.set_defaults(function=cmd_goal)

    reconcile = subparsers.add_parser("reconcile-events", help="repair a detected event revision gap")
    reconcile.add_argument("--root", required=True)
    reconcile.add_argument("--expected-task-id", required=True)
    reconcile.add_argument("--expected-revision", type=int, required=True)
    reconcile.add_argument("--reason", required=True)
    reconcile.add_argument("--actor", default="operator")
    reconcile.set_defaults(function=cmd_reconcile_events)

    doctor = subparsers.add_parser("doctor", help="read-only recovery diagnostics and structured next operations")
    doctor.add_argument("--root", required=True)
    doctor.set_defaults(function=cmd_doctor)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--root", required=True)
    validate.set_defaults(function=cmd_validate)
    for command in subparsers.choices.values():
        command.add_argument("--output", choices=("full", "summary"), default="full",
                             help="summary keeps CAS/version fields and omits state history")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output = args.function(args)
    except StateError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"error": f"state operation failed safely: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    if args.output == "summary":
        output = summarize_output(output)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
