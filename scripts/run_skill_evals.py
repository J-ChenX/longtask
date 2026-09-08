#!/usr/bin/env python3
"""Validate the invocation corpus and optionally score fresh-session results."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "evals" / "invocation_cases.json"
ENTRIES = {"setup", "retrofit", "continue", "modify", "review"}
DISCOVERY_FILES = (
    "SKILL.md",
    "skills/longtask/agents/openai.yaml",
    ".codex-plugin/plugin.json",
    "skills/longtask/SKILL.md",
    "skills/longtask-setup/SKILL.md",
    "skills/longtask-continue/SKILL.md",
    "skills/longtask-review/SKILL.md",
    "skills/longtask-modify/SKILL.md",
    "skills/longtask-retrofit/SKILL.md",
)


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        # Invalid file contents share the ValueError contract of malformed JSON.
        raise ValueError(f"{path} must contain a JSON object")  # noqa: TRY004
    return value


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def aggregate_sha256(paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        encoded = relative.encode("utf-8")
        content = (ROOT / relative).read_bytes()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_result_manifest(results: dict[str, Any], corpus: Path) -> list[str]:
    errors: list[str] = []
    if results.get("schema_version") != 3:
        errors.append("results schema_version must be 3")
    evaluation = results.get("evaluation")
    if not isinstance(evaluation, dict):
        return errors + ["results must contain an evaluation object"]
    expected_fields = {
        "method", "evaluator", "evaluated_at", "assurance", "model", "harness",
        "repetitions", "trace_available", "trace_id",
    }
    if set(evaluation) != expected_fields:
        errors.append("evaluation provenance fields are incomplete or unexpected")
    assurance = evaluation.get("assurance")
    if assurance not in {"unattested_classification", "trace_backed"}:
        errors.append("evaluation assurance is invalid")
    repetitions = evaluation.get("repetitions")
    if type(repetitions) is not int or repetitions < 1:
        errors.append("evaluation repetitions must be a positive integer")
    if not isinstance(evaluation.get("trace_available"), bool):
        errors.append("evaluation trace_available must be boolean")
    if assurance == "trace_backed":
        if evaluation.get("trace_available") is not True:
            errors.append("trace_backed results require trace_available=true")
        if evaluation.get("model") == "unrecorded":
            errors.append("trace_backed results require an exact model identifier")
        runs = results.get("repeat_runs")
        if not isinstance(runs, list) or len(runs) < 2:
            errors.append("trace_backed results require at least two actual repeat_runs")
            runs = []
        if type(repetitions) is not int or repetitions != len(runs):
            errors.append("evaluation repetitions must equal actual repeat_runs count")
        identities: dict[str, set[str]] = {key: set() for key in ("run_id", "trace_id", "trace_sha256")}
        for index, run in enumerate(runs):
            if not isinstance(run, dict) or set(run) != {"run_id", "trace_id", "trace_sha256", "model", "results"}:
                errors.append(f"repeat_runs[{index}] envelope is malformed")
                continue
            if run.get("model") != evaluation.get("model"):
                errors.append(f"repeat_runs[{index}] model differs from evaluation model")
            for field, seen in identities.items():
                value = run.get(field)
                pattern = r"[0-9a-f]{64}" if field == "trace_sha256" else r"[A-Za-z0-9][A-Za-z0-9._:/-]{7,255}"
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    errors.append(f"repeat_runs[{index}] {field} is invalid")
                elif value in seen:
                    errors.append(f"repeat_runs[{index}] duplicates {field}")
                else:
                    seen.add(value)
        if runs and isinstance(runs[0], dict):
            if evaluation.get("trace_id") != runs[0].get("trace_id"):
                errors.append("evaluation trace_id must identify the first repeat run")
            if results.get("results") != runs[0].get("results"):
                errors.append("top-level results must match the first repeat run")
    else:
        if type(repetitions) is not int or repetitions != 1:
            errors.append("unattested single-classification results require repetitions=1")
        if evaluation.get("trace_available") or evaluation.get("trace_id") is not None:
            errors.append("unattested results must not claim a trace")
        if "repeat_runs" in results:
            errors.append("unattested results must not claim repeat_runs")
    for field in ("method", "evaluator", "evaluated_at", "model", "harness"):
        if not isinstance(evaluation.get(field), str) or not evaluation.get(field):
            errors.append(f"evaluation.{field} must be non-empty")
    inputs = results.get("inputs")
    if not isinstance(inputs, dict):
        return errors + ["results must contain an inputs object"]
    expected_inputs = {
        "corpus_sha256": file_sha256(corpus),
        "discovery_surface_sha256": aggregate_sha256(DISCOVERY_FILES),
        "runner_sha256": file_sha256(Path(__file__)),
    }
    if set(inputs) != set(expected_inputs):
        errors.append("results input manifest fields are incomplete or unexpected")
    for field, expected in expected_inputs.items():
        if inputs.get(field) != expected:
            errors.append(f"results are stale: {field}")
    return errors


def validate_corpus(data: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    cases = data.get("cases")
    if data.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if not isinstance(cases, list) or not cases:
        return [], errors + ["cases must be a non-empty array"]
    ids: set[str] = set()
    positive = 0
    negative = 0
    entries: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] must be an object")
            continue
        missing = {"id", "prompt", "should_trigger", "expected_entry", "rationale"} - case.keys()
        if missing:
            errors.append(f"cases[{index}] missing {', '.join(sorted(missing))}")
            continue
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"cases[{index}].id must be non-empty")
        elif case_id in ids:
            errors.append(f"duplicate case id: {case_id}")
        ids.add(case_id)
        if not isinstance(case["prompt"], str) or not case["prompt"].strip():
            errors.append(f"{case_id}: prompt must be non-empty")
        if not isinstance(case["rationale"], str) or not case["rationale"].strip():
            errors.append(f"{case_id}: rationale must be non-empty")
        if not isinstance(case["should_trigger"], bool):
            errors.append(f"{case_id}: should_trigger must be boolean")
        elif case["should_trigger"]:
            positive += 1
            if case["expected_entry"] not in ENTRIES:
                errors.append(f"{case_id}: triggered case needs a valid expected_entry")
            else:
                entries.add(case["expected_entry"])
        else:
            negative += 1
            if case["expected_entry"] is not None:
                errors.append(f"{case_id}: non-trigger case must have expected_entry=null")
    if positive != negative:
        errors.append(f"corpus must be balanced, got {positive} trigger and {negative} non-trigger cases")
    missing_entries = ENTRIES - entries
    if missing_entries:
        errors.append(f"trigger corpus does not cover entries: {', '.join(sorted(missing_entries))}")
    return cases, errors


def score_classifications(cases: list[dict[str, Any]], results: dict[str, Any]) -> tuple[dict[str, float | int], list[str]]:
    errors: list[str] = []
    raw_results = results.get("results")
    if not isinstance(raw_results, list):
        return {}, ["results file must contain a results array"]
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw_results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            errors.append("each result must be an object with an id")
            continue
        if item["id"] in by_id:
            errors.append(f"duplicate result id: {item['id']}")
        by_id[item["id"]] = item
    expected_ids = {case["id"] for case in cases}
    for case_id in by_id.keys() - expected_ids:
        errors.append(f"unexpected result: {case_id}")
    true_positive = false_positive = false_negative = route_correct = routed = 0
    for case in cases:
        result = by_id.get(case["id"])
        if result is None:
            errors.append(f"missing result: {case['id']}")
            continue
        selected = result.get("selected")
        if not isinstance(selected, bool):
            errors.append(f"{case['id']}: selected must be boolean")
            continue
        entry = result.get("entry")
        if entry is not None and entry not in ENTRIES:
            errors.append(f"{case['id']}: entry must be null or a valid mode")
            continue
        if not selected and entry is not None:
            errors.append(f"{case['id']}: deselected result must have entry=null")
            continue
        if selected and entry is None:
            errors.append(f"{case['id']}: selected result must provide an entry")
            continue
        expected = case["should_trigger"]
        true_positive += int(selected and expected)
        false_positive += int(selected and not expected)
        false_negative += int(not selected and expected)
        if expected and selected:
            routed += 1
            route_correct += int(entry == case["expected_entry"])
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    routing_accuracy = route_correct / routed if routed else 0.0
    metrics: dict[str, float | int] = {
        "cases": len(cases),
        "routed_positives": routed,
        "correctly_routed_positives": route_correct,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "invocation_precision": round(precision, 4),
        "invocation_recall": round(recall, 4),
        "entry_accuracy_on_selected_positives": round(routing_accuracy, 4),
    }
    return metrics, errors


def score(cases: list[dict[str, Any]], results: dict[str, Any]) -> tuple[dict[str, float | int], list[str]]:
    if results.get("evaluation", {}).get("assurance") != "trace_backed":
        return score_classifications(cases, results)
    runs = results.get("repeat_runs")
    if not isinstance(runs, list) or not runs:
        return {}, ["trace_backed scoring requires actual repeat_runs"]
    totals = dict.fromkeys(("cases", "true_positive", "false_positive", "false_negative",
                           "routed_positives", "correctly_routed_positives"), 0)
    errors: list[str] = []
    for index, run in enumerate(runs):
        metrics, run_errors = score_classifications(cases, run if isinstance(run, dict) else {})
        errors.extend(f"repeat_runs[{index}]: {error}" for error in run_errors)
        for key in totals:
            totals[key] += int(metrics.get(key, 0))
    tp, fp, fn = (totals[key] for key in ("true_positive", "false_positive", "false_negative"))
    return {
        **totals,
        "evaluated_runs": len(runs),
        "invocation_precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
        "invocation_recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
        "entry_accuracy_on_selected_positives": round(
            totals["correctly_routed_positives"] / totals["routed_positives"], 4,
        ) if totals["routed_positives"] else 0.0,
    }, errors


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    result.add_argument("--results", type=Path, help="fresh-session result JSON to score")
    result.add_argument("--min-precision", type=float, default=0.90)
    result.add_argument("--min-recall", type=float, default=0.90)
    result.add_argument("--min-routing", type=float, default=0.90)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        cases, errors = validate_corpus(load_object(args.corpus))
        metrics: dict[str, float | int] | None = None
        if not errors and args.results:
            result_data = load_object(args.results)
            errors.extend(validate_result_manifest(result_data, args.corpus))
            score_errors: list[str] = []
            if not errors:
                metrics, score_errors = score(cases, result_data)
                errors.extend(score_errors)
            if metrics is not None and not score_errors:
                if metrics["invocation_precision"] < args.min_precision:
                    errors.append("invocation precision is below threshold")
                if metrics["invocation_recall"] < args.min_recall:
                    errors.append("invocation recall is below threshold")
                if metrics["entry_accuracy_on_selected_positives"] < args.min_routing:
                    errors.append("entry routing accuracy is below threshold")
    except ValueError as exc:
        errors = [str(exc)]
        metrics = None
        cases = []
    output = {
        "valid": not errors,
        "corpus_cases": len(cases),
        "mode": "scored_results" if args.results else "corpus_contract_only",
        "metrics": metrics,
        "evidence_verification": "external_verification_required" if args.results else "not_applicable",
        "evidence_scope": "Validates recorded decisions and manifest bindings; does not authenticate raw traces.",
        "errors": errors,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
