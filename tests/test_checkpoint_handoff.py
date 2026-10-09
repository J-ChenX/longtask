"""The optional combined save preserves the singleton journal and double-CAS contract."""
from argparse import Namespace
import json
import subprocess
import sys
import unittest
from unittest import mock
import test_longtask_state as fixtures

STATE = fixtures.STATE


class CheckpointHandoffTests(unittest.TestCase):
    setUp = fixtures.LongtaskStateTests.setUp
    tearDown = fixtures.LongtaskStateTests.tearDown
    run_cli = fixtures.LongtaskStateTests.run_cli
    init = fixtures.LongtaskStateTests.init
    package = staticmethod(fixtures.LongtaskStateTests.package)
    mutation = fixtures.LongtaskStateTests.mutation
    start_pair = fixtures.LongtaskStateTests.start_pair
    handoff_payload = fixtures.LongtaskStateTests.handoff_payload

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in (self.root / '.longtask').rglob('*') if p.is_file()}

    def args(self, state, **kwargs):
        return Namespace(root=str(self.root), expected_task_id=state['task_id'], expected_revision=state['revision'],
                         actor='worker', next_action=None, handoff_data=self.handoff_payload('plan'), **kwargs)

    def assert_binding(self, state):
        frame = state['handoff']
        self.assertEqual(frame['created_revision'], state['revision'])
        for key in ('artifact_digest', 'head_commit', 'evidence_epoch'):
            self.assertEqual(frame[key], state[key])
        self.assertEqual(state['next_action'], frame['objective'])

    def test_single_revision_binds_new_payload_after_product_change(self):
        state = self.init()
        (self.root / 'product.py').write_text('new product')
        saved = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'))
        self.assertEqual(saved['revision'], state['revision'] + 1)
        self.assertEqual(saved['event_revision'], saved['revision'])
        self.assert_binding(saved)
        route = self.run_cli('route', '--root', str(self.root), '--resume-choice', 'resume')
        self.assertFalse(route['handoff_stale'])
        self.assertTrue(route['selection_available'])
        events = (self.root / '.longtask/events.jsonl').read_text().splitlines()
        self.assertEqual(len(events), 2)
        self.assertEqual(json.loads(events[-1])['event'], 'checkpointed')

    def test_legacy_checkpoint_never_rebinds_old_frame(self):
        state = self.init()
        saved = self.mutation(state, 'checkpoint', '--next-action', 'explicit old path')
        self.assertEqual(saved['handoff'], state['handoff'])
        self.assertTrue(self.run_cli('route', '--root', str(self.root))['handoff_stale'])

    def test_current_approval_is_preserved_but_digest_or_head_drift_invalidates(self):
        state = self.init()
        state = self.mutation(state, 'approve', '--scope', 'architecture')
        saved = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'))
        self.assertEqual(saved['approval'], state['approval'])
        (self.root / 'changed.py').write_text('change')
        changed = self.mutation(saved, 'checkpoint', '--handoff-data', self.handoff_payload('plan'))
        self.assertIsNone(changed['approval'])
        state = self.mutation(changed, 'approve', '--scope', 'architecture')
        with mock.patch.object(STATE, 'git_head', return_value='new-head'):
            head_changed = STATE.cmd_checkpoint(self.args(state))
        self.assertEqual(head_changed['evidence_epoch'], state['evidence_epoch'] + 1)
        self.assertIsNone(head_changed['approval'])
        self.assertEqual(head_changed['review']['status'], 'superseded')
        self.assert_binding(head_changed)

    def test_review_completion_is_not_reapproved_or_recompleted(self):
        fixture = fixtures.LongtaskStateTests()
        fixture.root = self.root
        state = fixture.complete_review_checkpoint()
        self.task_id = state['task_id']
        old_evidence = state['validation_evidence']
        (self.root / 'docs/ARCHITECTURE.md').write_text('# changed knowledge')
        changed = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('review', 'artifact', 'current-workspace'))
        self.assertEqual(changed['validation_evidence'], old_evidence)
        self.assertEqual((changed['phase'], changed['status']), ('review', 'active'))
        self.assertEqual(changed['review']['status'], 'superseded')
        self.assertIsNone(changed['approval'])
        self.assertIsNone(changed['approved_digest'])
        self.assert_binding(changed)

    def test_bad_payload_runtime_binding_unknown_target_and_both_cas_reject_without_save(self):
        state = self.init()
        payload = json.loads(self.handoff_payload('plan'))
        invalid = [('{', 'JSON'), ('[]', 'object'), (json.dumps({'intent': 'plan'}), 'missing'),
                   (json.dumps({**payload, 'created_revision': 0}), 'unexpected'),
                   (self.handoff_payload('plan', 'work_package', 'unknown'), 'unknown')]
        for raw, message in invalid:
            before = self.inventory()
            failed = self.mutation(state, 'checkpoint', '--handoff-data', raw, ok=False)
            self.assertIn(message, failed['error'])
            self.assertEqual(before, self.inventory())
        before = self.inventory()
        args = self.args(state)
        args.expected_task_id = state['task_id'] + '-other'
        with self.assertRaisesRegex(STATE.StateError, 'stale task'):
            STATE.cmd_checkpoint(args)
        self.assertEqual(before, self.inventory())
        saved = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'))
        before = self.inventory()
        failed = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'), ok=False)
        self.assertIn('stale revision', failed['error'])
        self.assertEqual(before, self.inventory())
        self.assert_binding(saved)

    def test_ambiguous_next_action_input_rejected_even_for_direct_call(self):
        state = self.init()
        args = self.args(state)
        args.next_action = 'competing objective'
        before = self.inventory()
        with self.assertRaisesRegex(STATE.StateError, 'mutually exclusive'):
            STATE.cmd_checkpoint(args)
        self.assertEqual(before, self.inventory())

    def test_expired_owner_or_scope_drift_refuses_whole_combined_save(self):
        state, _ = self.start_pair()
        before = self.inventory()
        (self.root / 'outside.py').write_text('unattributed')
        failed = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'), ok=False)
        self.assertIn('reconciliation', failed['error'])
        self.assertEqual(before, self.inventory())
        (self.root / 'outside.py').unlink()
        state['work_packages'][0]['lease_expires'] = '2000-01-01T00:00:00+00:00'
        (self.root / '.longtask/state.json').write_text(json.dumps(state))
        before = self.inventory()
        failed = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'), ok=False)
        self.assertIn('reconciliation', failed['error'])
        self.assertEqual(before, self.inventory())
        # Legacy checkpoint remains the coordination path; the refusal cannot hide drift.
        saved = self.mutation(state, 'checkpoint')
        self.assertEqual(saved['work_packages'][0]['status'], 'blocked')

    def test_product_observation_change_during_save_refuses_without_persisting(self):
        state = self.init()
        args = self.args(state)
        before = self.inventory()
        real = STATE.invalidate_stale_packages
        def change_after_package_check(*arguments):
            result = real(*arguments)
            (self.root / 'concurrent.py').write_text('changed')
            return result
        with mock.patch.object(STATE, 'invalidate_stale_packages', side_effect=change_after_package_check), \
             self.assertRaisesRegex(STATE.StateError, 'observation changed'):
            STATE.cmd_checkpoint(args)
        self.assertEqual(before, self.inventory())

    def test_head_observation_change_before_cas_payload_mutation_refuses(self):
        state = self.init()
        before = self.inventory()
        with mock.patch.object(STATE, 'git_head', side_effect=[None, 'new-head', 'new-head']), \
             self.assertRaisesRegex(STATE.StateError, 'observation changed'):
            STATE.cmd_checkpoint(self.args(state))
        self.assertEqual(before, self.inventory())

    def test_ready_package_drift_requires_coordination_without_partial_save(self):
        state = self.init()
        package = self.package('ready', status='ready', base_revision=state['artifact_digest'])
        state = self.mutation(state, 'package', '--data', json.dumps(package))
        (self.root / 'new-product.py').write_text('change')
        before = self.inventory()
        rejected = self.mutation(state, 'checkpoint', '--handoff-data', self.handoff_payload('plan'), ok=False)
        self.assertIn('reconciliation', rejected['error'])
        self.assertEqual(before, self.inventory())

    def test_process_crash_never_persists_only_half_the_combined_save(self):
        child = r'''
import importlib.util, json, os, sys
from argparse import Namespace
spec = importlib.util.spec_from_file_location('runtime', sys.argv[1])
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
root, task, stage, payload = sys.argv[2:]
original = runtime.atomic_write
writes = 0
def crashing_write(path, text):
    global writes
    if path.name == 'state.json':
        writes += 1
        if (stage == 'before' and writes == 1) or (stage == 'final' and writes == 2):
            os._exit(77)
    original(path, text)
    if stage == 'after' and path.name == 'state.json' and writes == 1:
        os._exit(77)
runtime.atomic_write = crashing_write
runtime.cmd_checkpoint(Namespace(root=root, expected_task_id=task, expected_revision=0,
                                 actor='worker', next_action=None, handoff_data=payload))
'''
        for stage in ('before', 'after', 'final'):
            with self.subTest(stage=stage):
                if (self.root / '.longtask').exists():
                    self.tearDown()
                    self.setUp()
                state = self.init()
                (self.root / 'product.py').write_text('changed product')
                payload = self.handoff_payload('plan')
                before = self.inventory()
                result = subprocess.run([sys.executable, '-c', child, str(fixtures.STATE_SCRIPT),
                                         str(self.root), state['task_id'], stage, payload], capture_output=True, text=True)
                self.assertEqual(result.returncode, 77, result.stderr)
                if stage == 'before':
                    self.assertEqual(before, self.inventory())
                    self.mutation(state, 'checkpoint', '--handoff-data', payload)
                    continue
                _, persisted = STATE.load_state(self.root)
                self.assertEqual((persisted['revision'], persisted['event_revision']), (1, 0))
                self.assert_binding(persisted)
                view = self.run_cli('context', '--root', str(self.root), '--resume-choice', 'resume')
                self.assertEqual(view['route']['entry'], 'error')
                recovered = self.mutation(persisted, 'reconcile-events', '--reason', 'recover disposable crash fixture')
                self.assertEqual(recovered['event_revision'], 1)
                self.assert_binding(recovered)
                self.assertFalse(self.run_cli('route', '--root', str(self.root))['handoff_stale'])
                self.assertEqual(len((self.root / '.longtask/events.jsonl').read_text().splitlines()), 2)

    def test_event_append_failure_reports_atomic_combined_state_with_warning(self):
        state = self.init()
        with mock.patch.object(STATE, 'append_event', side_effect=OSError('fixture disk full')):
            saved = STATE.cmd_checkpoint(self.args(state))
        self.assert_binding(saved)
        self.assertIn('_runtime_warnings', saved)
        recovered = self.mutation(saved, 'reconcile-events', '--reason', 'recover disposable event fixture')
        self.assert_binding(recovered)
        self.assertEqual(recovered['revision'], saved['revision'])


if __name__ == '__main__':
    unittest.main()
