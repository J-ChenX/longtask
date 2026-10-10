#!/usr/bin/env python3
"""Resolve selected v3 inputs without executing text or granting authorization.

Refs: Markdown path[#section], state:field or state:/JSON/pointer,
work_package:ID, code:relative.py#Qualified.symbol, and
 evidence:global:KIND:CHECK, or
 evidence:package:ID:KIND:CHECK. Evidence components are percent encoded.
thread:HOST/ID#QUERY produces an unloaded locator for host retrieval, never history.
Only Python class/function AST symbols are supported. --expect-sha256 REF=HASH
binds document/code selections, not approval. --from-handoff requires a fresh
frame. Existing validation_evidence:CHECK shorthand requires a unique scope/kind.
All budgets count the entire UTF-8 JSON output including its newline.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import unquote

import knowledge_context as kc
import longtask_state as runtime

MAX_REFS = 128
TRUST = kc.TRUST
Error = kc.KnowledgeError
fail = kc.fail


def safe_path(value):
    if (not value or len(value.encode()) > kc.MAX_REF_BYTES or value.startswith('/')
            or '\\' in value or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or any(p in {'', '.', '..'} for p in value.split('/'))
            or value.split('/')[0] == '.git'):
        fail('invalid_path', 'Expected a bounded repository-relative path without traversal')
    return value


def safe_read(workspace, path):
    """Share Workspace no-follow descriptors, limits, snapshots and final verify."""
    safe_path(path)
    prior = workspace.files.get(path)
    fd = workspace.open_path(path)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            fail('unsafe_path', f'Input is not a regular file: {path}')
        if before.st_size > kc.MAX_FILE_BYTES:
            fail('input_too_large', f'Input exceeds {kc.MAX_FILE_BYTES} bytes: {path}')
        data = workspace.read_bytes(fd, path)
        os.lseek(fd, 0, os.SEEK_SET)
        if (kc.fingerprint(before) != kc.fingerprint(os.fstat(fd))
                or len(data) != before.st_size or data != workspace.read_bytes(fd, path)):
            fail('read_race', f'Input changed while reading: {path}')
        if prior and (prior[0] != kc.fingerprint(before) or prior[1] != kc.sha256(data)):
            fail('read_race', f'Input changed between selected reads: {path}')
        workspace.remember(workspace.files, path, kc.fingerprint(before), kc.sha256(data), fd)
        workspace.total_bytes += len(data)
        if workspace.total_bytes > kc.MAX_SCAN_BYTES:
            fail('input_too_large', 'Selected inputs exceed the aggregate read limit')
        return data
    finally:
        os.close(fd)


def decode(value):
    if not value or re.search(r'%(?![0-9a-fA-F]{2})', value):
        fail('invalid_reference', 'Empty or malformed reference component')
    try:
        value = unquote(value, errors='strict')
    except UnicodeError:
        fail('invalid_reference', 'Reference component is not UTF-8')
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        fail('invalid_reference', 'Control character in reference component')
    return value


class Session:
    """Pin selected inputs and reobserve state/version immediately before output."""
    def __init__(self, root):
        self.workspace = kc.Workspace(root)
        self.observation = None
        self.diagnostics = None
        self.state_observed = False

    def close(self):
        self.workspace.close()

    def observe(self, required=True):
        if not self.state_observed:
            self.state_observed = True
            path = '.longtask/state.json'
            # Pin checkpoint/event bytes before the runtime observation. An absent
            # file is also remembered, so an init during this query fails closed.
            if self.workspace.exists(path):
                safe_read(self.workspace, path)
            if self.workspace.exists('.longtask/events.jsonl'):
                safe_read(self.workspace, '.longtask/events.jsonl')
            args = argparse.Namespace(root=self.workspace.root, resume_choice='inspect')
            diagnostics, observation = runtime.read_query(args)
            if diagnostics['route'].get('entry') == 'error':
                fail('invalid_state', diagnostics['route']['reason'])
            self.diagnostics, self.observation = diagnostics, observation or None
        if required and self.observation is None:
            fail('missing_state', 'No current v3 checkpoint; this input cannot be loaded')
        return self.observation

    def binding(self):
        if not self.observation:
            return None
        state = self.observation['state']
        return {**{k: state[k] for k in ('task_id', 'revision', 'evidence_epoch')},
                'artifact_digest': self.observation['manifest']['artifact_digest'],
                'head_commit': self.observation['head']}

    def verify(self):
        self.workspace.verify()
        if self.state_observed:
            diagnostics, current = runtime.read_query(argparse.Namespace(
                root=self.workspace.root, resume_choice='inspect'))
            if diagnostics['route'].get('entry') == 'error':
                fail('observation_changed', diagnostics['route']['reason'])
            if (current or None) != self.observation:
                fail('observation_changed', 'Checkpoint, workspace or HEAD changed; repeat the query')
        self.workspace.verify()


def document(session, ref, expected=None):
    path, _ = kc.parse_ref(ref)
    prior = session.workspace.files.get(path)
    result = kc.read(session.workspace, ref, kc.MAX_FILE_BYTES, expected)
    current = session.workspace.files[path]
    if prior and (prior[0] != current[0] or prior[1] != current[1]):
        fail('read_race', f'Knowledge changed between selected reads: {path}')
    if not result['complete']:
        fail('input_too_large', 'Selected document could not be completely loaded')
    return {'kind': 'knowledge', 'text': result['text'], 'binding': result['binding'],
            'path': result['path']}


def symbol(session, ref, expected=None):
    value = ref.removeprefix('code:')
    if value.count('#') != 1:
        fail('invalid_reference', 'Code reference requires path#Qualified.symbol')
    path, name = map(decode, value.split('#'))
    safe_path(path)
    if path.split('/')[0] == '.longtask':
        fail('invalid_path', 'Checkpoint files are not source code')
    if not path.endswith('.py'):
        fail('unsupported_language', 'Only Python AST class/function symbols are supported')
    if not all(part.isidentifier() for part in name.split('.')):
        fail('invalid_symbol', 'Expected a dot-qualified Python class/function symbol')
    data = safe_read(session.workspace, path)
    digest = kc.sha256(data)
    if expected is not None and expected != digest:
        fail('stale_binding', f'Source digest changed: {path}')
    try:
        text = data.decode('utf-8')
        tree = ast.parse(text, filename=path)
    except (UnicodeError, SyntaxError, ValueError, RecursionError):
        fail('invalid_source', f'Source is not parseable UTF-8 Python: {path}')
    matches = []

    def walk(node, parents=()):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                parts = (*parents, child.name)
                if '.'.join(parts) == name:
                    matches.append(child)
                walk(child, parts)
            else:
                walk(child, parents)
    try:
        walk(tree)
    except RecursionError:
        fail('input_too_large', 'Python AST nesting exceeds the reader limit')
    if not matches:
        fail('missing_symbol', f'Symbol no longer resolves: {ref}; repair the consumer reference')
    if len(matches) != 1:
        fail('ambiguous_symbol', f'Multiple declarations resolve to {ref}')
    node = matches[0]
    lines = text.splitlines(keepends=True)
    start_line = min([node.lineno, *[d.lineno for d in node.decorator_list]])
    selected = ''.join(lines[start_line - 1:node.end_lineno])
    return {'kind': 'code_symbol', 'path': path, 'symbol': name, 'text': selected,
            'binding': {'file_sha256': digest, 'selected_sha256': kc.sha256(selected.encode()),
                        'line_start': start_line, 'line_end': node.end_lineno},
            'assurance': 'observed_source_only'}


def state_field(session, ref):
    state = session.observe()['state']
    key = ref.removeprefix('state:')
    if key == 'current':
        return {'kind': 'state', 'value': state, 'binding': session.binding()}
    if key.startswith('/'):
        parts = key[1:].split('/')
        if any(re.search(r'~(?![01])', p) for p in parts):
            fail('invalid_reference', 'Invalid JSON pointer escape')
        parts = [p.replace('~1', '/').replace('~0', '~') for p in parts]
    else:
        if not re.fullmatch(r'[a-z_][a-z_0-9]*', key):
            fail('invalid_reference', 'State reference requires a field name or JSON pointer')
        parts = [key]
    value = state
    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r'0|[1-9][0-9]*', part) and int(part) < len(value):
            value = value[int(part)]
        else:
            fail('missing_state_field', f'State field no longer resolves: {ref}')
    return {'kind': 'state', 'value': value, 'binding': session.binding()}


def package(session, package_id):
    packages = [p for p in session.observe()['state']['work_packages'] if p['id'] == package_id]
    if len(packages) != 1:
        fail('missing_package', f'No unique current work package: {package_id}')
    return packages[0]


def current_evidence(session, scope, kind, check, package_id=None):
    observation = session.observe()
    state = observation['state']
    if scope == 'global':
        records = runtime.global_evidence_records(state['validation_evidence'], state['work_packages'])
    elif scope == 'package':
        p = package(session, package_id)
        if p['status'] == 'superseded':
            return None, 'superseded_package'
        records = p.get('evidence', [])
        acceptance_records = runtime.package_acceptance_evidence(p)
    else:
        fail('invalid_reference', 'Evidence scope must be global or package')
    matching = [e for e in records if e['kind'] == kind and e['check_id'] == check]
    # A HEAD change invalidates epoch semantics even before a checkpoint write.
    if observation['head'] != state['head_commit']:
        return None, 'head_drift'
    current = [e for e in matching
               if e['artifact_digest'] == observation['manifest']['artifact_digest']
               and e['evidence_epoch'] == state['evidence_epoch']]
    if not current:
        return None, 'stale_evidence' if matching else 'missing_evidence'
    latest = current[-1]
    if scope == 'package' and latest['result'] == 'pass' and latest not in acceptance_records:
        return None, 'invalidated_package_evidence'
    return latest, None


def evidence_verified(item):
    return bool(item and item['result'] == 'pass' and item['kind'] != 'discovery'
                and (item.get('details') or {}).get('record_semantics') != 'non_validation')


def evidence(session, ref):
    parts = ref.removeprefix('evidence:').split(':')
    if len(parts) == 3 and parts[0] == 'global':
        scope, kind, check = map(decode, parts)
        package_id = None
    elif len(parts) == 4 and parts[0] == 'package':
        scope, package_id, kind, check = map(decode, parts)
    else:
        fail('invalid_reference', 'Expected evidence:global:KIND:CHECK or evidence:package:ID:KIND:CHECK')
    item, reason = current_evidence(session, scope, kind, check, package_id)
    if item is None:
        fail(reason, f'No current result for selected evidence: {ref}')
    return {'kind': 'evidence', 'value': item, 'verdict': item['result'],
            'verified': evidence_verified(item),
            'recording_success': item['result'] == 'pass' if item['kind'] == 'discovery' else None,
            'binding': session.binding()}


def legacy_evidence_ref(session, ref):
    """Consume the current v3 runtime's existing shorthand; refuse ambiguity."""
    check = decode(ref.removeprefix('validation_evidence:'))
    state = session.observe()['state']
    candidates = set()
    for item in runtime.global_evidence_records(state['validation_evidence'], state['work_packages']):
        if item['check_id'] == check:
            candidates.add(('global', None, item['kind']))
    for p in state['work_packages']:
        if p['status'] != 'superseded':
            for item in p.get('evidence', []):
                if item['check_id'] == check:
                    candidates.add(('package', p['id'], item['kind']))
    if len(candidates) != 1:
        fail('ambiguous_evidence' if candidates else 'missing_evidence',
             f'Existing shorthand has no unique evidence identity: {ref}; select scope, kind and check explicitly')
    scope, package_id, kind = candidates.pop()
    item, reason = current_evidence(session, scope, kind, check, package_id)
    if item is None:
        fail(reason, f'No current result for selected evidence: {ref}')
    return {'kind': 'evidence', 'value': item, 'verdict': item['result'],
            'verified': evidence_verified(item),
            'recording_success': item['result'] == 'pass' if item['kind'] == 'discovery' else None,
            'binding': session.binding()}


def thread_locator(ref):
    """Describe a selected history location; no host access, shell or authority."""
    value = ref.removeprefix('thread:')
    if value.count('#') != 1:
        fail('invalid_reference', 'Thread reference requires thread:HOST/ID#QUERY')
    address, query = value.split('#')
    if address.count('/') != 1:
        fail('invalid_reference', 'Thread reference requires one host and one thread ID')
    host, thread_id = map(decode, address.split('/'))
    query = decode(query)
    # IDs are opaque host identities, not repository paths or shell fragments.
    for identity in (host, thread_id):
        if (len(identity.encode()) > 256 or identity in {'.', '..'}
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', identity)):
            fail('invalid_reference', 'Expected a bounded opaque host/thread identity')
    return {'kind': 'thread_locator', 'loaded': False, 'verified': False,
            'locator': {'host_id': host, 'thread_id': thread_id, 'query': query},
            'diagnostic': {'code': 'host_retrieval_required',
                           'message': 'Locator only; retrieve selected history through available host tools'},
            'assurance': 'candidate_history_not_current_evidence'}


def resolve_one(session, ref, expected=None):
    if not ref or len(ref.encode()) > kc.MAX_REF_BYTES or any(ord(c) < 32 for c in ref):
        fail('invalid_reference', 'Empty, oversized or control-character reference')
    if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', expected):
        fail('invalid_binding', 'Expected a lowercase SHA-256 file digest')
    if ref.startswith('code:'):
        return symbol(session, ref, expected)
    if ref.startswith('thread:'):
        if expected is not None:
            fail('invalid_binding', 'A thread locator is not a file digest binding')
        return thread_locator(ref)
    if ref.startswith(('state:', 'work_package:', 'evidence:', 'validation_evidence:')):
        if expected is not None:
            fail('invalid_binding', 'File digest expectations only apply to knowledge and code refs')
        if ref.startswith('state:'):
            return state_field(session, ref)
        if ref.startswith('work_package:'):
            return {'kind': 'work_package', 'value': package(session, decode(ref[13:])),
                    'binding': session.binding()}
        return legacy_evidence_ref(session, ref) if ref.startswith('validation_evidence:') else evidence(session, ref)
    return document(session, ref, expected)


def bounded_output(result, entries, budget, key='inputs'):
    """Whole items only; omitted items are never labelled loaded or verified."""
    result[key] = []
    result['unloaded'] = [e['ref'] for e in entries]
    result['complete'] = False
    result['budget'] = {'unit': 'utf8_json_bytes', 'limit': budget, 'output_bytes': 0,
                        'selected_items': len(entries), 'emitted_items': 0, 'omitted_items': len(entries)}

    def size():
        result['budget']['emitted_items'] = len(result[key])
        result['budget']['omitted_items'] = len(result['unloaded'])
        result['complete'] = not result['unloaded'] and all(e.get('loaded', False) for e in result[key])
        for _ in range(8):
            count = len(kc.encoded_json(result)) + 1
            if result['budget']['output_bytes'] == count:
                return count
            result['budget']['output_bytes'] = count
        return len(kc.encoded_json(result)) + 1
    if size() > budget:
        fail('output_budget_too_small', 'Selection metadata exceeds budget; narrow inputs or increase --budget-bytes')
    for entry in entries:
        result[key].append(entry)
        result['unloaded'].remove(entry['ref'])
        if size() > budget:
            result[key].pop()
            result['unloaded'].append(entry['ref'])
            size()
    return result


def resolve(session, refs, budget=32768, expected=None, from_handoff=False):
    refs = list(refs or [])
    session.observe(required=False)
    if from_handoff:
        observation = session.observe()
        state = observation['state']
        frame = state['handoff']
        if (not frame or frame['artifact_digest'] != observation['manifest']['artifact_digest']
                or frame['head_commit'] != observation['head']
                or frame['evidence_epoch'] != state['evidence_epoch']
                or frame['created_revision'] != state['revision']
                or runtime.handoff_phase_conflict(state, frame)):
            fail('stale_handoff', 'Handoff is absent, stale or phase-conflicting; select current inputs explicitly')
        refs.extend(frame['required_inputs'])
    if not refs or len(refs) > MAX_REFS:
        fail('invalid_selection', f'Select 1..{MAX_REFS} inputs')
    if len(set(refs)) != len(refs):
        fail('ambiguous_selection', 'Duplicate input references are not allowed')
    expected = expected or {}
    if set(expected) - set(refs):
        fail('invalid_binding', 'Expected digest names an unselected input')
    entries = []
    for ref in refs:
        try:
            item = resolve_one(session, ref, expected.get(ref))
            entries.append({'ref': ref, 'loaded': True, **item})
        except Error as exc:
            if exc.code in {'read_race', 'observation_changed', 'invalid_state'}:
                raise
            entries.append({'ref': ref, 'loaded': False,
                            'diagnostic': {'code': exc.code, 'message': str(exc)}})
    result = bounded_output({'command': 'resolve', 'root': session.workspace.root,
                             'trust': TRUST, 'binding': session.binding()}, entries, budget)
    session.verify()
    return result


def parse_expect(values):
    expected = {}
    for value in values:
        ref, separator, digest = value.rpartition('=')
        if not separator or ref in expected or not re.fullmatch('[0-9a-f]{64}', digest):
            fail('invalid_binding', 'Use unique --expect-sha256 REF=LOWERCASE_SHA256')
        expected[ref] = digest
    return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    fetch = commands.add_parser('resolve')
    fetch.add_argument('--root', required=True)
    fetch.add_argument('--ref', action='append', default=[])
    fetch.add_argument('--from-handoff', action='store_true')
    fetch.add_argument('--expect-sha256', action='append', default=[])
    fetch.add_argument('--budget-bytes', type=kc.bounded_int(kc.MAX_READ_BYTES), default=32768)
    args = parser.parse_args(argv)
    session = None
    try:
        session = Session(args.root)
        result = resolve(session, args.ref, args.budget_bytes, parse_expect(args.expect_sha256), args.from_handoff)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (Error, OSError, runtime.StateError) as exc:
        print(json.dumps({'error': {'code': getattr(exc, 'code', 'read_failed'), 'message': str(exc)},
                          'trust': TRUST}, ensure_ascii=False, sort_keys=True))
        return 2
    finally:
        if session:
            session.close()


if __name__ == '__main__':
    sys.exit(main())
