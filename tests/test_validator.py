from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "validate_longtask.py"
SPEC = importlib.util.spec_from_file_location("validate_longtask", SCRIPT)
assert SPEC and SPEC.loader
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)
FORWARD_SPEC = importlib.util.spec_from_file_location("run_forward_evals", PROJECT / "scripts" / "run_forward_evals.py")
assert FORWARD_SPEC and FORWARD_SPEC.loader
FORWARD = importlib.util.module_from_spec(FORWARD_SPEC)
FORWARD_SPEC.loader.exec_module(FORWARD)


RELEASE_SPEC = importlib.util.spec_from_file_location("validator_release_builder", PROJECT / "scripts/build_release.py")
assert RELEASE_SPEC and RELEASE_SPEC.loader
RELEASE = importlib.util.module_from_spec(RELEASE_SPEC)
RELEASE_SPEC.loader.exec_module(RELEASE)


def candidate_binding() -> tuple[str, dict]:
    data, manifest = RELEASE.archive_bytes(PROJECT)
    return hashlib.sha256(data).hexdigest(), manifest


class ValidatorTests(unittest.TestCase):
    def host_fixture(self) -> dict:
        """Synthetic schema fixture; never a record of real host execution."""
        data = json.loads((PROJECT / "tests/fixtures/synthetic_host_results_v7.json").read_text())
        archive_digest, manifest = candidate_binding()
        data["schema_version"] = VALIDATOR.HOST_RESULTS_SCHEMA_VERSION
        data["release"].update(archive_sha256=archive_digest, source_tree_sha256=manifest["source_tree_sha256"],
                               file_count=len(manifest["source_entries"]))
        data["host"].update(status="pass", version="codex-cli 1.2.3")
        data["host"]["installation"].update(manifest_preflight="pass", validated_archive_sha256=archive_digest,
                                           workflow_trace_archive_sha256=archive_digest)
        for assertion in data["host"]["assertions"]:
            samples = []
            for repeat in range(2):
                identity = f"synthetic-{assertion['id']}-{repeat}"
                samples.append({"status": "pass", "run_id": identity, "session_id": identity, "trace_id": identity,
                    "trace_sha256": hashlib.sha256(identity.encode()).hexdigest(), "exit_status": 0, "failure_class": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1}, "latency_ms": 1, "retries": 0,
                    "human_interventions": 0, "tool_call_count": 1, "evaluated_archive_sha256": archive_digest,
                    "agent_id": "synthetic-agent", "model": "synthetic-model", "tool_ids": ["synthetic-tool"],
                    "guardrail_ids": ["synthetic-guard"], "handoff_id": identity,
                    "capabilities": {kind: {"status": "observed", "reason": "Synthetic capability observation.",
                                             "evidence": [f"synthetic://{identity}/{kind}"]}
                                     for kind in ("guardrail", "handoff")}})
            assertion.update(samples[0], replicate_runs=samples[1:])
        for field in VALIDATOR.RECOVERY_CHECK_FIELDS:
            data["recovery"][field] = "pass"
        data["recovery"].update(status="pass", run_id="synthetic-recovery", trace_id="synthetic-recovery",
                                trace_sha256="b" * 64, archive_sha256=archive_digest)
        data["release_gate"]["status"] = "pass"
        data["benchmark_gate"]["status"] = "pass"
        data["analysis"]["grader_calibration"].update(calibrated=True, human_reviewer="synthetic-reviewer",
                                                     calibration_case_ids=["setup"])
        for comparison in data["analysis"]["comparisons"]:
            comparison.update(status="pass", sample_count=len(VALIDATOR.REQUIRED_HOST_CASES), success_rate=1, trace_set_sha256="c" * 64,
                              baseline_archive_sha256="d" * 64 if comparison["kind"] == "previous_candidate" else None)
        for metric in ("invocation_precision", "invocation_recall", "entry_routing_accuracy",
                       "safe_recovery_completion_accuracy", "stale_conflict_detection_rate"):
            data["analysis"]["metrics"][metric] = 1
        data["analysis"]["metrics"].update(duplicate_rework_rate=0, document_drift_rate=0)
        self.refresh_host_aggregates(data)
        return data

    def test_host_gate_aggregates_all_sample_permutations(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        # Threshold success, observed failure, and insufficient/unknown evidence
        # each keep their outcome when the first sample changes.
        scenarios = (
            (("pass", "pass", "fail"), "pass"),
            (("pass", "pass", "unable_to_verify"), "pass"),
            (("pass", "fail", "fail"), "failed"),
            (("pass", "pass", "fail", "fail"), "failed"),
            (("pass", "unable_to_verify"), "unable_to_verify"),
            (("pass", "pass", "unable_to_verify", "unable_to_verify"), "unable_to_verify"),
            (("pass",), "unable_to_verify"),
        )
        for statuses, expected in scenarios:
            samples = []
            template = baseline["host"]["assertions"][0]
            for index, status in enumerate(statuses):
                sample = {field: copy.deepcopy(template[field]) for field in VALIDATOR.HOST_SAMPLE_FIELDS}
                identity = f"synthetic-permutation-{index}"
                sample.update(status=status, run_id=identity, session_id=identity, trace_id=identity,
                              trace_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                              failure_class=None if status == "pass" else "synthetic_failure",
                              exit_status=0 if status == "pass" else 1)
                samples.append(sample)
            for permutation in itertools.permutations(samples):
                with self.subTest(statuses=statuses, first=permutation[0]["status"]):
                    data = copy.deepcopy(baseline)
                    assertion = data["host"]["assertions"][0]
                    assertion.update(permutation[0], replicate_runs=list(permutation[1:]))
                    data["host"]["status"] = expected
                    data["release_gate"]["status"] = "pass" if expected == "pass" else "blocked"
                    data["benchmark_gate"]["status"] = data["release_gate"]["status"]
                    self.refresh_host_aggregates(data)
                    snapshot = copy.deepcopy(data)
                    self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
                    self.assertEqual(data, snapshot, "validation must retain failed samples")
                    data["release_gate"]["status"] = "blocked" if expected == "pass" else "pass"
                    self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_nonpassing_samples_cannot_hide_invalid_bindings_or_duplicate_identity(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for status in ("fail", "unable_to_verify"):
            for primary in (False, True):
                for field, value in (("evaluated_archive_sha256", "e" * 64), ("usage", {}),
                                     ("trace_sha256", "invalid"), ("latency_ms", -1),
                                     ("run_id", "duplicate"), ("session_id", "duplicate"),
                                     ("trace_id", "duplicate"), ("trace_sha256", "duplicate")):
                    with self.subTest(status=status, primary=primary, field=field):
                        data = copy.deepcopy(baseline)
                        assertion = data["host"]["assertions"][0]
                        failed = {key: copy.deepcopy(assertion[key]) for key in VALIDATOR.HOST_SAMPLE_FIELDS}
                        identity = "synthetic-failed-sample"
                        failed.update(status=status, failure_class="synthetic_failure", exit_status=1,
                                      run_id=identity, session_id=identity, trace_id=identity,
                                      trace_sha256=hashlib.sha256(identity.encode()).hexdigest())
                        if value == "duplicate":
                            failed[field] = assertion[field]
                        else:
                            failed[field] = value
                        if primary:
                            original = {key: assertion[key] for key in VALIDATOR.HOST_SAMPLE_FIELDS}
                            assertion.update(failed, replicate_runs=[original, *assertion["replicate_runs"]])
                        else:
                            assertion["replicate_runs"].append(failed)
                        # Do not recalculate invalid numeric telemetry; rejection
                        # must include the per-sample validation diagnostic.
                        errors = VALIDATOR.host_results_errors(data, *arguments)
                        self.assertTrue(any("sample" in error for error in errors), errors)

    def test_execution_snapshot_schema_rejects_weakened_binding(self) -> None:
        original_loader = VALIDATOR.load_json
        schema_path = PROJECT / "references/state.schema.json"
        original = json.loads(schema_path.read_text())
        mutations = (
            lambda schema: schema["$defs"]["workspaceManifest"].update(additionalProperties=True),
            lambda schema: schema["properties"]["work_packages"]["items"]["properties"].update(
                settlement_manifest={"type": "object"}),
            lambda schema: schema["properties"]["work_packages"]["items"]["properties"].update(
                execution_group={"type": "string"}),
            lambda schema: schema["properties"]["work_packages"]["items"]["allOf"].pop(0),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                schema = copy.deepcopy(original)
                mutate(schema)
                def load(path, errors, schema=schema):
                    return schema if path == schema_path else original_loader(path, errors)
                errors = []
                with mock.patch.object(VALIDATOR, "load_json", side_effect=load):
                    VALIDATOR.check_metadata(errors)
                self.assertTrue(any("state schema" in error for error in errors), errors)

    def test_repeated_host_samples_require_distinct_execution_and_current_bindings(self) -> None:
        data = self.host_fixture()
        arguments = (data["release"]["archive_sha256"], data["release"]["source_tree_sha256"], data["release"]["file_count"])
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        for field in ("run_id", "session_id", "trace_id", "trace_sha256", "evaluated_archive_sha256",
                      "guardrail_ids", "handoff_id"):
            with self.subTest(field=field):
                forged = copy.deepcopy(data)
                first = forged["host"]["assertions"][0]
                sample = first["replicate_runs"][0]
                if field in {"run_id", "session_id", "trace_id", "trace_sha256"}:
                    sample[field] = first[field]
                elif field == "evaluated_archive_sha256":
                    sample[field] = "f" * 64
                else:
                    sample[field] = None
                self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))

    @staticmethod
    def refresh_host_aggregates(data: dict) -> None:
        assertions = {item["id"]: item for item in data["host"]["assertions"]}
        all_samples = []
        for statistic in data["analysis"]["sampling"]["case_statistics"]:
            assertion = assertions[statistic["id"]]
            samples = [assertion, *assertion["replicate_runs"]]
            all_samples.extend(samples)
            passed = sum(sample["status"] == "pass" for sample in samples)
            total = len(samples)
            rate = passed / total
            statistic.update({
                "repetitions": total,
                "pass_count": passed,
                "fail_count": sum(sample["status"] == "fail" for sample in samples),
                "unable_to_verify_count": sum(sample["status"] == "unable_to_verify" for sample in samples),
                "success_rate": rate,
                "bernoulli_variance": rate * (1 - rate),
                "confidence_interval_95": list(VALIDATOR.wilson_interval(passed, total)),
            })
        def complete(field: str) -> bool:
            return bool(all_samples) and all(sample[field] is not None for sample in all_samples)

        latencies = sorted(float(sample["latency_ms"]) for sample in all_samples
                           if sample["latency_ms"] is not None)
        data["analysis"]["metrics"].update({
            "token_usage_total": sum(sample["usage"]["input_tokens"] + sample["usage"]["output_tokens"]
                                     for sample in all_samples) if complete("usage") else None,
            "tool_calls_total": sum(sample["tool_call_count"] for sample in all_samples)
                                if complete("tool_call_count") else None,
            "latency_ms_mean": sum(latencies) / len(latencies) if complete("latency_ms") else None,
            "latency_ms_p95": latencies[max(0, -(-95 * len(latencies) // 100) - 1)]
                              if complete("latency_ms") else None,
            "retries_total": sum(sample["retries"] for sample in all_samples) if complete("retries") else None,
            "human_intervention_rate": sum(sample["human_interventions"] > 0 for sample in all_samples)
                                       / len(all_samples) if complete("human_interventions") else None,
        })

    def test_personal_core_accepts_unknown_optional_telemetry_and_identity(self) -> None:
        data = self.host_fixture()
        arguments = tuple(data["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for assertion in data["host"]["assertions"]:
            for sample in [assertion, *assertion["replicate_runs"]]:
                sample.update(agent_id=None, model=None, tool_ids=None, usage=None, latency_ms=None, retries=None,
                              human_interventions=None, tool_call_count=None)
        data["benchmark_gate"]["status"] = "blocked"
        self.refresh_host_aggregates(data)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        self.assertTrue(VALIDATOR.release_gate_pass(data))
        data["benchmark_gate"]["status"] = "pass"
        self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_missing_comparisons_block_only_benchmark_and_missing_grader_blocks_core(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for missing in ("without_skill", "previous_candidate", "grader"):
            with self.subTest(missing=missing):
                data = copy.deepcopy(baseline)
                data["benchmark_gate"]["status"] = "blocked"
                if missing == "grader":
                    data["analysis"]["grader_calibration"].update(
                        calibrated=False, human_reviewer=None, calibration_case_ids=[])
                    data["release_gate"]["status"] = "blocked"
                else:
                    comparison = next(item for item in data["analysis"]["comparisons"] if item["kind"] == missing)
                    comparison.update(status="unable_to_verify", sample_count=0, success_rate=None,
                                      trace_set_sha256=None, baseline_archive_sha256=None)
                self.assertEqual(data["host"]["status"], "pass")
                self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
                self.assertEqual(VALIDATOR.release_gate_pass(data), missing != "grader")
                data["benchmark_gate"]["status"] = "pass"
                self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))
                if missing == "grader":
                    data["benchmark_gate"]["status"] = "blocked"
                    data["release_gate"]["status"] = "pass"
                    self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_unknown_measurement_never_becomes_partial_or_zero_aggregate(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        fields = {"usage": ("token_usage_total",), "tool_call_count": ("tool_calls_total",),
                  "latency_ms": ("latency_ms_mean", "latency_ms_p95"), "retries": ("retries_total",),
                  "human_interventions": ("human_intervention_rate",)}
        for field, aggregates in fields.items():
            for primary in (False, True):
                with self.subTest(field=field, primary=primary):
                    data = copy.deepcopy(baseline)
                    sample = data["host"]["assertions"][0]
                    if not primary:
                        sample = sample["replicate_runs"][0]
                    sample[field] = None
                    data["benchmark_gate"]["status"] = "blocked"
                    self.refresh_host_aggregates(data)
                    self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
                    for metric in aggregates:
                        self.assertIsNone(data["analysis"]["metrics"][metric])
                        for forged_value in (0, baseline["analysis"]["metrics"][metric]):
                            forged = copy.deepcopy(data)
                            forged["analysis"]["metrics"][metric] = forged_value
                            self.assertTrue(any("aggregate metrics" in error for error in
                                                VALIDATOR.host_results_errors(forged, *arguments)))
                    data["benchmark_gate"]["status"] = "pass"
                    self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_failed_sample_measurement_gaps_remain_in_population_aggregates(self) -> None:
        data = self.host_fixture()
        arguments = tuple(data["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        assertion = data["host"]["assertions"][0]
        sample = {field: copy.deepcopy(assertion[field]) for field in VALIDATOR.HOST_SAMPLE_FIELDS}
        identity = "synthetic-unmeasured-failure"
        sample.update(status="fail", failure_class="synthetic_failure", exit_status=1, run_id=identity,
                      session_id=identity, trace_id=identity, trace_sha256=hashlib.sha256(identity.encode()).hexdigest(),
                      usage=None, latency_ms=None, tool_call_count=None, retries=None, human_interventions=None)
        assertion["replicate_runs"].append(sample)
        data["benchmark_gate"]["status"] = "blocked"
        self.refresh_host_aggregates(data)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        self.assertTrue(VALIDATOR.release_gate_pass(data))
        for metric in ("token_usage_total", "tool_calls_total", "latency_ms_mean", "latency_ms_p95",
                       "retries_total", "human_intervention_rate"):
            self.assertIsNone(data["analysis"]["metrics"][metric])
        self.assertEqual(assertion["replicate_runs"][-1]["status"], "fail")

    def test_complete_zero_measurements_are_distinct_from_unknown(self) -> None:
        data = self.host_fixture()
        arguments = tuple(data["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for assertion in data["host"]["assertions"]:
            for sample in [assertion, *assertion["replicate_runs"]]:
                sample.update(usage={"input_tokens": 0, "output_tokens": 0}, tool_ids=[], tool_call_count=0,
                              latency_ms=0, retries=0, human_interventions=0)
        self.refresh_host_aggregates(data)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        for metric in ("token_usage_total", "tool_calls_total", "latency_ms_mean", "latency_ms_p95",
                       "retries_total", "human_intervention_rate"):
            self.assertEqual(data["analysis"]["metrics"][metric], 0)
            forged = copy.deepcopy(data)
            forged["analysis"]["metrics"][metric] = None
            forged["benchmark_gate"]["status"] = "blocked"
            self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))

    def test_capability_applicability_has_explicit_evidence_and_correlations(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for kind, identifier, empty in (("guardrail", "guardrail_ids", []), ("handoff", "handoff_id", None)):
            data = copy.deepcopy(baseline)
            sample = data["host"]["assertions"][0]
            sample[identifier] = empty
            sample["capabilities"][kind] = {
                "status": "not_applicable", "reason": "The cooperative host trace has no such event.",
                "evidence": [f"synthetic://applicability/{kind}"],
            }
            self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
            for field, value in (("reason", ""), ("evidence", []), ("evidence", [""]),
                                 ("status", "observed"), ("status", "unavailable"), ("status", {})):
                with self.subTest(kind=kind, field=field, value=value):
                    forged = copy.deepcopy(data)
                    forged["host"]["assertions"][0]["capabilities"][kind][field] = value
                    self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))
            forged = copy.deepcopy(data)
            forged["host"]["assertions"][0][identifier] = baseline["host"]["assertions"][0][identifier]
            self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))
        for kind in ("guardrail", "handoff"):
            forged = copy.deepcopy(baseline)
            forged["host"]["assertions"][0]["capabilities"][kind]["evidence"] = []
            self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))

    def test_unknown_capabilities_are_valid_unverified_evidence_but_cannot_pass(self) -> None:
        data = self.host_fixture()
        arguments = tuple(data["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        sample = data["host"]["assertions"][0]
        sample.update(status="unable_to_verify", failure_class="capability_unavailable", exit_status=None,
                      guardrail_ids=None, handoff_id=None)
        sample["capabilities"] = {kind: {"status": "unavailable", "reason": "Host did not expose events.", "evidence": []}
                                  for kind in ("guardrail", "handoff")}
        data["host"]["status"] = "unable_to_verify"
        data["release_gate"]["status"] = data["benchmark_gate"]["status"] = "blocked"
        self.refresh_host_aggregates(data)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        sample.update(status="pass", failure_class=None, exit_status=0)
        data["host"]["status"] = "pass"
        data["release_gate"]["status"] = data["benchmark_gate"]["status"] = "pass"
        self.refresh_host_aggregates(data)
        self.assertTrue(any("sample" in error for error in VALIDATOR.host_results_errors(data, *arguments)))

    def test_tool_counts_cannot_undercount_unique_calls_or_duplicate_ids(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for ids, count in ((["one"], 0), (["one", "two"], 1), (["one", "one"], 2)):
            for primary in (False, True):
                with self.subTest(ids=ids, count=count, primary=primary):
                    data = copy.deepcopy(baseline)
                    sample = data["host"]["assertions"][0]
                    if not primary:
                        sample = sample["replicate_runs"][0]
                    sample.update(tool_ids=ids, tool_call_count=count)
                    self.refresh_host_aggregates(data)
                    self.assertTrue(any("sample" in error for error in VALIDATOR.host_results_errors(data, *arguments)))

    def test_malformed_json_enum_values_return_errors_without_crashing(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        paths = (
            ("recovery", "clean_install"), ("host", "status"),
            ("host", "installation", "manifest_preflight"),
            ("analysis", "sampling", "case_statistics", 0, "id"),
            ("analysis", "metrics", "cost_status"),
            ("analysis", "comparisons", 0, "kind"),
            ("analysis", "comparisons", 0, "status"),
            ("release_gate", "status"), ("benchmark_gate", "status"),
        )
        for path in paths:
            for value in ([], {}):
                with self.subTest(path=path, value=value):
                    data = copy.deepcopy(baseline)
                    target = data
                    for component in path[:-1]:
                        target = target[component]
                    target[path[-1]] = value
                    errors = VALIDATOR.host_results_errors(data, *arguments)
                    self.assertIsInstance(errors, list)
                    self.assertTrue(errors)
                    self.assertTrue(all(isinstance(error, str) and error for error in errors))

    def test_oversized_json_numbers_return_structured_validation_errors(self) -> None:
        oversized = 10 ** 400
        self.assertFalse(VALIDATOR.nonnegative_number(oversized))
        self.assertFalse(VALIDATOR.close_number(oversized, 1))
        self.assertFalse(VALIDATOR.close_number(1, oversized))
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for path in (("host", "assertions", 0, "latency_ms"),
                     ("analysis", "metrics", "invocation_precision")):
            with self.subTest(path=path):
                data = copy.deepcopy(baseline)
                target = data
                for component in path[:-1]:
                    target = target[component]
                target[path[-1]] = oversized
                # Exercise values exactly as loaded from a syntactically valid JSON report.
                errors = VALIDATOR.host_results_errors(json.loads(json.dumps(data)), *arguments)
                self.assertIsInstance(errors, list)
                self.assertTrue(errors)
                self.assertTrue(all(isinstance(error, str) and error for error in errors))

    def test_compaction_hook_configuration_rejects_missing_scope_async_and_unbounded_callbacks(self) -> None:
        valid = json.loads((PROJECT / 'hooks/hooks.json').read_text())
        variants = [valid]
        for field, value in (('async', True), ('timeout', 300), ('command', 'python3 /unbound/bridge.py'),
                             ('additionalContextLimit', 100000)):
            data = copy.deepcopy(valid)
            data['hooks']['SessionStart'][0]['hooks'][0][field] = value
            variants.append(data)
        data = copy.deepcopy(valid)
        data['hooks']['SessionStart'][0]['matcher'] = '.*'
        variants.append(data)
        variants.append({'hooks': {'SessionStart': valid['hooks']['SessionStart']}})
        for index, data in enumerate(variants):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'hooks').mkdir()
                (root / 'hooks/hooks.json').write_text(json.dumps(data))
                errors = []
                with mock.patch.object(VALIDATOR, 'ROOT', root):
                    VALIDATOR.check_compaction_hooks(errors)
                self.assertEqual(bool(errors), index != 0, errors)

    def test_documentation_names_are_validated_as_links_not_historical_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "evals").mkdir()
            (root / "evals/host_results.json").write_text("{}")
            (root / "overview.md").write_text("# Overview\n")
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(VALIDATOR, "ROOT", root))
                stack.enter_context(mock.patch.object(VALIDATOR, "REQUIRED_FILES", ()))
                stack.enter_context(mock.patch.object(VALIDATOR, "SKILL_NAMES", ()))
                for name in ("check_skill", "check_skill_interface", "check_discovery_surface", "check_disclosure_graph", "check_metadata", "check_compaction_hooks",
                             "check_release_archive", "check_host_results", "check_corpus_contracts",
                             "check_invocation_results", "check_forward_results", "check_memory_results"):
                    stack.enter_context(mock.patch.object(VALIDATOR, name))
                for target, expected in (("overview.md", 0), ("doc-architecture.md", 1)):
                    with self.subTest(target=target):
                        (root / "README.md").write_text(
                            f"Former name: doc-architecture.md. [Guide]({target})\n")
                        errors = io.StringIO()
                        with redirect_stderr(errors), redirect_stdout(io.StringIO()):
                            self.assertEqual(VALIDATOR.main([]), expected)
                        if expected:
                            self.assertIn("dead local link", errors.getvalue())
                        else:
                            self.assertEqual(errors.getvalue(), "")

    def test_discovery_rejects_extra_skills_and_orphan_interfaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill = root / "skills/longtask"
            (skill / "agents").mkdir(parents=True)
            (skill / "SKILL.md").write_text("# Entry\n")
            (skill / "agents/openai.yaml").write_text("interface:\n")
            with mock.patch.object(VALIDATOR, "ROOT", root):
                errors = []
                VALIDATOR.check_discovery_surface(errors)
                self.assertEqual(errors, [])
                for relative in ("longtask-review/SKILL.md", "longtask/assets/extra/SKILL.md",
                                 "orphan/agents/openai.yaml"):
                    with self.subTest(relative=relative):
                        extra = root / "skills" / relative
                        extra.parent.mkdir(parents=True, exist_ok=True)
                        extra.write_text("unexpected discovered resource\n")
                        errors = []
                        VALIDATOR.check_discovery_surface(errors)
                        self.assertTrue(errors)
                        extra.unlink()

    def test_release_cli_rejects_malformed_gate_with_errors_instead_of_traceback(self) -> None:
        baseline = self.host_fixture()
        binding = {key: baseline["release"][key]
                   for key in ("archive_sha256", "source_tree_sha256", "file_count")}
        for gate in ([], None):
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "evals").mkdir()
                (root / "dist").mkdir()
                (root / "dist" / f"longtask-{VALIDATOR.SKILL_VERSION}.zip").touch()
                data = copy.deepcopy(baseline)
                data["release_gate"] = gate
                (root / "evals/host_results.json").write_text(json.dumps(data))
                error = io.StringIO()
                with ExitStack() as stack:
                    stack.enter_context(mock.patch.object(VALIDATOR, "ROOT", root))
                    stack.enter_context(mock.patch.object(VALIDATOR, "REQUIRED_FILES", ()))
                    stack.enter_context(mock.patch.object(VALIDATOR, "SKILL_NAMES", ()))
                    stack.enter_context(mock.patch.object(VALIDATOR, "check_release_archive", return_value=binding))
                    for name in ("check_skill", "check_skill_interface", "check_discovery_surface", "check_disclosure_graph", "check_portable_markdown_paths",
                                 "check_metadata", "check_compaction_hooks", "check_corpus_contracts", "check_invocation_results", "check_forward_results", "check_memory_results",
                                 "check_markdown_links"):
                        stack.enter_context(mock.patch.object(VALIDATOR, name))
                    stack.enter_context(redirect_stderr(error))
                    self.assertEqual(VALIDATOR.main(["--require-release-pass"]), 1)
                self.assertIn("ERROR:", error.getvalue())
                self.assertIn("release readiness gate is not pass", error.getvalue())
                self.assertNotIn("Traceback", error.getvalue())

    def test_optional_values_are_nullable_but_supplied_values_remain_strict(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        invalid = [("agent_id", [""]), ("model", {}), ("model", []),
                   ("latency_ms", True), ("latency_ms", "1"), ("retries", False), ("retries", -1),
                   ("human_interventions", 1.5), ("tool_call_count", True)]
        for field, value in invalid:
            with self.subTest(field=field):
                data = copy.deepcopy(baseline)
                data["host"]["assertions"][0][field] = value
                self.assertTrue(any("sample" in error for error in VALIDATOR.host_results_errors(data, *arguments)))
        for field in ("token_usage_total", "tool_calls_total", "retries_total", "erroneous_approval_requests",
                      "latency_ms_mean", "latency_ms_p95", "human_intervention_rate"):
            for value in (False, "0", -1):
                with self.subTest(metric=field, value=value):
                    data = copy.deepcopy(baseline)
                    data["analysis"]["metrics"][field] = value
                    self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_quality_gaps_and_thresholds_block_benchmark_without_rewriting_host_result(self) -> None:
        baseline = self.host_fixture()
        arguments = tuple(baseline["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        for field in ("invocation_precision", "invocation_recall", "entry_routing_accuracy",
                      "safe_recovery_completion_accuracy", "stale_conflict_detection_rate"):
            for value in (None, 0.9):
                with self.subTest(field=field, value=value):
                    data = copy.deepcopy(baseline)
                    data["analysis"]["metrics"][field] = value
                    data["benchmark_gate"]["status"] = "blocked"
                    self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
                    self.assertTrue(VALIDATOR.release_gate_pass(data))
                    data["benchmark_gate"]["status"] = "pass"
                    self.assertTrue(VALIDATOR.host_results_errors(data, *arguments))

    def test_usage_requires_complete_integer_counters_for_every_passing_sample(self) -> None:
        data = self.host_fixture()
        arguments = (data["release"]["archive_sha256"], data["release"]["source_tree_sha256"], data["release"]["file_count"])
        invalid = [
            {}, {"unknown_counter": 123}, {"input_tokens": 1}, {"output_tokens": 1},
            {"input_tokens": [], "output_tokens": 1},
            {"input_tokens": 1.5, "output_tokens": 1},
            {"input_tokens": True, "output_tokens": 1},
            {"input_tokens": -1, "output_tokens": 1},
            {"input_tokens": 1, "output_tokens": 1, "unknown_counter": 123},
            {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": 2},
            {"input_tokens": 1, "output_tokens": 1, "cached_input_tokens": False},
        ]
        for usage in invalid:
            for replicate in (False, True):
                with self.subTest(usage=usage, replicate=replicate):
                    forged = copy.deepcopy(data)
                    sample = forged["host"]["assertions"][0]
                    if replicate:
                        sample = sample["replicate_runs"][0]
                    sample["usage"] = usage
                    self.assertTrue(any("usage" in error for error in
                                        VALIDATOR.host_results_errors(forged, *arguments)))
        for usage in ({"input_tokens": 0, "output_tokens": 0},
                      {"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 8}):
            with self.subTest(valid_usage=usage):
                sample = data["host"]["assertions"][0]
                sample["usage"] = usage
                self.refresh_host_aggregates(data)
                self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])

    def test_human_intervention_rate_counts_affected_samples_not_actions(self) -> None:
        data = self.host_fixture()
        arguments = (data["release"]["archive_sha256"], data["release"]["source_tree_sha256"], data["release"]["file_count"])
        for assertion in data["host"]["assertions"]:
            assertion["human_interventions"] = 2
            assertion["replicate_runs"][0]["human_interventions"] = 0
        self.refresh_host_aggregates(data)
        self.assertEqual(data["analysis"]["metrics"]["human_intervention_rate"], 0.5)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])
        self.assertEqual(data["host"]["assertions"][0]["human_interventions"], 2)
        data["analysis"]["metrics"]["human_intervention_rate"] = 1.0
        self.assertTrue(any("aggregate metrics" in error for error in
                            VALIDATOR.host_results_errors(data, *arguments)))
        # Failure reports retain all interventions and remain valid blocked evidence.
        data["host"]["status"] = "failed"
        data["release_gate"]["status"] = "blocked"
        data["benchmark_gate"]["status"] = "blocked"
        for assertion in data["host"]["assertions"]:
            assertion.update(status="fail", failure_class="synthetic_failure", exit_status=1)
            assertion["replicate_runs"][0].update(
                status="fail", failure_class="synthetic_failure", exit_status=1, human_interventions=3,
            )
        self.refresh_host_aggregates(data)
        self.assertEqual(data["analysis"]["metrics"]["human_intervention_rate"], 1.0)
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])

    def test_document_architecture_change_invalidates_forward_results(self) -> None:
        # This is synthetic contract evidence, independent of unpackaged run artifacts.
        data = {"schema_version": 3, "suite": "deterministic_state_contract",
                "assurance": "deterministic_local_process",
                "environment": {"python": "synthetic-python", "platform": "synthetic-platform"},
                "duration_seconds": 0}
        cases = FORWARD.validate_cases(FORWARD.load_object(FORWARD.CASES))
        data["inputs_sha256"] = FORWARD.input_hashes()
        data.update(cases=len(cases), passed=len(cases), failed=0, semantic_output_sha256=FORWARD.semantic_output_sha256(cases))
        self.assertEqual(FORWARD.check_saved(data, cases), [])
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            for relative in FORWARD.CONTRACT_INPUTS:
                target = fixture / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((PROJECT / relative).read_bytes())
            with (fixture / "文档架构.md").open("a") as stream:
                stream.write("\nChanged document contract.\n")
            with mock.patch.object(FORWARD, "ROOT", fixture):
                self.assertIn("forward results are stale: 文档架构.md", FORWARD.check_saved(data, cases))

    def test_release_readiness_mode_fails_closed_when_gate_is_blocked(self) -> None:
        data = self.host_fixture()
        data["release_gate"]["status"] = "blocked"
        self.assertFalse(VALIDATOR.release_gate_pass(data))
        self.assertFalse(VALIDATOR.release_gate_pass(None))

    def test_host_results_schema_rejects_missing_cases_and_false_green_gate(self) -> None:
        data = self.host_fixture()
        archive_digest, embedded = candidate_binding()
        arguments = (
            archive_digest,
            embedded["source_tree_sha256"],
            len(embedded["source_entries"]),
        )
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])

        missing = copy.deepcopy(data)
        missing["host"]["assertions"].pop()
        self.assertTrue(any("coverage is incomplete" in error for error in VALIDATOR.host_results_errors(missing, *arguments)))

        gate_mismatch = copy.deepcopy(data)
        gate_mismatch["release_gate"]["status"] = "blocked"
        self.assertTrue(any("release gate" in error for error in VALIDATOR.host_results_errors(gate_mismatch, *arguments)))

    def test_unverified_host_allows_null_uncollected_correlations(self) -> None:
        data = self.host_fixture()
        archive_digest, embedded = candidate_binding()
        arguments = (
            archive_digest,
            embedded["source_tree_sha256"],
            len(embedded["source_entries"]),
        )
        data["host"]["version"] = None
        data["host"]["status"] = "unable_to_verify"
        data["host"]["installation"]["manifest_preflight"] = "unable_to_verify"
        data["host"]["installation"]["workflow_trace_archive_sha256"] = None
        for assertion in data["host"]["assertions"]:
            assertion.update({
                "status": "unable_to_verify",
                "run_id": None,
                "session_id": None,
                "agent_id": None,
                "model": None,
                "tool_ids": None,
                "guardrail_ids": None,
                "handoff_id": None,
                "exit_status": None,
                "failure_class": "not_run_on_current_archive",
                "usage": None,
                "latency_ms": None,
                "retries": None,
                "human_interventions": None,
                "tool_call_count": None,
                "trace_id": None,
                "trace_sha256": None,
                "replicate_runs": [],
                "capabilities": {kind: {"status": "unavailable", "reason": "Not collected.", "evidence": []}
                                 for kind in ("guardrail", "handoff")},
            })
        data["release_gate"]["status"] = "blocked"
        data["benchmark_gate"]["status"] = "blocked"
        self.refresh_host_aggregates(data)
        self.assertIsNone(data["host"]["version"])
        self.assertTrue(all(
            item["run_id"] is None and item["agent_id"] is None
            for item in data["host"]["assertions"]
        ))
        self.assertEqual(VALIDATOR.host_results_errors(data, *arguments), [])

    def test_host_results_rejects_forged_green_telemetry_and_recovery(self) -> None:
        green = self.host_fixture()
        arguments = tuple(green["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count"))
        self.assertEqual(VALIDATOR.host_results_errors(green, *arguments), [])

        valid_prerelease_and_build = copy.deepcopy(green)
        valid_prerelease_and_build["host"]["version"] = "codex-cli 1.2.3-alpha.1+build.5"
        self.assertEqual(VALIDATOR.host_results_errors(valid_prerelease_and_build, *arguments), [])

        mutations = {
            "empty run identifier": lambda item: item["host"]["assertions"][0].__setitem__("run_id", ""),
            "stale Codex workflow archive": lambda item: item["host"]["installation"].__setitem__(
                "workflow_trace_archive_sha256", "c" * 64
            ),
            "placeholder passing Codex version": lambda item: item["host"].__setitem__(
                "version", "not-recorded"
            ),
            "empty prerelease identifiers": lambda item: item["host"].__setitem__(
                "version", "codex-cli 1.2.3-..."
            ),
            "leading zero major version": lambda item: item["host"].__setitem__(
                "version", "codex-cli 01.2.3"
            ),
            "wrong Codex plugin identity": lambda item: item["host"]["installation"].__setitem__(
                "plugin_id", "other-plugin"
            ),
            "mutable workspace installation": lambda item: item["host"]["installation"].__setitem__(
                "method", "direct mutable workspace copy"
            ),
            "missing installation": lambda item: item["host"].__setitem__("installation", None),
            "wrong host name": lambda item: item["host"].__setitem__("host", "unsupported"),
            "unhashable case id": lambda item: item["host"]["assertions"][0].__setitem__("id", []),
            "unhashable tool identifier": lambda item: item["host"]["assertions"][0].__setitem__(
                "tool_ids", [{}]
            ),
            "recovery summary mismatch": lambda item: item["recovery"].__setitem__(
                "current_version_reinstall", "fail"
            ),
            "stale recovery archive": lambda item: item["recovery"].__setitem__(
                "archive_sha256", "c" * 64
            ),
            "legacy host collection": lambda item: item.__setitem__("hosts", []),
            "forged sample statistics": lambda item: item["analysis"]["sampling"][
                "case_statistics"
            ][0].__setitem__("pass_count", 0),
            "forged aggregate token count": lambda item: item["analysis"]["metrics"].__setitem__(
                "token_usage_total", 0
            ),
            "missing no-skill comparison": lambda item: item["analysis"]["comparisons"].pop(0),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                forged = copy.deepcopy(green)
                mutate(forged)
                self.assertTrue(VALIDATOR.host_results_errors(forged, *arguments))

    def test_forward_runner_rejects_skips_and_wrong_test_count(self) -> None:
        with self.assertRaisesRegex(ValueError, "skipped 1"):
            FORWARD.validate_unittest_transcript("Ran 26 tests\n\nOK (skipped=1)\n", 26)
        with self.assertRaisesRegex(ValueError, "expected 26"):
            FORWARD.validate_unittest_transcript("Ran 25 tests\n\nOK\n", 26)

    def test_forward_manifest_requires_real_class_methods(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests").mkdir()
            suite = root / "tests/test_forward_workflows.py"
            suite.write_text("# def test_comment(\nclass ForwardWorkflowTests:\n    def test_actual(self): pass\n")
            (root / "tests/test_longtask_state.py").write_text("class LongtaskStateTests:\n    def test_actual(self): pass\n")
            with mock.patch.object(FORWARD, "ROOT", root):
                self.assertEqual(len(FORWARD.validate_cases({"schema_version": 1, "cases": [
                    {"id": "real", "test": "test_actual"}]})), 1)
                for test in ("test_comment", [], "", "test_actual.inject"):
                    with self.subTest(test=test), self.assertRaises(ValueError):
                        FORWARD.validate_cases({"schema_version": 1, "cases": [{"id": "case", "test": test}]})
                for suite_name in (None, [], "tests.other.OtherTests"):
                    with self.subTest(suite=suite_name), self.assertRaises(ValueError):
                        FORWARD.validate_cases({"schema_version": 1, "cases": [
                            {"id": "case", "test": "test_actual", "suite": suite_name}]})
                cases = [{"id": "forward", "test": "test_actual"},
                         {"id": "state", "test": "test_actual", "suite": "tests.test_longtask_state.LongtaskStateTests"}]
                FORWARD.validate_cases({"schema_version": 1, "cases": cases})
                self.assertEqual(FORWARD.test_ids(cases), [
                    "tests.test_forward_workflows.ForwardWorkflowTests.test_actual",
                    "tests.test_longtask_state.LongtaskStateTests.test_actual"])
                cases[1].pop("suite")
                with self.assertRaisesRegex(ValueError, "targets must be unique"):
                    FORWARD.validate_cases({"schema_version": 1, "cases": cases})

    def test_forward_case_check_needs_no_saved_results_or_execution(self) -> None:
        with mock.patch.object(FORWARD, "RESULTS", Path("missing-results.json")), mock.patch.object(
            FORWARD.subprocess, "run"
        ) as run, mock.patch.object(FORWARD, "atomic_write") as write, mock.patch.object(
            sys, "argv", ["run_forward_evals.py", "--check-cases"]
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(FORWARD.main(), 0)
        run.assert_not_called()
        write.assert_not_called()

    def test_ignored_artifacts_cannot_be_force_added_but_cases_and_fixtures_remain_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".gitignore").write_bytes((PROJECT / ".gitignore").read_bytes())
            outputs = [*VALIDATOR.SOURCE_EVALUATION_RESULTS, "evals/progress_results.json",
                       "evals/future_results.json", "evals/traces/run/trace.jsonl", ".artifacts/run/summary.json"]
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--stdin"],
                                     input="\n".join(outputs) + "\n", capture_output=True, text=True, check=True)
            self.assertEqual(ignored.stdout.splitlines(), outputs)
            for relative in ("evals/future_results.json", "evals/future_cases.json", "tests/fixtures/synthetic_host_results_v7.json"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}")
            subprocess.run(["git", "-C", str(root), "add", "-f", "."], check=True)
            errors = []
            with mock.patch.object(VALIDATOR, "ROOT", root):
                VALIDATOR.check_repository_content(errors)
            self.assertEqual(errors, ["generated artifact is tracked by Git: evals/future_results.json; remove it from the index"])

    def test_forward_run_does_not_replace_saved_results(self) -> None:
        cases = FORWARD.validate_cases(FORWARD.load_object(FORWARD.CASES))
        process = subprocess.CompletedProcess([], 0, "", f"Ran {len(cases)} tests\n\nOK\n")
        with mock.patch.object(FORWARD.subprocess, "run", return_value=process), mock.patch.object(
            FORWARD, "atomic_write"
        ) as write, mock.patch.object(sys, "argv", ["run_forward_evals.py", "--run"]), redirect_stdout(io.StringIO()):
            self.assertEqual(FORWARD.main(), 0)
        write.assert_not_called()

    def test_forward_runner_timeout_is_a_structured_failure(self) -> None:
        error = io.StringIO()
        with mock.patch.object(FORWARD.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)), mock.patch.object(
            sys, "argv", ["run_forward_evals.py", "--write", "--timeout-seconds", "1"],
        ), redirect_stderr(error):
            self.assertEqual(FORWARD.main(), 1)
        self.assertIn("forward suite exceeded 1 seconds", error.getvalue())

    def test_missing_dist_builds_temporary_candidate_and_keeps_host_binding_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            (root / "scripts/build_release.py").write_text("# fixture\n")
            (root / "release-manifest.json").write_text(json.dumps({"package": "longtask", "version": "4.1.0"}))
            (root / "evals").mkdir()
            data = self.host_fixture()
            (root / "evals/host_results.json").write_text(json.dumps(data))
            binding = {key: data["release"][key] for key in ("archive_sha256", "source_tree_sha256", "file_count")}
            response = subprocess.CompletedProcess([], 0, json.dumps(binding), "")
            errors = []
            with mock.patch.object(VALIDATOR, "ROOT", root), mock.patch.object(VALIDATOR.subprocess, "run", return_value=response) as run:
                observed = VALIDATOR.check_release_archive(errors)
                VALIDATOR.check_host_results(errors, observed)
            self.assertEqual(errors, [])
            self.assertIn("build", run.call_args.args[0])
            self.assertFalse((root / "dist").exists())
            output = Path(run.call_args.args[0][-1])
            self.assertFalse(output.parent.exists())
            data["release"]["archive_sha256"] = "f" * 64
            (root / "evals/host_results.json").write_text(json.dumps(data))
            with mock.patch.object(VALIDATOR, "ROOT", root):
                VALIDATOR.check_host_results(errors, binding)
            self.assertTrue(any("current archive digest is stale" in error for error in errors))

    def test_static_subprocess_checks_fail_closed_on_timeout(self) -> None:
        errors: list[str] = []
        with mock.patch.object(VALIDATOR.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)):
            VALIDATOR.check_release_archive(errors)
            VALIDATOR.check_forward_results(errors)
            VALIDATOR.check_invocation_results(errors)
            VALIDATOR.check_memory_results(errors)
        self.assertEqual(len([message for message in errors if "exceeded" in message]), 4)

    def test_fenced_examples_are_removed(self) -> None:
        obsolete = "code IS " + "the spec"
        text = f"before\n```text\n{obsolete}\n[dead](missing.md)\n```\nafter\n"
        stripped = VALIDATOR.strip_fenced_code(text)
        self.assertNotIn(obsolete, stripped)
        self.assertNotIn("missing.md", stripped)
        self.assertIn("before", stripped)
        self.assertIn("after", stripped)

    def test_reference_links_validate_and_undefined_references_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide(1).md").write_text("# Guide\n", encoding="utf-8")
            source = root / "README.md"
            source.write_text(
                "# Readme\n\n[inline](<guide(1).md>) and [reference][guide].\n\n[guide]: <guide(1).md>\n",
                encoding="utf-8",
            )
            errors: list[str] = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertEqual(errors, [])
            source.write_text("# Readme\n\n[missing][nowhere]\n", encoding="utf-8")
            errors = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertTrue(any("undefined reference" in error for error in errors))

    def test_skill_check_rejects_malformed_frontmatter_and_missing_h1(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT) as temporary:
            path = Path(temporary) / "SKILL.md"
            path.write_text(
                "---\nname: temporary\nmetadata:\n   broken\n---\nnot a heading\n",
                encoding="utf-8",
            )
            errors: list[str] = []
            VALIDATOR.check_skill(path, "temporary", errors)
            self.assertTrue(any("unsupported YAML" in error for error in errors))
            self.assertTrue(any("H1" in error for error in errors))

    def test_frontmatter_allows_apostrophes_and_rejects_unclosed_quotes(self) -> None:
        self.assertEqual(VALIDATOR.parse_yaml_scalar("'user''s workflow'"), "user's workflow")
        self.assertIsNone(VALIDATOR.parse_yaml_scalar("'user's workflow'"))
        with tempfile.TemporaryDirectory(dir=PROJECT) as temporary:
            path = Path(temporary) / "SKILL.md"
            path.write_text(
                "---\nname: temporary\ndescription: user's durable workflow\nlicense: MIT\ncompatibility: Codex with Python 3.14+\nmetadata:\n  version: 4.1.0\n---\n# Temporary\n",
                encoding="utf-8",
            )
            errors: list[str] = []
            VALIDATOR.check_skill(path, "temporary", errors)
            self.assertEqual(errors, [])
            path.write_text(
                "---\nname: temporary\ndescription: \"unclosed\nmetadata:\n  version: 4.1.0\n---\n# Temporary\n",
                encoding="utf-8",
            )
            errors = []
            VALIDATOR.check_skill(path, "temporary", errors)
            self.assertTrue(any("invalid quoted scalar" in error for error in errors))

    def test_skill_metadata_rejects_ambiguous_keys_and_oversized_compatibility(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT) as temporary:
            path = Path(temporary) / "SKILL.md"
            base = ("---\nname: temporary\ndescription: Durable workflow\nlicense: MIT\n"
                    "compatibility: Codex with Python 3.14+\nmetadata:\n  version: \"4.1.0\"\n---\n# Temporary\n")
            for text in (base.replace("license: MIT", "license: MIT\nlicense: Apache-2.0"),
                         base.replace('  version: "4.1.0"', '  version: "4.1.0"\n  version: "4.1.0"'),
                         base.replace("Codex with Python 3.14+", "x" * 501)):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    errors = []
                    VALIDATOR.check_skill(path, "temporary", errors)
                    self.assertTrue(errors)

    def test_skill_interface_rejects_wrong_invocation_and_string_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir=PROJECT) as temporary:
            path = Path(temporary) / "openai.yaml"
            base = ("interface:\n  display_name: \"Temporary\"\n"
                    "  short_description: \"Restore a task with version-bound evidence\"\n"
                    "  default_prompt: \"Use $temporary to restore this task.\"\n"
                    "policy:\n  allow_implicit_invocation: true\n")
            path.write_text(base, encoding="utf-8")
            errors = []
            VALIDATOR.check_skill_interface(path, "temporary", errors)
            self.assertEqual(errors, [])
            for text in (base.replace("$temporary", "$temporary-other"),
                         base.replace("allow_implicit_invocation: true", 'allow_implicit_invocation: "true"'),
                         base.replace('display_name: "Temporary"', 'display_name: "Temporary"\n  display_name: "Other"'),
                         base.replace('display_name: "Temporary"', 'display_name: "Unclosed'),
                         base.replace('display_name: "Temporary"', "display_name: 'Temporary'broken'"),
                         base.replace("  default_prompt:", "  unused_prompt:")):
                with self.subTest(text=text):
                    path.write_text(text, encoding="utf-8")
                    errors = []
                    VALIDATOR.check_skill_interface(path, "temporary", errors)
                    self.assertTrue(errors)

    def test_markdown_heading_fragments_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "guide.md"
            target.write_text("# Existing heading\n", encoding="utf-8")
            source = root / "README.md"
            source.write_text("# Readme\n\n[good](guide.md#existing-heading)\n", encoding="utf-8")
            errors: list[str] = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertEqual(errors, [])
            source.write_text("# Readme\n\n[bad](guide.md#missing-heading)\n", encoding="utf-8")
            errors = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertTrue(any("missing heading anchor" in error for error in errors))

    def test_setext_heading_fragment_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text("Setext heading\n==============\n", encoding="utf-8")
            (root / "README.md").write_text("# Readme\n\n[guide](guide.md#setext-heading)\n", encoding="utf-8")
            errors: list[str] = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertEqual(errors, [])

    def test_github_slug_collisions_are_disambiguated_globally(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text("# Foo\n\n# Foo\n\n# Foo-1\n", encoding="utf-8")
            (root / "README.md").write_text("# Readme\n\n[third](guide.md#foo-1-1)\n", encoding="utf-8")
            errors: list[str] = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertEqual(errors, [])

    def test_github_slug_preserves_consecutive_hyphens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "guide.md").write_text("# Foo--bar\n\n# A  b\n", encoding="utf-8")
            (root / "README.md").write_text(
                "# Readme\n\n[first](guide.md#foo--bar) [second](guide.md#a--b)\n", encoding="utf-8",
            )
            errors: list[str] = []
            VALIDATOR.check_markdown_links(root, errors)
            self.assertEqual(errors, [])

    def test_portable_markdown_paths_accept_chinese_and_reject_ambiguous_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "权限审计.md").write_text("# 权限审计\n", encoding="utf-8")
            errors: list[str] = []
            VALIDATOR.check_portable_markdown_paths(root, errors)
            self.assertEqual(errors, [])

            (root / "CON.md").write_text("# Reserved\n", encoding="utf-8")
            (root / "Cafe\u0301.md").write_text("# Decomposed\n", encoding="utf-8")
            (root / "Guide.md").write_text("# Guide\n", encoding="utf-8")
            (root / "guide.md").write_text("# guide\n", encoding="utf-8")
            errors = []
            VALIDATOR.check_portable_markdown_paths(root, errors)
            self.assertTrue(any("Windows reserved" in error for error in errors))
            self.assertTrue(any("not Unicode NFC" in error for error in errors))
            self.assertTrue(any("collide after NFC/case folding" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
