#!/usr/bin/env python3
"""Read-only UTF-8 context inventory; this does not measure tokens or runtime gains."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

try:
    from scripts import validate_longtask as markdown
except ModuleNotFoundError:
    import validate_longtask as markdown

ROOT = Path(__file__).resolve().parents[1]
MODES = ("setup", "retrofit", "continue", "modify", "review")
MODE_REFERENCES = dict(zip(MODES, ("新建项目", "既有项目接入", "任务续接", "架构变更", "任务审查")))


def counts(text: str) -> dict[str, int]:
    return {"utf8_bytes": len(text.encode("utf-8")), "characters": len(text)}


def local_path(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError(f"measurement path must be package-relative: {relative}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"measurement path escapes package: {relative}")
    return path


def measure(root: Path, selected: list[str]) -> dict:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"not a package directory: {root}")
    files = []
    references = set()
    canonical = {local_path(root, relative).resolve().relative_to(root).as_posix() for relative in selected}
    for relative in sorted(canonical):
        path = local_path(root, relative)
        content = path.read_bytes()
        text = content.decode("utf-8")
        stripped = markdown.strip_fenced_code(text)
        targets, unresolved = markdown.reference_links(stripped)
        links = []
        for raw in sorted(set(markdown.inline_link_targets(stripped) + targets)):
            resolved = markdown.resolve_link(path, raw)
            if resolved is None:
                continue
            target, fragment = resolved
            inside = target.is_relative_to(root)
            target_name = target.relative_to(root).as_posix() if inside else str(target)
            links.append({"path": target_name, "fragment": fragment,
                          "exists": target.exists(), "within_package": inside})
            references.add(target_name)
        files.append({"path": relative, **counts(text),
                      "sha256": hashlib.sha256(content).hexdigest(),
                      "local_references": links, "unresolved_reference_labels": sorted(unresolved)})
    discovery = []
    for path in sorted((root / "skills").glob("*/SKILL.md")):
        local_path(root, path.relative_to(root).as_posix())
        fields = markdown.frontmatter(path)
        if "name" not in fields or "description" not in fields:
            raise ValueError(f"cannot read discovery metadata: {path}")
        discovery.append({"path": path.relative_to(root).as_posix(), "name": fields["name"],
                          "description": fields["description"], **counts(fields["description"]),
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not discovery:
        raise ValueError(f"no installed-entry discovery metadata found: {root}")
    return {"directory": str(root), "selected_files": files,
            "selected_total": {key: sum(item[key] for item in files) for key in counts("")},
            "discovery_descriptions": discovery,
            "discovery_description_total": {key: sum(item[key] for item in discovery) for key in counts("")},
            "declared_local_reference_set": sorted(references)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, default=ROOT)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--load", action="append", default=[], metavar="PACKAGE_RELATIVE_PATH",
                        help="include a selected reference or a path observed in a host trace; repeatable")
    parser.add_argument("--only-loaded", action="store_true",
                        help="measure exactly --load paths, without adding root or mode instructions")
    args = parser.parse_args(argv)
    if args.only_loaded and (not args.load or args.mode):
        parser.error("--only-loaded needs at least one --load and cannot be combined with --mode")
    selected = list(args.load) if args.only_loaded else ["SKILL.md", *args.load]
    if args.mode:
        selected.append(f"references/{MODE_REFERENCES[args.mode]}.md")
    try:
        candidate = measure(args.candidate, selected)
        baseline = measure(args.baseline, selected) if args.baseline else None
        delta = None
        if baseline:
            delta = {group: {key: candidate[group][key] - baseline[group][key]
                             for key in counts("")}
                     for group in ("selected_total", "discovery_description_total")}
        output = {"schema_version": 1, "valid": True, "selection": sorted(set(selected)),
                  "selection_origin": "caller_selected_paths" if args.only_loaded else "entry_profile_plus_caller_paths",
                  "candidate": candidate, "baseline": baseline, "candidate_minus_baseline": delta,
                  "scope": "Selected UTF-8 file and description sizes; declared Markdown references are not observed reads.",
                  "model_tokens": None, "runtime_benefit": None}
    except (OSError, UnicodeError, ValueError) as exc:
        output = {"schema_version": 1, "valid": False, "errors": [str(exc)]}
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if output["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
