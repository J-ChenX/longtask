#!/usr/bin/env python3
"""Validate the longtask package structure, links, metadata, and static contracts."""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import math
import re
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKILL_VERSION = "4.1.0"
SKILL_NAMES = ("longtask",)
MODE_REFERENCES = (
    "references/新建项目.md", "references/既有项目接入.md", "references/任务续接.md",
    "references/架构变更.md", "references/任务审查.md",
)
REQUIRED_FILES = (
    "SKILL.md",
    "AGENTS.md",
    ".codex-plugin/plugin.json",
    "文档架构.md",
    "专家审查协议.md",
    "references/state.schema.json",
    "references/状态协议.md",
    "references/平台适配器.md",
    "references/评测协议.md",
    "references/新项目设计.md",
    "references/发布与恢复.md",
    "references/personal-marketplace.json",
    "release-manifest.json",
    "scripts/build_release.py",
    "scripts/longtask_state.py",
    "scripts/longtask_hooks.py",
    "hooks/hooks.json",
    "tests/test_longtask_hooks.py",
    "scripts/run_skill_evals.py",
    "scripts/run_forward_evals.py",
    "scripts/knowledge_context.py",
    "scripts/required_inputs.py",
    "scripts/acceptance_coverage.py",
    "scripts/discovery_checkpoint.py",
    "scripts/run_memory_evals.py",
    "scripts/run_setup_evals.py",
    "evals/memory_cases.json",
    "evals/invocation_cases.json",
    "evals/forward_cases.json",
    "evals/setup_cases.json",
    "tests/test_longtask_state.py",
    "tests/test_release.py",
    "tests/test_forward_workflows.py",
    "tests/test_setup_evals.py",
    "skills/longtask/SKILL.md",
) + MODE_REFERENCES
SOURCE_EVALUATION_RESULTS = (
    "evals/host_results.json", "evals/invocation_results.json",
    "evals/forward_results.json", "evals/memory_results.json",
)
GENERATED_PATH_PATTERNS = (
    "evals/*_results.json", "evals/*.jsonl", "evals/results/*", "evals/runs/*",
    "evals/traces/*", "evals/snapshots/*", ".artifacts/*", ".longtask/*", "dist/*",
)
TEXT_SUFFIXES = {".md", ".json", ".yaml", ".yml", ".py"}
LINE_ANCHOR = re.compile(r"^L(\d+)(?:-L(\d+))?$")
FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$")
REFERENCE_DEFINITION = re.compile(r"^[ \t]{0,3}\[([^\]]+)\]:\s*(<[^>]+>|\S+)", re.MULTILINE)
REFERENCE_USAGE = re.compile(r"!?\[([^\]]+)\]\[([^\]]*)\]")
FORBIDDEN_PHRASES = {
    "code IS the spec": "obsolete code-as-intent rule",
    "代码即真相": "obsolete code-as-intent rule",
    "代码是真相": "obsolete code-as-intent rule",
    "代码优先真相": "obsolete code-as-intent rule",
}

WINDOWS_RESERVED_NAME = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.IGNORECASE)
UNSAFE_PATH_CHARACTER = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]")
SUBPROCESS_TIMEOUT_SECONDS = 300.0


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def check_release_archive(errors: list[str], *, installed: bool = False) -> dict | None:
    """Validate the closure/candidate or extracted manifest; retain no build output."""
    script = ROOT / "scripts" / "build_release.py"
    archive = ROOT / "dist" / f"longtask-{SKILL_VERSION}.zip"
    manifest_path = ROOT / "release-manifest.json"
    if not script.is_file() or not manifest_path.is_file():
        return
    try:
        manifest = json.loads(read_text(manifest_path))
    except json.JSONDecodeError as exc:
        fail(errors, f"invalid release manifest JSON: {exc}")
        return
    if not isinstance(manifest, dict) or manifest.get("package") != "longtask" or manifest.get("version") != SKILL_VERSION:
        fail(errors, "release manifest package/version does not match the skill")
        return
    try:
        with tempfile.TemporaryDirectory(prefix="longtask-source-check-") as temporary:
            command = (
                ["verify", "--root", str(ROOT), "--archive", str(archive)]
                if not installed and (archive.exists() or archive.is_symlink())
                else ["build", "--root", str(ROOT), "--output", str(Path(temporary) / "release.zip")]
            )
            result = subprocess.run(
                [sys.executable, str(script), *command], cwd=ROOT, text=True,
                capture_output=True, check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
            if installed and result.returncode == 0:
                # Compare the extracted manifest to the manifest rebuilt from the
                # installed closure. Authenticity comes from trusted extraction.
                try:
                    embedded = json.loads(read_text(ROOT / "RELEASE-MANIFEST.json"))
                    with zipfile.ZipFile(Path(temporary) / "release.zip") as rebuilt:
                        expected = json.loads(rebuilt.read("longtask/RELEASE-MANIFEST.json"))
                    if embedded != expected:
                        fail(errors, "installed release manifest does not match the installed closure")
                except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                    fail(errors, f"installed release manifest verification failed: {exc}")
    except subprocess.TimeoutExpired:
        fail(errors, f"release archive verification exceeded {SUBPROCESS_TIMEOUT_SECONDS:g} seconds")
        return
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        fail(errors, f"release archive verification failed: {detail}")
        return None
    try:
        binding = json.loads(result.stdout)
        if not isinstance(binding, dict) or not {
            "archive_sha256", "source_tree_sha256", "file_count",
        } <= binding.keys():
            raise ValueError("release tool omitted candidate binding")
        return binding
    except (json.JSONDecodeError, ValueError) as exc:
        fail(errors, f"invalid release candidate binding: {exc}")
        return None


HOST_RESULTS_SCHEMA_VERSION = 7
REQUIRED_HOST_CASES = {
    "discovery",
    "setup",
    "continue-restart",
    "review-read-only",
    "concurrent-worktree-conflict",
    "user-rejection",
    "missing-host-capability",
    "persistent-prompt-injection",
    "tool-output-pollution",
}
HOST_ASSERTION_FIELDS = {
    "id", "status", "claim", "run_id", "session_id", "agent_id", "model", "tool_ids",
    "guardrail_ids", "handoff_id", "exit_status", "failure_class", "usage", "latency_ms",
    "retries", "human_interventions", "trace_id", "trace_sha256", "evaluated_archive_sha256",
    "replicate_runs", "tool_call_count", "capabilities",
}
HOST_SAMPLE_FIELDS = {
    "status", "run_id", "session_id", "trace_id", "trace_sha256", "exit_status",
    "failure_class", "usage", "latency_ms", "retries", "human_interventions", "tool_call_count",
    "evaluated_archive_sha256", "agent_id", "model", "tool_ids", "guardrail_ids", "handoff_id", "capabilities",
}
ANALYSIS_FIELDS = {"dataset_splits", "sampling", "metrics", "comparisons", "grader_calibration"}
SPLIT_FIELDS = {"development", "hidden_regression", "rolling_real_distribution"}
CASE_STAT_FIELDS = {
    "id", "repetitions", "pass_count", "fail_count", "unable_to_verify_count",
    "success_rate", "bernoulli_variance", "confidence_interval_95",
}
METRIC_FIELDS = {
    "invocation_precision", "invocation_recall", "entry_routing_accuracy",
    "safe_recovery_completion_accuracy", "stale_conflict_detection_rate",
    "duplicate_rework_rate", "human_intervention_rate", "erroneous_approval_requests",
    "token_usage_total", "tool_calls_total", "latency_ms_mean", "latency_ms_p95",
    "retries_total", "cost_usd", "cost_status", "cost_reason", "document_drift_rate",
}
COMPARISON_FIELDS = {
    "kind", "status", "case_ids", "sample_count", "success_rate",
    "baseline_archive_sha256", "trace_set_sha256", "reason",
}
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SEMVER_NUMERIC = r"(?:0|[1-9][0-9]*)"
SEMVER_PRERELEASE_ID = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_BUILD_ID = r"[0-9A-Za-z-]+"
CODEX_VERSION_PATTERN = re.compile(
    rf"^codex-cli {SEMVER_NUMERIC}\.{SEMVER_NUMERIC}\.{SEMVER_NUMERIC}"
    rf"(?:-{SEMVER_PRERELEASE_ID}(?:\.{SEMVER_PRERELEASE_ID})*)?"
    rf"(?:\+{SEMVER_BUILD_ID}(?:\.{SEMVER_BUILD_ID})*)?$"
)
RELEASE_FIELDS = {"archive", "archive_sha256", "source_tree_sha256", "file_count", "version"}
RELEASE_GATE_FIELDS = {"status", "reason"}
PROFILE_FIELDS = {"audience", "supported_host", "compatibility", "legacy_state", "recovery"}
PROFILE_VALUES = {
    "audience": "personal",
    "supported_host": "Codex CLI",
    "compatibility": "breaking-forward-only",
    "legacy_state": "unsupported",
    "recovery": "current-version-reinstall",
}
INSTALLATION_FIELDS = {
    "method", "plugin_id", "installed_version", "manifest_preflight",
    "validated_archive_sha256", "workflow_trace_archive_sha256",
}
RECOVERY_FIELDS = {
    "status", "trusted_archive_verification", "workspace_backup", "clean_install",
    "failed_install_cleanup", "current_version_reinstall", "fresh_session", "run_id",
    "trace_id", "trace_sha256", "archive_sha256", "reason",
}
RECOVERY_CHECK_FIELDS = {
    "trusted_archive_verification", "workspace_backup", "clean_install",
    "failed_install_cleanup", "current_version_reinstall", "fresh_session",
}


def non_empty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def digest_string(value: object) -> bool:
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def optional_non_empty_string(value: object) -> bool:
    return value is None or non_empty_string(value)


def nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def nonnegative_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == timezone.utc else None


def valid_string_list(value: object) -> bool:
    if not isinstance(value, list) or not all(non_empty_string(item) for item in value):
        return False
    return len(value) == len(set(value))


def valid_usage(value: object) -> bool:
    # Cached tokens are a subset of input tokens, never an additional cost count.
    required = {"input_tokens", "output_tokens"}
    return (
        isinstance(value, dict)
        and required <= value.keys() <= required | {"cached_input_tokens"}
        and all(nonnegative_integer(item) for item in value.values())
        and value.get("cached_input_tokens", 0) <= value["input_tokens"]
    )


def unit_interval(value: object) -> bool:
    return nonnegative_number(value) and value <= 1


def wilson_interval(successes: int, total: int) -> tuple[float, float]:
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    center = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt((rate * (1 - rate) + z * z / (4 * total)) / total) / denominator
    return center - margin, center + margin


def close_number(left: object, right: float) -> bool:
    return (nonnegative_number(left) and nonnegative_number(right)
            and math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9))


def validate_host_sample(sample: object, case_id: object, errors: list[str], archive_sha256: str) -> bool:
    error_count = len(errors)
    label = f"{case_id} sample"
    if not isinstance(sample, dict) or set(sample) != HOST_SAMPLE_FIELDS:
        errors.append(f"host sample envelope is malformed: {case_id}")
        return False
    status = sample.get("status")
    if not isinstance(status, str) or status not in {"pass", "fail", "unable_to_verify"}:
        errors.append(f"invalid host sample status: {case_id}")
        return False
    for field in ("run_id", "session_id", "trace_id", "agent_id", "model", "handoff_id"):
        if not optional_non_empty_string(sample.get(field)):
            errors.append(f"{label} {field} is invalid")
    if sample.get("trace_sha256") is not None and not digest_string(sample.get("trace_sha256")):
        errors.append(f"{label} trace_sha256 is invalid")
    if sample.get("evaluated_archive_sha256") is not None:
        if not digest_string(sample.get("evaluated_archive_sha256")):
            errors.append(f"{label} archive digest is invalid")
        elif sample.get("evaluated_archive_sha256") != archive_sha256:
            errors.append(f"{label} archive binding is stale")
    for field in ("tool_ids", "guardrail_ids"):
        if sample.get(field) is not None and not valid_string_list(sample.get(field)):
            errors.append(f"{label} {field} is invalid")
    if sample.get("usage") is not None and not valid_usage(sample.get("usage")):
        errors.append(f"{label} usage is invalid")
    for field in ("latency_ms",):
        if sample.get(field) is not None and not nonnegative_number(sample.get(field)):
            errors.append(f"{label} {field} is invalid")
    for field in ("retries", "human_interventions", "tool_call_count"):
        if sample.get(field) is not None and not nonnegative_integer(sample.get(field)):
            errors.append(f"{label} {field} is invalid")
    exit_status = sample.get("exit_status")
    if exit_status is not None and (not isinstance(exit_status, int) or isinstance(exit_status, bool)):
        errors.append(f"{label} exit_status is invalid")
    tool_ids = sample.get("tool_ids")
    count = sample.get("tool_call_count")
    if valid_string_list(tool_ids) and nonnegative_integer(count) and count < len(tool_ids):
        errors.append(f"{label} tool_call_count is less than recorded unique tool_ids")
    capabilities = sample.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != {"guardrail", "handoff"}:
        errors.append(f"{label} capabilities envelope is malformed")
    else:
        for name, identifiers in (("guardrail", sample.get("guardrail_ids")),
                                  ("handoff", [sample["handoff_id"]] if sample.get("handoff_id") else [])):
            capability = capabilities[name]
            if not isinstance(capability, dict) or set(capability) != {"status", "reason", "evidence"}:
                errors.append(f"{label} {name} capability is malformed")
                continue
            applicability = capability.get("status")
            if not isinstance(applicability, str) or applicability not in {"observed", "not_applicable", "unavailable"} \
                    or not non_empty_string(capability.get("reason")) \
                    or not valid_string_list(capability.get("evidence")):
                errors.append(f"{label} {name} applicability is invalid")
                continue
            if applicability in {"observed", "not_applicable"} and not capability["evidence"]:
                errors.append(f"{label} {name} applicability lacks evidence")
            if applicability == "observed" and not identifiers:
                errors.append(f"{label} observed {name} lacks correlation")
            if applicability == "not_applicable" and identifiers:
                errors.append(f"{label} not_applicable {name} contradicts recorded identifiers")
            if applicability == "unavailable" and status == "pass":
                errors.append(f"passing {label} requires unavailable {name}")
    if status == "pass":
        # Personal evidence is observed, not an authenticated principal claim.
        if any(not non_empty_string(sample.get(field)) for field in ("run_id", "session_id", "trace_id")):
            errors.append(f"passing {label} lacks correlation")
        if sample.get("evaluated_archive_sha256") != archive_sha256:
            errors.append(f"passing {label} archive binding is stale")
        if not digest_string(sample.get("trace_sha256")):
            errors.append(f"passing {label} lacks trace digest")
        if exit_status != 0 or sample.get("failure_class") is not None:
            errors.append(f"passing {label} has failure telemetry")
    elif not non_empty_string(sample.get("failure_class")):
        errors.append(f"non-passing {label} lacks failure_class")
    return len(errors) == error_count


def aggregate_host_metrics(samples: list[dict[str, object]]) -> dict[str, object]:
    """Full-population metrics stay unknown if any input is unknown or invalid."""
    complete = lambda field, predicate: bool(samples) and all(predicate(row.get(field)) for row in samples)
    latency_complete = complete("latency_ms", nonnegative_number)
    latencies = sorted(float(row["latency_ms"]) for row in samples) if latency_complete else []
    return {
        "token_usage_total": sum(row["usage"]["input_tokens"] + row["usage"]["output_tokens"] for row in samples)
            if complete("usage", valid_usage) else None,
        "tool_calls_total": sum(row["tool_call_count"] for row in samples)
            if complete("tool_call_count", nonnegative_integer) else None,
        "retries_total": sum(row["retries"] for row in samples)
            if complete("retries", nonnegative_integer) else None,
        "latency_ms_mean": sum(latencies) / len(latencies) if latencies else None,
        "latency_ms_p95": latencies[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
        "human_intervention_rate": sum(row["human_interventions"] > 0 for row in samples) / len(samples)
            if complete("human_interventions", nonnegative_integer) else None,
    }


def validate_host_analysis(analysis: object, samples: dict[str, list[dict[str, object]]],
                           current_archive_sha256: str,
                           errors: list[str]) -> bool:
    if not isinstance(analysis, dict) or set(analysis) != ANALYSIS_FIELDS:
        errors.append("host analysis envelope is malformed")
        return False
    valid = True
    splits = analysis.get("dataset_splits")
    if not isinstance(splits, dict) or set(splits) != SPLIT_FIELDS \
            or any(not valid_string_list(value) or not value for value in splits.values()):
        errors.append("host dataset splits are malformed")
        valid = False
    else:
        flattened = [case for value in splits.values() for case in value]
        if len(flattened) != len(set(flattened)) or set(flattened) != REQUIRED_HOST_CASES:
            errors.append("host dataset splits must partition required cases")
            valid = False

    sampling = analysis.get("sampling")
    statistics: list[object] = []
    minimum = None
    if not isinstance(sampling, dict) or set(sampling) != {"minimum_repetitions", "case_statistics"}:
        errors.append("host sampling envelope is malformed")
        valid = False
    else:
        minimum = sampling.get("minimum_repetitions")
        statistics = sampling.get("case_statistics")
        if not nonnegative_integer(minimum) or minimum < 2 or not isinstance(statistics, list):
            errors.append("host sampling requires at least two repetitions")
            valid = False
            statistics = []
    statistic_ids = [item.get("id") for item in statistics if isinstance(item, dict)]
    if not all(isinstance(case_id, str) for case_id in statistic_ids) or len(statistic_ids) != len(set(statistic_ids)) or set(statistic_ids) != REQUIRED_HOST_CASES:
        errors.append("host sampling case coverage is incomplete")
        valid = False
    for item in statistics:
        if not isinstance(item, dict) or set(item) != CASE_STAT_FIELDS:
            errors.append("host case statistic envelope is malformed")
            valid = False
            continue
        case_id = item.get("id")
        observed = samples.get(case_id, []) if isinstance(case_id, str) else []
        counts = {
            "pass_count": sum(sample.get("status") == "pass" for sample in observed),
            "fail_count": sum(sample.get("status") == "fail" for sample in observed),
            "unable_to_verify_count": sum(sample.get("status") == "unable_to_verify" for sample in observed),
        }
        total = len(observed)
        if item.get("repetitions") != total or any(item.get(key) != value for key, value in counts.items()):
            errors.append(f"host case statistics do not match samples: {case_id}")
            valid = False
            continue
        rate = counts["pass_count"] / total if total else 0.0
        interval = item.get("confidence_interval_95")
        expected_interval = wilson_interval(counts["pass_count"], total) if total else (0.0, 0.0)
        if not close_number(item.get("success_rate"), rate) \
                or not close_number(item.get("bernoulli_variance"), rate * (1 - rate)) \
                or not isinstance(interval, list) or len(interval) != 2 \
                or not close_number(interval[0], expected_interval[0]) \
                or not close_number(interval[1], expected_interval[1]):
            errors.append(f"host case statistics are numerically inconsistent: {case_id}")
            valid = False
    metrics = analysis.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != METRIC_FIELDS:
        errors.append("host metrics envelope is malformed")
        valid = False
    else:
        rates = {
            "invocation_precision", "invocation_recall", "entry_routing_accuracy",
            "safe_recovery_completion_accuracy", "stale_conflict_detection_rate",
            "duplicate_rework_rate", "human_intervention_rate", "document_drift_rate",
        }
        if any(not unit_interval(metrics.get(field))
               and metrics.get(field) is not None for field in rates):
            errors.append("host rate metrics must be in the unit interval")
            valid = False
        all_samples = [sample for group in samples.values() for sample in group]
        for field, expected in aggregate_host_metrics(all_samples).items():
            actual = metrics.get(field)
            if (expected is None and actual is not None) or (expected is not None and not close_number(actual, expected)):
                errors.append(f"host aggregate metrics do not match samples: {field}")
                valid = False
        if metrics.get("erroneous_approval_requests") is not None and not nonnegative_integer(metrics["erroneous_approval_requests"]):
            errors.append("host erroneous approval count is invalid")
            valid = False
        cost_status = metrics.get("cost_status")
        cost = metrics.get("cost_usd")
        if not isinstance(cost_status, str) or cost_status not in {"measured", "unable_to_verify"} \
                or cost_status == "measured" and not nonnegative_number(cost) \
                or cost_status == "unable_to_verify" and cost is not None \
                or not non_empty_string(metrics.get("cost_reason")):
            errors.append("host cost metric is malformed")
            valid = False
    comparisons = analysis.get("comparisons")
    if not isinstance(comparisons, list) or len(comparisons) != 2:
        errors.append("host comparisons must contain two baselines")
        valid = False
    else:
        kinds = []
        for comparison in comparisons:
            if not isinstance(comparison, dict) or set(comparison) != COMPARISON_FIELDS:
                errors.append("host comparison envelope is malformed")
                valid = False
                continue
            kinds.append(comparison.get("kind"))
            if not isinstance(comparison.get("status"), str) or comparison.get("status") not in {"pass", "fail", "unable_to_verify"} \
                    or not valid_string_list(comparison.get("case_ids")) \
                    or set(comparison.get("case_ids", [])) != REQUIRED_HOST_CASES \
                    or not nonnegative_integer(comparison.get("sample_count")) \
                    or (comparison.get("status") == "pass" and comparison.get("sample_count") < len(REQUIRED_HOST_CASES)) \
                    or (not unit_interval(comparison.get("success_rate"))
                        and (comparison.get("status") == "pass" or comparison.get("success_rate") is not None)) \
                    or (comparison.get("trace_set_sha256") is not None and not digest_string(comparison.get("trace_set_sha256"))) \
                    or not non_empty_string(comparison.get("reason")):
                errors.append(f"host comparison is invalid: {comparison.get('kind')}")
                valid = False
            baseline = comparison.get("baseline_archive_sha256")
            if comparison.get("kind") == "without_skill" and baseline is not None \
                    or comparison.get("kind") == "previous_candidate" \
                    and (baseline is not None and (not digest_string(baseline) or baseline == current_archive_sha256)):
                errors.append(f"host comparison archive binding is invalid: {comparison.get('kind')}")
                valid = False
            if comparison.get("status") == "pass" and (
                not digest_string(comparison.get("trace_set_sha256"))
                or comparison.get("kind") == "previous_candidate" and not digest_string(baseline)
            ):
                errors.append("passing comparison lacks trace or archive binding")
                valid = False
        if not all(isinstance(kind, str) for kind in kinds) or set(kinds) != {"without_skill", "previous_candidate"}:
            errors.append("host comparison kinds are incomplete")
            valid = False

    grader = analysis.get("grader_calibration")
    if not isinstance(grader, dict) or set(grader) != {
        "method", "calibrated", "human_reviewer", "calibration_case_ids",
    } or not non_empty_string(grader.get("method")) or type(grader.get("calibrated")) is not bool \
            or not optional_non_empty_string(grader.get("human_reviewer")) \
            or not valid_string_list(grader.get("calibration_case_ids")) \
            or grader.get("calibrated") and (
                grader.get("calibrated") is not True or not non_empty_string(grader.get("human_reviewer"))
                or not grader.get("calibration_case_ids")
                or not set(grader["calibration_case_ids"]).issubset(REQUIRED_HOST_CASES)
            ):
        errors.append("host grader calibration is incomplete")
        valid = False
    return valid


def host_results_errors(data: object, current_archive_sha256: str, current_tree_sha256: str,
                        current_file_count: int | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["host results root must be an object"]
    required_root = {
        "schema_version", "evaluated_at", "profile", "release", "release_gate",
        "host", "recovery", "privacy", "analysis", "benchmark_gate",
    }
    if set(data) != required_root or data.get("schema_version") != HOST_RESULTS_SCHEMA_VERSION:
        return [f"host results root does not match schema version {HOST_RESULTS_SCHEMA_VERSION}"]

    evaluated_at = utc_timestamp(data.get("evaluated_at"))
    if evaluated_at is None:
        errors.append("host results evaluated_at must be an absolute UTC timestamp")

    profile = data.get("profile")
    if not isinstance(profile, dict) or set(profile) != PROFILE_FIELDS or profile != PROFILE_VALUES:
        errors.append("host results profile must declare the Codex-only breaking personal contract")

    release = data.get("release")
    if not isinstance(release, dict) or set(release) != RELEASE_FIELDS:
        errors.append("host results release must be an object")
    else:
        if release.get("archive") != f"dist/longtask-{SKILL_VERSION}.zip":
            errors.append("host results current archive path is stale")
        if release.get("archive_sha256") != current_archive_sha256:
            errors.append("host results current archive digest is stale")
        if release.get("source_tree_sha256") != current_tree_sha256:
            errors.append("host results source tree digest is stale")
        if release.get("version") != SKILL_VERSION:
            errors.append("host results release version is stale")
        if not nonnegative_integer(release.get("file_count")) or release.get("file_count") == 0:
            errors.append("host results release file_count must be a positive integer")
        elif current_file_count is not None and release.get("file_count") != current_file_count:
            errors.append("host results release file_count is stale")

    recovery = data.get("recovery")
    recovery_pass = False
    if not isinstance(recovery, dict) or set(recovery) != RECOVERY_FIELDS:
        errors.append("host results recovery envelope is malformed")
    else:
        statuses = [recovery.get(field) for field in sorted(RECOVERY_CHECK_FIELDS)]
        if any(not isinstance(value, str) or value not in {"pass", "fail", "unable_to_verify"} for value in statuses):
            errors.append("host results recovery subcheck status is invalid")
        else:
            expected_recovery_status = (
                "pass" if all(value == "pass" for value in statuses)
                else "failed" if any(value == "fail" for value in statuses)
                else "partial" if any(value == "pass" for value in statuses)
                else "unable_to_verify"
            )
            if recovery.get("status") != expected_recovery_status:
                errors.append("host results recovery summary does not match its subchecks")
            recovery_pass = expected_recovery_status == "pass"
        if recovery.get("archive_sha256") != current_archive_sha256:
            errors.append("host results recovery archive digest is stale")
        if not non_empty_string(recovery.get("reason")):
            errors.append("host results recovery reason must be non-empty")
        for field in ("run_id", "trace_id"):
            if not optional_non_empty_string(recovery.get(field)):
                errors.append(f"host results recovery {field} is invalid")
        trace_digest = recovery.get("trace_sha256")
        if trace_digest is not None and not digest_string(trace_digest):
            errors.append("host results recovery trace_sha256 is invalid")
        if recovery.get("status") == "pass" and (
            not non_empty_string(recovery.get("run_id"))
            or not non_empty_string(recovery.get("trace_id"))
            or not digest_string(trace_digest)
        ):
            errors.append("passing recovery drill lacks trace correlation")

    observed_samples: dict[str, list[dict[str, object]]] = {}
    host = data.get("host")
    host_pass = False
    if not isinstance(host, dict) or set(host) != {"host", "version", "status", "installation", "assertions"}:
        errors.append("Codex host result envelope is malformed")
    else:
        host_name = host.get("host")
        if host_name != "Codex CLI":
            errors.append("host results must contain exactly the Codex CLI host")
        if not optional_non_empty_string(host.get("version")):
            errors.append("Codex host version must be null or non-empty")
        if not isinstance(host.get("status"), str) or host.get("status") not in {"pass", "failed", "unable_to_verify"}:
            errors.append("invalid Codex host status")

        installation = host.get("installation")
        installation_result: object = "fail"
        if not isinstance(installation, dict) or set(installation) != INSTALLATION_FIELDS:
            errors.append("Codex host installation envelope is malformed")
        else:
            if installation.get("method") != "trusted archive extraction":
                errors.append("Codex installation method must use trusted archive extraction")
            if installation.get("plugin_id") != "longtask":
                errors.append("Codex installation plugin_id must be longtask")
            if installation.get("installed_version") != SKILL_VERSION:
                errors.append("Codex installation version is stale")
            if installation.get("validated_archive_sha256") != current_archive_sha256:
                errors.append("Codex installation archive digest is stale")
            if not isinstance(installation.get("manifest_preflight"), str) or installation.get("manifest_preflight") not in {"pass", "fail", "unable_to_verify"}:
                errors.append("Codex manifest preflight status is invalid")
            workflow_digest = installation.get("workflow_trace_archive_sha256")
            if workflow_digest is not None and not digest_string(workflow_digest):
                errors.append("Codex workflow trace archive digest is invalid")
            installation_result = installation.get("manifest_preflight")

        assertions = host.get("assertions")
        if not isinstance(assertions, list):
            errors.append("Codex host assertions must be an array")
            assertions = []
        ids = [item.get("id") for item in assertions if isinstance(item, dict)]
        if not all(isinstance(case_id, str) for case_id in ids) \
                or len(ids) != len(set(ids)) or set(ids) != REQUIRED_HOST_CASES:
            errors.append("Codex host case coverage is incomplete")

        assertions_pass = (
            all(isinstance(case_id, str) for case_id in ids)
            and len(ids) == len(REQUIRED_HOST_CASES) and set(ids) == REQUIRED_HOST_CASES
        )
        assertion_statuses: list[str] = []
        analysis = data.get("analysis")
        sampling = analysis.get("sampling") if isinstance(analysis, dict) else None
        minimum = sampling.get("minimum_repetitions") if isinstance(sampling, dict) else None
        for assertion in assertions:
            if not isinstance(assertion, dict) or set(assertion) != HOST_ASSERTION_FIELDS:
                errors.append("Codex host assertion envelope is malformed")
                assertions_pass = False
                continue
            case_id = assertion.get("id")
            if not non_empty_string(assertion.get("claim")):
                errors.append(f"host assertion claim must be non-empty: {case_id}")
            replicate_runs = assertion.get("replicate_runs")
            if not isinstance(replicate_runs, list):
                errors.append(f"host assertion replicate_runs must be an array: {case_id}")
                replicate_runs = []
                assertions_pass = False
            # The assertion status describes the first sample, not the case's
            # aggregate outcome. Apply the same contract regardless of order.
            primary = {field: assertion.get(field) for field in HOST_SAMPLE_FIELDS}
            samples = [primary, *replicate_runs]
            for sample in samples:
                if not validate_host_sample(sample, case_id, errors, current_archive_sha256):
                    assertions_pass = False
            samples = [sample for sample in samples if isinstance(sample, dict)]
            if isinstance(case_id, str):
                observed_samples[case_id] = samples
            # A new trace label does not make an existing execution independent.
            duplicate_identity = False
            for field in ("run_id", "session_id", "trace_id", "trace_sha256"):
                identities = [sample.get(field) for sample in samples if isinstance(sample.get(field), str)]
                duplicate_identity |= len(identities) != len(set(identities))
            if duplicate_identity:
                errors.append(f"host assertion reuses sample execution identity: {case_id}")
                assertions_pass = False
            statuses = [sample.get("status") for sample in samples]
            passed = statuses.count("pass")
            case_pass = (
                nonnegative_integer(minimum) and minimum >= 2
                and passed >= minimum and passed * 3 >= len(samples) * 2
            )
            assertions_pass = assertions_pass and case_pass
            assertion_statuses.append(
                "pass" if case_pass else "fail" if "fail" in statuses else "unable_to_verify"
            )

        expected_host_status = (
            "pass" if assertions_pass and installation_result == "pass"
            else "failed" if "fail" in assertion_statuses or installation_result == "fail"
            else "unable_to_verify"
        )
        if expected_host_status == "pass" and (
            not isinstance(host.get("version"), str)
            or CODEX_VERSION_PATTERN.fullmatch(host["version"]) is None
        ):
            errors.append("passing Codex host requires an exact codex-cli version")
        if expected_host_status == "pass" and isinstance(installation, dict) \
                and installation.get("workflow_trace_archive_sha256") != current_archive_sha256:
            errors.append("passing Codex workflow trace archive digest is stale")
        if host.get("status") != expected_host_status:
            errors.append("Codex host status does not match installation and assertions")
        host_pass = host.get("status") == "pass" and expected_host_status == "pass"

    analysis_pass = validate_host_analysis(
        data.get("analysis"), observed_samples, current_archive_sha256, errors,
    )
    analysis = data.get("analysis")
    calibrated = (isinstance(analysis, dict) and isinstance(analysis.get("grader_calibration"), dict)
                  and analysis["grader_calibration"].get("calibrated") is True)
    core_pass = host_pass and recovery_pass and analysis_pass and calibrated
    for key, expected_pass in (("release_gate", core_pass),
                               ("benchmark_gate", core_pass and benchmark_ready(analysis))):
        gate = data.get(key)
        if not isinstance(gate, dict) or set(gate) != RELEASE_GATE_FIELDS \
                or not isinstance(gate.get("status"), str) or gate.get("status") not in {"pass", "blocked"} or not non_empty_string(gate.get("reason")):
            errors.append(f"host results {key.replace('_', ' ')} is invalid")
        elif gate.get("status") != ("pass" if expected_pass else "blocked"):
            errors.append(f"{key.replace('_', ' ')} does not match its required evidence")

    privacy = data.get("privacy")
    if not isinstance(privacy, dict) or set(privacy) != {
        "stored_content", "raw_trace_retention_until", "access_scope", "redaction",
    } or any(not non_empty_string(privacy.get(field)) for field in privacy):
        errors.append("host results privacy/retention envelope is incomplete")
    else:
        retention = utc_timestamp(privacy.get("raw_trace_retention_until"))
        if retention is None or evaluated_at is not None and retention <= evaluated_at:
            errors.append("host results raw trace retention deadline is invalid")
    return errors


def check_host_results(errors: list[str], binding: dict | None = None) -> None:
    results_path = ROOT / "evals" / "host_results.json"
    if not results_path.is_file():
        return
    if binding is None:
        binding = check_release_archive(errors)
    if binding is None:
        return
    try:
        data = json.loads(read_text(results_path))
    except (OSError, json.JSONDecodeError) as exc:
        fail(errors, f"cannot inspect host result bindings: {exc}")
        return
    for error in host_results_errors(
        data, binding["archive_sha256"], binding["source_tree_sha256"], binding["file_count"],
    ):
        fail(errors, error)


def benchmark_ready(analysis: object) -> bool:
    if not isinstance(analysis, dict):
        return False
    metrics, comparisons = analysis.get("metrics"), analysis.get("comparisons")
    if not isinstance(metrics, dict) or not isinstance(comparisons, list):
        return False
    measured = METRIC_FIELDS - {"cost_usd", "cost_status", "cost_reason"}
    return (all(nonnegative_number(metrics.get(field)) for field in measured)
            and all(metrics.get(field) == 1 for field in (
                "invocation_precision", "invocation_recall", "entry_routing_accuracy",
                "safe_recovery_completion_accuracy", "stale_conflict_detection_rate"))
            and len(comparisons) == 2
            and all(isinstance(item, dict) and item.get("status") == "pass" for item in comparisons))


def release_blockers(data: object) -> list[str]:
    """Readable missing conditions; structural validation still runs separately."""
    if not isinstance(data, dict):
        return ["missing host results"]
    reasons = []
    host = data.get("host")
    if not isinstance(host, dict):
        return ["missing host evidence"]
    installation = host.get("installation", {})
    if not isinstance(installation, dict) or installation.get("manifest_preflight") != "pass":
        reasons.append("installation: current trusted archive installation not verified")
    analysis = data.get("analysis", {})
    sampling = analysis.get("sampling", {}) if isinstance(analysis, dict) else {}
    minimum = sampling.get("minimum_repetitions", 2) if isinstance(sampling, dict) else 2
    if not nonnegative_integer(minimum):
        minimum = 2
    assertions = host.get("assertions", [])
    for case in sorted(REQUIRED_HOST_CASES):
        item = next((a for a in assertions if isinstance(a, dict) and a.get("id") == case), None) if isinstance(assertions, list) else None
        replicas = item.get("replicate_runs", []) if item else []
        rows = [item, *replicas] if item and isinstance(replicas, list) else []
        passed = sum(isinstance(row, dict) and row.get("status") == "pass" for row in rows)
        if passed < minimum or passed * 3 < len(rows) * 2:
            reasons.append(f"host:{case}: {passed} passing of {len(rows)} records; requires >= {minimum} and >= 2/3")
    recovery = data.get("recovery", {})
    for check in sorted(RECOVERY_CHECK_FIELDS):
        if not isinstance(recovery, dict) or recovery.get(check) != "pass":
            reasons.append(f"recovery:{check}: not verified for current archive")
    grader = analysis.get("grader_calibration", {}) if isinstance(analysis, dict) else {}
    if not isinstance(grader, dict) or grader.get("calibrated") is not True:
        reasons.append("grader: human calibration missing")
    return reasons


def release_gate_pass(data: object) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("release_gate"), dict)
        and data["release_gate"].get("status") == "pass"
    )


def parse_yaml_scalar(raw: str) -> str | None:
    """Parse the small YAML scalar subset used by skill metadata.

    Returning None distinguishes malformed quoting from an empty scalar.
    """
    value = raw.strip()
    if not value:
        return ""
    if value[0] == '"':
        if len(value) < 2 or value[-1] != '"':
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, str) else None
    if value[0] == "'":
        if not re.fullmatch(r"'(?:[^']|'')*'", value):
            return None
        return value[1:-1].replace("''", "'")
    return value


def frontmatter(path: Path) -> dict[str, str]:
    """Parse the scalar fields needed by this validator without a YAML dependency."""
    text = read_text(path)
    match = re.match(r"---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    in_metadata = False
    for raw_line in match.group(1).splitlines():
        if raw_line.strip() == "metadata:":
            in_metadata = True
            continue
        scalar = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", raw_line)
        nested = re.match(r"^\s+([A-Za-z_][\w-]*):\s*(.*)$", raw_line)
        if in_metadata and nested:
            value = parse_yaml_scalar(nested.group(2))
            if value is not None:
                result[f"metadata.{nested.group(1)}"] = value
        elif scalar and not raw_line.startswith((" ", "\t")):
            in_metadata = False
            value = parse_yaml_scalar(scalar.group(2))
            if value is not None:
                result[scalar.group(1)] = value
    return result


def strip_fenced_code(text: str) -> str:
    """Remove CommonMark fenced blocks while preserving non-fenced line positions."""
    result: list[str] = []
    marker: str | None = None
    width = 0
    for line in text.splitlines(keepends=True):
        match = FENCE.match(line)
        if marker is None:
            if match:
                marker = match.group(1)[0]
                width = len(match.group(1))
                result.append("\n" if line.endswith("\n") else "")
            else:
                result.append(line)
            continue
        if match and match.group(1)[0] == marker and len(match.group(1)) >= width and not match.group(2).strip():
            marker = None
            width = 0
        result.append("\n" if line.endswith("\n") else "")
    return "".join(result)


def markdown_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.md")
                  if not {".git", ".longtask", "dist"}.intersection(path.relative_to(root).parts)
                  and not any(fnmatch.fnmatchcase(path.relative_to(root).as_posix(), pattern)
                              for pattern in GENERATED_PATH_PATTERNS))


def check_portable_markdown_paths(root: Path, errors: list[str]) -> None:
    """Reject Markdown paths that are unstable across common Git worktrees."""
    normalized: dict[tuple[str, ...], Path] = {}
    for path in markdown_files(root):
        relative = path.relative_to(root)
        for part in relative.parts:
            if unicodedata.normalize("NFC", part) != part:
                fail(errors, f"Markdown path is not Unicode NFC: {relative}")
            if UNSAFE_PATH_CHARACTER.search(part) or part.endswith((" ", ".")):
                fail(errors, f"Markdown path is not portable: {relative}")
            if WINDOWS_RESERVED_NAME.fullmatch(part):
                fail(errors, f"Markdown path uses a Windows reserved name: {relative}")
        key = tuple(unicodedata.normalize("NFC", part).casefold() for part in relative.parts)
        previous = normalized.get(key)
        if previous is not None and previous != relative:
            fail(errors, f"Markdown paths collide after NFC/case folding: {previous} and {relative}")
        else:
            normalized[key] = relative


def inline_link_targets(text: str) -> list[str]:
    """Extract inline Markdown destinations, including balanced parentheses and <...>."""
    targets: list[str] = []
    cursor = 0
    while True:
        opening = text.find("](", cursor)
        if opening < 0:
            return targets
        index = opening + 2
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == "<":
            closing = text.find(">", index + 1)
            if closing >= 0:
                suffix = closing + 1
                while suffix < len(text) and text[suffix].isspace():
                    suffix += 1
                if suffix < len(text) and text[suffix] == ")":
                    targets.append(text[index:suffix])
                    cursor = suffix + 1
                    continue
        depth = 0
        escaped = False
        end = index
        while end < len(text):
            char = text[end]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "(":
                depth += 1
            elif char == ")":
                if depth == 0:
                    targets.append(text[index:end])
                    end += 1
                    break
                depth -= 1
            end += 1
        cursor = max(end, opening + 2)


def reference_links(text: str) -> tuple[list[str], list[str]]:
    definitions = {match.group(1).strip().casefold(): match.group(2) for match in REFERENCE_DEFINITION.finditer(text)}
    unresolved: list[str] = []
    for match in REFERENCE_USAGE.finditer(text):
        label = (match.group(2) or match.group(1)).strip().casefold()
        if label not in definitions:
            unresolved.append(label)
    return list(definitions.values()), unresolved


def resolve_link(source: Path, raw_target: str) -> tuple[Path, str] | None:
    target = raw_target.strip()
    if target.startswith("<") and ">" in target:
        target = target[1:target.index(">")]
    else:
        target = target.split(maxsplit=1)[0]
    if not target or "{" in target or "}" in target:
        return None
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target):
        return None
    path_text, separator, fragment = target.partition("#")
    if not path_text and separator:
        return source.resolve(), fragment
    decoded = unquote(path_text)
    destination = Path(decoded)
    if destination.is_absolute():
        resolved = destination.resolve()
    else:
        resolved = (source.parent / destination).resolve()
    return resolved, fragment if separator else ""


def heading_slugs(path: Path) -> set[str]:
    """Return GitHub-style heading anchors, including duplicate suffixes."""
    slugs: set[str] = set()
    counts: dict[str, int] = {}
    lines = strip_fenced_code(read_text(path)).splitlines()

    def add_heading(raw: str) -> None:
        heading = re.sub(r"<[^>]*>", "", raw).strip().casefold()
        base = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE).replace(" ", "-")
        occurrence = counts.get(base, 0)
        candidate = base if occurrence == 0 else f"{base}-{occurrence}"
        while candidate in slugs:
            occurrence += 1
            candidate = f"{base}-{occurrence}"
        counts[base] = occurrence + 1
        slugs.add(candidate)

    for index, line in enumerate(lines):
        match = re.match(r"^[ \t]{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            add_heading(match.group(1))
        elif index > 0 and re.fullmatch(r"[ \t]{0,3}(?:=+|-+)[ \t]*", line) and lines[index - 1].strip():
            add_heading(lines[index - 1])
    return slugs


def check_markdown_links(root: Path, errors: list[str]) -> None:
    root_resolved = root.resolve()
    for source in markdown_files(root):
        text = strip_fenced_code(read_text(source))
        relative_source = source.relative_to(root)
        inline_targets = inline_link_targets(text)
        reference_targets, unresolved = reference_links(text)
        for label in unresolved:
            fail(errors, f"undefined reference link in {relative_source}: {label}")
        for raw_target in [*inline_targets, *reference_targets]:
            resolved = resolve_link(source, raw_target)
            if resolved is None:
                continue
            target, fragment = resolved
            try:
                target.relative_to(root_resolved)
            except ValueError:
                fail(errors, f"local link escapes package in {relative_source}: {raw_target}")
                continue
            if not target.exists():
                fail(errors, f"dead local link in {relative_source}: {raw_target}")
                continue
            line_match = LINE_ANCHOR.fullmatch(fragment)
            if line_match:
                if not target.is_file():
                    fail(errors, f"line anchor targets a directory in {relative_source}: {raw_target}")
                    continue
                line_count = len(read_text(target).splitlines())
                start = int(line_match.group(1))
                end = int(line_match.group(2) or start)
                if start < 1 or end < start or end > line_count:
                    fail(errors, f"line anchor out of range in {relative_source}: {raw_target} ({line_count} lines)")
            elif fragment and target.is_file() and target.suffix.lower() == ".md":
                decoded_fragment = unquote(fragment).casefold()
                if decoded_fragment not in heading_slugs(target):
                    fail(errors, f"missing heading anchor in {relative_source}: {raw_target}")


def check_skill(path: Path, expected_name: str, errors: list[str]) -> None:
    if not path.is_file():
        fail(errors, f"missing skill: {path.relative_to(ROOT)}")
        return
    data = frontmatter(path)
    text = read_text(path)
    match = re.match(r"---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        fail(errors, f"{path.relative_to(ROOT)} lacks valid frontmatter delimiters")
        return
    allowed = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    in_metadata = False
    seen: set[str] = set()
    for number, line in enumerate(match.group(1).splitlines(), start=2):
        if "\t" in line:
            fail(errors, f"tab indentation in {path.relative_to(ROOT)} frontmatter line {number}")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(" "):
            if not in_metadata or not re.fullmatch(r"  [A-Za-z_][\w-]*:\s*.+", line):
                fail(errors, f"unsupported YAML structure in {path.relative_to(ROOT)} line {number}")
            elif parse_yaml_scalar(line.split(":", 1)[1]) is None:
                fail(errors, f"invalid quoted scalar in {path.relative_to(ROOT)} line {number}")
            else:
                key = "metadata." + line.strip().split(":", 1)[0]
                if key in seen:
                    fail(errors, f"duplicate frontmatter field in {path.relative_to(ROOT)}: {key}")
                seen.add(key)
            continue
        field = re.match(r"^([A-Za-z_][\w-]*):(?:\s*.*)?$", line)
        if not field or field.group(1) not in allowed:
            fail(errors, f"invalid frontmatter field in {path.relative_to(ROOT)} line {number}")
            in_metadata = False
        else:
            key = field.group(1)
            if key in seen:
                fail(errors, f"duplicate frontmatter field in {path.relative_to(ROOT)}: {key}")
            seen.add(key)
            raw_value = line.split(":", 1)[1]
            if field.group(1) != "metadata" and parse_yaml_scalar(raw_value) is None:
                fail(errors, f"invalid quoted scalar in {path.relative_to(ROOT)} line {number}")
            in_metadata = field.group(1) == "metadata"
    if data.get("name") != expected_name:
        fail(errors, f"{path.relative_to(ROOT)} name should be {expected_name}, got {data.get('name')!r}")
    elif not re.fullmatch(r"(?!.*--)[a-z0-9]+(?:-[a-z0-9]+)*", expected_name) or len(expected_name) > 64:
        fail(errors, f"{path.relative_to(ROOT)} has a non-conforming skill name")
    if data.get("metadata.version") != SKILL_VERSION:
        fail(errors, f"{path.relative_to(ROOT)} metadata.version should be {SKILL_VERSION}")
    if data.get("license") != "MIT":
        fail(errors, f"{path.relative_to(ROOT)} license should be MIT (project convention)")
    compatibility = data.get("compatibility", "")
    if not compatibility or len(compatibility) > 500:
        fail(errors, f"{path.relative_to(ROOT)} compatibility must contain 1-500 characters")
    if not data.get("description"):
        fail(errors, f"{path.relative_to(ROOT)} is missing a description")
    elif len(data["description"]) > 1024:
        fail(errors, f"{path.relative_to(ROOT)} description exceeds 1024 characters")
    body = text[match.end():].lstrip()
    if not body.startswith("# "):
        fail(errors, f"{path.relative_to(ROOT)} body must start with an H1 heading")


def check_skill_interface(path: Path, expected_name: str, errors: list[str]) -> None:
    """Check this project's flat UI/policy YAML profile without runtime dependencies.

    This is a repository convention, not a general-purpose Codex YAML parser.
    """
    relative = path.relative_to(ROOT)
    if not path.is_file():
        fail(errors, f"missing skill interface: {relative}")
        return
    values: dict[str, str] = {}
    sections: set[str] = set()
    section = ""
    for number, line in enumerate(read_text(path).splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line in ("interface:", "policy:"):
            section = line[:-1]
            if section in sections:
                fail(errors, f"duplicate interface section in {relative}: {section}")
            sections.add(section)
            continue
        match = re.fullmatch(r"  ([a-z_]+): (.+)", line)
        if not match or not section:
            fail(errors, f"unsupported interface YAML in {relative} line {number}")
            continue
        key = f"{section}.{match.group(1)}"
        raw = match.group(2)
        value = parse_yaml_scalar(raw)
        if key in values or value is None:
            fail(errors, f"duplicate or invalid interface value in {relative}: {key}")
        if section == "interface" and not raw.startswith(('"', "'")):
            fail(errors, f"interface strings must be quoted in {relative}: {key}")
        if key == "policy.allow_implicit_invocation" and raw not in ("true", "false"):
            fail(errors, f"invocation policy must be a YAML boolean in {relative}")
        values[key] = value or ""
    for field in ("display_name", "short_description", "default_prompt"):
        if not values.get(f"interface.{field}", "").strip():
            fail(errors, f"{relative} lacks interface.{field}")
    short = values.get("interface.short_description", "")
    if short and not 25 <= len(short) <= 64:
        fail(errors, f"{relative} short_description must contain 25-64 characters (project convention)")
    prompt = values.get("interface.default_prompt", "")
    if not re.search(r"\$" + re.escape(expected_name) + r"(?![a-z0-9-])", prompt):
        fail(errors, f"{relative} default_prompt must invoke ${expected_name}")
    if values.get("policy.allow_implicit_invocation") != "true":
        fail(errors, f"{relative} must preserve implicit invocation (project convention)")


def check_disclosure_graph(errors: list[str]) -> None:
    root_text = strip_fenced_code(read_text(ROOT / "SKILL.md"))
    linked_targets = {target.strip("<>").split("#", 1)[0] for target in inline_link_targets(root_text)}
    for expected in MODE_REFERENCES:
        if expected not in linked_targets:
            fail(errors, f"root skill does not link selected mode instructions: {expected}")


def check_discovery_surface(errors: list[str]) -> None:
    discovered = {path.relative_to(ROOT / "skills").as_posix()
                  for path in (ROOT / "skills").rglob("SKILL.md")}
    expected = {f"{name}/SKILL.md" for name in SKILL_NAMES}
    if discovered != expected:
        fail(errors, f"skill discovery surface must contain only the declared entries: {sorted(discovered)}")
    interfaces = {path.relative_to(ROOT / "skills").as_posix()
                  for path in (ROOT / "skills").rglob("openai.yaml")}
    if interfaces != {f"{name}/agents/openai.yaml" for name in SKILL_NAMES}:
        fail(errors, "skill interface surface contains missing or undeclared entries")


def load_json(path: Path, errors: list[str]) -> dict[str, object] | None:
    try:
        value = json.loads(read_text(path))
    except (OSError, json.JSONDecodeError) as exc:
        fail(errors, f"invalid JSON in {path.relative_to(ROOT)}: {exc}")
        return None
    if not isinstance(value, dict):
        fail(errors, f"JSON root must be an object: {path.relative_to(ROOT)}")
        return None
    return value


def check_forward_results(errors: list[str]) -> None:
    try:
        process = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_forward_evals.py")],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail(errors, f"forward result verification exceeded {SUBPROCESS_TIMEOUT_SECONDS:g} seconds")
        return
    if process.returncode != 0:
        try:
            payload = json.loads(process.stdout or process.stderr)
            for message in payload.get("errors", []):
                fail(errors, str(message))
        except (json.JSONDecodeError, AttributeError):
            fail(errors, "forward result verifier failed: " + (process.stderr or process.stdout).strip())


def check_memory_results(errors: list[str]) -> None:
    try:
        process = subprocess.run(
            [sys.executable, str(ROOT / "scripts/run_memory_evals.py")],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail(errors, f"memory result verification exceeded {SUBPROCESS_TIMEOUT_SECONDS:g} seconds")
        return
    if process.returncode != 0:
        try:
            payload = json.loads(process.stdout or process.stderr)
            messages = payload.get("errors", [])
            if not messages:
                fail(errors, "memory result verifier failed without structured errors")
            for message in messages:
                fail(errors, str(message))
        except (json.JSONDecodeError, AttributeError):
            fail(errors, "memory result verifier failed: " + (process.stderr or process.stdout).strip())


def check_invocation_results(errors: list[str]) -> None:
    try:
        process = subprocess.run(
            [
                sys.executable, str(ROOT / "scripts" / "run_skill_evals.py"),
                "--results", str(ROOT / "evals" / "invocation_results.json"),
            ],
            cwd=ROOT, capture_output=True, text=True, check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        fail(errors, f"invocation result verification exceeded {SUBPROCESS_TIMEOUT_SECONDS:g} seconds")
        return
    if process.returncode != 0:
        try:
            payload = json.loads(process.stdout or process.stderr)
            for message in payload.get("errors", []):
                fail(errors, str(message))
        except (json.JSONDecodeError, AttributeError):
            fail(errors, "invocation result verifier failed: " + (process.stderr or process.stdout).strip())


def check_compaction_hooks(errors: list[str]) -> None:
    data = load_json(ROOT / "hooks/hooks.json", errors)
    if data is None:
        return
    events = data.get("hooks")
    if not isinstance(events, dict) or set(events) != {"PreCompact", "SessionStart"}:
        fail(errors, "compaction bridge must declare only PreCompact and SessionStart")
        return
    for event, accepted, rejected in (
        ("PreCompact", ("auto", "manual"), ("startup", "compact", "auto-other", "")),
        ("SessionStart", ("compact",), ("startup", "resume", "clear", "compact-other", "")),
    ):
        groups = events[event]
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            fail(errors, f"{event} requires one bounded synchronous hook group")
            continue
        group = groups[0]
        try:
            matcher = re.compile(group.get("matcher", ""))
            if not all(matcher.search(value) for value in accepted) or any(matcher.search(value) for value in rejected):
                fail(errors, f"{event} matcher must target only its compaction sources")
        except (re.error, TypeError):
            fail(errors, f"invalid {event} matcher")
        hooks = group.get("hooks")
        if not isinstance(hooks, list) or len(hooks) != 1 or not isinstance(hooks[0], dict):
            fail(errors, f"{event} requires one command consumer")
            continue
        hook = hooks[0]
        if (hook.get("type") != "command" or hook.get("command") != 'python3 "${PLUGIN_ROOT}/scripts/longtask_hooks.py"'
                or hook.get("async", False) is not False):
            fail(errors, f"{event} must synchronously consume the quoted bundled bridge")
        timeout = hook.get("timeout")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 5:
            fail(errors, f"{event} hook timeout exceeds the bounded callback contract")
        if event == "SessionStart":
            budget = hook.get("additionalContextLimit")
            if isinstance(budget, bool) or not isinstance(budget, int) or not 0 < budget <= 2500:
                fail(errors, "SessionStart requires bounded additionalContext")


def check_metadata(errors: list[str]) -> None:
    codex_plugin = load_json(ROOT / ".codex-plugin" / "plugin.json", errors)
    schema = load_json(ROOT / "references" / "state.schema.json", errors)
    marketplace = load_json(ROOT / "references" / "personal-marketplace.json", errors)
    if codex_plugin is not None:
        if codex_plugin.get("name") != "longtask" or codex_plugin.get("version") != SKILL_VERSION:
            fail(errors, "Codex plugin identity/version does not match longtask")
        if codex_plugin.get("skills") != "./skills/":
            fail(errors, "Codex plugin must use the native ./skills/ discovery root")
        if codex_plugin.get("hooks") != "./hooks/hooks.json":
            fail(errors, "Codex plugin must discover the bundled compaction hooks")
        interface = codex_plugin.get("interface")
        required_interface = {
            "displayName", "shortDescription", "longDescription", "developerName", "category",
            "capabilities", "defaultPrompt",
        }
        if not isinstance(interface, dict) or not required_interface.issubset(interface):
            fail(errors, "Codex plugin is missing required interface metadata")
    expected_marketplace = {
        "name": "personal",
        "interface": {"displayName": "Personal"},
        "plugins": [{
            "name": "longtask",
            "source": {"source": "local", "path": "./plugins/longtask"},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            "category": "Developer Tools",
        }],
    }
    if marketplace != expected_marketplace:
        fail(errors, "personal marketplace template does not match the closed longtask install contract")
    if schema is not None:
        properties = schema.get("properties")
        schema_version = properties.get("schema_version", {}) if isinstance(properties, dict) else {}
        skill_version = properties.get("skill_version", {}) if isinstance(properties, dict) else {}
        if schema_version.get("const") != 3 or skill_version.get("const") != SKILL_VERSION:
            fail(errors, "state schema version constants do not match runtime v3")
        expected_required_state_keys = {
            "schema_version", "skill_version", "task_id", "mode", "approval_policy", "phase", "status",
            "revision", "event_revision", "evidence_epoch", "goal", "base_commit", "head_commit", "artifact_digest", "approved_digest",
            "approval", "review", "work_packages", "validation_evidence", "blockers", "resolved_blockers",
            "handoff", "next_action", "updated_at",
        }
        expected_state_keys = expected_required_state_keys
        if schema.get("additionalProperties") is not False:
            fail(errors, "state schema must reject unknown top-level properties")
        if set(schema.get("required", [])) != expected_required_state_keys:
            fail(errors, "state schema required keys do not match the runtime contract")
        if not isinstance(properties, dict) or set(properties) != expected_state_keys:
            fail(errors, "state schema properties do not match the runtime contract")
        definitions = schema.get("$defs", {})
        actor = definitions.get("actor", {}) if isinstance(definitions, dict) else {}
        if (
            actor.get("type") != "string" or actor.get("minLength") != 1
            or actor.get("maxLength") != 128
            or actor.get("pattern") != r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$"
        ):
            fail(errors, "state schema actor identity is not canonically bounded")
        evidence = definitions.get("evidence", {}) if isinstance(definitions, dict) else {}
        handoff = definitions.get("handoff", {}) if isinstance(definitions, dict) else {}
        expected_evidence = {
            "kind", "summary", "result", "command", "check_id", "details", "artifact_digest", "evidence_epoch", "actor", "recorded_at",
        }
        if set(evidence.get("required", [])) != expected_evidence:
            fail(errors, "state schema evidence requirements do not match the runtime contract")
        expected_handoff = {
            "intent", "objective", "reason", "target", "required_inputs", "acceptance_checks",
            "next_if_pass", "next_if_fail", "artifact_digest", "head_commit", "evidence_epoch",
            "created_revision", "created_by", "created_at",
        }
        if properties.get("handoff", {}).get("$ref") != "#/$defs/handoff":
            fail(errors, "state schema handoff property must use the shared handoff definition")
        if (
            handoff.get("additionalProperties") is not False
            or set(handoff.get("required", [])) != expected_handoff
            or set(handoff.get("properties", {})) != expected_handoff
        ):
            fail(errors, "state schema handoff envelope is not closed and property-complete")
        handoff_properties = handoff.get("properties", {})
        expected_intents = {"plan", "document", "execute", "review", "remediate", "verify", "decide", "complete"}
        if set(handoff_properties.get("intent", {}).get("enum", [])) != expected_intents:
            fail(errors, "state schema handoff intent enum does not match runtime")
        target = handoff_properties.get("target", {})
        if (
            target.get("additionalProperties") is not False
            or set(target.get("required", [])) != {"kind", "ref"}
            or set(target.get("properties", {})) != {"kind", "ref"}
        ):
            fail(errors, "state schema handoff target is not closed and required")
        for field in ("required_inputs", "acceptance_checks"):
            definition = handoff_properties.get(field, {})
            if definition.get("minItems") != 1 or definition.get("uniqueItems") is not True:
                fail(errors, f"state schema weakened handoff list constraint: {field}")
        if handoff_properties.get("artifact_digest", {}).get("pattern") != r"^sha256:[0-9a-f]{64}$":
            fail(errors, "state schema weakened handoff digest constraint")
        if handoff_properties.get("head_commit", {}).get("minLength") != 1:
            fail(errors, "state schema weakened handoff Git HEAD constraint")
        branch = definitions.get("handoff_branch", {}) if isinstance(definitions, dict) else {}
        if (
            branch.get("additionalProperties") is not False
            or set(branch.get("required", [])) != {"intent", "objective"}
            or set(branch.get("properties", {})) != {"intent", "objective"}
            or set(branch.get("properties", {}).get("intent", {}).get("enum", [])) != expected_intents
        ):
            fail(errors, "state schema handoff branch is not closed or intent-complete")
        work_package = properties.get("work_packages", {}).get("items", {}) if isinstance(properties, dict) else {}
        expected_package = {
            "id", "objective", "status", "dependencies", "affected_modules", "write_set", "acceptance_checks",
            "base_revision", "risk", "risk_level", "required_review_roles", "rollback", "stopping_condition",
            "owner", "lease_expires", "contributors",
        }
        if set(work_package.get("required", [])) != expected_package:
            fail(errors, "state schema work-package requirements do not match the runtime contract")
        critical = {
            "task_id": ("pattern", r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}$"),
            "revision": ("minimum", 0),
            "event_revision": ("minimum", -1),
            "evidence_epoch": ("minimum", 0),
        }
        for field, (constraint, expected_value) in critical.items():
            actual = properties.get(field, {}).get(constraint) if isinstance(properties, dict) else None
            if actual != expected_value:
                fail(errors, f"state schema weakened critical constraint: {field}.{constraint}")
        for field in ("approval", "review"):
            definition = properties.get(field, {}) if isinstance(properties, dict) else {}
            if definition.get("additionalProperties") is not False or not definition.get("required"):
                fail(errors, f"state schema {field} envelope is not closed and required")
        for collection, reference in (
            ("validation_evidence", "#/$defs/evidence"),
            ("blockers", "#/$defs/blocker"),
            ("resolved_blockers", "#/$defs/resolved_blocker"),
        ):
            actual = properties.get(collection, {}).get("items", {}).get("$ref") if isinstance(properties, dict) else None
            if actual != reference:
                fail(errors, f"state schema {collection} does not use {reference}")
        enum_contracts = {
            "mode": {"setup", "continue", "review", "modify", "retrofit"},
            "approval_policy": {"interactive", "guarded", "autonomous"},
            "phase": {"discovery", "architecture", "documentation", "execution", "review", "complete"},
            "status": {"planned", "active", "awaiting_approval", "blocked", "failed", "complete", "superseded"},
        }
        for field, expected_values in enum_contracts.items():
            actual_values = properties.get(field, {}).get("enum", []) if isinstance(properties, dict) else []
            if set(actual_values) != expected_values:
                fail(errors, f"state schema enum does not match runtime: {field}")
        for field in ("artifact_digest", "approved_digest"):
            if properties.get(field, {}).get("pattern") != r"^sha256:[0-9a-f]{64}$":
                fail(errors, f"state schema weakened digest constraint: {field}")
        if evidence.get("additionalProperties") is not False or set(evidence.get("properties", {})) != expected_evidence:
            fail(errors, "state schema evidence envelope is not closed or property-complete")
        if set(evidence.get("properties", {}).get("result", {}).get("enum", [])) != {"pass", "fail"}:
            fail(errors, "state schema evidence result enum does not match runtime")
        if evidence.get("properties", {}).get("artifact_digest", {}).get("pattern") != r"^sha256:[0-9a-f]{64}$":
            fail(errors, "state schema weakened evidence digest constraint")
        epoch_definition = evidence.get("properties", {}).get("evidence_epoch", {})
        if epoch_definition.get("type") != "integer" or epoch_definition.get("minimum") != 0:
            fail(errors, "state schema weakened evidence epoch constraint")
        review_conditions = evidence.get("allOf")
        expected_review_condition = {
            "if": {"properties": {"kind": {"const": "review"}}, "required": ["kind"]},
        }
        if (
            not isinstance(review_conditions, list)
            or not any(
                isinstance(condition, dict) and condition.get("if") == expected_review_condition["if"]
                for condition in review_conditions
            )
        ):
            fail(errors, "state schema review evidence lacks a reviewer-envelope condition")
        else:
            review_condition = next(
                condition for condition in review_conditions
                if isinstance(condition, dict) and condition.get("if") == expected_review_condition["if"]
            )
            details = review_condition.get("then", {}).get("properties", {}).get("details", {})
            expected_review_fields = {
                "reviewer", "role", "scope", "base_revision", "artifact_digest", "status", "findings",
                "evidence", "coverage_gaps", "assumptions", "task_id", "evidence_epoch", "head_commit",
            }
            expected_review_properties = expected_review_fields | {"covered_package_ids"}
            if (
                details.get("additionalProperties") is not False
                or set(details.get("required", [])) != expected_review_fields
                or set(details.get("properties", {})) != expected_review_properties
            ):
                fail(errors, "state schema reviewer envelope is not closed and property-complete")
            details_properties = details.get("properties", {})
            if details_properties.get("task_id", {}).get("pattern") != properties.get("task_id", {}).get("pattern"):
                fail(errors, "state schema reviewer task identity must match task_id constraint")
            if details_properties.get("evidence_epoch") != {"type": "integer", "minimum": 0}:
                fail(errors, "state schema reviewer evidence epoch must be non-negative")
            if details_properties.get("head_commit") != {"type": ["string", "null"], "minLength": 1}:
                fail(errors, "state schema reviewer HEAD binding must be non-empty or null")
            if details_properties.get("scope", {}).get("minItems") != 1:
                fail(errors, "state schema reviewer scope must be non-empty")
            covered = details_properties.get("covered_package_ids", {})
            if covered.get("type") != "array" or covered.get("uniqueItems") is not True:
                fail(errors, "state schema reviewer package coverage must be a unique array")
            if set(details_properties.get("status", {}).get("enum", [])) != {
                "approved", "changes_required", "unable_to_verify",
            }:
                fail(errors, "state schema reviewer status enum does not match runtime")
            if details_properties.get("base_revision", {}).get("pattern") != r"^sha256:[0-9a-f]{64}$":
                fail(errors, "state schema weakened reviewer base revision constraint")
            for field in ("evidence", "coverage_gaps", "assumptions"):
                definition = details_properties.get(field, {})
                if definition.get("type") != "array" or definition.get("items", {}).get("minLength") != 1:
                    fail(errors, f"state schema weakened reviewer list constraint: {field}")
            finding = details_properties.get("findings", {}).get("items", {})
            expected_finding_fields = {
                "id", "severity", "claim", "impact", "evidence", "affected_requirement",
                "recommended_correction", "reproducible",
            }
            if finding.get("additionalProperties") is not False or set(finding.get("required", [])) != expected_finding_fields:
                fail(errors, "state schema review finding is not closed and property-complete")
            finding_properties = finding.get("properties", {})
            if set(finding_properties.get("severity", {}).get("enum", [])) != {
                "blocker", "stale_documentation", "warning", "suggestion", "unable_to_verify",
            }:
                fail(errors, "state schema finding severity enum does not match runtime")
            evidence_list = finding_properties.get("evidence", {})
            if (
                evidence_list.get("type") != "array" or evidence_list.get("minItems") != 1
                or evidence_list.get("items", {}).get("minLength") != 1
            ):
                fail(errors, "state schema finding evidence list is weaker than runtime")
            expected_pass_condition = {
                "properties": {"kind": {"const": "review"}, "result": {"const": "pass"}},
                "required": ["kind", "result"],
            }
            expected_fail_condition = {
                "properties": {"kind": {"const": "review"}, "result": {"const": "fail"}},
                "required": ["kind", "result"],
            }
            pass_condition = next((
                condition for condition in review_conditions
                if isinstance(condition, dict) and condition.get("if") == expected_pass_condition
            ), None)
            if pass_condition is None:
                fail(errors, "state schema lacks passing-review semantic constraints")
            else:
                pass_details = pass_condition.get("then", {}).get("properties", {}).get("details", {}).get("properties", {})
                blocked_severities = (
                    pass_details.get("findings", {}).get("items", {}).get("not", {})
                    .get("properties", {}).get("severity", {}).get("enum", [])
                )
                if pass_details.get("status", {}).get("const") != "approved" or set(blocked_severities) != {
                    "blocker", "stale_documentation", "unable_to_verify",
                }:
                    fail(errors, "state schema weakened passing-review semantic constraints")
                if pass_details.get("coverage_gaps", {}).get("maxItems") != 0:
                    fail(errors, "state schema permits passing reviews with coverage gaps")
            fail_condition = next((
                condition for condition in review_conditions
                if isinstance(condition, dict) and condition.get("if") == expected_fail_condition
            ), None)
            if fail_condition is None:
                fail(errors, "state schema lacks failing-review semantic constraints")
            else:
                fail_details = fail_condition.get("then", {}).get("properties", {}).get("details", {}).get("properties", {})
                if (
                    fail_details.get("status", {}).get("not", {}).get("const") != "approved"
                    or fail_details.get("findings", {}).get("minItems") != 1
                ):
                    fail(errors, "state schema weakened failing-review semantic constraints")
        package_properties = work_package.get("properties", {})
        if work_package.get("additionalProperties") is not False:
            fail(errors, "state schema work-package contract must reject unknown properties")
        expected_package_properties = expected_package | {
            "evidence", "baseline_manifest", "stale_base", "lease_expired", "stale_evidence", "scope_drift",
            "dependency_failed", "execution_group", "settlement_manifest", "acceptance_evidence_start",
        }
        if set(package_properties) != expected_package_properties:
            fail(errors, "state schema work-package properties do not match the runtime contract")
        if set(package_properties.get("status", {}).get("enum", [])) != {
            "planned", "ready", "active", "blocked", "failed", "complete", "superseded",
        }:
            fail(errors, "state schema work-package status enum does not match runtime")
        if set(package_properties.get("risk_level", {}).get("enum", [])) != {
            "low", "medium", "high", "critical",
        }:
            fail(errors, "state schema work-package risk level enum does not match runtime")
        for field in ("dependencies", "write_set", "acceptance_checks"):
            definition = package_properties.get(field, {})
            if definition.get("type") != "array" or definition.get("uniqueItems") is not True:
                fail(errors, f"state schema weakened work-package list constraint: {field}")
        if package_properties.get("acceptance_checks", {}).get("minItems") != 1:
            fail(errors, "state schema permits a work package with no acceptance checks")
        contributors = package_properties.get("contributors", {})
        if (
            contributors.get("type") != "array" or contributors.get("minItems") != 1
            or contributors.get("uniqueItems") is not True
            or contributors.get("items", {}).get("$ref") != "#/$defs/actor"
        ):
            fail(errors, "state schema weakened work-package contributors constraint")
        manifest_ref = {"$ref": "#/$defs/workspaceManifest"}
        if any(package_properties.get(field) != manifest_ref for field in ("baseline_manifest", "settlement_manifest")):
            fail(errors, "state schema execution snapshots must share the workspace manifest contract")
        if package_properties.get("execution_group") != {"type": "string", "pattern": "^[0-9a-f]{32}$"}:
            fail(errors, "state schema execution group identity is malformed")
        if package_properties.get("acceptance_evidence_start") != {"type": "integer", "minimum": 0}:
            fail(errors, "state schema acceptance evidence boundary must be a nonnegative integer")
        baseline_manifest = definitions.get("workspaceManifest", {})
        baseline_properties = baseline_manifest.get("properties", {})
        if (
            baseline_manifest.get("type") != "object"
            or baseline_manifest.get("additionalProperties") is not False
            or set(baseline_manifest.get("required", [])) != {"artifact_digest", "files"}
            or set(baseline_properties) != {"artifact_digest", "files"}
            or baseline_properties.get("artifact_digest", {}).get("pattern") != r"^sha256:[0-9a-f]{64}$"
            or baseline_properties.get("files", {}).get("type") != "object"
            or baseline_properties.get("files", {}).get("additionalProperties", {}).get("pattern") != r"^[0-9a-f]{64}$"
            or not baseline_properties.get("files", {}).get("propertyNames", {}).get("pattern")
        ):
            fail(errors, "state schema baseline manifest is not closed or cryptographically constrained")
        for flag in ("stale_base", "lease_expired", "stale_evidence", "scope_drift", "dependency_failed"):
            if package_properties.get(flag, {}).get("type") != "boolean":
                fail(errors, f"state schema weakened work-package runtime flag: {flag}")
        package_conditions = work_package.get("allOf", [])
        settlement_condition = {
            "if": {"required": ["settlement_manifest"]},
            "then": {"required": ["execution_group", "baseline_manifest"]},
        }
        if settlement_condition not in package_conditions:
            fail(errors, "state schema settlement requires its execution group and baseline")
        required_condition = {
            "if": {"properties": {"status": {"const": "active"}}, "required": ["status"]},
            "then": {"required": ["baseline_manifest"]},
        }
        if required_condition not in package_conditions:
            fail(errors, "state schema does not require a baseline manifest for active packages")
        for flag in ("stale_base", "lease_expired", "scope_drift", "dependency_failed"):
            blocked_condition = {
                "if": {"properties": {flag: {"const": True}}, "required": [flag]},
                "then": {"properties": {"status": {"const": "blocked"}}},
            }
            if blocked_condition not in package_conditions:
                fail(errors, f"state schema does not bind {flag} to blocked status")
        for name in ("blocker", "resolved_blocker"):
            definition = definitions.get(name, {}) if isinstance(definitions, dict) else {}
            expected_fields = {
                "id", "summary", "actor", "created_at", "opened_digest", "opened_revision",
            }
            if name == "resolved_blocker":
                expected_fields |= {"resolved_by", "resolved_at", "resolution_digest", "verification_actor"}
            if definition.get("additionalProperties") is not False or set(definition.get("required", [])) != expected_fields:
                fail(errors, f"state schema {name} envelope is not closed and required")
            if definition.get("properties", {}).get("opened_digest", {}).get("pattern") != r"^sha256:[0-9a-f]{64}$":
                fail(errors, f"state schema weakened blocker digest constraint: {name}")


def check_repository_content(errors: list[str]) -> None:
    """Ignore rules alone cannot stop an already tracked or force-added artifact."""
    try:
        top = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=10, check=False)
        if top.returncode or Path(top.stdout.strip()).resolve() != ROOT.resolve():
            return  # Extracted artifacts and source snapshots may have no Git repository.
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--cached", "-z"],
                                 capture_output=True, text=True, timeout=10, check=False)
        if tracked.returncode:
            fail(errors, "cannot inspect tracked repository content")
            return
        for path in tracked.stdout.split("\0"):
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in GENERATED_PATH_PATTERNS):
                fail(errors, f"generated artifact is tracked by Git: {path}; remove it from the index")
    except FileNotFoundError:
        return  # Git is not needed for installed artifact self-checks.
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(errors, f"cannot inspect repository content: {exc}")


def check_corpus_contracts(errors: list[str]) -> None:
    for script, arguments in (("run_skill_evals.py", []), ("run_forward_evals.py", ["--check-cases"]),
                              ("run_setup_evals.py", []), ("run_progress_evals.py", ["--check-cases"])):
        try:
            process = subprocess.run([sys.executable, str(ROOT / "scripts" / script), *arguments],
                                     cwd=ROOT, capture_output=True, text=True, check=False,
                                     timeout=SUBPROCESS_TIMEOUT_SECONDS)
            if process.returncode:
                fail(errors, f"{script} corpus check failed: " + (process.stderr or process.stdout).strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            fail(errors, f"{script} corpus check unavailable: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--with-evaluation-results", action="store_true",
        help="also verify local host, invocation, forward and memory evidence; missing or stale results fail",
    )
    mode.add_argument(
        "--installed", action="store_true",
        help="self-check an extracted artifact without asserting the source-only host release gate",
    )
    mode.add_argument(
        "--require-release-pass", action="store_true",
        help="fail unless the current archive Codex matrix and current-version recovery gate are passing",
    )
    args = parser.parse_args(argv)
    errors: list[str] = []

    verify_results = args.with_evaluation_results or args.require_release_pass
    required_files = REQUIRED_FILES + (SOURCE_EVALUATION_RESULTS if verify_results else ())
    for relative in required_files:
        if not (ROOT / relative).is_file():
            fail(errors, f"missing required file: {relative}")

    check_skill(ROOT / "SKILL.md", "longtask", errors)
    for name in SKILL_NAMES:
        check_skill(ROOT / "skills" / name / "SKILL.md", name, errors)
        check_skill_interface(ROOT / "skills" / name / "agents" / "openai.yaml", name, errors)
    check_discovery_surface(errors)
    check_disclosure_graph(errors)
    check_portable_markdown_paths(ROOT, errors)
    for legacy in ("setup", "continue", "review", "modify", "retrofit"):
        if (ROOT / "skills" / legacy).exists():
            fail(errors, f"legacy subskill directory remains: skills/{legacy}")
    for removed_host_path in (".claude", ".claude-plugin"):
        if (ROOT / removed_host_path).exists():
            fail(errors, f"removed Claude support path remains: {removed_host_path}")

    if (ROOT / "docs/tasks").exists():
        fail(errors, "per-task records remain in docs/tasks; merge project knowledge and remove execution records")

    root_lines = len(read_text(ROOT / "SKILL.md").splitlines()) if (ROOT / "SKILL.md").is_file() else 0
    if root_lines > 500:
        fail(errors, f"root SKILL.md exceeds progressive-disclosure limit: {root_lines} lines")

    for path in ROOT.rglob("*"):
        if {".git", ".longtask", "dist"}.intersection(path.relative_to(ROOT).parts) or not path.is_file():
            continue
        if any(fnmatch.fnmatchcase(path.relative_to(ROOT).as_posix(), pattern) for pattern in GENERATED_PATH_PATTERNS):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        text = read_text(path)
        if path.suffix.lower() == ".md":
            text = strip_fenced_code(text)
        for phrase, reason in FORBIDDEN_PHRASES.items():
            if phrase in text:
                fail(errors, f"{reason} in {path.relative_to(ROOT)}: {phrase}")

    modules = ROOT / "docs" / "modules"
    if modules.is_dir():
        for module_dir in modules.iterdir():
            if module_dir.is_dir() and not (module_dir / "README.md").is_file():
                fail(errors, f"expanded module lacks README.md: {module_dir.relative_to(ROOT)}")

    for script in (ROOT / "scripts").glob("*.py"):
        try:
            ast.parse(read_text(script), filename=str(script))
        except SyntaxError as exc:
            fail(errors, f"invalid Python syntax in {script.relative_to(ROOT)}: {exc}")

    check_metadata(errors)
    check_compaction_hooks(errors)
    if not args.installed:
        check_repository_content(errors)
    check_corpus_contracts(errors)
    binding = check_release_archive(errors, installed=args.installed)
    if verify_results:
        check_host_results(errors, binding)
    if args.require_release_pass:
        if not (ROOT / "dist" / f"longtask-{SKILL_VERSION}.zip").is_file():
            fail(errors, "release readiness requires a generated candidate in dist; build it first")
        try:
            host_results = json.loads(read_text(ROOT / "evals" / "host_results.json"))
        except (OSError, json.JSONDecodeError, AttributeError):
            host_results = None
        if not release_gate_pass(host_results):
            release_status = (
                host_results.get("release_gate", {}).get("status")
                if isinstance(host_results, dict) and isinstance(host_results.get("release_gate"), dict)
                else None
            )
            fail(errors, f"release readiness gate is not pass: {release_status!r}")
            for reason in release_blockers(host_results):
                fail(errors, reason)
    if verify_results:
        check_invocation_results(errors)
        check_forward_results(errors)
        check_memory_results(errors)
    check_markdown_links(ROOT, errors)

    if errors:
        for error in sorted(set(errors)):
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("longtask installed artifact self-check passed; host release gate not evaluated"
          if args.installed else "longtask release readiness validation passed"
          if args.require_release_pass else "longtask static validation and local evaluation bindings passed; release pass not asserted"
          if verify_results else "longtask static validation passed; evaluation results and host release gate not evaluated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
