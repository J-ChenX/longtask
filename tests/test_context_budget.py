"""Disposable state fixtures verify bounded discovery, not model token estimates."""
from argparse import Namespace
import copy
import json
import unittest
from unittest import mock
import test_longtask_state as fixtures

STATE = fixtures.STATE


class ContextBudgetTests(unittest.TestCase):
    setUp = fixtures.LongtaskStateTests.setUp
    tearDown = fixtures.LongtaskStateTests.tearDown
    run_cli = fixtures.LongtaskStateTests.run_cli
    init = fixtures.LongtaskStateTests.init
    package = staticmethod(fixtures.LongtaskStateTests.package)
    mutation = fixtures.LongtaskStateTests.mutation
    start_pair = fixtures.LongtaskStateTests.start_pair
    handoff_payload = fixtures.LongtaskStateTests.handoff_payload

    def query(self, *args):
        return self.run_cli('context', '--root', str(self.root), *args)

    def inventory(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def packages(self, count):
        state = self.init()
        state = self.mutation(state, 'package', '--data', json.dumps(self.package('p0')))
        # Expand one genuine runtime record into disposable scale fixtures.
        template = state['work_packages'][0]
        state['work_packages'] = []
        for index in range(count):
            package = copy.deepcopy(template)
            package.update(id=f'p{index}', objective=f'Implement feature {index}', write_set=[f'p{index}.py'],
                           acceptance_checks=[f'test p{index}'])
            state['work_packages'].append(package)
        (self.root / '.longtask/state.json').write_text(json.dumps(state))
        return self.mutation(state, 'handoff', '--data', self.handoff_payload('plan'))

    def assert_budget(self, view, chars=None, utf8=None):
        encoded = json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
        self.assertEqual(view['budget']['chars'], len(encoded))
        self.assertEqual(view['budget']['utf8_bytes'], len(encoded.encode('utf-8')))
        if chars is not None:
            self.assertLessEqual(len(encoded), chars)
        if utf8 is not None:
            self.assertLessEqual(len(encoded.encode('utf-8')), utf8)
        self.assertTrue(view['consumption']['incomplete'])
        self.assertFalse(view['consumption']['executable'])
        self.assertIsNone(view.get('handoff', {}).get('usable_target'))
        self.assertFalse(view.get('recovery', {}).get('selection_available', False))
        self.assertIsNone(view.get('next_operation'))

    def test_one_ten_and_hundred_packages_paged_then_load_detail(self):
        for count in (1, 10, 100):
            with self.subTest(count=count):
                if (self.root / '.longtask').exists():
                    self.tearDown()
                    self.setUp()
                state = self.packages(count)
                before = self.inventory()
                offset, seen = 0, []
                while True:
                    view = self.query('--view', 'overview', '--offset', str(offset), '--limit', '10')
                    self.assert_budget(view, chars=12000)
                    self.assertEqual(view['diagnostic_summary']['by_code']['package_evidence_missing'], count)
                    self.assertEqual(view['diagnostic_summary']['pending_evidence'], count + 1)
                    self.assertTrue(view['package_index'])
                    seen += [entry['id'] for entry in view['package_index']]
                    self.assertTrue(all(entry['objective_hint'] and entry['detail_query'] for entry in view['package_index']))
                    if not view['pagination']['has_more']:
                        break
                    offset = view['pagination']['next_offset']
                self.assertEqual(seen, [p['id'] for p in state['work_packages']])
                detail = self.query('--view', 'package', '--package-id', f'p{count - 1}')
                self.assertEqual([p['id'] for p in detail['package_contracts']], [f'p{count - 1}'])
                self.assertEqual(detail['package_contracts'][0]['unmet_acceptance_checks'], [f'test p{count - 1}'])
                default = self.query()
                self.assertEqual(len(default['package_contracts']), count)
                self.assertNotIn('consumption', default)
                self.assertEqual(before, self.inventory())

    def test_off_page_blocking_package_is_located_without_loading_all_checks(self):
        state = self.packages(100)
        state['work_packages'][-1]['dependencies'] = ['p0']
        (self.root / '.longtask/state.json').write_text(json.dumps(state))
        view = self.query('--view', 'overview', '--limit', '10')
        self.assert_budget(view, chars=12000)
        self.assertEqual(view['diagnostic_summary']['by_code']['dependency_unmet'], 1)
        self.assertEqual(view['diagnostic_locations']['dependency_unmet'], [{'command': 'package', 'package_id': 'p99'}])
        self.assertNotIn('p99', [p['id'] for p in view['package_index']])
        detail = self.query('--view', 'package', '--package-id', 'p99')
        self.assertEqual(detail['package_contracts'][0]['dependency_status'], [{'id': 'p0', 'status': 'planned'}])

    def test_hundred_active_owners_are_discoverable_across_budgeted_pages(self):
        state, _ = self.start_pair()
        template = state['work_packages'][0]
        packages = []
        for index in range(100):
            package = copy.deepcopy(template)
            package.update(id=f'active{index}', owner=f'owner{index}', write_set=[f'active{index}.py'],
                           contributors=template['contributors'] + [f'owner{index}'])
            packages.append(package)
        state['work_packages'] = packages
        (self.root / '.longtask/state.json').write_text(json.dumps(state))
        before = self.inventory()
        offset, seen = 0, []
        while True:
            view = self.query('--view', 'overview', '--limit', '10', '--offset', str(offset), '--max-chars', '9000')
            self.assert_budget(view, chars=9000)
            self.assertTrue(view['package_selection_required'])
            if 'active_ownership' not in view:
                self.assertEqual(view['consumption']['unloaded']['active_ownership']['count'], 100)
            seen.extend(entry['owner'] for entry in view['package_index'])
            self.assertTrue(view['package_index'])
            if not view['pagination']['has_more']:
                break
            offset = view['pagination']['next_offset']
        self.assertEqual(seen, [f'owner{i}' for i in range(100)])
        detail = self.query('--view', 'package', '--package-id', 'active99')
        self.assertEqual(len(detail['active_ownership']), 100)
        self.assertEqual(before, self.inventory())

    def test_long_unicode_fields_and_missing_inputs_are_explicitly_unloaded(self):
        state = self.packages(10)
        state['goal'] = '长目标' * 10000
        state['work_packages'][0]['objective'] = '详情' * 20000
        state['handoff']['required_inputs'] = ['missing/path.md', 'state:work_packages']
        state['handoff']['objective'] = '很长的交接' * 10000
        (self.root / '.longtask/state.json').write_text(json.dumps(state))
        before = self.inventory()
        view = self.query('--view', 'overview', '--max-chars', '9000', '--max-bytes', '10000')
        self.assert_budget(view, chars=9000, utf8=10000)
        self.assertTrue(view['consumption']['budget_limited'])
        self.assertIn('required_inputs', view['consumption']['unloaded'])
        self.assertTrue(view['package_index'][0]['objective_truncated'])
        detail = self.query('--view', 'package', '--package-id', 'p0')
        self.assertEqual(detail['package_contracts'][0]['objective'], state['work_packages'][0]['objective'])
        self.assertIn('missing/path.md', detail['required_inputs'])
        self.assertEqual(before, self.inventory())

    def test_all_active_owners_and_global_barriers_survive_detail_selection(self):
        state, _ = self.start_pair()
        state = self.mutation(state, 'blocker', '--action', 'add', '--blocker-id', 'global', '--summary', 'wait')
        overview = self.query('--view', 'overview')
        self.assertEqual([p['owner'] for p in overview['active_ownership']], ['first', 'second'])
        self.assertTrue(overview['package_selection_required'])
        self.assertEqual(overview['diagnostic_summary']['by_code']['open_blocker'], 1)
        self.assertIn('open_blocker', [i['code'] for i in overview['diagnostics']])
        self.assertEqual(overview['diagnostic_locations']['open_blocker'], [{'command': 'blocker', 'blocker_id': 'global'}])
        detail = self.query('--view', 'package', '--package-id', 'second')
        self.assertEqual(detail['active_ownership'], overview['active_ownership'])
        self.assertTrue(detail['package_selection_required'])
        self.assertEqual(len(detail['package_contracts']), 1)
        self.assertEqual(detail['blockers'], state['blockers'])

    def test_tiny_budget_refuses_executable_consumption(self):
        self.packages(100)
        view = self.query('--view', 'overview', '--max-bytes', '700')
        self.assert_budget(view, utf8=700)
        self.assertTrue(view['consumption']['budget_limited'])
        self.assertIn('ownership', view['consumption']['unloaded'])
        failure = self.run_cli('context', '--root', str(self.root), '--view', 'overview', '--max-chars', '1', ok=False)
        self.assertIn('incomplete', failure['error'])

    def test_budget_reduces_page_without_skipping_unreturned_packages(self):
        self.packages(100)
        view = self.query('--view', 'overview', '--limit', '100', '--max-chars', '6000')
        self.assert_budget(view, chars=6000)
        self.assertGreater(view['pagination']['returned'], 0)
        self.assertLess(view['pagination']['returned'], 100)
        self.assertEqual(view['pagination']['next_offset'], view['pagination']['returned'])
        self.assertEqual(view['diagnostic_summary']['by_code']['package_evidence_missing'], 100)
        self.assertEqual(view['consumption']['unloaded']['package_contracts']['count'], 100)
        next_page = self.query('--view', 'overview', '--offset', str(view['pagination']['next_offset']), '--max-chars', '6000')
        self.assertEqual(next_page['package_index'][0]['id'], f"p{view['pagination']['returned']}")

    def test_drift_full_observation_and_summary_remain_fail_closed(self):
        self.packages(10)
        (self.root / 'drift.py').write_text('changed')
        args = Namespace(root=str(self.root), resume_choice='resume', view='overview')
        with mock.patch.object(STATE, 'git_head', return_value='new-head'), \
             mock.patch.object(STATE, 'load_state', wraps=STATE.load_state) as loader:
            view = STATE.cmd_context(args)
        self.assertEqual(loader.call_count, 1)
        self.assertIn('workspace_drift', view['diagnostic_summary']['by_code'])
        self.assertIn('head_drift', view['diagnostic_summary']['by_code'])
        self.assertEqual(STATE.summarize_output(view), view)
        self.assert_budget(view, chars=12000)

    def test_corrupt_required_input_contract_and_changing_overview_fail_closed(self):
        self.packages(1)
        args = Namespace(root=str(self.root), resume_choice='resume', view='overview')
        real = STATE.diagnose_observation
        def changing(route, observation):
            result = real(route, observation)
            if observation:
                (self.root / 'concurrent.py').write_text('change during query')
            return result
        with mock.patch.object(STATE, 'diagnose_observation', side_effect=changing):
            view = STATE.cmd_context(args)
        self.assertIsNone(view['current_task'])
        self.assertIn('invalid_state', view['diagnostic_summary']['by_code'])
        self.assert_budget(view, chars=12000)
        path = self.root / '.longtask/state.json'
        state = json.loads(path.read_text())
        del state['handoff']['required_inputs']
        path.write_text(json.dumps(state))
        before = self.inventory()
        view = self.query('--view', 'overview')
        self.assertIsNone(view['current_task'])
        self.assertIn('required_inputs', view['route']['reason'])
        self.assertEqual(before, self.inventory())

    def test_absent_invalid_and_invalid_selectors_do_not_write(self):
        before = self.inventory()
        self.assertFalse(self.query('--view', 'overview')['current_task'])
        self.assertEqual(before, self.inventory())
        state = self.packages(1)
        for args in (('--view', 'package'), ('--view', 'package', '--package-id', 'unknown'),
                     ('--max-chars', '1000'), ('--view', 'overview', '--max-bytes', '-1'),
                     ('--view', 'overview', '--offset', '2'), ('--view', 'overview', '--limit', '101')):
            before = self.inventory()
            self.run_cli('context', '--root', str(self.root), *args, ok=False)
            self.assertEqual(before, self.inventory())
        (self.root / '.longtask/state.json').write_text('{')
        view = self.query('--view', 'overview')
        self.assertIsNone(view['current_task'])
        self.assertIn('invalid_state', view['diagnostic_summary']['by_code'])
        self.assert_budget(view, chars=12000)


if __name__ == '__main__':
    unittest.main()
