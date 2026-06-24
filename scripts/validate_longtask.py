#!/usr/bin/env python3
"""Validate longtask skill conventions."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_EXTERNAL = "super" + "powers"
LEGACY_MODULE_DOC = "overview" + ".md"

# Validates markdown code pointer links to .md files only.
# Template examples in doc-architecture.md reference .ts paths that don't exist;
# all real cross-references within this project target .md files.
#   [text](relative/path.md#L42)       — markdown link with line anchor
#   [text](relative/path.md#L42-L50)   — markdown link with line range anchor
CODE_POINTER_LINK = re.compile(
    r"\[([^\]]+)\]\(([^)#]+\.md)#L(\d+)(?:-L(\d+))?\)"
)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def frontmatter_name(path: Path) -> str | None:
    text = read_text(path)
    match = re.match(r"---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return None
    for line in match.group(1).splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def strip_fenced_code(text: str) -> str:
    """Remove fenced code blocks (handles nesting) to avoid false positives from template examples."""
    lines = text.splitlines(keepends=True)
    result: list[str] = []
    depth = 0
    for line in lines:
        if re.match(r"^```", line.lstrip()):
            if depth == 0:
                depth += 1
            else:
                depth -= 1
            continue
        if depth == 0:
            result.append(line)
    return "".join(result)


def check_code_pointers(root: Path, errors: list[str]) -> None:
    """Validate that markdown code pointers reference existing files with valid line numbers."""
    for md_path in sorted(root.rglob("*.md")):
        if ".git" in md_path.parts:
            continue
        raw = read_text(md_path)
        text = strip_fenced_code(raw)
        rel_md = md_path.relative_to(root)
        for match in CODE_POINTER_LINK.finditer(text):
            href = match.group(2)
            line_start = int(match.group(3))
            line_end = int(match.group(4)) if match.group(4) else None
            target = (md_path.parent / href).resolve()
            if not target.is_file():
                fail(errors, f"dead pointer in {rel_md}: {href} not found")
                continue
            nlines = len(target.read_text(encoding="utf-8").splitlines())
            if line_start > nlines:
                fail(errors, f"line out of range in {rel_md}: {href}#L{line_start} (file has {nlines} lines)")
            elif line_end and line_end > nlines:
                fail(errors, f"line out of range in {rel_md}: {href}#L{line_start}-L{line_end} (file has {nlines} lines)")


def main() -> int:
    errors: list[str] = []

    expected_skill_names = {
        "setup": "longtask-setup",
        "continue": "longtask-continue",
        "review": "longtask-review",
        "modify": "longtask-modify",
        "retrofit": "longtask-retrofit",
    }

    root_name = frontmatter_name(ROOT / "SKILL.md")
    if root_name != "longtask":
        fail(errors, f"SKILL.md name should be longtask, got {root_name!r}")

    for folder, expected in expected_skill_names.items():
        path = ROOT / "skills" / folder / "SKILL.md"
        actual = frontmatter_name(path)
        if actual != expected:
            fail(errors, f"{path.relative_to(ROOT)} name should be {expected}, got {actual!r}")

    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_file() and path.name == LEGACY_MODULE_DOC:
            fail(errors, f"legacy module overview file remains: {path.relative_to(ROOT)}")

    text_files = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and path.suffix.lower() in {".md", ".json", ".yaml", ".yml", ".py"}
    ]
    for path in text_files:
        text = read_text(path)
        rel = path.relative_to(ROOT)
        if FORBIDDEN_EXTERNAL in text:
            fail(errors, f"external skill dependency reference remains in {rel}")
        if LEGACY_MODULE_DOC in text:
            fail(errors, f"legacy module overview reference remains in {rel}")

    for module_dir in (ROOT / "docs" / "modules").iterdir():
        if module_dir.is_dir() and not (module_dir / "README.md").exists():
            fail(errors, f"expanded module lacks README.md: {module_dir.relative_to(ROOT)}")

    plugin_path = ROOT / ".claude-plugin" / "plugin.json"
    if plugin_path.exists():
        plugin = json.loads(read_text(plugin_path))
        names = {item["name"] for item in plugin.get("skills", [])}
        for expected in ["longtask", *expected_skill_names.values()]:
            if expected not in names:
                fail(errors, f"plugin.json missing skill name {expected}")

    openai_yaml = ROOT / "agents" / "openai.yaml"
    if not openai_yaml.exists():
        fail(errors, "agents/openai.yaml is missing")

    check_code_pointers(ROOT, errors)

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print("longtask validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
