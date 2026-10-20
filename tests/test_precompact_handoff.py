from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import precompact_handoff as h
from longtask_state import StateError

SOURCE = '11111111-1111-4111-8111-111111111111'
TARGET = '22222222-2222-4222-8222-222222222222'
TURN = '33333333-3333-4333-8333-333333333333'
NEXT = '44444444-4444-4444-8444-444444444444'


class Host:
    def __init__(self, root, receipts):
        self.root, self.receipts, self.calls = root, receipts, []
        self.fail, self.source_status, self.turn_status = None, 'active', 'inProgress'
        self.turn_id, self.terminals, self.goal, self.children = TURN, [], None, []

    def connect(self):
        pass

    def rpc(self, method, params, timeout):
        self.calls.append((method, params))
        if method in ('thread/start', 'turn/start'):
            # Mutations must be recoverable even if the process dies at this point.
            with self.receipts.locked(TARGET) as fd:
                receipt = self.receipts.read(fd, SOURCE)
            assert receipt['phase'] == ('creating' if method == 'thread/start' else 'dispatching')
        if self.fail == method:
            raise h.AdapterError('rpc_timeout')
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'cwd': str(self.root),
                    'path': '/synthetic/source.jsonl', 'source': 'vscode',
                    'status': {'type': self.source_status}}}
        if method == 'thread/turns/list':
            return {'data': [{'id': self.turn_id, 'status': self.turn_status}]}
        if method == 'thread/backgroundTerminals/list':
            return {'data': self.terminals, 'nextCursor': None}
        if method == 'thread/goal/get':
            return {'goal': self.goal}
        if method == 'thread/list':
            return {'data': self.children, 'nextCursor': None}
        if method == 'thread/start':
            return {'thread': {'id': TARGET, 'cwd': str(self.root)},
                    'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                    'sandbox': {'type': 'dangerFullAccess'},
                    'activePermissionProfile': {'id': ':danger-full-access'}}
        if method == 'turn/start':
            return {'turn': {'id': NEXT}}
        if method == 'thread/name/set':
            return {}
        if method == 'thread/items/list':
            return {'data': [{'turnId': TURN, 'item': {'type': 'agentMessage', 'text': 'Observed result'}}],
                    'nextCursor': 'older'}
        raise AssertionError(method)


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = self.root / 'receipts'
        self.store.mkdir(mode=0o700)
        self.receipts = h.Receipts(self.store)
        self.host = Host(self.root, self.receipts)
        self.engine = h.Handoff(self.host, self.receipts, self.root)
        self.snapshot = {'start': {'permissions': ':danger-full-access', 'approvalPolicy': 'never',
                                  'approvalsReviewer': 'user'},
                         'expected': {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                                      'sandbox': {'type': 'dangerFullAccess'},
                                      'activePermissionProfile': {'id': ':danger-full-access'}},
                         'userMessages': ['Original task'], 'historyTruncated': False}
        self.patch = mock.patch.object(h, 'read_source_snapshot', return_value=self.snapshot)
        self.patch.start()
        self.event = {'hook_event_name': 'PreCompact', 'trigger': 'auto',
                      'session_id': SOURCE, 'turn_id': TURN, 'cwd': str(self.root)}

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_regular_task_dispatch_without_longtask_state_and_no_compaction(self):
        result = h.hook(self.event, self.engine)
        self.assertIs(result['continue'], False)
        with self.receipts.locked(SOURCE) as fd:
            receipt = self.receipts.read(fd, SOURCE)
        self.assertEqual((receipt['phase'], receipt['target']), ('dispatched', TARGET))
        self.assertFalse((self.root / '.longtask').exists())
        self.assertLessEqual(len(self.host.calls[-1][1]['input'][0]['text']), 6000)

    def test_repeated_and_restarted_delivery_never_creates_again(self):
        self.engine.dispatch(self.event)
        restarted = h.Handoff(self.host, h.Receipts(self.store), self.root)
        self.assertEqual(restarted.dispatch(self.event)['target'], TARGET)
        self.assertEqual(sum(method == 'thread/start' for method, _ in self.host.calls), 1)

    def test_uncertain_creation_and_dispatch_do_not_replay(self):
        for failure in ('thread/start', 'turn/start', 'thread/name/set'):
            with self.subTest(failure=failure):
                for path in self.store.iterdir():
                    path.unlink()
                self.host.calls = []
                self.host.fail = failure
                receipt = self.engine.dispatch(self.event)
                self.assertEqual(receipt['phase'], 'unknown')
                count = len(self.host.calls)
                self.assertEqual(self.engine.dispatch(self.event), receipt)
                self.assertEqual(len(self.host.calls), count)
                if failure != 'thread/start':
                    self.assertEqual(receipt['target'], TARGET)

    def test_crash_pending_is_not_replayed_by_new_process(self):
        with self.receipts.locked(SOURCE) as fd:
            self.receipts.write(fd, {'schema': 1, 'source': SOURCE, 'phase': 'creating',
                                    'source_turn': TURN, 'cwd': str(self.root), 'target': None, 'target_turn': None})
        result = self.engine.dispatch(self.event)
        self.assertEqual(result['phase'], 'creating')
        self.assertEqual(self.host.calls, [])

    def test_source_must_be_active_on_same_host_and_turn(self):
        for mode in ('notLoaded', 'wrong_turn'):
            self.host.source_status = 'notLoaded' if mode == 'notLoaded' else 'active'
            self.host.turn_id = NEXT if mode == 'wrong_turn' else TURN
            result = h.hook(self.event, self.engine)
            self.assertIs(result['continue'], False)
            self.assertNotIn('thread/start', [name for name, _ in self.host.calls])

    def test_background_goal_and_child_writers_refuse_dispatch(self):
        for field, value in [('terminals', [{}]), ('goal', {'status': 'active'}), ('children', [{}])]:
            with self.subTest(field=field):
                setattr(self.host, field, value)
                with self.assertRaises(h.HandoffError):
                    self.engine.dispatch(self.event)
                setattr(self.host, field, None if field == 'goal' else [])
        self.assertNotIn('thread/start', [name for name, _ in self.host.calls])

    def test_receiver_waits_for_exact_source_stop_then_releases(self):
        self.engine.dispatch(self.event)
        self.assertEqual(self.engine.resume(SOURCE, TARGET)['writerGate'], 'closed')
        self.host.turn_status = 'interrupted'
        result = self.engine.resume(SOURCE, TARGET)
        self.assertEqual(result['writerGate'], 'open')
        self.assertEqual(result['userMessages'], ['Original task'])
        self.assertEqual(result['nextCursor'], 'older')
        self.assertTrue(all(params.get('includeTurns') is not True for _, params in self.host.calls))

    def test_receiver_can_start_before_dispatch_rpc_returns(self):
        rpc = self.host.rpc
        observations = []
        def concurrent(method, params, timeout):
            if method == 'turn/start':
                observations.append(self.engine.resume(SOURCE, TARGET))
            return rpc(method, params, timeout)
        self.host.rpc = concurrent
        self.assertEqual(self.engine.dispatch(self.event)['phase'], 'dispatched')
        self.assertEqual(observations, [{'writerGate': 'closed', 'status': 'waiting_for_dispatch_commit'}])
        self.host.turn_status = 'interrupted'
        self.assertEqual(self.engine.resume(SOURCE, TARGET)['writerGate'], 'open')

    def test_truncated_history_can_be_read_without_losing_page_items(self):
        rpc = self.host.rpc
        data = [{'text': 'x' * 20000}, {'text': 'necessary original goal'}]
        self.host.rpc = lambda method, params, timeout: ({'data': data, 'nextCursor': 'older'}
                if method == 'thread/items/list' else rpc(method, params, timeout))
        first = self.engine.history(SOURCE)
        self.assertTrue(first['historyTruncated'])
        second = self.engine.history(SOURCE, first['pageCursor'], first['nextOffset'], first['pageDigest'])
        self.assertEqual(first['history'] + second['history'], json.dumps(data, ensure_ascii=False))
        with self.assertRaises(h.HandoffError):
            self.engine.history(SOURCE, None, 12000, 'stale')

    def test_capacity_preserves_existing_unknown_receipts(self):
        self.engine.dispatch(self.event)
        with mock.patch.object(h, 'MAX_RECEIPTS', 1):
            with self.receipts.locked(TARGET) as fd:
                with self.assertRaises(h.HandoffError):
                    self.receipts.write(fd, {'schema': 1, 'source': TARGET, 'phase': 'creating'})
            with self.receipts.locked(SOURCE) as fd:
                receipt = self.receipts.read(fd, SOURCE)
                receipt['phase'] = 'unknown'
                self.receipts.write(fd, receipt)
                self.assertEqual(self.receipts.read(fd, SOURCE)['phase'], 'unknown')

    def test_new_source_turn_wrong_recipient_and_pending_never_release(self):
        self.engine.dispatch(self.event)
        with self.assertRaises(h.HandoffError):
            self.engine.resume(SOURCE, SOURCE)
        self.host.turn_status = 'interrupted'
        self.host.turn_id = NEXT
        with self.assertRaises(h.HandoffError):
            self.engine.resume(SOURCE, TARGET)

    def test_resources_are_rechecked_after_source_stops(self):
        self.engine.dispatch(self.event)
        self.host.turn_status = 'interrupted'
        self.host.terminals = [{}]
        with self.assertRaises(h.HandoffError):
            self.engine.resume(SOURCE, TARGET)

    def test_permissions_unsupported_and_downgraded_fail_closed(self):
        self.patch.stop()
        with mock.patch.object(h, 'read_source_snapshot', side_effect=h.SourceError('permission_source_policy_unsupported')):
            self.assertIs(h.hook(self.event, self.engine)['continue'], False)
        self.assertNotIn('thread/start', [name for name, _ in self.host.calls])
        self.patch.start()
        self.snapshot['expected']['approvalPolicy'] = 'on-request'
        receipt = self.engine.dispatch(self.event)
        self.assertEqual(receipt['phase'], 'unknown')
        self.assertNotIn('turn/start', [name for name, _ in self.host.calls])

    def test_deadline_refuses_new_rpc_and_still_vetoes(self):
        self.engine.deadline = time.monotonic() - 1
        self.assertIs(h.hook(self.event, self.engine)['continue'], False)
        self.assertEqual(self.host.calls, [])

    def test_early_warning_uses_last_request_ratio_and_never_dispatches(self):
        event = {**self.event, 'hook_event_name': 'PostToolUse', 'transcript_path': '/synthetic/source.jsonl'}
        self.snapshot['contextUsage'] = {'lastRequestTokens': 49, 'modelContextWindow': 100}
        self.assertEqual(h.hook(event, self.engine), {})
        self.snapshot['contextUsage']['lastRequestTokens'] = 50
        result = h.hook(event, self.engine)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('50%', result['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.host.calls, [])

    def test_global_instruction_does_not_force_regular_task_into_skill(self):
        result = h.hook({**self.event, 'hook_event_name': 'UserPromptSubmit'}, self.engine)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')
        self.assertEqual(self.host.calls, [])
        self.assertFalse((self.root / '.longtask').exists())

    def test_private_no_follow_store_and_lock(self):
        self.store.chmod(0o755)
        with self.assertRaises(h.HandoffError):
            with self.receipts.locked(SOURCE):
                pass
        self.store.chmod(0o700)
        link = self.root / 'alias'
        link.symlink_to(self.store, target_is_directory=True)
        with self.assertRaises((h.HandoffError, OSError, StateError)):
            with h.Receipts(link).locked(SOURCE):
                pass
        (self.store / (SOURCE + '.lock')).symlink_to(self.root / 'outside')
        with self.assertRaises(OSError):
            with self.receipts.locked(SOURCE):
                pass
        self.assertFalse((self.root / 'outside').exists())

    def test_hardlink_receipt_and_concurrent_dispatch_are_rejected(self):
        with self.receipts.locked(SOURCE) as fd:
            with self.assertRaises(h.HandoffError):
                with self.receipts.locked(SOURCE):
                    pass
            self.receipts.write(fd, {'schema': 1, 'source': SOURCE})
        os.link(self.store / (SOURCE + '.json'), self.root / 'other')
        with self.receipts.locked(SOURCE) as fd:
            with self.assertRaises(h.HandoffError):
                self.receipts.read(fd, SOURCE)

    def test_malformed_or_oversize_cli_input_vetoes_without_echo(self):
        for raw in ('PRIVATE INVALID', '[]', 'null', '42', 'x' * (h.MAX_EVENT + 1)):
            result = subprocess.run([sys.executable, str(Path(h.__file__)), '--store', str(self.store), 'hook'],
                                    input=raw, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertIs(json.loads(result.stdout)['continue'], False)
            self.assertNotIn('PRIVATE', result.stdout)
            self.assertEqual(result.stderr, '')

    def test_cli_missing_or_linked_store_still_vetoes(self):
        link = self.root / 'alias'
        link.symlink_to(self.store, target_is_directory=True)
        for store in (self.root / 'missing', link):
            result = subprocess.run([sys.executable, str(Path(h.__file__)), '--store', str(store), 'hook'],
                                    input=json.dumps(self.event), text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0)
            self.assertIs(json.loads(result.stdout)['continue'], False)
            self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
