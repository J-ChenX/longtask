#!/usr/bin/env python3
"""Run an isolated CLI tutorial; synthetic reviewer labels are not release evidence."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().with_name('longtask_state.py')


def demonstrate_planning() -> dict:
    helper = SCRIPT.with_name('planning_checkpoint.py')
    with tempfile.TemporaryDirectory(prefix='longtask-planning-demo-') as directory:
        root = Path(directory)
        (root / '.gitignore').write_text('/.longtask/\n')
        state = {}
        def run(action, *extra):
            nonlocal state
            args = [sys.executable, str(helper), action, '--root', directory,
                    '--actor', 'demo-planner', '--next-action', 'Continue the synthetic plan; no implementation authorization']
            if action == 'begin':
                args += ['--goal', 'Plan a reader and dependent output module']
            else:
                args += ['--expected-task-id', state['task_id'], '--expected-revision', str(state['revision'])]
            result = subprocess.run([*args, *extra], capture_output=True, text=True, check=True, timeout=30)
            state = json.loads(result.stdout)
            if not state['checkpoint_saved'] or state['handoff_stale']:
                raise RuntimeError('Planning handoff is not fresh')
        run('begin')
        early = (root / '.longtask/state.json').is_file() and not (root / 'docs').exists()
        (root / 'docs').mkdir()
        (root / 'docs/ARCHITECTURE.md').write_text('# Synthetic plan\n\nReader precedes output; implementation remains unstarted.\n')
        run('save')
        for identifier, dependencies in [('reader', []), ('output', ['reader'])]:
            package = {'id': identifier, 'objective': 'Plan ' + identifier, 'status': 'planned',
                       'dependencies': dependencies, 'affected_modules': [identifier],
                       'write_set': [identifier + '.py'], 'acceptance_checks': ['fixed-input'],
                       'risk': 'synthetic local fixture', 'risk_level': 'low', 'required_review_roles': ['quality'],
                       'rollback': 'Establish a restorable baseline before implementation',
                       'stopping_condition': 'Conflicting requirements', 'owner': None,
                       'lease_expires': None, 'contributors': []}
            run('save', '--package', json.dumps(package))
        saved = json.loads((root / '.longtask/state.json').read_text())
        if not early or saved['approval'] is not None or saved['validation_evidence']:
            raise RuntimeError('Planning crossed an authorization or evidence boundary')
        return {'scenario': 'planning', 'passed': True, 'assurance': 'synthetic_cli_tutorial',
                'checkpoint_before_documents': early, 'handoff_fresh': True,
                'planned_packages': len(saved['work_packages']), 'checkpoint_cleared': False,
                'implementation_started': False}


def demonstrate(scenario: str = 'lifecycle') -> dict:
    if scenario == 'planning':
        return demonstrate_planning()
    with tempfile.TemporaryDirectory(prefix='longtask-demo-') as directory:
        root = Path(directory) / 'project'
        root.mkdir()
        (root / '.gitignore').write_text('/.longtask/\n')
        steps: list[str] = []
        state: dict = {}

        def command(name: str, *args: str, actor: str = 'demo-author') -> dict:
            nonlocal state
            argv = [sys.executable, str(SCRIPT), name, '--root', str(root)]
            if name not in {'init', 'route', 'doctor', 'validate'}:
                argv += ['--expected-task-id', state['task_id'], '--expected-revision', str(state['revision'])]
            if name not in {'route', 'doctor', 'validate'}:
                argv += ['--actor', actor]
            process = subprocess.run([*argv, *args], capture_output=True, text=True, check=False, timeout=30)
            if process.returncode:
                raise RuntimeError(f'{name}: {process.stderr}')
            result = json.loads(process.stdout)
            if 'schema_version' in result:
                state = result
            steps.append(name)
            return result

        def evidence(kind: str, check: str, *, package: str | None = None,
                     result: str = 'pass', actor: str = 'demo-author', details: dict | None = None) -> None:
            args = ['--kind', kind, '--check-id', check, '--summary', f'Demo fixture: {check}', '--result', result]
            if package:
                args += ['--package-id', package]
            if details:
                args += ['--details', json.dumps(details)]
            command('evidence', *args, actor=actor)

        def advance(phase: str) -> None:
            evidence(f"phase:{state['phase']}", f"phase:{state['phase']}")
            command('transition', '--phase', phase, '--status', 'active')

        def review() -> None:
            evidence('review', 'demo-review', actor='demo-reviewer', details={
                **{key: state[key] for key in ('task_id', 'evidence_epoch', 'head_commit')},
                'reviewer': 'demo-reviewer', 'role': 'quality', 'scope': ['isolated tutorial fixture'],
                'covered_package_ids': [p['id'] for p in state['work_packages'] if p['status'] != 'superseded'],
                'base_revision': state['artifact_digest'], 'artifact_digest': state['artifact_digest'],
                'status': 'approved', 'findings': [], 'evidence': ['Synthetic tutorial review envelope'],
                'coverage_gaps': [], 'assumptions': ['Role labels are simulated; this is not independent release approval.']})
            command('approve', '--scope', 'review', actor='demo-approver')
            evidence('phase:review', 'phase:review')
            command('transition', '--phase', 'complete', '--status', 'complete')

        def git(at: Path, *args: str) -> None:
            subprocess.run(['git', '-C', str(at), *args], capture_output=True, check=True, timeout=30)

        command('init', '--mode', 'setup', '--goal', 'Tutorial fixture prints 42 and preserves current project knowledge')
        advance('architecture')
        (root / 'docs').mkdir()
        (root / 'docs/ARCHITECTURE.md').write_text('# Tutorial fixture\n\nApproved: app.py prints 42. Verify by executing it.\n')
        advance('documentation')
        advance('execution')
        if scenario == 'worktree':
            git(root, 'init', '-q')
            git(root, 'add', '.')
            git(root, '-c', 'user.name=demo', '-c', 'user.email=demo@example.invalid', 'commit', '-qm', 'fixture baseline')
            command('checkpoint')
        package = {
            'id': 'output', 'objective': 'Print 42', 'status': 'active', 'dependencies': [],
            'affected_modules': ['demo'], 'write_set': ['app.py'], 'acceptance_checks': ['prints-42'],
            'base_revision': state['artifact_digest'], 'risk': 'isolated fixture', 'risk_level': 'low',
            'required_review_roles': ['quality'], 'rollback': 'Discard this temporary fixture',
            'stopping_condition': 'Any CLI or execution check fails', 'owner': None,
            'lease_expires': None, 'contributors': []}
        command('package', '--data', json.dumps(package))
        if scenario == 'modify':
            command('modify', '--package-id', 'output', '--reason', 'User requests an explicit replacement contract')
            command('approve', '--scope', 'design')
            old = state['work_packages'][0]
            # Caller supplies only contractual fields; runtime provenance is retained by the tool.
            old = {key: value for key, value in old.items() if key in package}
            old['status'] = 'superseded'
            command('package', '--data', json.dumps(old))
            advance('documentation')
            advance('execution')
            package.update(id='output-v2', base_revision=state['artifact_digest'])
            command('package', '--data', json.dumps(package))
        if scenario == 'worktree':
            worker = Path(directory) / 'worker'
            git(root, 'worktree', 'add', '--detach', str(worker), 'HEAD')
            (worker / 'app.py').write_text('print(42)\n')
            # Only the integrator writes the primary checkout and its state.
            (root / 'app.py').write_bytes((worker / 'app.py').read_bytes())
            git(root, 'worktree', 'remove', '--force', str(worker))
        else:
            (root / 'app.py').write_text('print(42)\n')
        observed = subprocess.run([sys.executable, str(root / 'app.py')], capture_output=True, text=True, check=True)
        if observed.stdout.strip() != '42':
            raise RuntimeError('fixture acceptance failed')
        package_id = package['id']
        evidence('test', 'prints-42', package=package_id)
        package['status'] = 'complete'
        command('package', '--data', json.dumps(package))
        evidence('knowledge', 'current-project-content')
        advance('review')
        review()
        if scenario == 'recovery':
            evidence('test', 'external-fixture', result='fail', actor='demo-test-operator')
            evidence('test', 'external-fixture', actor='demo-independent-tester',
                     details={'recovery_observation': 'The isolated external fixture was restarted and independently checked.'})
            review()
        digest = state['artifact_digest']
        finished = command('finish')
        if not finished['finished'] or (root / '.longtask/state.json').exists():
            raise RuntimeError('fixture cleanup failed')
        return {'scenario': scenario, 'passed': True, 'assurance': 'synthetic_cli_tutorial',
                'steps': steps, 'artifact_digest': digest, 'output': observed.stdout.strip(),
                'retained_project_knowledge': (root / 'docs/ARCHITECTURE.md').exists(),
                'checkpoint_cleared': True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=('planning', 'lifecycle', 'recovery', 'modify', 'worktree'), default='lifecycle')
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.scenario), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
