#!/usr/bin/env python3
"""Save setup progress through the existing state CLI; never approve or finish work."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import longtask_state as runtime

SCRIPT = Path(__file__).with_name('longtask_state.py')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    begin = commands.add_parser('begin', help='Initialize a new project before writing its planning documents')
    begin.add_argument('--goal', required=True)
    begin.add_argument('--approval-policy', choices=['guarded', 'interactive', 'autonomous'], default='guarded')
    save = commands.add_parser('save', help='Save planning packages and refresh the next-session handoff')
    save.add_argument('--expected-task-id', required=True)
    save.add_argument('--expected-revision', type=int, required=True)
    save.add_argument('--package', action='append', default=[], help='Planned package JSON; omit base_revision to use current snapshot')
    for command in (begin, save):
        command.add_argument('--root', required=True)
        command.add_argument('--actor', default='planner')
        command.add_argument('--next-action', required=True, help='Current next step, including any remaining authorization boundary')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    completed: list[str] = []
    warnings: list[str] = []
    state: dict = {}

    def invoke(name: str, *extra: str) -> dict:
        argv = [sys.executable, str(SCRIPT), name, '--root', str(root)]
        if name not in {'init', 'route', 'validate'}:
            argv += ['--expected-task-id', state['task_id'], '--expected-revision', str(state['revision'])]
        if name not in {'route', 'validate'}:
            argv += ['--actor', args.actor]
        result = subprocess.run([*argv, *extra], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise runtime.StateError(result.stderr.strip() or 'State operation failed')
        data = json.loads(result.stdout)
        completed.append(name)
        warnings.extend(data.get('_runtime_warnings', []))
        return data

    def freeze(*, pending_packages: bool = False) -> dict:
        intent = 'plan' if state['phase'] in {'discovery', 'architecture'} else 'document'
        objective = args.next_action
        if pending_packages:
            objective = ('Package registration is incomplete. Inspect state:work_packages for the actual saved '
                         'packages and resolve remaining registration before proceeding. The following is only '
                         'the intended next step AFTER all requested packages succeed: ' + args.next_action)
        payload = {
            'intent': intent, 'objective': objective,
            'reason': 'Planning checkpoint; no implementation, acceptance or approval is implied.',
            'target': {'kind': 'task', 'ref': state['task_id']},
            'required_inputs': ['state:goal', 'state:work_packages'] + (
                ['docs/ARCHITECTURE.md'] if (root / 'docs/ARCHITECTURE.md').is_file() else []),
            'acceptance_checks': ['Review the recorded goal, planned packages and current documents before taking the stated next step.'],
            'next_if_pass': {'intent': 'decide', 'objective': 'Check the next-session user scope before starting implementation.'},
            'next_if_fail': {'intent': 'plan', 'objective': 'Resolve missing or conflicting planning decisions and refresh this checkpoint.'},
        }
        return invoke('handoff', '--data', json.dumps(payload, ensure_ascii=False))

    try:
        runtime.require_actor(args.actor)
        if not args.next_action.strip():
            raise runtime.StateError('next-action must not be empty')
        if args.command == 'begin':
            route = invoke('route')
            if route['entry'] != 'setup':
                raise runtime.StateError('begin requires a new project; inspect the existing checkpoint or use the appropriate entry')
            state = invoke('init', '--mode', 'setup', '--goal', args.goal, '--approval-policy', args.approval_policy)
        else:
            _, state = runtime.load_state(root)
            errors = runtime.validate_state(state, root, allow_workspace_drift=True)
            if errors:
                raise runtime.StateError('invalid checkpoint: ' + '; '.join(errors))
            if state['task_id'] != args.expected_task_id or state['revision'] != args.expected_revision:
                raise runtime.StateError('stale task or revision; inspect the current checkpoint before retrying')
            if state['mode'] != 'setup' or state['phase'] not in {'discovery', 'architecture', 'documentation'}:
                raise runtime.StateError('save is limited to setup planning; use the state CLI for other work')
            if any(p['status'] not in {'planned', 'superseded'} for p in state['work_packages']):
                raise runtime.StateError('save cannot manage packages that have entered execution')
            packages = [json.loads(value) for value in args.package]
            for package in packages:
                if not isinstance(package, dict) or package.get('status') != 'planned':
                    raise runtime.StateError('all supplied packages must be planned objects')
            # First persist any document edits. A failure later retains this checkpoint.
            state = freeze(pending_packages=bool(packages))
            for index, package in enumerate(packages):
                package.setdefault('base_revision', state['artifact_digest'])
                state = invoke('package', '--data', json.dumps(package, ensure_ascii=False))
                state = freeze(pending_packages=index + 1 < len(packages))
        if args.command == 'begin':
            state = freeze()
        invoke('validate')
        route = invoke('route', '--resume-choice', 'inspect')
        if (route.get('task_id') != state['task_id'] or route.get('revision') != state['revision']
                or route.get('stale') or route.get('handoff_stale') or route.get('handoff_conflict')):
            raise runtime.StateError('checkpoint changed during validation; inspect current state')
        output = runtime.summarize_output(state)
        output.update(checkpoint_saved=True, handoff_stale=False, handoff_conflict=False,
                      scope='planning only; not task completion or implementation authorization')
        if warnings:
            output['_runtime_warnings'] = warnings
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (runtime.StateError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'error': str(exc), 'completed_operations': completed,
                          'next_step': 'Inspect the saved checkpoint; do not rerun begin or overwrite state.'}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
