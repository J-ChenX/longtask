"""Portable native-notice consumers; fixtures never claim real hook trust."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import native_notice as notice
import session_permissions as source

SOURCE = '11111111-1111-4111-8111-111111111111'


class NativeNoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.sessions = self.base / 'sessions'
        self.sessions.mkdir()
        self.path = self.sessions / 'rollout.jsonl'
        self.event = {'hook_event_name': 'PostToolUse', 'cwd': str(self.base),
                      'session_id': SOURCE, 'transcript_path': str(self.path)}
        patch = mock.patch.dict(os.environ, {'CODEX_HOME': str(self.base)})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, tokens=50, window=100, *, identity=SOURCE, extra=()):
        records = [{'type': 'session_meta', 'payload': {'id': identity, 'cwd': str(self.base)}},
                   # Context observation must not require or transfer full permissions.
                   {'type': 'turn_context', 'payload': {'sandbox_policy': {'type': 'read-only'}}},
                   {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                       'total_token_usage': {'total_tokens': 9999999},
                       'last_token_usage': {'total_tokens': tokens}, 'model_context_window': window}}},
                   *extra]
        self.path.write_text(''.join(json.dumps(r) + '\n' for r in records), encoding='utf-8')

    def test_last_request_threshold_and_latest_unknown(self):
        self.write(49)
        self.assertEqual(notice.notice(self.event), {})
        self.write(50)
        result = notice.notice(self.event)
        self.assertEqual(result['hookSpecificOutput']['hookEventName'], 'PostToolUse')
        self.assertIn('50%', result['hookSpecificOutput']['additionalContext'])
        self.write(extra=[{'type': 'event_msg', 'payload': {'type': 'token_count', 'info': None}}])
        self.assertIn('systemMessage', notice.notice(self.event))
        for tokens, window in ((True, 100), (-1, 100), (50, 0), ('50', 100)):
            self.write(tokens, window)
            self.assertIn('systemMessage', notice.notice(self.event))

    def test_context_is_usage_only_and_does_not_authorize_permissions(self):
        self.write()
        self.assertEqual(source.read_context_usage(self.path, SOURCE, self.base),
                         {'lastRequestTokens': 50, 'modelContextWindow': 100})
        with self.assertRaises(source.SourceError):
            source.read_source_snapshot(self.path, SOURCE, self.base)
        before = {p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        notice.notice(self.event)
        notice.notice({'hook_event_name': 'UserPromptSubmit'})
        self.assertEqual({p: p.read_bytes() for p in self.base.rglob('*') if p.is_file()}, before)
        self.assertFalse((self.base / '.longtask').exists())

    def test_stale_identity_and_outside_paths_are_unknown(self):
        self.write(identity='22222222-2222-4222-8222-222222222222')
        self.assertIn('systemMessage', notice.notice(self.event))
        self.write()
        os.utime(self.path, (time.time() - 301, time.time() - 301))
        self.assertIn('systemMessage', notice.notice(self.event))
        self.write()
        for change in ({'cwd': str(self.sessions)}, {'session_id': 'bad'},
                       {'transcript_path': str(self.base / 'outside.jsonl')}):
            self.assertIn('systemMessage', notice.notice({**self.event, **change}))

    def test_limits_and_duplicate_json_are_diagnostic(self):
        self.write()
        for field, value in (('MAX_FILE_BYTES', 1), ('MAX_RECORD_BYTES', 2)):
            with mock.patch.object(source, field, value):
                self.assertIn('systemMessage', notice.notice(self.event))
        self.path.write_bytes(b'{"type":"session_meta","type":"event_msg"}\n')
        self.assertIn('systemMessage', notice.notice(self.event))

    def test_hardlink_and_link_ancestors_are_rejected(self):
        self.write()
        alias = self.sessions / 'alias.jsonl'
        alias.hardlink_to(self.path)
        self.assertIn('systemMessage', notice.notice(self.event))
        alias.unlink()
        linked = self.sessions / 'linked'
        if os.name == 'nt':
            process = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(linked), str(self.sessions)], capture_output=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.addCleanup(lambda: os.rmdir(linked))
        else:
            linked.symlink_to(self.sessions, target_is_directory=True)
        self.assertIn('systemMessage', notice.notice({**self.event, 'transcript_path': str(linked / self.path.name)}))

    def test_cli_is_bounded_ascii_json_without_dispatch(self):
        self.write()
        for raw in (json.dumps(self.event).encode(), b'[]', b'{"x":1,"x":2}', b'x' * (notice.MAX_EVENT + 1)):
            process = subprocess.run([sys.executable, str(ROOT / 'scripts/native_notice.py')], input=raw,
                                     capture_output=True, env=dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='cp936'))
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertLess(len(process.stdout), 8192)
            result = json.loads(process.stdout)
            self.assertTrue(result)
            self.assertNotIn('continue', result)
            self.assertNotIn(b'\r\n', process.stdout)
            process.stdout.decode('ascii')
