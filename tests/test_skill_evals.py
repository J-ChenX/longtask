from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT / "scripts" / "run_skill_evals.py"
SPEC = importlib.util.spec_from_file_location("run_skill_evals", SCRIPT)
assert SPEC and SPEC.loader
EVALS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVALS)


class SkillEvalTests(unittest.TestCase):
    @staticmethod
    def result_manifest() -> dict:
        corpus = PROJECT / "evals" / "invocation_cases.json"
        return {
            "schema_version": 3,
            "evaluation": {
                "method": "fresh-session classification",
                "evaluator": "test-evaluator",
                "evaluated_at": "2026-09-03T00:00:00Z",
                "assurance": "unattested_classification",
                "model": "unrecorded",
                "harness": "test harness",
                "repetitions": 1,
                "trace_available": False,
                "trace_id": None,
            },
            "inputs": {
                "corpus_sha256": EVALS.file_sha256(corpus),
                "discovery_surface_sha256": EVALS.aggregate_sha256(EVALS.DISCOVERY_FILES),
                "runner_sha256": EVALS.file_sha256(SCRIPT),
            },
        }

    def test_corpus_is_balanced_and_covers_every_entry(self) -> None:
        data = json.loads((PROJECT / "evals" / "invocation_cases.json").read_text(encoding="utf-8"))
        cases, errors = EVALS.validate_corpus(data)
        self.assertEqual(errors, [])
        self.assertEqual(sum(case["should_trigger"] for case in cases), len(cases) // 2)

    def test_malformed_case_id_and_entry_fail_as_contract_errors(self) -> None:
        data = json.loads((PROJECT / "evals/invocation_cases.json").read_text())
        for field in ("id", "expected_entry"):
            with self.subTest(field=field):
                malformed = copy.deepcopy(data)
                malformed["cases"][0][field] = []
                _, errors = EVALS.validate_corpus(malformed)
                self.assertTrue(errors)
        _, errors = EVALS.score_classifications(
            [{"id": "case", "should_trigger": True, "expected_entry": "continue"}],
            {"results": [{"id": "case", "selected": True, "entry": []}]},
        )
        self.assertTrue(errors)

    def test_scoring_penalizes_false_invocation_and_wrong_route(self) -> None:
        cases = [
            {"id": "positive", "should_trigger": True, "expected_entry": "setup"},
            {"id": "negative", "should_trigger": False, "expected_entry": None},
        ]
        results = {"results": [
            {"id": "positive", "selected": True, "entry": "review"},
            {"id": "negative", "selected": True, "entry": "setup"},
        ]}
        metrics, errors = EVALS.score(cases, results)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["invocation_precision"], 0.5)
        self.assertEqual(metrics["entry_accuracy_on_selected_positives"], 0.0)

    def test_scoring_rejects_entry_when_not_selected(self) -> None:
        cases = [{"id": "negative", "should_trigger": False, "expected_entry": None}]
        metrics, errors = EVALS.score(
            cases,
            {"results": [{"id": "negative", "selected": False, "entry": "setup"}]},
        )
        self.assertEqual(metrics["cases"], 1)
        self.assertTrue(any("entry=null" in error for error in errors))

    def repeated_manifest(self) -> dict:
        manifest = self.result_manifest()
        cases = json.loads((PROJECT / "evals/invocation_cases.json").read_text())["cases"]
        decisions = [{"id": case["id"], "selected": case["should_trigger"],
                      "entry": case["expected_entry"]} for case in cases]
        manifest["evaluation"].update(
            assurance="trace_backed", repetitions=2, model="exact-model-version",
            trace_available=True, trace_id="trace/run-0000",
        )
        manifest["repeat_runs"] = [
            {"run_id": f"run/independent-{index}", "trace_id": f"trace/run-{index:04d}",
             "trace_sha256": hashlib.sha256(str(index).encode()).hexdigest(),
             "model": "exact-model-version", "results": copy.deepcopy(decisions)}
            for index in range(2)
        ]
        manifest["results"] = copy.deepcopy(decisions)
        return manifest

    def test_repetitions_must_represent_actual_independent_runs(self) -> None:
        manifest = self.repeated_manifest()
        corpus = PROJECT / "evals/invocation_cases.json"
        self.assertEqual(EVALS.validate_result_manifest(manifest, corpus), [])
        mutations = {
            "label inflation": lambda value: value["evaluation"].update(repetitions=200),
            "missing runs": lambda value: value.pop("repeat_runs"),
            "duplicate run": lambda value: value["repeat_runs"][1].update(run_id=value["repeat_runs"][0]["run_id"]),
            "duplicate trace": lambda value: value["repeat_runs"][1].update(trace_id=value["repeat_runs"][0]["trace_id"]),
            "duplicate digest": lambda value: value["repeat_runs"][1].update(trace_sha256=value["repeat_runs"][0]["trace_sha256"]),
            "different model": lambda value: value["repeat_runs"][1].update(model="another-model"),
            "missing digest": lambda value: value["repeat_runs"][1].pop("trace_sha256"),
            "unbound trace": lambda value: value["evaluation"].update(trace_id="trace/unbound"),
            "unbound decisions": lambda value: value.update(results=[]),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                forged = copy.deepcopy(manifest)
                mutate(forged)
                self.assertTrue(EVALS.validate_result_manifest(forged, corpus))

    def test_repeated_scoring_counts_failed_later_runs_and_checks_case_coverage(self) -> None:
        manifest = self.repeated_manifest()
        cases = json.loads((PROJECT / "evals/invocation_cases.json").read_text())["cases"]
        for decision in manifest["repeat_runs"][1]["results"]:
            decision.update(selected=False, entry=None)
        metrics, errors = EVALS.score(cases, manifest)
        self.assertEqual(errors, [])
        self.assertEqual(metrics["evaluated_runs"], 2)
        self.assertEqual(metrics["cases"], len(cases) * 2)
        self.assertEqual(metrics["invocation_recall"], 0.5)
        manifest["repeat_runs"][1]["results"].pop()
        _, errors = EVALS.score(cases, manifest)
        self.assertTrue(any("missing result" in error for error in errors))
        manifest["repeat_runs"][1]["results"].append({"id": "invented", "selected": False, "entry": None})
        _, errors = EVALS.score(cases, manifest)
        self.assertTrue(any("unexpected result" in error for error in errors))

    def test_cli_reports_external_verification_and_rejects_inflated_manifest(self) -> None:
        manifest = self.repeated_manifest()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-results.json"
            for repetitions, expected_status in ((2, 0), (200, 1)):
                manifest["evaluation"]["repetitions"] = repetitions
                path.write_text(json.dumps(manifest))
                output = io.StringIO()
                with mock.patch.object(sys, "argv", ["run_skill_evals.py", "--results", str(path)]), redirect_stdout(output):
                    self.assertEqual(EVALS.main(), expected_status)
                report = json.loads(output.getvalue())
                self.assertEqual(report["evidence_verification"], "external_verification_required")
                if expected_status:
                    self.assertIsNone(report["metrics"])

    def test_unattested_manifest_cannot_claim_repetitions(self) -> None:
        manifest = self.result_manifest()
        manifest["evaluation"]["repetitions"] = 200
        errors = EVALS.validate_result_manifest(manifest, PROJECT / "evals/invocation_cases.json")
        self.assertTrue(any("repetitions=1" in error for error in errors))

    def test_unattested_single_classification_manifest_is_valid(self) -> None:
        self.assertEqual(EVALS.validate_result_manifest(
            self.result_manifest(), PROJECT / "evals" / "invocation_cases.json",
        ), [])

    def test_invalid_repetition_types_fail_cleanly(self) -> None:
        for repetitions in (True, "200", None, [], 1.0):
            with self.subTest(repetitions=repetitions):
                manifest = self.result_manifest()
                manifest["evaluation"]["repetitions"] = repetitions
                self.assertTrue(EVALS.validate_result_manifest(
                    manifest, PROJECT / "evals" / "invocation_cases.json",
                ))

    def test_unattested_manifest_cannot_claim_trace(self) -> None:
        manifest = self.result_manifest()
        manifest["evaluation"].update(trace_available=True, trace_id="trace/run-1234")
        errors = EVALS.validate_result_manifest(
            manifest, PROJECT / "evals" / "invocation_cases.json",
        )
        self.assertTrue(any("must not claim a trace" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
