#!/usr/bin/env python3
"""Check setup cases or audit explicitly supplied, independently graded evidence.

No model execution, automatic semantic grader, result writer or evidence rebinding.
Byte checks establish internal consistency, not authentic host/model identity.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'evals/setup_cases.json'
DIMENSIONS = {f'SU{i:02}' for i in range(1, 13)}
STATUSES = {'pass', 'fail', 'unable_to_verify'}
CASE_IDS = {'broad-service', 'fixed-stack', 'fullstack-workflow', 'small-cli',
            'visual-reference', 'visual-no-reference', 'missing-parameters',
            'multi-turn-scope', 'planning-boundary', 'independent-consumer'}
BINDINGS = ('evals/setup_cases.json', 'scripts/run_setup_evals.py',
            'scripts/run_memory_evals.py', 'scripts/collect_host_trace.py',
            'references/评测协议.md')


def helper():
    name = '_longtask_setup_evidence_safety'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/run_memory_evals.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def canonical(value):
    return helper().canonical(value)


def bindings():
    return {name: helper().digest(ROOT / name) for name in BINDINGS}


def load_object(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object: ' + str(path))
    return value


def validate_cases(data):
    errors = []
    if data.get('schema_version') != 1 or data.get('suite') != 'setup_document_quality':
        errors.append('Unsupported setup corpus identity')
    dimensions = data.get('dimensions', {})
    if not isinstance(dimensions, dict) or set(dimensions) != DIMENSIONS:
        errors.append('The complete SU01–SU12 dimension contract is required')
    else:
        for key, dimension in dimensions.items():
            if not isinstance(dimension, dict) or any(not isinstance(dimension.get(field), str)
                    or not dimension[field].strip() for field in ('name', 'question', 'pass_criteria', 'failure_signals')):
                errors.append('Incomplete semantic rubric: ' + key)
    cases = data.get('cases', [])
    if not isinstance(cases, list) or not cases:
        return errors + ['Setup cases must be a non-empty array']
    seen, coverage = set(), set()
    for case in cases:
        if not isinstance(case, dict):
            errors.append('Invalid case object')
            continue
        cid = case.get('id')
        if not isinstance(cid, str) or not cid or cid in seen:
            errors.append('Missing or duplicate case ID')
        else:
            seen.add(cid)
        selected = case.get('dimensions')
        if not isinstance(selected, list) or not selected or any(not isinstance(d, str) for d in selected):
            errors.append(f'{cid}: missing dimensions')
        elif len(selected) != len(set(selected)) or not set(selected) <= DIMENSIONS:
            errors.append(f'{cid}: duplicate or unknown dimensions')
        else:
            coverage.update(selected)
        for field in ('write_scope', 'must_observe', 'failure_signals'):
            values = case.get(field)
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                errors.append(f'{cid}: missing {field}')
        phases = case.get('phases')
        phase_ids = set()
        if not isinstance(phases, list) or not phases:
            errors.append(f'{cid}: missing phases')
            phases = []
        for phase in phases:
            if (not isinstance(phase, dict) or not isinstance(phase.get('id'), str)
                    or not phase.get('id') or phase['id'] in phase_ids
                    or phase.get('role') not in ('author', 'independent_consumer')
                    or not isinstance(phase.get('prompt'), str) or not phase['prompt'].strip()):
                errors.append(f'{cid}: invalid phase')
            else:
                phase_ids.add(phase['id'])
        roles = [phase.get('role') for phase in phases if isinstance(phase, dict)]
        expected_roles = {'multi-turn-scope': ['author', 'author'],
                          'independent-consumer': ['author', 'independent_consumer']}.get(cid, ['author'])
        if roles != expected_roles:
            errors.append(f'{cid}: required scenario role/phase boundary is missing')
        files = case.get('fixture_files')
        if not isinstance(files, dict) or any(not isinstance(name, str) or not name
                or Path(name).is_absolute() or '..' in Path(name).parts
                or not isinstance(value, str) for name, value in files.items()):
            errors.append(f'{cid}: invalid fixture files')
    if seen != CASE_IDS:
        errors.append('Required representative setup scenarios are missing or unexpected')
    if coverage != DIMENSIONS:
        errors.append('Cases do not cover all SU01–SU12 dimensions')
    return errors


def artifact_errors(value, private):
    if (not isinstance(value, dict) or set(value) != {'path', 'sha256'}
            or not isinstance(value['path'], str) or not Path(value['path']).is_absolute()
            or not re.fullmatch(r'[0-9a-f]{64}', str(value['sha256']))):
        return ['Invalid evidence artifact descriptor']
    if private:
        try:
            if helper().digest(value['path']) != value['sha256']:
                return ['Evidence bytes differ: ' + value['path']]
        except (OSError, ValueError) as exc:
            return ['Evidence unavailable: ' + str(exc)]
    return []


def aggregate(statuses):
    if 'fail' in statuses:
        return 'fail'
    return 'pass' if statuses and all(status == 'pass' for status in statuses) else 'unable_to_verify'


def verify(data, corpus, source_root=ROOT, private=False):
    """Report every case/dimension. Unknown, missing and failed samples stay visible."""
    errors = validate_cases(corpus)
    expected_inputs = bindings()
    expected_source = helper().source_manifest(Path(source_root))
    if data.get('schema_version') != 1 or data.get('suite') != 'setup_document_quality':
        errors.append('Unsupported setup results identity')
    if data.get('inputs_sha256') != expected_inputs:
        errors.append('Stale or incomplete scoring input bindings')
    if data.get('evaluated_source_sha256') != expected_source or not expected_source:
        errors.append('Evaluated source differs from selected source root')
    samples = data.get('samples')
    if not isinstance(samples, list):
        return errors + ['Samples must be an array'], {}
    cases = {case['id']: case for case in corpus['cases']}
    observed = {cid: [] for cid in cases}
    sample_ids, trace_owners, context_owners = set(), {}, {}
    reported_failures = []
    for sample in samples:
        local_errors = []
        if not isinstance(sample, dict) or sample.get('case_id') not in cases:
            errors.append('Unknown sample case')
            continue
        case, cid = cases[sample['case_id']], sample['case_id']
        sid = sample.get('id')
        if not isinstance(sid, str) or not sid or sid in sample_ids:
            local_errors.append('Duplicate or missing sample identity')
        elif sid:
            sample_ids.add(sid)
        if sample.get('case_sha256') != canonical(case) or sample.get('source_sha256') != canonical(expected_source):
            local_errors.append('Sample target/source binding mismatch')
        if sample.get('inputs_sha256') != expected_inputs:
            local_errors.append('Sample scoring inputs mismatch')
        if (not isinstance(sample.get('harness'), str) or not sample['harness'].strip()
                or 'model' not in sample or (sample['model'] is not None and not isinstance(sample['model'], str))
                or sample.get('collection_status') not in ('collected', 'fail', 'unable_to_verify')):
            local_errors.append('Missing collection provenance/status')
        stages = sample.get('stages', [])
        phase_ids = [p['id'] for p in case['phases']]
        if not isinstance(stages, list):
            local_errors.append('Invalid stages')
            stages = []
        if len(stages) > len(phase_ids) or [s.get('phase_id') for s in stages if isinstance(s, dict)] != phase_ids[:len(stages)]:
            local_errors.append('Stages must preserve case phase order')
        authors, author_contexts, output_actors = set(), set(), set()
        for stage, phase in zip(stages, case['phases']):
            if not isinstance(stage, dict) or stage.get('status') not in ('collected', 'fail', 'unable_to_verify'):
                local_errors.append('Invalid stage')
                continue
            actor, context = stage.get('actor_id'), stage.get('context_id')
            if isinstance(actor, str) and actor:
                output_actors.add(actor)
            if not isinstance(actor, str) or not actor or not isinstance(context, str) or not context:
                local_errors.append('Stage lacks actual actor/context identity')
            elif phase['role'] == 'author':
                authors.add(actor)
                author_contexts.add(context)
            elif actor in authors or context in author_contexts:
                local_errors.append('Independent consumer reuses author actor/context')
            if isinstance(context, str):
                owner = context_owners.setdefault(context, sid)
                if owner != sid:
                    local_errors.append('Independent samples reuse context identity')
            trace = stage.get('trace')
            local_errors.extend(artifact_errors(trace, private))
            if isinstance(trace, dict) and isinstance(trace.get('sha256'), str):
                owner = trace_owners.setdefault(trace['sha256'], sid)
                if owner != sid and stage.get('status') == 'collected':
                    local_errors.append('Independent samples reuse trace bytes')
        complete = len(stages) == len(phase_ids) and all(isinstance(s, dict) and s.get('status') == 'collected' for s in stages)
        if sample.get('collection_status') == 'collected' and not complete:
            local_errors.append('Collected sample lacks complete collected phases')
        documents = sample.get('documents', [])
        if not isinstance(documents, list):
            local_errors.append('Invalid document artifacts')
            documents = []
        for document in documents:
            local_errors.extend(artifact_errors(document, private))
        payload = {key: value for key, value in sample.items() if key != 'judgments'}
        judgments, dimension_statuses = sample.get('judgments', []), {d: [] for d in case['dimensions']}
        if not isinstance(judgments, list):
            local_errors.append('Invalid judgments')
            judgments = []
        for judgment in judgments:
            if not isinstance(judgment, dict):
                local_errors.append('Invalid semantic judgment')
                continue
            if judgment.get('sample_sha256') != canonical(payload):
                local_errors.append('Semantic judgment not bound to this sample')
            reviewer = judgment.get('reviewer')
            if not isinstance(reviewer, str) or not reviewer or reviewer in output_actors or judgment.get('independent') is not True:
                local_errors.append('Semantic reviewer must be independent of all output authors, including consumers')
            grading = judgment.get('dimensions')
            if not isinstance(grading, dict) or set(grading) != set(case['dimensions']):
                local_errors.append('Every applicable dimension needs an explicit judgment')
                continue
            for dimension, grade in grading.items():
                if (not isinstance(grade, dict) or grade.get('status') not in STATUSES
                        or not isinstance(grade.get('reason'), str) or not grade['reason'].strip()
                        or not isinstance(grade.get('evidence'), list) or not grade['evidence']
                        or any(not isinstance(e, str) or not e.strip() for e in grade['evidence'])):
                    local_errors.append('Dimension lacks semantic reason/evidence: ' + dimension)
                else:
                    dimension_statuses[dimension].append(grade['status'])
            record = judgment.get('record')
            local_errors.extend(artifact_errors(record, private))
            if private and isinstance(record, dict) and not artifact_errors(record, True):
                content, snapshot = helper().read_artifact(record['path'])
                if content is None or not snapshot['safe'] or snapshot['sha256'] != record['sha256']:
                    local_errors.append('Independent judgment changed or cannot be safely read')
                elif json.loads(content) != {key: value for key, value in judgment.items() if key != 'record'}:
                    local_errors.append('Judgment differs from saved independent record')
        statuses = {d: aggregate(values) for d, values in dimension_statuses.items()}
        for dimension, status in statuses.items():
            if status == 'fail':
                reported_failures.append({'sample_id': sid, 'case_id': cid,
                                          'dimension': dimension, 'evidence_valid': not bool(local_errors)})
        if local_errors:
            statuses = {d: 'unable_to_verify' for d in statuses}
        elif not complete or not documents or sample.get('collection_status') != 'collected':
            statuses = {d: 'fail' if status == 'fail' else 'unable_to_verify'
                        for d, status in statuses.items()}
        errors.extend(f'{cid}/{sid}: {error}' for error in local_errors)
        observed[cid].append(statuses)
    case_status = {cid: {d: aggregate([s[d] for s in observed[cid]]) for d in case['dimensions']}
                   for cid, case in cases.items()}
    dimensions = {d: aggregate([grades[d] for grades in case_status.values() if d in grades]) for d in DIMENSIONS}
    return errors, {'cases': case_status, 'dimensions': dict(sorted(dimensions.items())),
                    'behavior_status': 'unable_to_verify' if errors else aggregate(list(dimensions.values())),
                    'sample_count': len(samples), 'evidence_bytes_checked': private,
                    'reported_failures': reported_failures,
                    'host_and_reviewer_authenticity': 'independent_trace_verification_required',
                    'release_gate': 'not_evaluated', 'performance_improvement': 'unknown'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-cases', action='store_true', help='check source corpus only')
    parser.add_argument('--results', type=Path, help='explicit local ignored result file')
    parser.add_argument('--source-root', type=Path, default=ROOT, help='explicit evaluated source (default current repository)')
    parser.add_argument('--verify-private', action='store_true', help='also verify actual evidence bytes')
    args = parser.parse_args()
    try:
        corpus = load_object(CORPUS)
        errors, report = validate_cases(corpus), {'behavior_status': 'not_evaluated'}
        if args.check_cases and args.results:
            raise ValueError('--check-cases cannot consume results')
        if args.verify_private and not args.results:
            raise ValueError('--verify-private requires --results')
        if args.results and not errors:
            errors, report = verify(load_object(args.results), corpus, args.source_root, args.verify_private)
        print(json.dumps({'errors': errors, **report}, ensure_ascii=False, indent=2))
        return 1 if errors or (args.results and report.get('behavior_status') != 'pass') else 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'errors': [str(exc)], 'behavior_status': 'unable_to_verify'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
