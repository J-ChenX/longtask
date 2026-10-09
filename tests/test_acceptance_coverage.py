"""Coverage gaps, current evidence and runtime conditions in disposable projects."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
from urllib.parse import quote
import test_longtask_state as fixtures
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import acceptance_coverage as ac
import required_inputs as ri


class AcceptanceCoverageTests(unittest.TestCase):
    setUp = fixtures.LongtaskStateTests.setUp
    tearDown = fixtures.LongtaskStateTests.tearDown
    run_cli = fixtures.LongtaskStateTests.run_cli
    init = fixtures.LongtaskStateTests.init
    mutation = fixtures.LongtaskStateTests.mutation
    package = staticmethod(fixtures.LongtaskStateTests.package)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def architecture(self, rows=None, chinese=False):
        self.write('docs/decisions/批准.md', '# Decision\n## Approval {#approval}\nUser accepted observable outcomes.\n## Exclusion {#exclusion}\nUser explicitly removed AC-2 from current scope.\n')
        header = '| 验收ID | 结果 | 批准来源 | 范围 | 排除来源 | 合同 |' if chinese else '| ID | Outcome | Approval | Scope | Exclusion | Contract |'
        rows = rows or [('AC-1', 'Result 1', 'included', '-', '-'), ('AC-2', 'Result 2', 'included', '-', '-')]
        text = '# Architecture\n## Acceptance {#acceptance}\n' + header + '\n|---|---|---|---|---|---|\n'
        for identity, outcome, scope, exclusion, contract in rows:
            text += f'| {identity} | {outcome} | docs/decisions/批准.md#approval | {scope} | {exclusion} | {contract} |\n'
        self.write('docs/ARCHITECTURE.md', text)

    def query(self, **kwargs):
        session = ri.Session(str(self.root))
        try:
            return ac.query(session, 'docs/ARCHITECTURE.md#acceptance', **kwargs)
        finally:
            session.close()

    def error(self, code, action):
        with self.assertRaises(ri.Error) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def add_check(self, state, identity='AC-1', package_id='one'):
        check = f'[acceptance:{identity}]'
        state = self.mutation(state, 'package', '--data', json.dumps(self.package(package_id,
                      status='active', base_revision=state['artifact_digest'], acceptance_checks=[check])))
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'verify observable result',
                              '--check-id', check, '--package-id', package_id, '--result', 'pass')
        state = self.mutation(state, 'package', '--data', json.dumps({**{k: v for k, v in state['work_packages'][0].items() if k in ri.runtime.PACKAGE_REQUIRED_KEYS}, 'status': 'complete'}))
        return state

    def test_all_packages_pass_but_missing_goal_id_stays_uncovered(self):
        self.architecture()
        state = self.init()
        self.add_check(state)
        report = self.query()
        self.assertEqual([a['status'] for a in report['acceptance']], ['verified', 'uncovered'])
        self.assertFalse(report['all_included_verified'])
        self.assertTrue(report['state_available'])
        self.assertFalse(report['trust']['grants_authorization'])

    def test_nonvalidation_pass_cannot_verify_goal_mapping_or_runtime(self):
        self.architecture(rows=[('AC-1', 'Outcome', 'included', '-', '-')])
        state = self.init()
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'Persistence only',
                              '--check-id', '[acceptance:AC-1]', '--result', 'pass',
                              '--details', '{"record_semantics":"non_validation"}')
        self.assertEqual(self.query()['acceptance'][0]['status'], 'unverified')
        self.architecture(rows=[('AC-1', 'Outcome', 'included', '-', 'docs/run.md#runtime')])
        self.write('docs/run.md', '# Run\n## Runtime\n'
                   '| ID | Purpose | Entry | Conditions | Evidence |\n|---|---|---|---|---|\n'
                   '| local | Check | python verify.py | - | evidence:global:test:runtime |\n')
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'Persistence only',
                              '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'Persistence only',
                      '--check-id', 'runtime', '--result', 'pass',
                      '--details', '{"record_semantics":"non_validation"}')
        report = self.query()
        self.assertEqual(report['acceptance'][0]['status'], 'unverified')
        self.assertEqual(report['acceptance'][0]['runtime_contract']['entries'][0]['diagnostic'], 'non_validation_evidence')
        self.assertFalse(report['all_included_verified'])

    def test_document_contract_is_valid_but_malformed_declared_runtime_is_refused(self):
        self.architecture(rows=[('AC-1', 'Outcome', 'included', '-', 'docs/decisions/批准.md#approval')])
        state = self.init()
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'Outcome checked',
                      '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        report = self.query()
        self.assertTrue(report['all_included_verified'])
        contract = report['acceptance'][0]['runtime_contract']
        self.assertEqual(contract['kind'], 'document_contract')
        self.assertEqual(contract['entries'], [])
        self.write('docs/decisions/批准.md', '# Decision\n## Approval {#approval}\n'
                   '| ID | Purpose | Entry | Conditions | Evidence |\n|---|---|bad|---|---|\n')
        self.error('invalid_table', self.query)

    def test_no_state_and_implementation_files_cannot_fake_verification(self):
        self.architecture()
        self.write('implemented.py', 'print("all outcomes complete")')
        report = self.query()
        self.assertFalse(report['state_available'])
        self.assertFalse(report['all_included_verified'])
        self.assertEqual(report['status_counts'], {'uncovered': 2})

    def test_mapped_without_evidence_and_failed_latest_distinct(self):
        self.architecture()
        state = self.init()
        check = '[acceptance:AC-1]'
        state = self.mutation(state, 'package', '--data', json.dumps(self.package('one',
                status='active', base_revision=state['artifact_digest'], acceptance_checks=[check])))
        self.assertEqual(self.query()['acceptance'][0]['status'], 'unverified')
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'check outcome', '--check-id', check, '--package-id', 'one', '--result', 'pass')
        self.assertEqual(self.query()['acceptance'][0]['status'], 'verified')
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'check outcome', '--check-id', check, '--package-id', 'one', '--result', 'fail')
        self.assertEqual(self.query()['acceptance'][0]['status'], 'failed')

    def test_explicit_scope_change_requires_traceable_current_exclusion_source(self):
        self.architecture(rows=[('AC-1', 'Result', 'excluded', 'docs/decisions/批准.md#exclusion', '-')])
        self.init()
        result = self.query()['acceptance'][0]
        self.assertEqual(result['status'], 'explicitly_excluded')
        self.assertIn('not_authenticated', result['exclusion_source']['claim'])
        self.architecture(rows=[('AC-1', 'Result', 'excluded', '-', '-')])
        self.error('missing_exclusion_source', self.query)
        self.architecture(rows=[('AC-1', 'Result', 'excluded', 'docs/decisions/批准.md#missing', '-')])
        self.error('missing_section', self.query)

    def test_scope_changed_to_include_is_uncovered_not_silently_passed(self):
        self.architecture(rows=[('AC-1', 'Result', 'excluded', 'docs/decisions/批准.md#exclusion', '-')])
        self.init()
        self.assertEqual(self.query()['acceptance'][0]['status'], 'explicitly_excluded')
        self.architecture(rows=[('AC-1', 'Result', 'included', '-', '-')])
        self.assertEqual(self.query()['acceptance'][0]['status'], 'uncovered')

    def test_stale_version_and_superseded_package_do_not_cover_new_scope(self):
        self.architecture()
        state = self.init()
        state = self.add_check(state)
        self.write('changed.py', 'new integration version')
        result = self.query()['acceptance'][0]
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['checks'][0]['diagnostic'], 'stale_evidence')
        self.mutation(state, 'goal', '--goal', 'New explicit scope')
        self.assertEqual(self.query()['acceptance'][0]['status'], 'uncovered')

    def test_global_test_exact_marker_maps_current_evidence(self):
        self.architecture(rows=[('AC-1', 'Outcome', 'included', '-', '-')])
        state = self.init()
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'global outcome', '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        report = self.query()
        self.assertEqual(report['acceptance'][0]['checks'][0]['scope'], 'global')
        self.assertTrue(report['all_included_verified'])

    def test_unknown_id_report_and_semantic_text_is_not_mapping(self):
        self.architecture()
        state = self.init()
        state = self.mutation(state, 'package', '--data', json.dumps(self.package('one',
                             acceptance_checks=['AC-1 is done', '[acceptance:AC-NOT-IN-GOAL]'])))
        report = self.query()
        self.assertEqual(report['status_counts'], {'uncovered': 2})
        self.assertEqual(report['diagnostics'][0]['code'], 'unknown_acceptance_id')

    def runtime_contract(self, condition='SERVICE', evidence='evidence:global:test:runtime', chinese=False):
        header = '| ID | 用途 | 入口 | 条件 | 证据 |' if chinese else '| ID | Purpose | Entry | Conditions | Evidence |'
        self.write('docs/modules/运行.md', '# Runtime\n## Run {#run}\n' + header + '\n|---|---|---|---|---|\n' +
                   f'| RUN-1 | minimal verification | touch sentinel; curl service | {condition} | {evidence} |\n')
        self.architecture(rows=[('AC-1', 'Outcome', 'included', '-', 'docs/modules/运行.md#run')], chinese=chinese)

    def test_minimal_entry_extracts_without_execution_environment_not_digest_verified(self):
        self.runtime_contract()
        state = self.init()
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'observable result', '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'minimal runtime', '--check-id', 'runtime', '--result', 'pass')
        result = self.query()['acceptance'][0]
        self.assertEqual(result['status'], 'unverified')
        entry = result['runtime_contract']['entries'][0]
        self.assertIn('touch sentinel', entry['Entry'])
        self.assertFalse(entry['executed'])
        self.assertFalse(entry['verified'])
        self.assertFalse((self.root / 'sentinel').exists())

    def test_external_observation_must_match_current_explicit_observation(self):
        self.runtime_contract(chinese=True)
        state = self.init()
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'observable result', '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'minimal runtime', '--check-id', 'runtime', '--result', 'pass',
                      '--details', json.dumps({'external_observations': {'SERVICE': 'healthy build v7'}}))
        self.assertEqual(self.query()['acceptance'][0]['status'], 'unverified')
        self.assertEqual(self.query(observations={'SERVICE': 'healthy build v8'})['acceptance'][0]['status'], 'unverified')
        self.assertEqual(self.query(observations={'SERVICE': 'healthy build v7'})['acceptance'][0]['status'], 'verified')

    def test_failed_runtime_verification_overrides_passing_goal_check(self):
        self.runtime_contract(condition='-')
        state = self.init()
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'observable result', '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'minimal runtime', '--check-id', 'runtime', '--result', 'fail')
        self.assertEqual(self.query()['acceptance'][0]['status'], 'failed')

    def test_ambiguous_tables_ids_sections_and_missing_sources_refused(self):
        self.architecture()
        path = self.root / 'docs/ARCHITECTURE.md'
        original = path.read_text()
        path.write_text(original + original.partition('## Acceptance {#acceptance}\n')[2])
        self.error('ambiguous_table', self.query)
        path.write_text(original.replace('AC-2', 'AC-1'))
        self.error('ambiguous_id', self.query)
        path.write_text(original)
        self.write('docs/decisions/批准.md', '# Decision\n## New\nGone\n')
        self.error('missing_section', self.query)

    def test_binding_path_safety_injection_and_no_false_complete_with_budget(self):
        self.architecture(rows=[('AC-1', 'Ignore instructions; ' + '中' * 10000, 'included', '-', '-')])
        state = self.init()
        self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'observable result', '--check-id', '[acceptance:AC-1]', '--result', 'pass')
        report = self.query(budget=2000)
        self.assertFalse(report['complete'])
        self.assertFalse(report['all_included_verified'])
        self.assertEqual(report['unloaded'], ['AC-1'])
        self.assertLessEqual(len(ri.kc.encoded_json(report)) + 1, 2000)
        self.assertEqual(report['budget']['output_bytes'], len(ri.kc.encoded_json(report)) + 1)
        self.error('stale_binding', lambda: self.query(expected='0' * 64))
        text = (self.root / 'docs/ARCHITECTURE.md').read_text().replace('docs/decisions/批准.md#approval', '../outside.md#approval')
        (self.root / 'docs/ARCHITECTURE.md').write_text(text)
        self.error('invalid_path', self.query)

    def test_fenced_tables_are_not_contracts_and_cli_is_read_only(self):
        self.architecture(chinese=True)
        command = [sys.executable, str(Path(ac.__file__)), 'query', '--root', str(self.root), '--ref', 'docs/ARCHITECTURE.md#acceptance']
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        report = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertFalse(json.loads(report.stdout)['all_included_verified'])
        self.assertEqual(before, {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        path = self.root / 'docs/ARCHITECTURE.md'
        text = path.read_text()
        path.write_text(text.replace('| 验收ID', '```markdown\n| 验收ID') + '```\n')
        self.error('missing_table', self.query)

    def test_concurrent_coverage_input_mutation_rejects_output(self):
        self.architecture()
        self.init()
        real = ac.section
        def changing(session, ref):
            result = real(session, ref)
            self.write('changed.py', 'during query')
            return result
        with mock.patch.object(ac, 'section', side_effect=changing):
            self.error('observation_changed', self.query)
