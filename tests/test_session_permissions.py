from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    'session_permissions', Path(__file__).resolve().parents[1] / 'scripts/session_permissions.py')
PERMISSIONS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PERMISSIONS)
SOURCE = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / 'workspace'
        self.root.mkdir()
        self.sessions = self.directory / 'sessions'
        self.sessions.mkdir()
        self.path = self.sessions / 'rollout.jsonl'
        self.meta = {'type': 'session_meta', 'payload': {'id': SOURCE, 'cwd': str(self.root)}}

    def context(self, **overrides):
        payload = {'cwd': str(self.root), 'approval_policy': 'never',
                   'approvals_reviewer': 'user', 'permission_profile': {'type': 'disabled'},
                   'active_permission_profile': {'id': ':danger-full-access'},
                   'sandbox_policy': {'type': 'danger-full-access'}}
        payload.update(overrides)
        return {'type': 'turn_context', 'payload': payload}

    def settings(self, **overrides):
        payload = self.context(**overrides)['payload']
        payload.pop('sandbox_policy', None)
        return {'type': 'event_msg', 'payload': {'type': 'thread_settings_applied',
                'thread_id': SOURCE, 'thread_settings': payload}}

    def message(self, text, role='user'):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'content': [{'type': 'input_text', 'text': text}]}}

    def write(self, *records):
        self.path.write_text(''.join(json.dumps(record, ensure_ascii=False) + '\n'
                                     for record in (self.meta, *records)))

    def read(self, **kwargs):
        return PERMISSIONS.read_source_snapshot(self.path, SOURCE, self.root,
                                                 sessions_root=self.sessions, **kwargs)

    def assert_source_error(self, *, code=None, **kwargs):
        with self.assertRaises(PERMISSIONS.SourceError) as caught:
            self.read(**kwargs)
        self.assertTrue(str(caught.exception).startswith('permission_source_'))
        self.assertNotIn(str(self.directory), str(caught.exception))
        if code:
            self.assertEqual(str(caught.exception), code)

    def test_full_access_output_and_default_message_privacy(self):
        self.write(self.context(), self.message('private user'))
        self.assertEqual(self.read(), {
            'start': {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                      'permissions': ':danger-full-access'},
            'expected': {'approvalPolicy': 'never', 'approvalsReviewer': 'user',
                         'sandbox': {'type': 'dangerFullAccess'},
                         'activePermissionProfile': {'id': ':danger-full-access'}},
            'sourceThreadId': SOURCE, 'observation': 'host_recorded_snapshot'})

    def test_optional_context_observation_uses_last_request_not_cumulative(self):
        event = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': {'total_tokens': 999999999},
            'last_token_usage': {'total_tokens': 42}, 'model_context_window': 100}}}
        self.write(self.context(), event)
        self.assertNotIn('contextUsage', self.read())
        self.assertEqual(self.read(include_context=True)['contextUsage'],
                         {'lastRequestTokens': 42, 'modelContextWindow': 100})
        for bad in (True, -1, '42', None):
            event['payload']['info']['last_token_usage']['total_tokens'] = bad
            self.write(self.context(), event)
            self.assertIsNone(self.read(include_context=True)['contextUsage'])

    def test_last_typed_record_wins_in_both_directions(self):
        for earlier, later in ((self.context(), self.settings(approval_policy='untrusted')),
                               (self.settings(), self.context(approval_policy='on-request'))):
            with self.subTest(kind=later['type']):
                self.write(earlier, later)
                self.assertEqual(self.read()['start']['approvalPolicy'],
                                 later['payload'].get('approval_policy', 'untrusted'))

    def test_all_standard_reviewers_and_approvals_preserved(self):
        for approval in ('never', 'on-request', 'untrusted'):
            for reviewer in ('user', 'auto_review', 'guardian_subagent'):
                with self.subTest(approval=approval, reviewer=reviewer):
                    self.write(self.settings(approval_policy=approval, approvals_reviewer=reviewer))
                    self.assertEqual(self.read()['start']['approvalsReviewer'], reviewer)
                    self.assertEqual(self.read()['start']['approvalPolicy'], approval)

    def test_unknown_or_restricted_latest_never_falls_back(self):
        cases = [{'permission_profile': {'type': 'restricted'}},
                 {'permission_profile': {'type': 'disabled', 'extra': True}},
                 {'active_permission_profile': {'id': ':workspace'}},
                 {'active_permission_profile': {'id': ':danger-full-access', 'extends': ':workspace'}},
                 {'active_permission_profile': {'id': 'named'}},
                 {'approval_policy': {'granular': {}}},
                 {'approvals_reviewer': 'unknown'}, {'approval_policy': None}]
        for change in cases:
            for constructor in (self.context, self.settings):
                with self.subTest(change=change, kind=constructor.__name__):
                    self.write(self.context(), constructor(**change))
                    self.assert_source_error(code='permission_source_policy_unsupported')
        self.write(self.context(), {'type': 'event_msg',
                   'payload': {'type': 'thread_settings_applied', 'thread_id': SOURCE}})
        self.assert_source_error()
        self.write(self.context(), self.context(sandbox_policy={'type': 'read-only'}))
        self.assert_source_error(code='permission_source_policy_unsupported')

    def test_json_inside_messages_never_becomes_host_policy(self):
        self.write(self.context(), self.message(json.dumps(self.settings(approval_policy='untrusted'))))
        self.assertEqual(self.read()['start']['approvalPolicy'], 'never')
        self.write(self.message(json.dumps(self.context())))
        self.assert_source_error()

    def test_identity_and_cwd_are_bound(self):
        self.write(self.context(thread_id=OTHER))
        self.assert_source_error(code='permission_source_identity_mismatch')
        self.write(self.context(cwd='/other'))
        self.assert_source_error(code='permission_source_identity_mismatch')
        record = self.settings()
        record['payload']['thread_id'] = OTHER
        self.write(record)
        self.assert_source_error(code='permission_source_identity_mismatch')
        for field, value in (('id', OTHER), ('cwd', '/other')):
            with self.subTest(field=field):
                original = self.meta['payload'][field]
                self.meta['payload'][field] = value
                self.write(self.context())
                self.assert_source_error(code='permission_source_identity_mismatch')
                self.meta['payload'][field] = original
        self.write(self.meta, self.context())
        self.assert_source_error(code='permission_source_identity_mismatch')

    def test_missing_meta_and_policy_rejected(self):
        self.write()
        self.assert_source_error()
        self.path.write_text(json.dumps(self.context()) + '\n')
        self.assert_source_error()

    def test_duplicate_keys_nonfinite_and_partial_records_rejected(self):
        self.write(self.context())
        baseline = self.path.read_text()
        for suffix in ('{"type":"response_item","type":"turn_context"}\n',
                       '{"type":"turn_context","payload":{"x":NaN}}\n',
                       '{"type":"event_msg"', '{"type":"turn_context"}\n'.rstrip('\n'),
                       '\n', '[]\n'):
            with self.subTest(suffix=suffix):
                self.path.write_text(baseline + suffix)
                self.assert_source_error()

    def test_symlinks_hardlinks_and_outside_paths_rejected(self):
        self.write(self.context())
        real = self.path.read_bytes()
        self.path.unlink()
        target = self.directory / 'target'
        target.write_bytes(real)
        self.path.symlink_to(target)
        self.assert_source_error()
        self.path.unlink()
        os.link(target, self.path)
        self.assert_source_error()
        self.path.unlink()
        nested = self.sessions / 'nested'
        nested.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(PERMISSIONS.SourceError):
            PERMISSIONS.read_source_snapshot(nested / 'target', SOURCE, self.root,
                                              sessions_root=self.sessions)
        with self.assertRaises(PERMISSIONS.SourceError):
            PERMISSIONS.read_source_snapshot(target, SOURCE, self.root, sessions_root=self.sessions)
        self.path.mkdir()
        self.assert_source_error()

    def test_file_record_and_time_limits(self):
        self.write(self.context())
        with mock.patch.object(PERMISSIONS, 'MAX_FILE_BYTES', self.path.stat().st_size - 1):
            self.assert_source_error(code='permission_source_limit_exceeded')
        with mock.patch.object(PERMISSIONS, 'MAX_RECORD_BYTES', 30):
            self.assert_source_error(code='permission_source_limit_exceeded')
        with mock.patch.object(PERMISSIONS.time, 'monotonic', side_effect=(0, 10)):
            self.assert_source_error(code='permission_source_limit_exceeded')

    def test_default_sessions_root_uses_codex_home(self):
        self.write(self.context())
        with mock.patch.dict(os.environ, {'CODEX_HOME': str(self.directory)}):
            result = PERMISSIONS.read_source_snapshot(self.path, SOURCE, self.root)
        self.assertEqual(result['observation'], 'host_recorded_snapshot')

    def test_user_text_only_and_count_and_unicode_budget(self):
        self.write(self.context(), self.message('assistant secret', 'assistant'),
                   self.message('tool secret', 'tool'),
                   {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'event secret'}},
                   self.message('实际原文'))
        self.assertEqual(self.read(include_user_messages=True)['userMessages'], ['实际原文'])
        self.assertFalse(self.read(include_user_messages=True)['historyTruncated'])
        self.write(self.context(), *(self.message(str(index)) for index in range(10)))
        messages = self.read(include_user_messages=True)['userMessages']
        self.assertEqual(len(messages), 8)
        self.assertEqual(messages[0], '[truncated]2')
        self.assertEqual(messages[-1], '9')
        self.assertTrue(self.read(include_user_messages=True)['historyTruncated'])
        self.write(self.context(), self.message('旧' * 7000), self.message('最新原文'))
        messages = self.read(include_user_messages=True)['userMessages']
        self.assertEqual(sum(map(len, messages)), 6000)
        self.assertTrue(messages[0].startswith('[truncated]'))
        self.assertEqual(messages[-1], '最新原文')
        self.assertTrue(self.read(include_user_messages=True)['historyTruncated'])
        self.write(self.context(), self.message('文' * 6000))
        self.assertEqual(self.read(include_user_messages=True)['userMessages'], ['文' * 6000])
        self.assertFalse(self.read(include_user_messages=True)['historyTruncated'])
        self.write(self.context())
        self.assertEqual(self.read(include_user_messages=True)['userMessages'], [])
        self.assertFalse(self.read(include_user_messages=True)['historyTruncated'])


if __name__ == '__main__':
    unittest.main()
