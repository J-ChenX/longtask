"""Native bridge reads; explicit interpreter tests do not prove host hook trust."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import longtask_hooks as hooks


@unittest.skipUnless(os.name == 'nt', 'requires native Windows hook consumer')
class WindowsHookTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='longtask-hooks-native-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / 'workspace'
        self.root.mkdir()
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/longtask_state.py'),
                                  'init', '--root', str(self.root), '--task-id', 'hooks',
                                  '--mode', 'continue', '--goal', 'Native bridge'], capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.task_id = json.loads(process.stdout)['task_id']
        self.event = {'hook_event_name': 'SessionStart', 'source': 'compact',
                      'cwd': str(self.root), 'session_id': 'native'}

    def context(self):
        return hooks.handle(self.event)['hookSpecificOutput']['additionalContext']

    def test_checkpoint_is_read_only_bounded_locator(self):
        path = self.root / '.longtask/state.json'
        before = path.read_bytes()
        self.assertEqual(hooks.checkpoint(self.root)['task_id'], self.task_id)
        text = self.context()
        self.assertIn('"task_id":"' + self.task_id + '"', text)
        self.assertNotIn('checkpoint_unavailable_or_unsafe', text)
        self.assertLess(len(text.encode('utf-8')), hooks.MAX_OUTPUT_BYTES)
        self.assertEqual(path.read_bytes(), before)

    def test_hardlink_and_interrupted_markers_are_diagnostics(self):
        alias = self.base / 'alias'
        alias.hardlink_to(self.root / '.longtask/state.json')
        self.assertIn('checkpoint_unavailable_or_unsafe', self.context())
        alias.unlink()
        (self.root / '.longtask/.finishing.json').write_bytes(b'{}')
        self.assertIn('checkpoint_unavailable_or_unsafe', self.context())

    def test_junction_ancestor_never_resolves_to_checkpoint(self):
        alias = self.base / 'alias'
        process = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias), str(self.root)],
                                 capture_output=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.addCleanup(lambda: os.rmdir(alias))
        self.event['cwd'] = str(alias)
        self.assertIn('checkpoint_unavailable_or_unsafe', self.context())

    def test_utf8_output_with_ansi_interpreter_defaults(self):
        process = subprocess.run([sys.executable, str(ROOT / 'scripts/longtask_hooks.py')],
                                 input=json.dumps(self.event).encode('utf-8') + b'\n',
                                 capture_output=True, env=dict(os.environ, PYTHONUTF8='0', PYTHONIOENCODING='cp936'))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn(b'\r\n', process.stdout)
        self.assertIn('longtask 压缩信号', json.loads(process.stdout)['hookSpecificOutput']['additionalContext'])
