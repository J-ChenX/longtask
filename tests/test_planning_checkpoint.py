"""Planning boundaries are exercised through the public CLI in disposable projects."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/planning_checkpoint.py'


def package(identifier='reader', dependencies=None):
    return {'id': identifier, 'objective': 'Parse synthetic input', 'status': 'planned',
            'dependencies': dependencies or [], 'affected_modules': [identifier],
            'write_set': [identifier + '.py'], 'acceptance_checks': ['fixed-input'],
            'risk': 'synthetic local fixture', 'risk_level': 'low', 'required_review_roles': ['quality'],
            'rollback': 'Restore the synthetic baseline before implementation',
            'stopping_condition': 'Conflicting requirements', 'owner': None,
            'lease_expires': None, 'contributors': []}


class PlanningCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / '.gitignore').write_text('/.longtask/\n')

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        return json.loads((self.root / '.longtask/state.json').read_text())

    def call(self, action, *extra, ok=True, expected=None):
        args = [sys.executable, str(SCRIPT), action, '--root', str(self.root), '--actor', 'fixture-planner',
                '--next-action', 'Continue planning only; implementation not authorized']
        if action == 'begin':
            args += ['--goal', 'Synthetic planning-only project']
        else:
            state = expected or self.state()
            args += ['--expected-task-id', state['task_id'], '--expected-revision', str(state['revision'])]
        run = subprocess.run([*args, *extra], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode == 0, ok, run.stdout + run.stderr)
        return json.loads(run.stdout if ok else run.stderr)

    def test_early_checkpoint_and_incremental_planning_without_false_completion(self):
        started = self.call('begin')
        self.assertTrue(started['checkpoint_saved'])
        self.assertFalse((self.root / 'docs').exists())
        self.assertEqual(self.state()['handoff']['created_revision'], self.state()['revision'])
        (self.root / 'docs').mkdir()
        (self.root / 'docs/ARCHITECTURE.md').write_text('# Synthetic plan\nReader precedes output.\n')
        self.call('save', '--package', json.dumps(package()), '--package', json.dumps(package('output', ['reader'])))
        self.call('save', '--next-action', 'Plan is ready; next session must review implementation scope')
        state = self.state()
        self.assertEqual(state['handoff']['created_revision'], state['revision'])
        self.assertEqual(state['handoff']['evidence_epoch'], state['evidence_epoch'])
        self.assertEqual([p['status'] for p in state['work_packages']], ['planned', 'planned'])
        self.assertEqual(state['phase'], 'discovery')
        self.assertIsNone(state['approval'])
        self.assertEqual(state['validation_evidence'], [])
        self.assertFalse((self.root / 'reader.py').exists())
        events = [json.loads(line) for line in (self.root / '.longtask/events.jsonl').read_text().splitlines()]
        for index, event in enumerate(events):
            if event['event'] == 'package_upserted':
                self.assertEqual(events[index + 1]['event'], 'handoff_recorded')

    def test_stale_identity_and_revision_cannot_overwrite_progress(self):
        old = self.call('begin')
        self.call('save')
        before = (self.root / '.longtask/state.json').read_bytes()
        for stale in (old, {**self.state(), 'task_id': 'unrelated-task'}):
            result = self.call('save', expected=stale, ok=False)
            self.assertIn('stale', result['error'])
            self.assertEqual((self.root / '.longtask/state.json').read_bytes(), before)

    def test_existing_project_and_existing_checkpoint_cannot_be_reinitialized(self):
        self.call('begin')
        before = (self.root / '.longtask/state.json').read_bytes()
        self.call('begin', ok=False)
        self.assertEqual((self.root / '.longtask/state.json').read_bytes(), before)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'service.py').write_text('print(1)\n')
            run = subprocess.run([sys.executable, str(SCRIPT), 'begin', '--root', directory,
                                  '--goal', 'Do not overwrite', '--next-action', 'Inspect'], capture_output=True, text=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertFalse((root / '.longtask').exists())

    def test_failed_later_package_preserves_successful_package_and_fresh_handoff(self):
        self.call('begin')
        invalid = package('output', ['missing'])
        result = self.call('save', '--package', json.dumps(package()), '--package', json.dumps(invalid),
                           '--next-action', 'Both packages are ready; review the implementation scope', ok=False)
        self.assertIn('package', result['completed_operations'])
        state = self.state()
        self.assertEqual([p['id'] for p in state['work_packages']], ['reader'])
        self.assertEqual(state['handoff']['created_revision'], state['revision'])
        self.assertTrue(state['handoff']['objective'].startswith('Package registration is incomplete.'))
        self.assertIn('only the intended next step AFTER', state['handoff']['objective'])
        self.assertNotIn('checkpoint_saved', result)

    def test_untrusted_next_action_is_literal_and_active_payload_is_rejected(self):
        self.call('begin')
        marker = self.root / 'should-not-exist'
        action = f'$(touch {marker}) `touch {marker}`'
        self.call('save', '--next-action', action)
        self.assertEqual(self.state()['handoff']['objective'], action)
        self.assertFalse(marker.exists())
        before = (self.root / '.longtask/state.json').read_bytes()
        self.call('save', '--package', json.dumps({**package(), 'status': 'active'}), ok=False)
        self.assertEqual((self.root / '.longtask/state.json').read_bytes(), before)

    def test_other_mode_is_rejected_without_writing(self):
        subprocess.run([sys.executable, str(ROOT / 'scripts/longtask_state.py'), 'init', '--root', str(self.root),
                        '--mode', 'review', '--goal', 'Read-only audit'], check=True, capture_output=True)
        before = (self.root / '.longtask/state.json').read_bytes()
        self.call('save', ok=False)
        self.assertEqual((self.root / '.longtask/state.json').read_bytes(), before)

    def test_incomplete_state_returns_recovery_error_without_writing(self):
        expected = self.call('begin')
        path = self.root / '.longtask/state.json'
        damaged = self.state()
        del damaged['phase']
        path.write_text(json.dumps(damaged))
        before = path.read_bytes()
        result = self.call('save', expected=expected, ok=False)
        self.assertIn('invalid checkpoint', result['error'])
        self.assertIn('phase', result['error'])
        self.assertEqual(result['completed_operations'], [])
        self.assertIn('Inspect', result['next_step'])
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
