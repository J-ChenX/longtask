"""Temporary interruption facts do not grant testing, review or authorization."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import test_longtask_state as fixtures

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/discovery_checkpoint.py'
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location('discovery_checkpoint', SCRIPT)
dc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dc)


class DiscoveryCheckpointTests(unittest.TestCase):
    setUp = fixtures.LongtaskStateTests.setUp
    tearDown = fixtures.LongtaskStateTests.tearDown
    init = fixtures.LongtaskStateTests.init
    run_cli = fixtures.LongtaskStateTests.run_cli
    mutation = fixtures.LongtaskStateTests.mutation
    handoff_payload = fixtures.LongtaskStateTests.handoff_payload
    start_pair = fixtures.LongtaskStateTests.start_pair
    package = staticmethod(fixtures.LongtaskStateTests.package)
    complete_review_checkpoint = fixtures.LongtaskStateTests.complete_review_checkpoint
    review_details = fixtures.LongtaskStateTests.review_details
    environment = {'python': '3.14.7', 'transport': 'offline'}

    def payload(self, **overrides):
        return {'event_id': 'transport-offline', 'outcome': 'excluded_option',
                'problem': 'Remote lookup cannot resolve the schema while disconnected.',
                'attempts': ['Tried the remote schema endpoint once.'],
                'observations': ['Endpoint returned no route to host.'],
                'conclusion': {'source': 'observed', 'statement': 'Remote retrieval failed in the offline environment.'},
                'affected_contracts': ['AC-schema-01'], 'conditions': 'Offline transport during this lookup.',
                'retry_when': 'Network route becomes available or endpoint changes.',
                'next_action': 'Inspect the repository schema locally.', **overrides}

    def save(self, state, payload=None, handoff=None):
        return dc.record(str(self.root), state['task_id'], state['revision'], 'investigator',
                         payload or self.payload(), self.environment, handoff)

    def state(self):
        return json.loads((self.root / '.longtask/state.json').read_text())

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_investigation_interruption_recovers_exclusion_without_test_pass_or_permission(self):
        state = self.init()
        saved = self.save(state, handoff=self.handoff_payload('plan'))
        self.assertTrue(saved['evidence_saved'])
        self.assertTrue(saved['handoff_saved'])
        before = self.inventory()
        result = dc.query(str(self.root), self.environment, 'transport-offline')
        self.assertEqual(before, self.inventory())
        item = result['records'][0]
        self.assertEqual(item['discovery']['attempts'], self.payload()['attempts'])
        self.assertEqual(item['discovery']['retry_when'], self.payload()['retry_when'])
        self.assertFalse(item['requires_recheck'])
        frozen = self.state()
        self.assertEqual([e['kind'] for e in frozen['validation_evidence']], ['discovery'])
        self.assertIsNone(frozen['approval'])
        self.assertNotEqual(frozen['review']['status'], 'approved')
        self.assertFalse(result['trust']['grants_acceptance'])
        self.assertTrue(self.run_cli('validate', '--root', str(self.root))['valid'])

    def test_code_modification_interruption_binds_current_artifact_and_explicit_new_frame(self):
        state = self.init()
        old_frame = state['handoff']
        (self.root / 'product.py').write_text('fixed local parsing\n')
        payload = self.payload(event_id='local-fix', outcome='fact', observations=['The local parser was updated; tests have not run.'])
        saved = self.save(state, payload, self.handoff_payload('plan'))
        frozen = self.state()
        self.assertNotEqual(state['artifact_digest'], frozen['artifact_digest'])
        self.assertEqual(frozen['handoff']['created_revision'], frozen['revision'])
        self.assertEqual(frozen['handoff']['artifact_digest'], frozen['artifact_digest'])
        self.assertNotEqual(old_frame, frozen['handoff'])
        self.assertFalse(dc.query(str(self.root), self.environment, 'local-fix')['records'][0]['requires_recheck'])
        self.assertTrue(saved['handoff_saved'])
        self.assertEqual(frozen['validation_evidence'][-1]['kind'], 'discovery')

    def test_real_validation_failure_stays_failed_after_recording_root_cause_and_resuming(self):
        state, _ = self.start_pair()
        state = self.mutation(state, 'evidence', '--kind', 'test', '--check-id', 'test first', '--package-id', 'first',
                              '--summary', 'First acceptance failed', '--result', 'fail', actor='tester')
        saved = self.save(state, self.payload(outcome='root_cause'), self.handoff_payload('remediate', 'work_package', 'first'))
        self.assertTrue(saved['handoff_saved'])
        frozen = self.state()
        self.assertEqual(frozen['work_packages'][0]['status'], 'failed')
        self.assertEqual(frozen['work_packages'][0]['evidence'][-1]['result'], 'fail')
        self.assertIsNone(frozen['approval'])
        doctor = self.run_cli('doctor', '--root', str(self.root))
        self.assertTrue(any(i['code'] == 'package_check_failed' for i in doctor['issues']))
        self.assertEqual(dc.query(str(self.root), self.environment)['records'][0]['discovery']['outcome'], 'root_cause')

    def test_repeated_unchanged_event_does_not_append_or_resign_old_frame(self):
        state = self.init()
        first = self.save(state)
        frozen = self.state()
        old_frame = frozen['handoff']
        before = self.inventory()
        second = self.save(frozen)
        self.assertTrue(second['existing_record_reused'])
        self.assertFalse(second['evidence_saved'])
        self.assertEqual(before, self.inventory())
        self.assertEqual(old_frame, self.state()['handoff'])
        self.assertEqual(first['binding'], second['binding'])

    def test_old_negative_evidence_rechecks_environment_version_and_head_instead_of_forbidding_retry(self):
        state = self.init()
        self.save(state)
        changed = dc.query(str(self.root), {'python': '3.14.7', 'transport': 'online'})['records'][0]
        self.assertIn('environment_changed', changed['recheck_reasons'])
        self.assertIn('never permanently forbids retry', changed['retry_policy'])
        (self.root / 'new.py').write_text('new endpoint\n')
        changed = dc.query(str(self.root), self.environment)['records'][0]
        self.assertIn('artifact_changed', changed['recheck_reasons'])
        changed = dc.query(str(self.root))['records'][0]
        self.assertIn('environment_not_rechecked', changed['recheck_reasons'])
        with mock.patch.object(dc.runtime, 'git_head', return_value='changed-head'):
            changed = dc.query(str(self.root), self.environment)['records'][0]
            self.assertIn('head_changed', changed['recheck_reasons'])

    def test_changed_conclusion_same_identity_has_latest_record_and_no_summary_contract_conflict(self):
        state = self.init()
        self.save(state)
        self.save(self.state(), self.payload(next_action='Use the local generated schema instead.'))
        result = dc.query(str(self.root), self.environment)
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['records'][0]['discovery']['next_action'], 'Use the local generated schema instead.')
        self.assertEqual(len(self.state()['validation_evidence']), 2)

    def test_handoff_failure_reports_discovery_already_saved_and_keeps_old_frame(self):
        state = self.init()
        payload = json.loads(self.handoff_payload('plan'))
        payload['target'] = {'kind': 'work_package', 'ref': 'missing'}
        saved = self.save(state, handoff=json.dumps(payload))
        self.assertTrue(saved['evidence_saved'])
        self.assertFalse(saved['handoff_saved'])
        self.assertFalse(saved['transactional'])
        self.assertEqual(saved['error']['code'], 'handoff_not_saved')
        self.assertEqual(self.state()['handoff'], state['handoff'])
        self.assertEqual(len(dc.query(str(self.root), self.environment)['records']), 1)

    def test_lost_handoff_result_reports_uncertain_instead_of_false_rejection(self):
        state = self.init()
        original = dc.cli_mutation
        def lose_handoff_result(command, *arguments):
            if command == 'checkpoint':
                raise dc.UncertainMutation('result unavailable')
            return original(command, *arguments)
        with mock.patch.object(dc, 'cli_mutation', side_effect=lose_handoff_result):
            result = self.save(state, handoff=self.handoff_payload('plan'))
        self.assertTrue(result['evidence_saved'])
        self.assertIsNone(result['handoff_saved'])
        self.assertEqual(result['error']['code'], 'handoff_outcome_uncertain')

    def test_cas_rejection_invalid_hypothesis_and_oversized_data_do_not_mutate(self):
        state = self.init()
        self.save(state)
        before = self.inventory()
        with self.assertRaises(dc.DiscoveryError):
            self.save(state)
        with self.assertRaises(dc.DiscoveryError):
            self.save(self.state(), self.payload(outcome='hypothesis'))
        with self.assertRaises(dc.DiscoveryError):
            self.save(self.state(), self.payload(problem='x' * 4097))
        self.assertEqual(before, self.inventory())

    def test_finish_cleans_discovery_and_process_without_long_term_archive(self):
        state = self.complete_review_checkpoint()
        self.save(state)
        self.assertEqual(len(dc.query(str(self.root), self.environment)['records']), 1)
        finished = self.mutation(self.state(), 'finish')
        self.assertTrue(finished['finished'])
        result = dc.query(str(self.root), self.environment)
        self.assertFalse(result['current_task'])
        self.assertEqual(result['records'], [])
        self.assertFalse((self.root / '.longtask/state.json').exists())
        self.assertFalse((self.root / '.longtask/events.jsonl').exists())
        self.assertEqual((self.root / 'docs/ARCHITECTURE.md').read_text(), '# Current project content\n')
        self.assertFalse((self.root / 'docs/tasks').exists())

    def test_query_race_and_no_checkpoint_query_are_read_only(self):
        before = self.inventory()
        self.assertFalse(dc.query(str(self.root))['current_task'])
        self.assertEqual(before, self.inventory())
        state = self.init()
        self.save(state)
        original = dc.records
        def mutate_during_projection(observation, environment):
            result = original(observation, environment)
            (self.root / 'changed.py').write_text('changed during projection')
            return result
        with mock.patch.object(dc, 'records', side_effect=mutate_during_projection):
            with self.assertRaisesRegex(dc.DiscoveryError, 'changed before output'):
                dc.query(str(self.root), self.environment)

    def test_json_depth_surrogate_and_non_finite_values_are_rejected(self):
        for raw in ('{"bad": NaN}', '{"bad": "\\ud800"}', '{"bad": ' + '[' * 20 + '0' + ']' * 20 + '}'):
            with self.subTest(raw=raw):
                with self.assertRaises(dc.DiscoveryError):
                    dc.object_json(raw, 'environment')

    def test_cli_read_and_missing_environment_are_bounded_candidate_data(self):
        state = self.init()
        self.save(state)
        output = subprocess.run([sys.executable, str(SCRIPT), 'read', '--root', str(self.root),
                                 '--event-id', 'transport-offline'], capture_output=True, text=True)
        self.assertEqual(output.returncode, 0, output.stderr + output.stdout)
        result = json.loads(output.stdout)
        self.assertTrue(result['records'][0]['requires_recheck'])
        self.assertFalse(result['trust']['grants_authorization'])


if __name__ == '__main__':
    unittest.main()
