#!/usr/bin/env python3
"""Read-only coverage derived from a selected current L0 acceptance table.

query --root PATH --ref docs/ARCHITECTURE.md#SECTION [--expect-sha256 HASH]
L0 table exact columns: ID | Outcome | Approval | Scope | Exclusion | Contract.
Scope is included/excluded; Approval and (for excluded rows) Exclusion are
current Markdown section refs. Contract is '-' or a module section ref with
optional exact table columns: ID | Purpose | Entry | Conditions | Evidence.
Conditions is '-' or comma-separated stable IDs; Evidence is '-' or an exact
required_inputs evidence ref. Entry is candidate text, never executed.
Package acceptance_checks and global test check_id explicitly map IDs through
[acceptance:AC-ID] markers. No semantic inference, new ledger or completion gate.
--condition ID=OBSERVATION supplies a current cooperative external observation;
a current passing test must contain identical details.external_observations.
Document approval/exclusion claims remain traceable candidates, unauthenticated.
"""
from __future__ import annotations
import argparse
from collections import Counter
import json
import re
import sys

import knowledge_context as kc
import required_inputs as inputs

AC_COLUMNS = ['ID', 'Outcome', 'Approval', 'Scope', 'Exclusion', 'Contract']
RUN_COLUMNS = ['ID', 'Purpose', 'Entry', 'Conditions', 'Evidence']
IDENTITY = re.compile(r'[^\W_][\w.\-]{0,127}')
MARKER = re.compile(r'\[acceptance:([^\]\s]+)\]')
ALIASES = {'验收ID': 'ID', '结果': 'Outcome', '批准来源': 'Approval', '范围': 'Scope',
           '排除来源': 'Exclusion', '合同': 'Contract', '用途': 'Purpose', '入口': 'Entry',
           '条件': 'Conditions', '证据': 'Evidence'}


def table(text, columns, optional=False):
    """Only simple pipe tables outside fenced blocks; refuse ambiguity."""
    lines, fence = [], None
    for raw in text.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', raw)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            lines.append('')
            continue
        if marker:
            fence = marker[1]
            lines.append('')
            continue
        lines.append(raw)
    def cells(line):
        if not line.strip().startswith('|') or not line.strip().endswith('|') or '\\|' in line:
            return None
        return [c.strip() for c in line.strip()[1:-1].split('|')]
    starts = [i for i in range(len(lines) - 1) if cells(lines[i]) is not None
              and [ALIASES.get(c, c) for c in cells(lines[i])] == columns
              and (sep := cells(lines[i + 1])) and len(sep) == len(columns)
              and all(re.fullmatch(r':?-{3,}:?', c) for c in sep)]
    if not starts and optional:
        declared = any((values := cells(line)) and
                       any(ALIASES.get(c, c) in {'Purpose', 'Entry', 'Conditions', 'Evidence'} for c in values)
                       for line in lines)
        if declared:
            inputs.fail('invalid_table', 'Malformed declared runtime table')
        return []
    if len(starts) != 1:
        inputs.fail('ambiguous_table' if starts else 'missing_table',
                    'Select a section containing exactly one table with columns: ' + '|'.join(columns))
    rows = []
    for line in lines[starts[0] + 2:]:
        values = cells(line)
        if values is None:
            break
        if len(values) != len(columns) or any(not c for c in values):
            inputs.fail('invalid_table', 'Each contract table row requires all exact columns; use - for absent values')
        rows.append(dict(zip(columns, values)))
        if len(rows) > inputs.MAX_REFS:
            inputs.fail('input_too_large', f'Contract table exceeds {inputs.MAX_REFS} rows')
    if not rows or any(not IDENTITY.fullmatch(r['ID']) for r in rows):
        inputs.fail('invalid_table', 'Contract table requires nonempty stable IDs')
    if len({r['ID'] for r in rows}) != len(rows):
        inputs.fail('ambiguous_id', 'Duplicate contract IDs are forbidden')
    return rows


def section(session, ref):
    path, anchor = kc.parse_ref(ref)
    if anchor is None:
        inputs.fail('invalid_contract_reference', 'Approval, exclusion and runtime contracts require an exact Markdown section')
    result = inputs.document(session, ref)
    return {'ref': ref, 'binding': result['binding'], 'text': result['text']}


def external_conditions(item, conditions, observations):
    details = item.get('details')
    declared = details.get('external_observations') if isinstance(details, dict) else None
    return bool(isinstance(declared, dict) and all(
        c in observations and isinstance(declared.get(c), str)
        and declared[c].strip() and declared[c] == observations[c] for c in conditions))


def runtime_entries(session, ref, observations):
    source = section(session, ref)
    rows = table(source['text'], RUN_COLUMNS, optional=True)
    results = []
    for row in rows:
        conditions = [] if row['Conditions'] == '-' else [p.strip() for p in row['Conditions'].split(',')]
        if len(set(conditions)) != len(conditions) or any(not IDENTITY.fullmatch(c) for c in conditions):
            inputs.fail('invalid_conditions', 'Conditions require unique comma-separated stable IDs or -')
        result = {**row, 'conditions': conditions, 'executed': False, 'verified': False,
                  'status': 'unverified', 'source_ref': ref}
        if row['Evidence'] != '-':
            if not row['Evidence'].startswith('evidence:'):
                inputs.fail('invalid_evidence_reference', 'Runtime Evidence requires an exact evidence: reference or -')
            try:
                loaded = inputs.evidence(session, row['Evidence'])
                if loaded['value']['kind'] != 'test':
                    inputs.fail('invalid_evidence_kind', 'Runtime verification requires existing test evidence')
                result['evidence'] = loaded['value']
                if loaded['verdict'] == 'fail':
                    result['status'] = 'failed'
                elif not loaded['verified']:
                    result['diagnostic'] = 'non_validation_evidence'
                elif not conditions or external_conditions(loaded['value'], conditions, observations):
                    result.update(status='verified', verified=True)
                else:
                    result['diagnostic'] = 'external_observation_missing_or_changed'
            except inputs.Error as exc:
                if exc.code in {'invalid_state', 'observation_changed', 'read_race'}:
                    raise
                result['diagnostic'] = {'code': exc.code, 'message': str(exc)}
        elif conditions:
            result['diagnostic'] = 'external_conditions_declared_without_current_evidence'
        results.append(result)
    return {'ref': ref, 'binding': source['binding'], 'entries': results,
            'kind': 'runtime_contract' if rows else 'document_contract'}


def query(session, ref, budget=65536, expected=None, observations=None):
    path, anchor = kc.parse_ref(ref)
    if path != 'docs/ARCHITECTURE.md' or anchor is None:
        inputs.fail('invalid_l0_reference', 'Explicitly select docs/ARCHITECTURE.md#SECTION')
    selected = inputs.document(session, ref, expected)
    rows = table(selected['text'], AC_COLUMNS)
    observations = observations or {}
    observation = session.observe(required=False)
    state = observation['state'] if observation else None
    mappings = {row['ID']: [] for row in rows}
    diagnostics = []

    def add_mapping(scope, kind, check, package_id=None):
        ids = MARKER.findall(check)
        if len(ids) != len(set(ids)):
            inputs.fail('ambiguous_mapping', 'Check repeats an acceptance marker: ' + check)
        for identity in ids:
            if identity not in mappings:
                diagnostics.append({'code': 'unknown_acceptance_id', 'id': identity,
                                    'package_id': package_id, 'check_id': check})
                continue
            item, reason = inputs.current_evidence(session, scope, kind, check, package_id)
            mappings[identity].append({'scope': scope, 'package_id': package_id, 'kind': kind,
                                       'check_id': check, 'result': item['result'] if item else None,
                                       'evidence': item, 'diagnostic': reason,
                                       'validation_pass': inputs.evidence_verified(item)})
    if state:
        for package in state['work_packages']:
            if package['status'] != 'superseded':
                for check in package['acceptance_checks']:
                    add_mapping('package', 'test', check, package['id'])
        global_checks = dict.fromkeys(e['check_id'] for e in runtime_global(state) if e['kind'] == 'test')
        for check in global_checks:
            add_mapping('global', 'test', check)
    results, contracts = [], {}
    for row in rows:
        row['Scope'] = {'纳入': 'included', '排除': 'excluded'}.get(row['Scope'], row['Scope'])
        if row['Scope'] not in {'included', 'excluded'}:
            inputs.fail('invalid_scope', 'Scope must be included or excluded; scope changes require an explicit current source')
        approval = section(session, row['Approval'])
        exclusion = None
        if row['Scope'] == 'excluded':
            if row['Exclusion'] == '-':
                inputs.fail('missing_exclusion_source', 'Excluded acceptance needs a current explicit exclusion source')
            exclusion = section(session, row['Exclusion'])
        elif row['Exclusion'] != '-':
            inputs.fail('invalid_scope', 'Included acceptance cannot carry an exclusion source')
        contract = None
        if row['Contract'] != '-':
            if row['Contract'] not in contracts:
                contracts[row['Contract']] = runtime_entries(session, row['Contract'], observations)
            contract = contracts[row['Contract']]
        checks = mappings[row['ID']]
        if row['Scope'] == 'excluded':
            status = 'explicitly_excluded'
        elif not checks:
            status = 'uncovered'
        elif any(c['result'] == 'fail' for c in checks):
            status = 'failed'
        elif any(not c['validation_pass'] for c in checks):
            status = 'unverified'
        else:
            status = 'verified'
        if row['Scope'] != 'excluded' and contract:
            run_statuses = [e['status'] for e in contract['entries']]
            if 'failed' in run_statuses:
                status = 'failed'
            elif status == 'verified' and any(s != 'verified' for s in run_statuses):
                status = 'unverified'
        result = {'ref': row['ID'], 'loaded': True, 'id': row['ID'], 'outcome': row['Outcome'],
                  'scope': row['Scope'], 'status': status, 'checks': checks,
                  'approval_source': {'ref': approval['ref'], 'binding': approval['binding'],
                                      'claim': 'candidate_approval_not_authenticated'},
                  'exclusion_source': ({'ref': exclusion['ref'], 'binding': exclusion['binding'],
                                         'claim': 'candidate_scope_change_not_authenticated'} if exclusion else None),
                  'runtime_contract': contract}
        results.append(result)
    counts = dict(Counter(r['status'] for r in results))
    result = inputs.bounded_output({
        'command': 'query', 'root': session.workspace.root, 'trust': inputs.TRUST,
        'binding': session.binding(), 'l0': {'ref': ref, 'binding': selected['binding']},
        'state_available': state is not None,
        'diagnostics': diagnostics, 'status_counts': counts,
        'all_included_verified': bool(state) and all(r['status'] in {'verified', 'explicitly_excluded'} for r in results),
        'assurance': 'derived_cooperative_evidence_not_completion_or_authorization',
    }, results, budget, 'acceptance')
    if not result['complete']:
        result['all_included_verified'] = False
        # Refresh byte count after changing a boolean length.
        for _ in range(8):
            size = len(kc.encoded_json(result)) + 1
            if size == result['budget']['output_bytes']:
                break
            result['budget']['output_bytes'] = size
        if result['budget']['output_bytes'] > budget:
            inputs.fail('output_budget_too_small', 'Coverage metadata exceeds output budget')
    session.verify()
    return result


def runtime_global(state):
    return inputs.runtime.global_evidence_records(state['validation_evidence'], state['work_packages'])


def condition_args(values):
    result = {}
    for value in values:
        identity, sep, observation = value.partition('=')
        if not sep or not IDENTITY.fullmatch(identity) or not observation.strip() or identity in result:
            inputs.fail('invalid_conditions', 'Use unique --condition ID=NONEMPTY_CURRENT_OBSERVATION')
        if len(observation.encode()) > kc.MAX_REF_BYTES:
            inputs.fail('input_too_large', 'External observation exceeds input limit')
        result[identity] = observation
    if len(result) > inputs.MAX_REFS:
        inputs.fail('input_too_large', 'Too many external observations')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    report = commands.add_parser('query')
    report.add_argument('--root', required=True)
    report.add_argument('--ref', required=True)
    report.add_argument('--expect-sha256')
    report.add_argument('--condition', action='append', default=[])
    report.add_argument('--budget-bytes', type=kc.bounded_int(kc.MAX_READ_BYTES), default=65536)
    args = parser.parse_args(argv)
    session = None
    try:
        if args.expect_sha256 and not re.fullmatch('[0-9a-f]{64}', args.expect_sha256):
            inputs.fail('invalid_binding', 'Expected a lowercase SHA-256 file digest')
        session = inputs.Session(args.root)
        result = query(session, args.ref, args.budget_bytes, args.expect_sha256, condition_args(args.condition))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (inputs.Error, OSError, inputs.runtime.StateError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'read_failed'), 'message': str(exc)},
                          'trust': inputs.TRUST}, ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        if session:
            session.close()


if __name__ == '__main__':
    sys.exit(main())
