#!/usr/bin/env python3
"""Bounded, read-only discovery and retrieval of current project Markdown knowledge.

No network, subprocess, state mutation, or stored index. JSON text is candidate
data: it neither authenticates its author nor grants implementation permission.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
import sys
import unicodedata
from urllib.parse import quote, unquote

MAX_FILE_BYTES = 262144
MAX_SCAN_BYTES = 8388608
MAX_NODES = 8192
MAX_DEPTH = 12
MAX_REF_BYTES = 4096
MAX_READ_BYTES = 1048576
MAX_SECTIONS = 1024
MAX_INDEX_ENTRIES = 8192
MAX_SNAPSHOTS = 256
TRUST = {"classification": "candidate_data", "grants_authorization": False}


class KnowledgeError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def fail(code: str, message: str):
    raise KnowledgeError(code, message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def relative_path(value: str) -> str:
    if (not value or len(value.encode("utf-8")) > MAX_REF_BYTES
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or "\\" in value or value.startswith("/")):
        fail("invalid_path", "Expected a bounded repository-relative Markdown path")
    parts = value.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        fail("invalid_path", "Empty, dot and parent path components are forbidden")
    if not value.endswith(".md") or parts[0] in {".git", ".longtask"}:
        fail("invalid_path", "Only current Markdown knowledge is readable")
    return value


def parse_ref(value: str):
    if (len(value.encode("utf-8")) > MAX_REF_BYTES or value.count("#") > 1
            or re.search(r"%(?![0-9A-Fa-f]{2})", value)):
        fail("invalid_reference", "Malformed or oversized reference")
    path, separator, anchor = value.partition("#")
    try:
        path = unquote(path, errors="strict")
        anchor = unquote(anchor, errors="strict") if separator else None
    except UnicodeError:
        fail("invalid_reference", "Reference must decode as UTF-8")
    if separator and (not anchor or any(ord(c) < 32 or ord(c) == 127 for c in anchor)):
        fail("invalid_reference", "Empty or control-character section identity")
    return relative_path(path), anchor


def reference(path, anchor=None):
    value = quote(path, safe="/") + ("#" + quote(anchor, safe="-_") if anchor else "")
    if len(value.encode("utf-8")) > MAX_REF_BYTES:
        fail("invalid_reference", f"Encoded reference exceeds {MAX_REF_BYTES} bytes")
    parse_ref(value)
    return value


class Workspace:
    """Use descriptor-relative, no-follow opens, then revalidate before output.

    Refuse unsupported hosts rather than fall back to check-then-open traversal.
    Each component, including root ancestors, is opened without following links.
    """

    def __init__(self, root):
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
                or os.scandir not in os.supports_fd):
            fail("unsupported_host", "Descriptor-relative no-follow reads are required")
        root = os.path.abspath(root)
        self.root = root
        self.files = {}
        self.directories = {}
        self.missing = set()
        self.probes = {}
        self.total_bytes = 0
        self.nodes = 0
        self.root_fd = self._open_absolute_root()

    def _open_absolute_root(self):
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in PurePosixPath(self.root).parts[1:]:
                new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                 dir_fd=fd)
                os.close(fd)
                fd = new_fd
            return fd
        except OSError:
            os.close(fd)
            fail("unsafe_root", "Root must be an existing directory without symlink components")

    def close(self):
        for values in (self.files, self.directories, self.probes):
            for _, _, fd in values.values():
                os.close(fd)
        os.close(self.root_fd)

    def remember(self, values, path, expected, content, fd):
        if path in values:
            os.close(values[path][2])
        elif len(self.files) + len(self.directories) + len(self.probes) >= MAX_SNAPSHOTS:
            fail("input_too_large", f"Knowledge scan exceeds {MAX_SNAPSHOTS} pinned snapshots; narrow --path")
        # Keep the original inode alive until output, preventing rapid reuse of
        # its identity after unlink on filesystems with coarse timestamps.
        values[path] = (expected, content, os.dup(fd))

    def open_path(self, path, directory=False):
        fd = os.dup(self.root_fd)
        try:
            parts = path.split("/")
            for i, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if i < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                new_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = new_fd
            return fd
        except OSError as exc:
            os.close(fd)
            if exc.errno == 2:
                fail("missing_path", f"Missing knowledge path: {path}")
            fail("unsafe_path", f"Cannot safely open knowledge path: {path}")

    def read_bytes(self, fd, path):
        chunks, count = [], 0
        while True:
            chunk = os.read(fd, min(65536, MAX_FILE_BYTES + 1 - count))
            if not chunk:
                break
            chunks.append(chunk)
            count += len(chunk)
            if count > MAX_FILE_BYTES:
                fail("input_too_large", f"File grew beyond input limit: {path}")
        return b"".join(chunks)

    def read(self, path):
        relative_path(path)
        fd = self.open_path(path)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                fail("unsafe_path", f"Knowledge input is not a regular file: {path}")
            if before.st_size > MAX_FILE_BYTES:
                fail("input_too_large", f"File exceeds {MAX_FILE_BYTES} bytes: {path}")
            data = self.read_bytes(fd, path)
            os.lseek(fd, 0, os.SEEK_SET)
            if (fingerprint(before) != fingerprint(os.fstat(fd)) or len(data) != before.st_size
                    or data != self.read_bytes(fd, path)):
                fail("read_race", f"Knowledge changed while reading: {path}")
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                fail("invalid_text", f"Knowledge is not UTF-8: {path}")
            if "\x00" in text:
                fail("invalid_text", f"NUL in knowledge text: {path}")
            self.remember(self.files, path, fingerprint(before), sha256(data), fd)
            self.total_bytes += len(data)
            if self.total_bytes > MAX_SCAN_BYTES:
                fail("input_too_large", f"Knowledge scan exceeds {MAX_SCAN_BYTES} bytes")
            return data, text
        finally:
            os.close(fd)

    def walk(self, path, depth=0):
        if depth > MAX_DEPTH:
            fail("input_too_large", "Knowledge directory nesting exceeds limit")
        fd = self.open_path(path, directory=True)
        try:
            before = os.fstat(fd)
            # scandir streams entries so enormous directories cannot allocate an
            # unbounded name list before the traversal limit is applied.
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    self.nodes += 1
                    if self.nodes > MAX_NODES:
                        fail("input_too_large", "Knowledge traversal exceeds entry limit")
                    names.append(entry.name)
            self.remember(self.directories, path, fingerprint(before), sorted(names), fd)
            for name in sorted(names):
                child = f"{path}/{name}"
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    fail("unsafe_path", f"Symlink in knowledge tree: {child}")
                if stat.S_ISDIR(info.st_mode):
                    yield from self.walk(child, depth + 1)
                elif child.endswith(".md"):
                    yield relative_path(child)
            if fingerprint(before) != fingerprint(os.fstat(fd)):
                fail("read_race", f"Knowledge directory changed: {path}")
        except OSError:
            fail("read_race", f"Knowledge directory changed or became inaccessible: {path}")
        finally:
            os.close(fd)

    def exists(self, path):
        try:
            fd = self.open_path(path)
        except KnowledgeError as exc:
            if exc.code == "missing_path":
                self.missing.add(path)
                return False
            raise
        os.close(fd)
        return True

    def probe(self, path):
        """Pin an unselected link target's identity without loading its body."""
        try:
            fd = self.open_path(path)
        except KnowledgeError as exc:
            if exc.code == "missing_path":
                self.missing.add(path)
                return False
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                fail("unsafe_path", f"Markdown target is not a regular file: {path}")
            self.remember(self.probes, path, fingerprint(info), None, fd)
            return True
        finally:
            os.close(fd)

    def verify(self):
        current_root = self._open_absolute_root()
        try:
            original = os.fstat(self.root_fd)
            current = os.fstat(current_root)
            if (original.st_dev, original.st_ino) != (current.st_dev, current.st_ino):
                fail("read_race", "Workspace root was replaced")
        finally:
            os.close(current_root)
        for directory, values in ((False, self.files), (True, self.directories)):
            for path, (expected, content, _) in values.items():
                try:
                    fd = self.open_path(path, directory=directory)
                except KnowledgeError:
                    fail("read_race", f"Knowledge path changed before output: {path}")
                try:
                    if fingerprint(os.fstat(fd)) != expected:
                        fail("read_race", f"Knowledge changed before output: {path}")
                    if directory:
                        names = []
                        with os.scandir(fd) as entries:
                            for entry in entries:
                                names.append(entry.name)
                                if len(names) > MAX_NODES:
                                    fail("read_race", f"Knowledge directory grew before output: {path}")
                        changed = sorted(names) != content
                    else:
                        changed = sha256(self.read_bytes(fd, path)) != content
                    if changed or fingerprint(os.fstat(fd)) != expected:
                        fail("read_race", f"Knowledge content changed before output: {path}")
                finally:
                    os.close(fd)
        for path, (expected, _, _) in self.probes.items():
            try:
                fd = self.open_path(path)
            except KnowledgeError:
                fail("read_race", f"Reference target changed before output: {path}")
            try:
                if fingerprint(os.fstat(fd)) != expected:
                    fail("read_race", f"Reference target changed before output: {path}")
            finally:
                os.close(fd)
        for path in self.missing:
            try:
                fd = self.open_path(path)
            except KnowledgeError as exc:
                if exc.code == "missing_path":
                    continue
                fail("read_race", f"Absent knowledge path changed before output: {path}")
            os.close(fd)
            fail("read_race", f"New knowledge path appeared before output: {path}")


def slug(title):
    plain = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", title)
    plain = re.sub(r"<[^>]*>", "", plain).replace("`", "")
    plain = unicodedata.normalize("NFC", plain).lower()
    return "".join(c for c in plain if c.isalnum() or c in "-_" or c.isspace()).strip().replace(" ", "-")


def sections(data: bytes):
    """ATX/setext sections and standalone HTML ids; ignore fenced examples.

    Heading identity never contains line numbers or auto-numbered duplicates.
    Explicit heading {#id} or preceding standalone <a id="id"></a> is preferred.
    Other Markdown/HTML constructs are not interpreted or executed.
    """
    lines = data.decode("utf-8").splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line.encode("utf-8"))
    headings, fence, previous = [], None, None
    pending_anchor = None
    for i, line in enumerate(lines):
        raw = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", raw)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            previous = pending_anchor = None
            continue
        if marker:
            fence = marker[1]
            previous = pending_anchor = None
            continue
        anchor = re.fullmatch(r'\s*<a\s+(?:id|name)=["\']([^"\']+)["\']\s*>\s*</a>\s*', raw)
        if anchor:
            pending_anchor = (anchor[1], i)
            previous = None
            continue
        atx = re.match(r"^ {0,3}(#{1,6})(?:\s+|$)(.*?)\s*$", raw)
        setext = re.fullmatch(r" {0,3}(=+|-+)\s*", raw)
        start = i
        if atx:
            level, title = len(atx[1]), re.sub(r"\s+#+\s*$", "", atx[2]).strip()
        elif setext and previous is not None:
            start, title = previous
            level = 1 if setext[1][0] == "=" else 2
        else:
            had_previous = previous is not None
            previous = (i, raw.strip()) if raw.strip() else None
            if had_previous or not raw.strip():
                pending_anchor = None
            continue
        explicit = re.search(r"\s+\{#([^{}\s]+)\}$", title)
        identity = explicit[1] if explicit else pending_anchor[0] if pending_anchor else slug(title)
        if explicit:
            title = title[:explicit.start()].strip()
        if pending_anchor:
            start = pending_anchor[1]
        headings.append({"title": title, "anchor": identity, "level": level,
                         "start": offsets[start], "line_start": start + 1})
        if len(headings) > MAX_SECTIONS:
            fail("input_too_large", f"Document exceeds {MAX_SECTIONS} headings")
        previous = pending_anchor = None
    counts = Counter(item["anchor"] for item in headings)
    for i, item in enumerate(headings):
        end = next((other for other in headings[i + 1:] if other["level"] <= item["level"]), None)
        item["end"] = end["start"] if end else len(data)
        item["line_end"] = end["line_start"] - 1 if end else len(lines)
        item["ambiguous"] = not item["anchor"] or counts[item["anchor"]] != 1
    return headings


def clues(text, query=""):
    lines = text.splitlines()
    purpose = next((s.strip()[:160] for s in lines if s.strip()
                    and not s.lstrip().startswith(("#", "```", "~~~", "---", "<a "))
                    and not re.match(r"(?:最后更新|来源|状态|版本|updated|date)\s*[：:]",
                                     s.strip().replace("*", ""), re.IGNORECASE)), "")
    matches = []
    for i, line in enumerate(lines):
        if query and query.casefold() in line.casefold():
            matches.append({"line": i + 1, "excerpt": line.strip()[:160]})
            if len(matches) == 3:
                break
    return purpose, matches


def encoded_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")


def index(workspace, paths=None, query="", limit=100, budget=16384, offset=0):
    explicit_paths = paths is not None
    paths = [relative_path(p) for p in paths] if paths else None
    if paths is None:
        paths = []
        if workspace.exists("docs/ARCHITECTURE.md"):
            paths.append("docs/ARCHITECTURE.md")
        for directory in ("docs/modules", "docs/decisions"):
            if workspace.exists(directory):
                paths.extend(workspace.walk(directory))
    if len(paths) > MAX_NODES:
        fail("input_too_large", "Too many selected knowledge paths")
    entries, diagnostics = [], []
    for path in sorted(set(paths)):
        data, text = workspace.read(path)
        digest = sha256(data)
        level = "L0" if path == "docs/ARCHITECTURE.md" else "L1" if path.startswith("docs/modules/") else "decision" if path.startswith("docs/decisions/") else "reference"
        purpose, matches = clues(text, query)
        base = {"path": path, "file_sha256": digest, "level": level}
        candidates = [{**base, "ref": reference(path), "title": path,
                       "bytes": len(data), "purpose": purpose, "matches": matches}]
        for section in sections(data):
            try:
                section_ref = reference(path, section["anchor"])
            except KnowledgeError:
                diagnostics.append({"code": "invalid_section_reference", "path": path,
                                    "anchor_excerpt": section["anchor"][:160], "line": section["line_start"]})
                continue
            if section["ambiguous"]:
                diagnostics.append({"code": "ambiguous_section", "path": path,
                                    "anchor": section["anchor"], "line": section["line_start"]})
                continue
            body = data[section["start"]:section["end"]].decode("utf-8")
            purpose, local_matches = clues(body, query)
            for match in local_matches:
                match["line"] += section["line_start"] - 1
            candidates.append({**base, "ref": section_ref,
                               "title": section["title"], "level": "L2" if level == "L1" else level,
                               "bytes": section["end"] - section["start"], "purpose": purpose,
                               "matches": local_matches, "line_start": section["line_start"]})
        for item in candidates:
            if not query or query.casefold() in (path + " " + item["title"]).casefold() or item["matches"]:
                entries.append(item)
                if len(entries) > MAX_INDEX_ENTRIES:
                    fail("input_too_large", f"Index exceeds {MAX_INDEX_ENTRIES} matching entries; narrow --path/--query")
    result = {"command": "index", "root": workspace.root, "trust": TRUST,
              "entries": [], "diagnostics": [],
              "budget": {"unit": "utf8_json_bytes", "limit": budget, "output_bytes": 0,
                         "entry_limit": limit, "matched_entries": len(entries),
                         "skipped_entries": min(offset, len(entries)), "omitted_entries": len(entries),
                         "omitted_diagnostics": len(diagnostics),
                         "scan_bytes": workspace.total_bytes, "max_scan_bytes": MAX_SCAN_BYTES},
              "complete": False,
              "continuation": None,
              "scope": "explicit_paths" if explicit_paths else "project_knowledge"}

    def update():
        emitted = len(result["entries"])
        remaining = max(0, len(entries) - offset - emitted)
        result["budget"]["omitted_entries"] = len(entries) - emitted
        result["budget"]["omitted_diagnostics"] = len(diagnostics) - len(result["diagnostics"])
        result["complete"] = not result["budget"]["omitted_entries"] and not result["budget"]["omitted_diagnostics"]
        result["continuation"] = ({"next_offset": offset + emitted,
                                   "hint": "Repeat --offset with the same --path/--query; recheck file_sha256"}
                                  if remaining else None)
        # Account for the digits of output_bytes itself and the stdout newline.
        for _ in range(8):
            size = len(encoded_json(result)) + 1
            if size == result["budget"]["output_bytes"]:
                return size
            result["budget"]["output_bytes"] = size
        return len(encoded_json(result)) + 1

    update()
    for item in entries[offset:offset + limit]:
        result["entries"].append(item)
        if update() > budget:
            result["entries"].pop()
            update()
            break
    for diagnostic in diagnostics[:512]:
        result["diagnostics"].append(diagnostic)
        if update() > budget:
            result["diagnostics"].pop()
            update()
            break
    if update() > budget or (offset < len(entries) and not result["entries"]):
        fail("output_budget_too_small", "Index metadata/first item exceeds output budget; increase --budget-bytes or narrow --path/--query")
    workspace.verify()
    return result


def read(workspace, ref, budget=16384, expected=None):
    path, anchor = parse_ref(ref)
    data, _ = workspace.read(path)
    digest = sha256(data)
    if expected is not None and digest != expected:
        fail("stale_binding", f"File digest no longer matches selection: {path}; run index again")
    start, end = 0, len(data)
    line_start, line_end = 1, len(data.decode("utf-8").splitlines())
    if anchor is not None:
        found = [s for s in sections(data) if s["anchor"] == anchor]
        if not found:
            fail("missing_section", f"Section no longer resolves: {ref}; run index on this path")
        if len(found) != 1 or found[0]["ambiguous"]:
            fail("ambiguous_section", f"Duplicate section identity: {ref}; choose an explicit unique id")
        start, end = found[0]["start"], found[0]["end"]
        line_start, line_end = found[0]["line_start"], found[0]["line_end"]
    selected = data[start:end]
    # Never split a UTF-8 codepoint; budget is source-text bytes, not tokens/JSON.
    text = selected[:budget].decode("utf-8", errors="ignore")
    returned = text.encode("utf-8")
    workspace.verify()
    return {"command": "read", "root": workspace.root, "ref": reference(path, anchor),
            "path": path, "trust": TRUST, "text": text,
            "binding": {"file_sha256": digest, "selected_sha256": sha256(selected),
                        "returned_sha256": sha256(returned), "byte_start": start, "byte_end": end,
                        "returned_byte_end": start + len(returned),
                        "line_start": line_start, "line_end": line_end},
            "budget": {"unit": "utf8_source_bytes", "limit": budget,
                       "selected_bytes": len(selected), "returned_bytes": len(returned),
                       "omitted_bytes": len(selected) - len(returned)},
            "complete": len(selected) == len(returned)}



def diagnostic_lines(text):
    """Keep line positions, ignoring fenced examples. No Markdown is executed."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            continue
        if marker:
            fence = marker[1]
            continue
        yield number, line


def local_link(source, raw):
    """Resolve only local Markdown references; never leave the workspace."""
    raw = raw.strip()
    if raw.startswith("<") and ">" in raw:
        raw = raw[1:raw.index(">")]
    else:
        raw = raw.split()[0] if raw else ""
    if not raw or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw) or raw.startswith("//"):
        return None
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        fail("invalid_reference", "Malformed percent encoding")
    try:
        path, separator, anchor = unquote(raw, errors="strict").partition("#")
    except UnicodeError:
        fail("invalid_reference", "Reference is not UTF-8")
    if "?" in path:
        return None  # Query-bearing URLs are outside this narrow Markdown contract.
    if not path:
        target = source
    else:
        if path.startswith("/") or "\\" in path:
            fail("invalid_reference", "Absolute or backslash local reference")
        parts = source.split("/")[:-1]
        for part in path.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                if not parts:
                    fail("invalid_reference", "Local reference escapes workspace")
                parts.pop()
            else:
                parts.append(part)
        target = "/".join(parts)
    if not target.endswith(".md"):
        return None
    relative_path(target)
    return target, anchor if separator else None


def diagnostic_purpose(text):
    """A prose clue, excluding headings, navigation, metadata and code examples."""
    for _, line in diagnostic_lines(text):
        line = re.sub(r"<!--.*?-->", "", line).strip()
        if not line or line.startswith(("#", "|", "<", "---")):
            continue
        if re.match(r"(?:Contract-ID|合同\s*ID|最后更新|来源|状态|版本|updated|date)\s*[：:]", line, re.I):
            continue
        if re.fullmatch(r"(?:[-*]\s+)?\[[^]]+\](?:\([^)]*\)|\[[^]]*\])", line):
            continue
        if re.match(r"^\[[^]]+\]:", line):
            continue
        return True
    return False


def diagnose(workspace, paths=None, limit=100, budget=16384, offset=0):
    """Advisory structural findings, not semantic freshness or completion gates.

    Supports inline Markdown links and same-document reference definitions,
    explicit <!-- contract: ID --> / Contract-ID: ID / 合同 ID: ID declarations,
    and Markdown tables with explicit 合同 ID / 接口 ID / Contract ID columns.
    Fenced examples are ignored. Other Markdown syntax is not interpreted.
    The selected knowledge tree is scanned once; linked documents outside it
    are never recursively followed or loaded.
    """
    explicit = paths is not None
    if paths is None:
        paths = ["docs/ARCHITECTURE.md"] if workspace.exists("docs/ARCHITECTURE.md") else []
        for directory in ("docs/modules", "docs/decisions"):
            if workspace.exists(directory):
                paths.extend(workspace.walk(directory))
    paths = sorted(set(relative_path(p) for p in paths))
    if len(paths) > MAX_NODES:
        fail("input_too_large", "Too many diagnostic inputs")
    documents, findings, contracts = {}, [], {}
    edges = {path: set() for path in paths}

    def finding(code, path, **details):
        if len(findings) >= MAX_INDEX_ENTRIES:
            fail("input_too_large", "Too many findings; narrow --path")
        findings.append({"code": code, "path": path, **details})

    for path in paths:
        data, text = workspace.read(path)
        documents[path] = (data, text, sections(data))
    for path, (data, text, headings) in documents.items():
        if not diagnostic_purpose(text):
            finding("missing_purpose_clue", path)
        for heading in headings:
            if heading["ambiguous"]:
                finding("ambiguous_section", path, anchor=heading["anchor"], line=heading["line_start"])
        lines = list(diagnostic_lines(text))
        definitions, contract_column = {}, None
        for number, line in lines:
            definition = re.match(r"^ {0,3}\[([^]]+)\]:\s*(.+)$", line)
            if definition:
                definitions[definition[1].casefold()] = definition[2]
            declaration = re.fullmatch(r"\s*(?:<!--\s*contract\s*:\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\s*-->|(?:Contract-ID|合同\s*ID)\s*[：:]\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127}))\s*", line, re.I)
            if declaration:
                identity = declaration[1] or declaration[2]
                contracts.setdefault(identity, []).append({"path": path, "line": number})
            if line.lstrip().startswith("|"):
                cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
                explicit_columns = [i for i, cell in enumerate(cells) if re.fullmatch(r"(?:合同|接口)\s*ID|Contract[ -]ID", cell, re.I)]
                if explicit_columns:
                    contract_column = explicit_columns[0]
                elif contract_column is not None and len(cells) > contract_column:
                    identity = cells[contract_column]
                    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", identity) and not re.fullmatch(r"-+", identity):
                        contracts.setdefault(identity, []).append({"path": path, "line": number})
            else:
                contract_column = None
            # Strong process markers are advisory; ordinary retry/ops text is not flagged.
            if path.startswith("docs/modules/") and re.search(r"(?:本次会话|下一窗口|任务进度|审查第\s*\d+\s*轮|会话交接|session transcript|current task progress|next window handoff)", line, re.I):
                finding("possible_task_process", path, line=number, excerpt=line.strip()[:160],
                        advisory="Check whether transient execution details should stay only in the checkpoint.")
        for number, line in lines:
            if re.match(r"^ {0,3}\[[^]]+\]:", line):
                continue
            raw_links = re.findall(r"(?<!!)\[[^]\n]+\]\(([^)\n]+)\)", line)
            for label, key in re.findall(r"(?<!!)\[([^]\n]+)\]\[([^]\n]*)\]", line):
                name = (key or label).casefold()
                if name not in definitions:
                    finding("broken_reference_definition", path, line=number, reference=name[:160])
                else:
                    raw_links.append(definitions[name])
            for raw in raw_links:
                try:
                    resolved = local_link(path, raw)
                except KnowledgeError as exc:
                    finding("invalid_local_reference", path, line=number, reference=raw[:160], reason=exc.code)
                    continue
                if resolved is None:
                    continue
                target, anchor = resolved
                if target not in documents:
                    if not workspace.probe(target):
                        finding("broken_document_reference", path, line=number, target=target)
                    else:
                        finding("reference_outside_scan", path, line=number, target=target,
                                advisory="Target exists; body and anchor were not loaded. Select --path to check it.")
                    continue
                edges[path].add(target)
                if anchor is not None:
                    matches = [h for h in documents[target][2] if h["anchor"] == anchor]
                    if len(matches) != 1 or matches[0]["ambiguous"]:
                        finding("broken_section_reference", path, line=number, target=target, anchor=anchor[:160])
    for identity, declarations in sorted(contracts.items()):
        if len(declarations) > 1:
            finding("duplicate_contract_id", declarations[0]["path"], contract_id=identity, locations=declarations)
    if "docs/ARCHITECTURE.md" in documents:
        reached, pending = set(), ["docs/ARCHITECTURE.md"]
        while pending:
            current = pending.pop()
            if current not in reached:
                reached.add(current)
                pending.extend(edges[current] - reached)
        for path in paths:
            if path not in reached:
                finding("orphan_document", path, advisory="No selected Markdown path from L0 reaches this document.")
    result = {"command": "diagnose", "read_only": True, "trust": TRUST,
              "advisory_only": True, "semantic_freshness": "not_determined",
              "scope": "explicit_paths" if explicit else "project_knowledge",
              "documents": [{"path": path, "file_sha256": sha256(data)} for path, (data, _, _) in documents.items()],
              "findings": [], "total_findings": len(findings), "omitted_findings": len(findings),
              "complete": False, "next_offset": None,
              "budget": {"unit": "utf8_json_bytes", "limit": budget, "output_bytes": 0}}

    def update():
        emitted = len(result["findings"])
        result["omitted_findings"] = len(findings) - emitted
        result["complete"] = result["omitted_findings"] == 0
        result["next_offset"] = offset + emitted if offset + emitted < len(findings) else None
        for _ in range(8):
            size = len(encoded_json(result)) + 1
            if size == result["budget"]["output_bytes"]:
                return size
            result["budget"]["output_bytes"] = size
        return len(encoded_json(result)) + 1

    for item in findings[offset:offset + limit]:
        result["findings"].append(item)
        if update() > budget:
            result["findings"].pop()
            update()
            break
    if update() > budget or (offset < len(findings) and not result["findings"]):
        fail("output_budget_too_small", "Diagnostic metadata/first finding exceeds budget; increase budget or narrow --path")
    workspace.verify()
    return result

def bounded_int(maximum, minimum=1):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("Expected an integer") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"Expected {minimum}..{maximum}")
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("index", help="Discover current L0/L1/L2 knowledge without returning bodies")
    find.add_argument("--root", required=True)
    find.add_argument("--path", action="append", help="Explicit relative Markdown path; repeatable")
    find.add_argument("--query", default="", help="Literal case-insensitive path/title/body substring")
    find.add_argument("--limit", type=bounded_int(512), default=100)
    find.add_argument("--budget-bytes", type=bounded_int(MAX_READ_BYTES), default=16384,
                      help="Maximum UTF-8 bytes of complete index JSON, including newline")
    find.add_argument("--offset", type=bounded_int(MAX_INDEX_ENTRIES, 0), default=0,
                      help="Continue from the stable sorted matching-entry position")
    doctor = commands.add_parser("diagnose", aliases=["doctor"], help="Read-only advisory knowledge structure diagnostics")
    doctor.add_argument("--root", required=True)
    doctor.add_argument("--path", action="append")
    doctor.add_argument("--limit", type=bounded_int(512), default=100)
    doctor.add_argument("--budget-bytes", type=bounded_int(MAX_READ_BYTES), default=16384)
    doctor.add_argument("--offset", type=bounded_int(MAX_INDEX_ENTRIES, 0), default=0)
    fetch = commands.add_parser("read", help="Read one exact document or section reference")
    fetch.add_argument("--root", required=True)
    fetch.add_argument("--ref", required=True)
    fetch.add_argument("--budget-bytes", type=bounded_int(MAX_READ_BYTES), default=16384)
    fetch.add_argument("--expect-sha256", help="Require the file_sha256 returned by index")
    args = parser.parse_args(argv)
    workspace = None
    try:
        if args.command == "index" and len(args.query.encode("utf-8")) > 1024:
            fail("input_too_large", "Query exceeds 1024 UTF-8 bytes")
        if args.command == "read" and args.expect_sha256 and not re.fullmatch(r"[0-9a-f]{64}", args.expect_sha256):
            fail("invalid_binding", "Expected a lowercase SHA-256 digest")
        workspace = Workspace(args.root)
        result = (index(workspace, args.path, args.query, args.limit, args.budget_bytes, args.offset) if args.command == "index"
                  else diagnose(workspace, args.path, args.limit, args.budget_bytes, args.offset)
                  if args.command in {"diagnose", "doctor"}
                  else read(workspace, args.ref, args.budget_bytes, args.expect_sha256))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except KnowledgeError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}, "trust": TRUST}, ensure_ascii=False))
        return 2
    except (OSError, UnicodeError) as exc:
        print(json.dumps({"error": {"code": "read_failed", "message": str(exc)}, "trust": TRUST}, ensure_ascii=False))
        return 2
    finally:
        if workspace is not None:
            workspace.close()


if __name__ == "__main__":
    sys.exit(main())
