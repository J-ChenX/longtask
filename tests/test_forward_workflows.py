"""Isolated, supported-CLI forward scenarios for the longtask lifecycle."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
STATE_SCRIPT = PROJECT / "scripts" / "longtask_state.py"


class ForwardWorkflowTests(unittest.TestCase):
    def test_planning_checkpoint_precedes_documents_and_stays_recoverable(self) -> None:
        from scripts.demo_workflow import demonstrate
        result = demonstrate('planning')
        self.assertTrue(result['checkpoint_before_documents'])
        self.assertTrue(result['handoff_fresh'])
        self.assertEqual(result['planned_packages'], 2)
        self.assertFalse(result['checkpoint_cleared'])
        self.assertFalse(result['implementation_started'])

    def test_executable_tutorial_preserves_knowledge_and_cleans_checkpoint(self) -> None:
        for scenario in ("lifecycle", "recovery", "modify", "worktree"):
            with self.subTest(scenario=scenario):
                result = subprocess.run(
                    [sys.executable, str(PROJECT / "scripts/demo_workflow.py"), "--scenario", scenario],
                    capture_output=True, text=True, timeout=60, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                output = json.loads(result.stdout)
                self.assertEqual(output["output"], "42")
                self.assertTrue(output["retained_project_knowledge"])
                self.assertTrue(output["checkpoint_cleared"])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.generation = "uninitialized"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def cli(self, *arguments: str, ok: bool = True) -> dict:
        mutations = {
            "checkpoint", "approve", "transition", "package", "handoff", "evidence",
            "blocker", "review", "goal", "modify", "reconcile-events", "finish",
        }
        if arguments and arguments[0] in mutations and "--expected-task-id" not in arguments:
            arguments = (arguments[0], "--expected-task-id", self.generation, *arguments[1:])
        result = subprocess.run(
            [sys.executable, str(STATE_SCRIPT), *arguments], capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and not ok:
            self.fail(f"command unexpectedly passed: {result.stdout}")
        if result.returncode != 0 and ok:
            self.fail(f"command failed: {result.stderr}")
        value = json.loads(result.stdout if result.returncode == 0 else result.stderr)
        if result.returncode == 0 and arguments and arguments[0] == "init":
            self.generation = value["task_id"]
        return value

    def init(self, mode: str, goal: str = "Deliver the accepted outcome") -> dict:
        return self.cli(
            "init", "--root", str(self.root), "--task-id", "forward", "--mode", mode, "--goal", goal,
        )

    @staticmethod
    def package(package_id: str, digest: str, write_set: list[str]) -> dict:
        return {
            "id": package_id,
            "objective": f"Implement {package_id}",
            "status": "active",
            "dependencies": [],
            "affected_modules": ["workflow"],
            "write_set": write_set,
            "acceptance_checks": [f"verify {package_id}"],
            "base_revision": digest,
            "risk": "medium",
            "risk_level": "medium",
            "required_review_roles": ["independent-quality"],
            "rollback": "revert package",
            "stopping_condition": "verification fails twice",
            "owner": package_id,
            "lease_expires": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "contributors": [package_id],
        }

    def advance(self, state: dict, command: str, *args: str, actor: str = "integrator", ok: bool = True) -> dict:
        return self.cli(command, "--root", str(self.root), "--expected-revision", str(state["revision"]),
                        "--actor", actor, *args, ok=ok)

    def reviewed_project(self, *, knowledge: bool = True) -> dict:
        architecture = self.root / "docs/ARCHITECTURE.md"
        architecture.parent.mkdir(parents=True, exist_ok=True)
        architecture.write_text("# Project\n\nCurrent behavior and verification: run verify.py.\n", encoding="utf-8")
        state = self.init("review")
        state = self.advance(state, "evidence", "--kind", "test", "--summary", "Current project checks passed", "--result", "pass")
        if knowledge:
            state = self.advance(state, "evidence", "--kind", "knowledge", "--check-id", "project-content-merged",
                                 "--summary", "Current behavior and verification are merged into docs/ARCHITECTURE.md", "--result", "pass")
        details = {
            **{key: state[key] for key in ("task_id", "evidence_epoch", "head_commit")},
            "reviewer": "reviewer", "role": "quality", "scope": ["current project content"],
            "base_revision": state["artifact_digest"], "artifact_digest": state["artifact_digest"],
            "status": "approved", "findings": [], "evidence": ["project checks"], "coverage_gaps": [],
            "assumptions": [], "covered_package_ids": [],
        }
        state = self.advance(state, "evidence", "--kind", "review", "--summary", "Project review passed",
                             "--result", "pass", "--details", json.dumps(details), actor="reviewer")
        state = self.advance(state, "evidence", "--kind", "phase:review", "--summary", "Review exit passed", "--result", "pass")
        state = self.advance(state, "approve", "--scope", "review")
        return self.advance(state, "transition", "--phase", "complete", "--status", "complete")

    def test_finished_work_keeps_project_content_and_discards_execution_records(self) -> None:
        state = self.reviewed_project()
        expected = (self.root / "docs/ARCHITECTURE.md").read_bytes()
        result = self.advance(state, "finish")
        self.assertTrue(result["finished"])
        self.assertEqual((self.root / "docs/ARCHITECTURE.md").read_bytes(), expected)
        self.assertEqual({path.name for path in (self.root / ".longtask").iterdir()}, {".state.lock"})
        self.assertFalse((self.root / "docs/tasks").exists())
        self.assertEqual(self.cli("route", "--root", str(self.root))["entry"], "continue")
        fresh = self.cli("init", "--root", str(self.root), "--task-id", "forward", "--mode", "continue",
                         "--goal", "Improve current project behavior")
        self.assertEqual(fresh["validation_evidence"], [])
        self.assertEqual(fresh["work_packages"], [])
        self.assertEqual(fresh["revision"], 0)
        self.assertNotEqual(fresh["task_id"], state["task_id"])
        stale = self.cli("checkpoint", "--root", str(self.root), "--expected-task-id", state["task_id"],
                         "--expected-revision", "0", "--next-action", "Delayed writer from deleted checkpoint", ok=False)
        self.assertIn("stale task", stale["error"])
        self.assertEqual((self.root / "docs/ARCHITECTURE.md").read_bytes(), expected)
        self.assertNotIn("project-content-merged", (self.root / ".longtask/events.jsonl").read_text())

    def test_finish_rejects_unmerged_or_changed_project_content(self) -> None:
        state = self.reviewed_project(knowledge=False)
        rejected = self.advance(state, "finish", ok=False)
        self.assertIn("knowledge", rejected["error"])
        self.assertTrue((self.root / ".longtask/state.json").is_file())
        state = self.advance(state, "evidence", "--kind", "knowledge", "--summary", "Current project content merged", "--result", "pass")
        (self.root / "docs/ARCHITECTURE.md").write_text("# Changed project content\n", encoding="utf-8")
        rejected = self.advance(state, "finish", ok=False)
        self.assertTrue((self.root / ".longtask/state.json").is_file())
        self.assertTrue((self.root / ".longtask/events.jsonl").is_file())
        self.assertIn("error", rejected)

    def test_parallel_delivery_recovers_failure_and_completes_each_owned_change(self) -> None:
        state = self.init("continue")
        first = self.package("first", state["artifact_digest"], ["first.py"])
        second = self.package("second", state["artifact_digest"], ["second.py"])
        for package in (first, second):
            state = self.advance(state, "package", "--data", json.dumps(package), actor=package["owner"])
        (self.root / "first.py").write_text("unfinished first\n", encoding="utf-8")
        (self.root / "second.py").write_text("unfinished second\n", encoding="utf-8")
        state = self.advance(state, "evidence", "--kind", "test", "--summary", "first acceptance failed",
                             "--result", "fail", "--check-id", "verify first", "--package-id", "first")
        state = self.advance(state, "package", "--data", json.dumps(first), actor="first")
        (self.root / "first.py").write_text("repaired first\n", encoding="utf-8")
        first["owner"] = "successor"
        state = self.advance(state, "package", "--data", json.dumps(first), actor="first")
        for package in (first, second):
            state = self.advance(state, "evidence", "--kind", "test", "--summary", "current owned check passed",
                                 "--result", "pass", "--check-id", "verify " + package["id"],
                                 "--package-id", package["id"])
            package["status"] = "complete"
            state = self.advance(state, "package", "--data", json.dumps(package), actor=package["owner"])
            if package is first:
                (self.root / "second.py").write_text("finished second\n", encoding="utf-8")
                state = self.advance(state, "checkpoint")
                self.assertEqual(state["work_packages"][1]["status"], "active")
        self.assertTrue(all(p["status"] == "complete" for p in state["work_packages"]))
        self.assertTrue(all(p["base_revision"] == first["base_revision"] for p in state["work_packages"]))
        self.assertEqual(len({p["execution_group"] for p in state["work_packages"]}), 1)
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_manual_failure_retry_cannot_reuse_pre_failure_acceptance(self) -> None:
        state = self.init("continue")
        package = self.package("implementation", state["artifact_digest"], ["implementation.py"])
        state = self.advance(state, "package", "--data", json.dumps(package), actor="implementation")
        (self.root / "implementation.py").write_text("implementation under verification\n", encoding="utf-8")
        state = self.advance(state, "evidence", "--kind", "test", "--summary", "passed before manual failure",
                             "--result", "pass", "--check-id", "verify implementation", "--package-id", "implementation")
        state = self.advance(state, "package", "--data", json.dumps(dict(package, status="failed")), actor="implementation")
        self.assertEqual(state["work_packages"][0]["acceptance_evidence_start"], 1)
        state = self.advance(state, "package", "--data", json.dumps(package), actor="implementation")
        rejected = self.advance(state, "package", "--data", json.dumps(dict(package, status="complete")),
                                actor="implementation", ok=False)
        self.assertIn("passing evidence", rejected["error"])
        state = self.advance(state, "evidence", "--kind", "test", "--summary", "verified after recovery",
                             "--result", "pass", "--check-id", "verify implementation", "--package-id", "implementation")
        state = self.advance(state, "package", "--data", json.dumps(dict(package, status="complete")), actor="implementation")
        self.assertEqual(state["work_packages"][0]["status"], "complete")
        self.assertEqual(len(state["work_packages"][0]["evidence"]), 2)
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_completed_parallel_peer_cannot_hide_later_tampering(self) -> None:
        state = self.init("continue")
        first = self.package("first", state["artifact_digest"], ["first.py"])
        second = self.package("second", state["artifact_digest"], ["second.py"])
        for package in (first, second):
            state = self.advance(state, "package", "--data", json.dumps(package), actor=package["owner"])
        (self.root / "first.py").write_text("accepted first\n", encoding="utf-8")
        state = self.advance(state, "evidence", "--kind", "test", "--summary", "first passed",
                             "--result", "pass", "--check-id", "verify first", "--package-id", "first")
        state = self.advance(state, "package", "--data", json.dumps(dict(first, status="complete")), actor="first")
        (self.root / "first.py").unlink()
        state = self.advance(state, "checkpoint")
        self.assertTrue(state["work_packages"][1]["scope_drift"])
        rejected = self.advance(state, "package", "--data", json.dumps(first), actor="first", ok=False)
        self.assertIn("scope", rejected["error"])

    def test_replacement_resets_single_checkpoint_without_retaining_history(self) -> None:
        state = self.init("continue")
        path = self.root / ".longtask/state.json"
        state.update(schema_version=2, skill_version="2.0.0")
        path.write_text(json.dumps(state), encoding="utf-8")
        (self.root / "implementation.py").write_text("current implementation\n", encoding="utf-8")
        self.assertEqual(self.cli("route", "--root", str(self.root))["entry"], "error")
        fresh = self.cli("init", "--root", str(self.root), "--task-id", "replacement", "--mode", "continue",
                         "--goal", "Create a fresh current contract", "--replace-active",
                         "--expected-task-id", state["task_id"], "--expected-revision", str(state["revision"]),
                         "--expected-artifact-digest", state["artifact_digest"])
        self.assertNotEqual(fresh["artifact_digest"], state["artifact_digest"])
        self.assertEqual(fresh["work_packages"], [])
        self.assertEqual(fresh["validation_evidence"], [])
        self.assertIsNone(fresh["approval"])
        self.assertEqual(json.loads(path.read_text())["task_id"], fresh["task_id"])
        self.assertNotEqual(fresh["task_id"], state["task_id"])
        self.assertFalse((self.root / "docs/tasks").exists())
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_goal_change_clears_scope_blockers_without_erasing_old_contracts(self) -> None:
        state = self.init("continue")
        package = self.package("work", state["artifact_digest"], ["owned.py"])
        state = self.advance(state, "package", "--data", json.dumps(package), actor="work")
        (self.root / "outside.py").write_text("unattributed change\n", encoding="utf-8")
        state = self.advance(state, "checkpoint")
        self.assertTrue(state["work_packages"][0]["scope_drift"])
        state = self.advance(state, "goal", "--goal", "Revised accepted outcome")
        self.assertEqual(state["work_packages"][0]["status"], "superseded")
        self.assertEqual(state["work_packages"][0]["write_set"], ["owned.py"])
        self.assertNotIn("scope_drift", state["work_packages"][0])
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_setup_initializes_then_revises_scope_safely(self) -> None:
        self.assertEqual(self.cli("route", "--root", str(self.root))["entry"], "setup")
        self.init("setup", "Initial outcome")
        revised = self.cli(
            "goal", "--root", str(self.root), "--expected-revision", "0", "--goal", "Revised outcome",
        )
        self.assertEqual((revised["goal"], revised["phase"]), ("Revised outcome", "discovery"))

    def test_retrofit_works_without_git_or_prior_state(self) -> None:
        (self.root / "main.py").write_text("print('legacy')\n", encoding="utf-8")
        self.assertEqual(self.cli("route", "--root", str(self.root))["entry"], "retrofit")
        state = self.init("retrofit", "Recover and document observed behavior")
        self.assertIsNone(state["base_commit"])
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_handoff_recovers_exact_checkpoint(self) -> None:
        initial = self.init("continue", "Ship a reviewed implementation across separate windows")
        payload = {
            "intent": "review",
            "objective": "Review the implementation completed in the prior window.",
            "reason": "Execution evidence is ready for an independent reviewer.",
            "target": {"kind": "artifact", "ref": "implementation-window-1"},
            "required_inputs": ["state:current", "evidence:execution-window-1"],
            "acceptance_checks": ["Review findings are bound to the frozen target digest."],
            "next_if_pass": {"intent": "execute", "objective": "Continue the next implementation package."},
            "next_if_fail": {"intent": "remediate", "objective": "Correct the recorded review findings."},
        }
        state = self.cli(
            "handoff", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(payload), "--actor", "implementer",
        )
        recovered = self.cli("route", "--root", str(self.root))
        self.assertEqual(recovered["entry"], "review")
        self.assertEqual(recovered["goal"], "Ship a reviewed implementation across separate windows")
        self.assertEqual(recovered["handoff"], state["handoff"])
        self.assertEqual(state["handoff"]["artifact_digest"], initial["artifact_digest"])
        self.assertTrue(recovered["choice_required"])
        self.assertEqual(recovered["recommended_choice"], "review")
        selected = self.cli(
            "route", "--root", str(self.root), "--resume-choice", "review",
        )
        self.assertFalse(selected["choice_required"])
        self.assertEqual(selected["selected_choice"], "review")
        self.assertEqual(selected["entry"], "review")

    def test_fresh_planning_checkpoint_supports_inspection_without_writes(self) -> None:
        state = self.init("setup", "Deliver only a recoverable design this round")
        checkpoint = self.root / ".longtask/state.json"
        events = self.root / ".longtask/events.jsonl"
        before = (checkpoint.read_bytes(), events.read_bytes())
        selected = self.cli("route", "--root", str(self.root), "--resume-choice", "inspect")
        self.assertTrue(selected["selection_available"])
        self.assertEqual(selected["goal"], state["goal"])
        self.assertEqual(selected["handoff"]["intent"], "plan")
        self.assertEqual(before, (checkpoint.read_bytes(), events.read_bytes()))
        self.assertFalse((self.root / "src").exists())

    def test_explicit_resume_selection_reuses_fresh_checkpoint_without_mutation(self) -> None:
        state = self.init("continue", "Complete the remaining accepted implementation")
        checkpoint = self.root / ".longtask/state.json"
        before = checkpoint.read_bytes()
        selected = self.cli("route", "--root", str(self.root), "--resume-choice", "resume")
        self.assertTrue(selected["selection_available"])
        self.assertFalse(selected["choice_required"])
        self.assertEqual(selected["entry"], "continue")
        self.assertEqual(selected["goal"], state["goal"])
        self.assertEqual(selected["handoff"], state["handoff"])
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_execution_architecture_change_reopens_architecture(self) -> None:
        for mode in ("setup", "continue", "retrofit", "modify"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                old_root = self.root
                self.root = Path(temporary)
                try:
                    state = self.init(mode)
                    while state["phase"] != "execution":
                        phase = state["phase"]
                        target = {"discovery": "architecture", "architecture": "documentation", "documentation": "execution"}[phase]
                        state = self.cli("evidence", "--root", str(self.root), "--expected-revision", str(state["revision"]),
                                         "--kind", f"phase:{phase}", "--summary", "phase verified", "--result", "pass")
                        state = self.cli("transition", "--root", str(self.root), "--expected-revision", str(state["revision"]),
                                         "--phase", target, "--status", "active")
                    for name in ("affected", "independent"):
                        state = self.cli("package", "--root", str(self.root), "--expected-revision", str(state["revision"]),
                                         "--data", json.dumps(self.package(name, state["artifact_digest"], [f"{name}.py"])))
                    before = next(p for p in state["work_packages"] if p["id"] == "independent")
                    changed = self.cli("modify", "--root", str(self.root), "--expected-revision", str(state["revision"]),
                                       "--package-id", "affected", "--reason", "User authorized the storage architecture change")
                    self.assertEqual((changed["mode"], changed["phase"]), ("modify", "architecture"))
                    self.assertEqual(next(p for p in changed["work_packages"] if p["id"] == "independent"), before)
                    self.assertEqual(next(p for p in changed["work_packages"] if p["id"] == "affected")["status"], "blocked")
                    self.assertEqual(changed["goal"], state["goal"])
                    self.assertEqual(changed["handoff"]["intent"], "plan")
                    self.cli("modify", "--root", str(self.root), "--expected-revision", str(state["revision"]),
                             "--package-id", "affected", "--reason", "stale retry", ok=False)
                finally:
                    self.root = old_root

    def test_parallel_writers_accept_disjoint_sets_and_reject_conflict(self) -> None:
        initial = self.init("setup")
        first = self.package("writer-a", initial["artifact_digest"], ["src/a.py"])
        second = self.package("writer-b", initial["artifact_digest"], ["src/b.py"])
        conflict = self.package("writer-c", initial["artifact_digest"], ["src"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0", "--data", json.dumps(first),
        )
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "1", "--data", json.dumps(second),
        )
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2", "--data", json.dumps(conflict),
            ok=False,
        )
        self.assertIn("write-set conflict", rejected["error"])

    def test_failed_review_and_user_rejection_remain_open(self) -> None:
        self.init("review")
        state = self.cli(
            "blocker", "--root", str(self.root), "--expected-revision", "0", "--action", "add",
            "--blocker-id", "user-rejected", "--summary", "user rejected the proposed scope",
        )
        self.assertEqual(state["blockers"][0]["id"], "user-rejected")
        approval = self.cli(
            "approve", "--root", str(self.root), "--expected-revision", "1", "--scope", "review",
            "--actor", "integrator", ok=False,
        )
        self.assertIn("blockers", approval["error"])

    def test_content_change_supersedes_review_and_approval(self) -> None:
        self.init("review")
        state = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "0", "--kind", "test",
            "--summary", "checks passed", "--result", "pass",
        )
        details = {
            **{key: state[key] for key in ("task_id", "evidence_epoch", "head_commit")},
            "reviewer": "reviewer", "role": "quality", "scope": ["integrated artifact"],
            "base_revision": state["artifact_digest"], "artifact_digest": state["artifact_digest"],
            "status": "approved", "findings": [], "evidence": ["checks"], "coverage_gaps": [],
            "assumptions": [], "covered_package_ids": [],
        }
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "1", "--kind", "review",
            "--summary", "review passed", "--result", "pass", "--actor", "reviewer",
            "--details", json.dumps(details),
        )
        self.cli(
            "approve", "--root", str(self.root), "--expected-revision", "2", "--scope", "review",
            "--actor", "integrator",
        )
        architecture = self.root / "docs" / "ARCHITECTURE.md"
        architecture.parent.mkdir(parents=True, exist_ok=True)
        architecture.write_text("# Revised architecture\n", encoding="utf-8")
        checkpoint = self.cli("checkpoint", "--root", str(self.root), "--expected-revision", "3")
        self.assertIsNone(checkpoint["approval"])
        self.assertEqual(checkpoint["review"]["status"], "superseded")

    def test_later_failed_acceptance_check_blocks_package_completion(self) -> None:
        initial = self.init("review")
        package = self.package("regression", initial["artifact_digest"], ["src/regression.py"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        for revision, result in ((1, "pass"), (2, "fail")):
            self.cli(
                "evidence", "--root", str(self.root), "--expected-revision", str(revision),
                "--kind", "test", "--summary", f"acceptance {result}", "--result", result,
                "--check-id", "verify regression", "--package-id", "regression", "--actor", "tester",
            )
        package["status"] = "complete"
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "3", "--data", json.dumps(package),
            "--actor", "implementer", ok=False,
        )
        self.assertIn("lacks passing evidence", rejected["error"])

    def test_noncurrent_state_schema_is_rejected(self) -> None:
        state = self.init("setup")
        state["schema_version"] = 2
        path = self.root / ".longtask" / "state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        routed = self.cli("route", "--root", str(self.root))
        self.assertEqual(routed["entry"], "error")
        self.assertIn("unsupported schema_version: 2", routed["reason"])

    def test_degraded_host_without_git_hooks_subagents_or_worktrees(self) -> None:
        state = self.init("continue", "Resume through portable state only")
        self.assertIsNone(state["head_commit"])
        checkpoint = self.cli(
            "checkpoint", "--root", str(self.root), "--expected-revision", "0",
            "--next-action", "Continue sequentially; optional host capabilities are unavailable",
        )
        self.assertEqual(checkpoint["event_revision"], checkpoint["revision"])
        self.assertTrue(self.cli("validate", "--root", str(self.root))["valid"])

    def test_contract_change_reopens_completed_package(self) -> None:
        initial = self.init("review")
        package = self.package("contract", initial["artifact_digest"], ["src/contract.py"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "1", "--kind", "test",
            "--summary", "old contract passed", "--result", "pass", "--check-id", "verify contract",
            "--package-id", "contract", "--actor", "tester",
        )
        package["status"] = "complete"
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        package["objective"] = "Changed contract"
        reopened = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "3", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        self.assertEqual(reopened["work_packages"][0]["status"], "blocked")
        self.assertTrue(reopened["work_packages"][0]["stale_evidence"])

    def test_post_completion_failure_is_durable(self) -> None:
        self.init("review")
        state = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "0", "--kind", "test",
            "--summary", "checks passed", "--result", "pass",
        )
        details = {
            **{key: state[key] for key in ("task_id", "evidence_epoch", "head_commit")},
            "reviewer": "reviewer", "role": "quality", "scope": ["integrated artifact"],
            "base_revision": state["artifact_digest"], "artifact_digest": state["artifact_digest"],
            "status": "approved", "findings": [], "evidence": ["checks"], "coverage_gaps": [],
            "assumptions": [], "covered_package_ids": [],
        }
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "1", "--kind", "review",
            "--summary", "review passed", "--result", "pass", "--actor", "reviewer",
            "--details", json.dumps(details),
        )
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "2", "--kind", "phase:review",
            "--summary", "review exit checks passed", "--result", "pass", "--actor", "reviewer",
        )
        self.cli(
            "approve", "--root", str(self.root), "--expected-revision", "3", "--scope", "review",
            "--actor", "integrator",
        )
        self.cli(
            "transition", "--root", str(self.root), "--expected-revision", "4",
            "--phase", "complete", "--status", "complete",
        )
        failed = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "5", "--kind", "test",
            "--summary", "post-release regression", "--result", "fail", "--actor", "tester",
        )
        self.assertEqual((failed["phase"], failed["review"]["status"]), ("review", "superseded"))
        self.assertNotEqual(self.cli("route", "--root", str(self.root))["entry"], "complete")

    def test_missing_event_history_blocks_recovery(self) -> None:
        self.init("continue")
        events = self.root / ".longtask" / "events.jsonl"
        events.unlink()
        routed = self.cli("route", "--root", str(self.root))
        self.assertEqual(routed["entry"], "error")
        self.assertIn("event log is missing", routed["reason"])

    def test_superseded_prerequisite_does_not_unlock_work(self) -> None:
        initial = self.init("review")
        dependency = self.package("dependency", initial["artifact_digest"], ["src/dependency.py"])
        dependency["status"] = "superseded"
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0", "--data", json.dumps(dependency),
        )
        consumer = self.package("consumer", initial["artifact_digest"], ["src/consumer.py"])
        consumer["dependencies"] = ["dependency"]
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "1", "--data", json.dumps(consumer),
            ok=False,
        )
        self.assertIn("unfinished dependency", rejected["error"])

    def test_high_risk_completion_requires_two_review_roles(self) -> None:
        initial = self.init("review")
        package = self.package("critical", initial["artifact_digest"], ["src/critical.py"])
        package.update(risk_level="high", required_review_roles=["architecture", "security"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "1", "--kind", "test",
            "--summary", "package passed", "--result", "pass", "--check-id", "verify critical",
            "--package-id", "critical", "--actor", "tester",
        )
        package["status"] = "complete"
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2", "--data", json.dumps(package),
            "--actor", "implementer",
        )
        state = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "3", "--kind", "test",
            "--summary", "integration passed", "--result", "pass", "--actor", "tester",
        )

        def review(role: str, reviewer: str) -> str:
            return json.dumps({
                **{key: state[key] for key in ("task_id", "evidence_epoch", "head_commit")},
                "reviewer": reviewer, "role": role, "scope": ["critical package"],
                "base_revision": state["artifact_digest"], "artifact_digest": state["artifact_digest"],
                "status": "approved", "findings": [], "evidence": ["checks"], "coverage_gaps": [],
                "assumptions": [], "covered_package_ids": ["critical"],
            })

        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "4", "--kind", "review",
            "--summary", "architecture review", "--result", "pass", "--actor", "architect",
            "--details", review("architecture", "architect"),
        )
        rejected = self.cli(
            "approve", "--root", str(self.root), "--expected-revision", "5", "--scope", "review",
            "--actor", "integrator", ok=False,
        )
        self.assertIn("security", rejected["error"])
        self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "5", "--kind", "review",
            "--summary", "security review", "--result", "pass", "--actor", "security-reviewer",
            "--details", review("security", "security-reviewer"),
        )
        approved = self.cli(
            "approve", "--root", str(self.root), "--expected-revision", "6", "--scope", "review",
            "--actor", "integrator",
        )
        self.assertEqual(approved["review"]["status"], "approved")

    def test_active_package_keeps_execution_base_and_records_result_evidence(self) -> None:
        initial = self.init("review")
        package = self.package("result", initial["artifact_digest"], ["src/result.py"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(package), "--actor", "result",
        )
        target = self.root / "src" / "result.py"
        target.parent.mkdir()
        target.write_text("result = True\n", encoding="utf-8")
        evidence = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "1", "--kind", "test",
            "--summary", "result verified", "--result", "pass", "--check-id", "verify result",
            "--package-id", "result", "--actor", "tester",
        )
        package["status"] = "complete"
        completed = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2",
            "--data", json.dumps(package), "--actor", "result",
        )
        self.assertEqual(completed["work_packages"][0]["base_revision"], initial["artifact_digest"])
        self.assertEqual(
            completed["work_packages"][0]["evidence"][-1]["artifact_digest"], evidence["artifact_digest"],
        )

    def test_out_of_scope_change_blocks_package_until_it_is_superseded(self) -> None:
        initial = self.init("review")
        package = self.package("scoped", initial["artifact_digest"], ["src/scoped.py"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(package), "--actor", "scoped",
        )
        (self.root / "unowned.py").write_text("unowned = True\n", encoding="utf-8")
        checkpoint = self.cli(
            "checkpoint", "--root", str(self.root), "--expected-revision", "1", "--actor", "integrator",
        )
        self.assertEqual(checkpoint["work_packages"][0]["status"], "blocked")
        self.assertTrue(checkpoint["work_packages"][0]["scope_drift"])
        package["status"] = "complete"
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2",
            "--data", json.dumps(package), "--actor", "scoped", ok=False,
        )
        self.assertIn("must be superseded", rejected["error"])

    def test_parallel_work_cannot_start_after_first_writer_changes_its_scope(self) -> None:
        initial = self.init("review")
        first = self.package("first", initial["artifact_digest"], ["src/first.py"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(first), "--actor", "first",
        )
        target = self.root / "src" / "first.py"
        target.parent.mkdir()
        target.write_text("first = True\n", encoding="utf-8")
        checkpoint = self.cli(
            "checkpoint", "--root", str(self.root), "--expected-revision", "1", "--actor", "first",
        )
        second = self.package("second", checkpoint["artifact_digest"], ["src/second.py"])
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "2",
            "--data", json.dumps(second), "--actor", "second", ok=False,
        )
        self.assertIn("cannot start parallel work", rejected["error"])

    def test_directory_write_sets_reject_descendant_hardlink_aliases(self) -> None:
        first_directory = self.root / "first"
        second_directory = self.root / "second"
        first_directory.mkdir()
        second_directory.mkdir()
        original = first_directory / "shared.txt"
        original.write_text("shared inode\n", encoding="utf-8")
        (second_directory / "alias.txt").hardlink_to(original)
        initial = self.init("review")
        first = self.package("first", initial["artifact_digest"], ["first"])
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(first), "--actor", "first", ok=False,
        )
        self.assertIn("hardlink conflict", rejected["error"])

    def test_directory_write_set_rejects_late_descendant_symlink_escape(self) -> None:
        safe = self.root / "safe"
        safe.mkdir()
        initial = self.init("review")
        package = self.package("safe", initial["artifact_digest"], ["safe"])
        self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(package), "--actor", "safe",
        )
        outside = self.root.parent / f"{self.root.name}-outside-write-target"
        outside.mkdir()
        try:
            (safe / "link").symlink_to(outside, target_is_directory=True)
            rejected = self.cli(
                "checkpoint", "--root", str(self.root), "--expected-revision", "1", ok=False,
            )
            self.assertIn("escapes workspace through symlink", rejected["error"])
        finally:
            (safe / "link").unlink(missing_ok=True)
            outside.rmdir()

    def test_directory_write_set_rejects_file_symlink_aliases(self) -> None:
        safe = self.root / "safe"
        safe.mkdir()
        outside = self.root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (safe / "alias.txt").symlink_to("../outside.txt")
        initial = self.init("review")
        package = self.package("safe", initial["artifact_digest"], ["safe"])
        rejected = self.cli(
            "package", "--root", str(self.root), "--expected-revision", "0",
            "--data", json.dumps(package), "--actor", "safe", ok=False,
        )
        self.assertIn("contains a symlink alias", rejected["error"])

    def test_directory_write_set_rejects_external_hardlink_inode(self) -> None:
        safe = self.root / "safe"
        safe.mkdir()
        outside = self.root.parent / f"{self.root.name}-external-hardlink.txt"
        outside.write_text("outside\n", encoding="utf-8")
        alias = safe / "alias.txt"
        alias.hardlink_to(outside)
        try:
            initial = self.init("review")
            package = self.package("safe", initial["artifact_digest"], ["safe"])
            rejected = self.cli(
                "package", "--root", str(self.root), "--expected-revision", "0",
                "--data", json.dumps(package), "--actor", "safe", ok=False,
            )
            self.assertIn("hardlink conflict", rejected["error"])
        finally:
            alias.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)

    def test_directory_write_set_symlink_loop_returns_structured_rejection(self) -> None:
        safe = self.root / "safe"
        safe.mkdir()
        loop = safe / "loop"
        loop.symlink_to("loop")
        try:
            initial = self.init("review")
            package = self.package("safe", initial["artifact_digest"], ["safe"])
            rejected = self.cli(
                "package", "--root", str(self.root), "--expected-revision", "0",
                "--data", json.dumps(package), "--actor", "safe", ok=False,
            )
            self.assertRegex(rejected["error"],
                             r"(?:cannot inspect active directory write-set safely|active directory write-set contains a symlink alias)")
            persisted = json.loads((self.root / ".longtask/state.json").read_text())
            self.assertEqual((persisted["revision"], persisted["event_revision"]), (0, 0))
            self.assertEqual(persisted["work_packages"], [])
        finally:
            loop.unlink(missing_ok=True)

    def test_review_with_coverage_gap_cannot_be_recorded_as_passing(self) -> None:
        state = self.init("review")
        details = {
            **{key: state[key] for key in ("task_id", "evidence_epoch", "head_commit")},
            "reviewer": "security-reviewer", "role": "security", "scope": ["security boundary"],
            "base_revision": state["artifact_digest"], "artifact_digest": state["artifact_digest"],
            "status": "approved", "findings": [], "evidence": ["static inspection"],
            "coverage_gaps": ["prompt injection was not tested"], "assumptions": [],
            "covered_package_ids": [],
        }
        rejected = self.cli(
            "evidence", "--root", str(self.root), "--expected-revision", "0", "--kind", "review",
            "--summary", "incomplete security review", "--result", "pass",
            "--actor", "security-reviewer", "--details", json.dumps(details), ok=False,
        )
        self.assertIn("coverage_gaps must be empty", rejected["error"])

    def test_review_exit_evidence_is_required_for_completion(self) -> None:
        self.init("review")
        rejected = self.cli(
            "transition", "--root", str(self.root), "--expected-revision", "0",
            "--phase", "complete", "--status", "complete", ok=False,
        )
        self.assertIn("phase:review", rejected["error"])

    def test_event_hardlink_cannot_write_outside_workspace(self) -> None:
        self.init("continue")
        events = self.root / ".longtask" / "events.jsonl"
        outside = Path(self.temporary.name).parent / f"{self.root.name}-events-outside.jsonl"
        events.replace(outside)
        events.hardlink_to(outside)
        before = outside.read_bytes()
        try:
            rejected = self.cli(
                "checkpoint", "--root", str(self.root), "--expected-revision", "0", ok=False,
            )
            self.assertIn("hardlink", rejected["error"])
            self.assertEqual(outside.read_bytes(), before)
        finally:
            events.unlink(missing_ok=True)
            outside.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
