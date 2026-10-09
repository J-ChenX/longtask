#!/usr/bin/env python3
"""Run or verify the version-bound deterministic forward contract suite."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evals" / "forward_cases.json"
RESULTS = ROOT / "evals" / "forward_results.json"
CONTRACT_INPUTS = (
    "SKILL.md",
    "AGENTS.md",
    "references/发布与恢复.md",
    "skills/longtask/agents/openai.yaml",
    "skills/longtask-setup/agents/openai.yaml",
    "skills/longtask-continue/agents/openai.yaml",
    "skills/longtask-review/agents/openai.yaml",
    "skills/longtask-modify/agents/openai.yaml",
    "skills/longtask-retrofit/agents/openai.yaml",
    ".codex-plugin/plugin.json",
    "skills/longtask/SKILL.md",
    "skills/longtask-setup/SKILL.md",
    "skills/longtask-continue/SKILL.md",
    "skills/longtask-review/SKILL.md",
    "skills/longtask-modify/SKILL.md",
    "skills/longtask-retrofit/SKILL.md",
    "references/state.schema.json",
    "references/状态协议.md",
    "references/平台适配器.md",
    "references/平行任务协作.md",
    "references/评测协议.md",
    "专家审查协议.md",
    "文档架构.md",
    "scripts/longtask_state.py",
    "scripts/knowledge_context.py",
    "scripts/required_inputs.py",
    "scripts/acceptance_coverage.py",
    "scripts/discovery_checkpoint.py",
    "scripts/demo_workflow.py",
    "scripts/planning_checkpoint.py",
    "references/初始化与交接.md",
    "references/操作示例.md",
    "scripts/run_forward_evals.py",
    "tests/test_forward_workflows.py",
    "tests/test_longtask_state.py",
    "evals/forward_cases.json",
)
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_SUITE = "tests.test_forward_workflows.ForwardWorkflowTests"
TEST_SUITES = {
    DEFAULT_SUITE: ("test_forward_workflows.py", "ForwardWorkflowTests"),
    "tests.test_longtask_state.LongtaskStateTests": ("test_longtask_state.py", "LongtaskStateTests"),
}


def semantic_output_sha256(cases: list[dict[str, Any]]) -> str:
    """Hash deterministic pass semantics, excluding timings and unittest prose."""
    payload = {
        "suite": "deterministic_state_contract",
        "passed_case_ids": sorted(item["id"] for item in cases),
        "failed_case_ids": [],
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        # Invalid file contents share the ValueError contract of malformed JSON.
        raise ValueError(f"{path} must contain an object")  # noqa: TRY004
    return value


def input_hashes() -> dict[str, str]:
    return {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in CONTRACT_INPUTS
    }


def validate_cases(data: dict[str, Any]) -> list[dict[str, Any]]:
    cases = data.get("cases")
    if data.get("schema_version") != 1 or not isinstance(cases, list) or not cases:
        raise ValueError("forward case manifest is invalid")
    for field in ("id", "test"):
        values = [item.get(field) for item in cases if isinstance(item, dict)]
        if len(values) != len(cases) or any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError(f"forward {field} names must be non-empty strings")
        if field == "id" and len(set(values)) != len(cases):
            raise ValueError(f"forward {field} names must be unique")
    targets = [(item.get("suite", DEFAULT_SUITE), item["test"]) for item in cases]
    if any(not isinstance(suite, str) or suite not in TEST_SUITES for suite, _ in targets):
        raise ValueError("forward suite must name an allowlisted test class")
    if len(set(targets)) != len(targets):
        raise ValueError("forward test targets must be unique")
    methods = {}
    for suite_name in {suite for suite, _ in targets}:
        filename, class_name = TEST_SUITES[suite_name]
        tree = ast.parse((ROOT / "tests" / filename).read_text(encoding="utf-8"))
        methods[suite_name] = {method.name for node in tree.body
                              if isinstance(node, ast.ClassDef) and node.name == class_name
                              for method in node.body if isinstance(method, ast.FunctionDef)}
    missing = [name for suite, name in targets if name not in methods[suite] or not name.startswith("test_")]
    if missing:
        raise ValueError("forward cases reference missing tests: " + ", ".join(missing))
    return cases


def test_ids(cases: list[dict[str, Any]]) -> list[str]:
    return [f"{item.get('suite', DEFAULT_SUITE)}.{item['test']}" for item in cases]


def validate_unittest_transcript(transcript: str, expected_count: int) -> None:
    matches = re.findall(r"Ran (\d+) tests?", transcript)
    if len(matches) != 1 or int(matches[0]) != expected_count:
        raise ValueError(f"forward runner executed {matches[-1] if matches else 'unknown'} tests; expected {expected_count}")
    skipped = re.search(r"skipped=(\d+)", transcript)
    if skipped and int(skipped.group(1)):
        raise ValueError(f"forward runner skipped {skipped.group(1)} required tests")


def check_saved(results: dict[str, Any], cases: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    case_count = len(cases)
    if results.get("schema_version") != 3:
        errors.append("forward results schema_version must be 3")
    if results.get("suite") != "deterministic_state_contract":
        errors.append("forward results suite classification is invalid")
    if results.get("assurance") != "deterministic_local_process":
        errors.append("forward results assurance is invalid")
    if results.get("cases") != case_count or results.get("passed") != case_count or results.get("failed") != 0:
        errors.append("forward results do not record a complete passing run")
    observed = results.get("inputs_sha256")
    expected = input_hashes()
    if not isinstance(observed, dict) or set(observed) != set(expected):
        errors.append("forward result input manifest is incomplete or unexpected")
    else:
        for relative, digest in expected.items():
            if observed.get(relative) != digest:
                errors.append(f"forward results are stale: {relative}")
    if results.get("semantic_output_sha256") != semantic_output_sha256(cases):
        errors.append("forward results semantic output digest is invalid")
    environment = results.get("environment")
    if not isinstance(environment, dict) or set(environment) != {"python", "platform"} or not all(
        isinstance(value, str) and value for value in environment.values()
    ):
        errors.append("forward results execution environment is incomplete")
    if not isinstance(results.get("duration_seconds"), (int, float)) or results.get("duration_seconds", -1) < 0:
        errors.append("forward results duration is invalid")
    return errors


def atomic_write(path: Path, text: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--write", action="store_true", help="run the suite and replace the saved result")
    execution.add_argument("--run", action="store_true", help="run the suite without changing saved results")
    execution.add_argument("--check-cases", action="store_true", help="validate case-to-test mappings without saved results or execution")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    try:
        if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
            raise ValueError("timeout-seconds must be a positive finite number")
        cases = validate_cases(load_object(CASES))
        if args.check_cases:
            print(json.dumps({"valid": True, "cases": len(cases), "mode": "corpus_contract_only"}))
            return 0
        if not (args.write or args.run):
            errors = check_saved(load_object(RESULTS), cases)
            print(json.dumps({"valid": not errors, "errors": errors}, ensure_ascii=False, indent=2))
            return 0 if not errors else 1

        started = time.monotonic()
        try:
            process = subprocess.run(
                [sys.executable, "-m", "unittest", *test_ids(cases), "-v"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=args.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"forward suite exceeded {args.timeout_seconds:g} seconds") from exc
        duration = round(time.monotonic() - started, 3)
        transcript = process.stdout + process.stderr
        if process.returncode != 0:
            sys.stderr.write(transcript)
            return process.returncode
        validate_unittest_transcript(transcript, len(cases))
        result = {
            "schema_version": 3,
            "evaluated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "runner": "python3 scripts/run_forward_evals.py " + ("--write" if args.write else "--run"),
            "suite": "deterministic_state_contract",
            "assurance": "deterministic_local_process",
            "cases": len(cases),
            "passed": len(cases),
            "failed": 0,
            "duration_seconds": duration,
            "semantic_output_sha256": semantic_output_sha256(cases),
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "inputs_sha256": input_hashes(),
        }
        if args.write:
            atomic_write(RESULTS, json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except ValueError as exc:
        print(json.dumps({"valid": False, "errors": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
