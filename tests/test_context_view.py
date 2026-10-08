from argparse import Namespace
import json
import unittest
from unittest import mock
import test_longtask_state as fixtures
STATE = fixtures.STATE


class ContextViewTests(unittest.TestCase):
    setUp = fixtures.LongtaskStateTests.setUp
    tearDown = fixtures.LongtaskStateTests.tearDown
    run_cli = fixtures.LongtaskStateTests.run_cli
    init = fixtures.LongtaskStateTests.init
    package = staticmethod(fixtures.LongtaskStateTests.package)
    mutation = fixtures.LongtaskStateTests.mutation
    start_pair = fixtures.LongtaskStateTests.start_pair
    handoff_payload = fixtures.LongtaskStateTests.handoff_payload

    def context(self, choice=None):
        args = ["context", "--root", str(self.root)]
        if choice:
            args += ["--resume-choice", choice]
        return self.run_cli(*args)

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_normal_view_binding_and_no_side_effects(self):
        state = self.init()
        before = self.inventory()
        view = self.context('resume')
        self.assertEqual(view['goal'], state['goal'])
        self.assertEqual(view['binding'], {key: state[key] for key in
                         ('task_id', 'revision', 'artifact_digest', 'head_commit', 'evidence_epoch')})
        self.assertEqual(view['required_inputs'], state['handoff']['required_inputs'])
        self.assertEqual(view['recovery']['selected_choice'], 'resume')
        self.assertTrue(view['recovery']['selection_available'])
        self.assertEqual(before, self.inventory())
        doctor = self.run_cli('doctor', '--root', str(self.root))
        self.assertEqual(view['diagnostics'], doctor['issues'])

    def test_multiple_active_packages_are_all_visible(self):
        self.start_pair()
        view = self.context()
        self.assertEqual(view['active_package_ids'], ['first', 'second'])
        self.assertTrue(view['package_selection_required'])
        self.assertEqual([p['id'] for p in view['package_contracts']], ['first', 'second'])
        self.assertTrue(all(p['unmet_acceptance_checks'] for p in view['package_contracts']))

    def test_absent_and_corrupt_state_fail_without_writes(self):
        self.assertEqual(self.context()['reason'], 'no_current_task')
        self.assertEqual(self.inventory(), {})
        self.init()
        path = self.root / '.longtask/state.json'
        for data in ('{', '[]', '{"phase": []}'):
            path.write_text(data)
            before = self.inventory()
            view = self.context()
            self.assertEqual(view['route']['entry'], 'error')
            self.assertIsNone(view['current_task'])
            self.assertEqual(before, self.inventory())

    def test_stale_frame_is_not_an_executable_target(self):
        self.init()
        (self.root / 'product.py').write_text('change')
        view = self.context('resume')
        self.assertTrue(view['handoff']['stale'])
        self.assertIsNone(view['handoff']['usable_target'])
        self.assertFalse(view['recovery']['selection_available'])
        self.assertIn('workspace_drift', [i['code'] for i in view['diagnostics']])

    def test_failure_dependency_and_blocker_are_visible(self):
        state = self.init('continue')
        for package in (self.package('first', status='active', base_revision=state['artifact_digest']),
                        self.package('second', dependencies=['first'])):
            state = self.mutation(state, 'package', '--data', json.dumps(package))
        state = self.mutation(state, 'evidence', '--kind', 'test', '--summary', 'failed',
                              '--result', 'fail', '--check-id', 'test first', '--package-id', 'first')
        self.mutation(state, 'blocker', '--action', 'add', '--blocker-id', 'blocked', '--summary', 'wait')
        view = self.context('inspect')
        codes = [i['code'] for i in view['diagnostics']]
        self.assertIn('dependency_unmet', codes)
        self.assertIn('package_check_failed', codes)
        self.assertIn('open_blocker', codes)
        self.assertEqual(view['package_contracts'][1]['dependency_status'], [{'id':'first', 'status':'failed'}])

    def test_changed_state_or_product_observation_is_rejected(self):
        self.init()
        args = Namespace(root=str(self.root), resume_choice='inspect')
        real = STATE.diagnose_observation
        for target in ('state', 'product'):
            def changing(route, observation):
                result = real(route, observation)
                if target == 'state':
                    path = self.root / '.longtask/state.json'
                    path.write_bytes(path.read_bytes() + b' ')
                else:
                    (self.root / 'product.py').write_text('change')
                return result
            # Error diagnostics also use the shared builder, without another observed state.
            with mock.patch.object(STATE, 'diagnose_observation',
                                   side_effect=lambda r,o: changing(r,o) if o else real(r,o)):
                view = STATE.cmd_context(args)
            self.assertEqual(view['route']['entry'], 'error')
            self.assertIn('observation changed', view['route']['reason'])

    def test_review_choice_and_shared_observation(self):
        self.init('review')
        args = Namespace(root=str(self.root), resume_choice='review')
        with mock.patch.object(STATE, 'load_state', wraps=STATE.load_state) as loader, \
             mock.patch.object(STATE, 'workspace_manifest', wraps=STATE.workspace_manifest) as manifest:
            view = STATE.cmd_context(args)
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(manifest.call_count, 1)
        self.assertEqual(view['route']['entry'], 'review')
        self.assertTrue(view['recovery']['selection_available'])
        self.assertEqual(view['diagnostics'], STATE.cmd_route(args)['diagnostics'])

    def test_head_drift_and_summary_keep_risks(self):
        self.init()
        args = Namespace(root=str(self.root), resume_choice='resume')
        with mock.patch.object(STATE, 'git_head', return_value='new-head'):
            view = STATE.cmd_context(args)
        self.assertEqual(view['binding']['head_commit'], 'new-head')
        self.assertIn('head_drift', [issue['code'] for issue in view['diagnostics']])
        self.assertFalse(view['recovery']['selection_available'])
        self.assertEqual(STATE.summarize_output(view), view)

    def test_execution_barriers_do_not_confuse_missing_acceptance_with_permission(self):
        state, _ = self.start_pair()
        # A valid execution-stage fixture; the handoff command binds it normally.
        path = self.root / '.longtask/state.json'
        state['phase'] = 'execution'
        path.write_text(json.dumps(state))
        state = self.mutation(state, 'handoff', '--data', self.handoff_payload('execute', 'work_package', 'first'))
        view = self.context('resume')
        self.assertTrue(view['recovery']['selection_available'])
        self.assertTrue(view['package_contracts'][0]['unmet_acceptance_checks'])
        with mock.patch.object(STATE, 'execution_scope_drift', return_value=True):
            view = STATE.cmd_context(Namespace(root=str(self.root), resume_choice='resume'))
        self.assertFalse(view['recovery']['selection_available'])
        self.assertEqual(view['recovery']['resume_options'][0]['unavailable_reason'], 'execution_scope_drift')
        (self.root / 'outside.py').write_text('unattributed write')
        view = self.context('resume')
        self.assertFalse(view['recovery']['selection_available'])
        self.assertIsNone(view['handoff']['usable_target'])
        self.assertIn('execution_scope_drift', [i['code'] for i in view['diagnostics']])

    def test_complete_head_drift_never_recommends_finish(self):
        fixture = fixtures.LongtaskStateTests()
        fixture.root = self.root
        fixture.complete_review_checkpoint()
        args = Namespace(root=str(self.root), resume_choice='inspect')
        with mock.patch.object(STATE, 'git_head', return_value='new-head'):
            view = STATE.cmd_context(args)
        self.assertFalse(view['healthy'])
        self.assertIsNone(view['next_operation'])
        self.assertIn('head_drift', [i['code'] for i in view['diagnostics']])

    def test_expired_lease_disables_resume(self):
        state, _ = self.start_pair()
        path = self.root / '.longtask/state.json'
        state['work_packages'][0]['lease_expires'] = '2000-01-01T00:00:00+00:00'
        path.write_text(json.dumps(state))
        view = self.context('resume')
        self.assertFalse(view['recovery']['selection_available'])
        self.assertIn('lease_expired', [i['code'] for i in view['diagnostics']])


if __name__ == '__main__':
    unittest.main()
