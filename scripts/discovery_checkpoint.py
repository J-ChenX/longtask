#!/usr/bin/env python3
"""Temporary discoveries in the existing v3 singleton checkpoint.

Records are candidate data, never authorization or acceptance. The v3 evidence
CLI requires pass/fail: kind=discovery/result=pass means successful recording,
NOT successful testing; actual failed checks must use the original evidence CLI.
An optional explicit new handoff is a SECOND CAS operation, not a transaction.
No old frame is resigned. finish removes these records with the checkpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

import longtask_state as runtime

MAX_INPUT_BYTES = 16384
MAX_OUTPUT_BYTES = 1048576
TRUST = {"classification": "candidate_data", "grants_authorization": False,
         "grants_acceptance": False}
FIELDS = {"event_id", "outcome", "problem", "attempts", "observations", "conclusion",
          "affected_contracts", "conditions", "retry_when", "next_action"}


class DiscoveryError(Exception):
    pass


class UncertainMutation(DiscoveryError):
    """A subprocess may have published state before its result was lost."""
    pass


def object_json(raw, label):
    if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
        raise DiscoveryError(f"{label} exceeds {MAX_INPUT_BYTES} UTF-8 bytes")
    try:
        value = json.loads(raw, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except (ValueError, RecursionError) as exc:
        raise DiscoveryError(f"{label} must be valid JSON") from exc
    if not isinstance(value, dict):
        raise DiscoveryError(f"{label} must be an object")
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 16 or count > 2048:
            raise DiscoveryError(f"{label} exceeds nesting/node limit")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise DiscoveryError(f"{label} contains invalid UTF-8 text") from exc
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def nonempty_text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 4096 and "\x00" not in value


def validate_payload(value):
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise DiscoveryError("Discovery fields must be exactly: " + ", ".join(sorted(FIELDS)))
    if not isinstance(value["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", value["event_id"]):
        raise DiscoveryError("event_id must be a stable bounded ASCII identifier")
    if not isinstance(value["outcome"], str) or value["outcome"] not in {"failure", "excluded_option", "root_cause", "fact", "hypothesis"}:
        raise DiscoveryError("Invalid discovery outcome")
    for field in ("problem", "conditions", "retry_when", "next_action"):
        if not nonempty_text(value[field]):
            raise DiscoveryError(f"{field} must be nonempty bounded text")
    for field in ("attempts", "observations", "affected_contracts"):
        if not isinstance(value[field], list) or not 1 <= len(value[field]) <= 32 or not all(nonempty_text(v) for v in value[field]):
            raise DiscoveryError(f"{field} must contain 1..32 bounded strings")
    conclusion = value["conclusion"]
    if not isinstance(conclusion, dict) or set(conclusion) != {"source", "statement"} or not isinstance(conclusion["source"], str) or conclusion["source"] not in {"approved", "observed", "inferred", "disputed"} or not nonempty_text(conclusion["statement"]):
        raise DiscoveryError("conclusion requires source=approved|observed|inferred|disputed and a statement")
    if value["outcome"] == "hypothesis" and conclusion["source"] != "inferred":
        raise DiscoveryError("A hypothesis must remain inferred")
    if len(canonical(value).encode("utf-8")) > MAX_INPUT_BYTES:
        raise DiscoveryError("Discovery exceeds input budget")
    return value


def observe(root):
    diagnosis, observation = runtime.read_query(argparse.Namespace(root=root, resume_choice="inspect"))
    if diagnosis["route"].get("entry") == "error":
        raise DiscoveryError(diagnosis["route"]["reason"])
    return diagnosis, observation


def binding(observation):
    if not observation:
        return None
    state = observation["state"]
    return {"task_id": state["task_id"], "revision": state["revision"],
            "artifact_digest": observation["manifest"]["artifact_digest"],
            "head_commit": observation["head"], "evidence_epoch": state["evidence_epoch"]}


def records(observation, environment=None):
    result = []
    if not observation:
        return result
    current = binding(observation)
    for evidence in observation["state"]["validation_evidence"]:
        details = evidence.get("details")
        if evidence["kind"] != "discovery" or not isinstance(details, dict) or details.get("record_semantics") != "non_validation":
            continue
        payload = details.get("discovery")
        try:
            validate_payload(payload)
        except (DiscoveryError, TypeError):
            continue
        reasons = []
        if evidence["artifact_digest"] != current["artifact_digest"]:
            reasons.append("artifact_changed")
        if evidence["evidence_epoch"] != current["evidence_epoch"]:
            reasons.append("epoch_changed")
        if details.get("observed_head") != current["head_commit"]:
            reasons.append("head_changed")
        if environment is None:
            reasons.append("environment_not_rechecked")
        elif details.get("environment_sha256") != digest(environment):
            reasons.append("environment_changed")
        result.append({"event_id": payload["event_id"], "discovery": payload, "environment": details.get("environment"),
                       "binding": {"artifact_digest": evidence["artifact_digest"],
                                   "evidence_epoch": evidence["evidence_epoch"],
                                   "head_commit": details.get("observed_head"),
                                   "environment_sha256": details.get("environment_sha256")},
                       "recorded_at": evidence["recorded_at"], "actor": evidence["actor"],
                       "requires_recheck": bool(reasons), "recheck_reasons": reasons,
                       "retry_policy": "Recheck conditions and retry_when; prior negative evidence never permanently forbids retry."})
    return result


def query(root, environment=None, event_id=None, offset=0, limit=20):
    if event_id is not None and (not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}", event_id)):
        raise DiscoveryError("event_id must be a stable bounded ASCII identifier")
    _, observation = observe(root)
    all_records = records(observation, environment)
    # One current record per identity; prior attempts remain in the singleton evidence history.
    latest = {record["event_id"]: record for record in all_records}
    items = sorted(latest.values(), key=lambda item: item["event_id"])
    if event_id is not None:
        items = [item for item in items if item["event_id"] == event_id]
    total = len(items)
    # Extend the existing read_query race check through projection, without a write/lock.
    _, final_observation = observe(root)
    if binding(observation) != binding(final_observation):
        raise DiscoveryError("Discovery query changed before output; query again before acting")
    return {"command": "read" if event_id else "list", "read_only": True,
            "trust": TRUST, "current_task": bool(observation), "binding": binding(observation),
            "records": items[offset:offset + limit], "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
            "complete": offset == 0 and total <= limit,
            "history_source": "validation_evidence(kind=discovery) in the current .longtask/state.json"}


def cli_mutation(command, root, task_id, revision, actor, extra):
    args = [sys.executable, str(Path(runtime.__file__)), command, "--root", root,
            "--expected-task-id", task_id, "--expected-revision", str(revision),
            "--actor", actor, "--output", "summary", *extra]
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise UncertainMutation("Runtime result unavailable; inspect checkpoint before retrying") from exc
    raw = completed.stdout if completed.returncode == 0 else completed.stderr
    if len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise UncertainMutation("Runtime response exceeds output limit; inspect checkpoint before retrying")
    try:
        output = json.loads(raw)
    except ValueError as exc:
        raise UncertainMutation("Uncertain runtime response; inspect checkpoint before retrying") from exc
    if completed.returncode:
        message = output.get("error", "Runtime mutation failed")
        if "state operation failed safely:" in message:
            raise UncertainMutation(message)
        raise DiscoveryError(message)
    return output


def record(root, task_id, revision, actor, payload, environment, handoff_data=None):
    validate_payload(payload)
    if not isinstance(environment, dict) or not environment:
        raise DiscoveryError("Recording requires an explicit nonempty environment object; values are candidate data")
    if len(canonical(environment).encode("utf-8")) > MAX_INPUT_BYTES:
        raise DiscoveryError("Environment exceeds input budget")
    if handoff_data is not None:
        object_json(handoff_data, "new handoff input")
    _, observation = observe(root)
    current = binding(observation)
    if not current or (current["task_id"], current["revision"]) != (task_id, revision):
        raise DiscoveryError("Current task_id/revision must match both CAS inputs")
    previous = next((r for r in reversed(records(observation, environment)) if r["event_id"] == payload["event_id"]), None)
    unchanged = previous is not None and previous["discovery"] == payload and not previous["requires_recheck"]
    saved = {**current}
    evidence_saved = False
    if not unchanged:
        details = {"record_semantics": "non_validation", "discovery": payload,
                   "environment_sha256": digest(environment), "environment": environment, "observed_head": current["head_commit"]}
        saved = cli_mutation("evidence", root, task_id, revision, actor,
                             ["--kind", "discovery", "--result", "pass", "--check-id", "discovery:" + payload["event_id"],
                              "--summary", "Discovery record " + payload["event_id"], "--details", canonical(details)])
        evidence_saved = True
    result = {"command": "record", "trust": TRUST, "transactional": False,
              "evidence_saved": evidence_saved, "existing_record_reused": unchanged,
              "handoff_requested": handoff_data is not None, "handoff_saved": False,
              "binding": {k: saved[k] for k in current}, "warnings": saved.get("_runtime_warnings", [])}
    if handoff_data is not None:
        try:
            handoff = cli_mutation("checkpoint", root, task_id, saved["revision"], actor,
                                   ["--handoff-data", handoff_data])
            result.update(handoff_saved=True, binding={k: handoff[k] for k in current},
                          warnings=result["warnings"] + handoff.get("_runtime_warnings", []))
        except (DiscoveryError, subprocess.TimeoutExpired, OSError) as exc:
            uncertain = isinstance(exc, (UncertainMutation, subprocess.TimeoutExpired, OSError))
            result.update(handoff_saved=None if uncertain else False,
                          error={"code": "handoff_outcome_uncertain" if uncertain else "handoff_not_saved", "message": str(exc)},
                          next_action="Read current state and retry only a newly checked explicit handoff with latest CAS.")
    return result


def bounded_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected integer 1..100") from exc
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Expected integer 1..100")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "list", "read"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--environment", help="Explicit observed environment JSON; absent query requires recheck")
        if name == "record":
            p.add_argument("--expected-task-id", required=True)
            p.add_argument("--expected-revision", type=int, required=True)
            p.add_argument("--actor", default="agent")
            p.add_argument("--data", required=True)
            p.add_argument("--handoff-data", help="Explicit NEW frame input, saved by a separate checkpoint CAS")
        elif name == "read":
            p.add_argument("--event-id", required=True)
        else:
            p.add_argument("--offset", type=int, default=0)
            p.add_argument("--limit", type=bounded_int, default=20)
    args = parser.parse_args(argv)
    try:
        environment = object_json(args.environment, "environment") if args.environment else None
        if args.command == "record":
            result = record(args.root, args.expected_task_id, args.expected_revision, args.actor,
                            object_json(args.data, "discovery"), environment, args.handoff_data)
        else:
            offset = getattr(args, "offset", 0)
            if offset < 0:
                raise DiscoveryError("offset must be nonnegative")
            result = query(args.root, environment, getattr(args, "event_id", None), offset, getattr(args, "limit", 1))
        output = canonical(result)
        if len(output.encode("utf-8")) + 1 > MAX_OUTPUT_BYTES:
            raise DiscoveryError("Output exceeds budget; use read or a smaller --limit")
        print(output)
        return 2 if "error" in result else 0
    except (DiscoveryError, runtime.StateError, subprocess.TimeoutExpired, OSError, UnicodeError, RecursionError) as exc:
        print(canonical({"error": str(exc), "trust": TRUST,
                         "evidence_saved": None if isinstance(exc, UncertainMutation) else False,
                         "next_action": "Inspect current checkpoint before retrying an uncertain write."}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
