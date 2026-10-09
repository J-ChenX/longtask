"""Real CLI fixtures and binding/failure contracts; no mocked host success claim."""
from argparse import Namespace
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('progress_eval', ROOT / 'scripts/run_progress_evals.py')
P = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(P)


class ProgressEvalTests(unittest.TestCase):
    def test_fixed_eight_journeys_and_real_multiturn_frontier(self):
        _, cases = P.load_cases()
        self.assertEqual({case['id'] for case in cases}, P.CASE_IDS)
        self.assertEqual(len([case for case in cases if not case.get('supplemental')]), 8)
        self.assertEqual({case['id'] for case in cases if case.get('supplemental')}, {'changed-goal'})
        for name in ('planning-only', 'inserted-constraint', 'investigation-return'):
            self.assertEqual(len(next(c for c in cases if c['id'] == name)['phases']), 2)

    def test_evidence_writes_reject_ancestor_symlinks_before_creating_nested_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'outside'
            outside.mkdir()
            link = root / 'alias'
            link.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                P.write_json(link / 'nested/results.json', {'synthetic': True})
            self.assertFalse((outside / 'nested').exists())
            with self.assertRaises(OSError):
                P.new_directory(link / 'fixture')
            self.assertFalse((outside / 'fixture').exists())
            with self.assertRaises(ValueError):
                P.write_json(root / 'path/../results.json', {})
            output = root / 'actual/results.json'
            P.write_json(output, {'synthetic': True})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                P.write_json(output, {'replace': True})

    def test_fixtures_create_state_only_through_real_public_cli(self):
        for case in P.load_cases()[1]:
            with self.subTest(case=case['id']), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / 'fixture'
                result = P.fixture(workspace, ROOT, case)
                if case['fixture'] in ('new', 'narrow'):
                    self.assertFalse((workspace / '.longtask').exists())
                    continue
                state = json.loads((workspace / '.longtask/state.json').read_text())
                self.assertEqual(result['cli_operations'][0], 'init')
                self.assertEqual(result['cli_operations'][-1], 'handoff')
                self.assertIn('transition:execution', result['cli_operations'])
                self.assertIn('package:active', result['cli_operations'])
                self.assertEqual(state['phase'], 'execution')
                self.assertIsNone(state['approval'])
                self.assertIsNone(state['approved_digest'])
                self.assertEqual(len([e for e in state['validation_evidence'] if e['kind'].startswith('phase:')]), 3)
                self.assertEqual(state['handoff']['target'], {'kind': 'work_package', 'ref': 'slug'})
                route = P.run_cli(ROOT, workspace, 'route')
                self.assertEqual(route['handoff_stale'], case['fixture'] == 'stale')
                self.assertFalse(route['handoff_conflict'])
                context = P.run_cli(ROOT, workspace, 'context', '--resume-choice', 'resume')
                self.assertEqual(context['recovery']['selection_available'], case['fixture'] != 'stale')
                if case['fixture'] == 'review':
                    failed = [e for e in state['validation_evidence'] if e['result'] == 'fail']
                    self.assertEqual(failed[0]['check_id'], 'whitespace')
                    self.assertEqual(failed[0]['details']['actual_probe_exit_code'], 2)
                    self.assertEqual(state['work_packages'][0]['status'], 'failed')
                elif case['fixture'] != 'stale':
                    self.assertEqual(state['work_packages'][0]['status'], 'active')
                    self.assertTrue(state['work_packages'][0]['lease_expires'])
                    self.assertFalse(state['work_packages'][0].get('scope_drift', False))
                before = P.inventory(workspace)
                P.freeze_fixture(workspace, Path(directory) / 'initial')
                self.assertEqual(before, P.inventory(workspace))

    def test_directory_prefix_write_sets_accept_docs_tests_and_reject_undeclared_paths(self):
        case = next(c for c in P.load_cases()[1] if c['id'] == 'fresh-checkpoint')
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'fixture'
            P.fixture(workspace, ROOT, case)
            state = json.loads((workspace / '.longtask/state.json').read_text())
            package = state['work_packages'][0]
            self.assertIn('docs', package['write_set'])
            self.assertIn('tests', package['write_set'])
            self.assertNotIn('docs/**', package['write_set'])
            self.assertNotIn('tests/**', package['write_set'])
            self.assertNotIn('docs/', package['write_set'])
            self.assertNotIn('tests/', package['write_set'])
            context = P.run_cli(ROOT, workspace, 'context', '--resume-choice', 'resume')
            self.assertTrue(context['recovery']['selection_available'])
            self.assertFalse(package.get('scope_drift', False))
            module = workspace / 'docs/modules/文本转换.md'
            module.write_text(module.read_text() + '\n来源: observed，合成子路径写入边界核验。\n')
            P.write(workspace / 'tests/test_slug.py', '# 合成子路径fixture，仅核验允许写集归属。\n')
            command = [sys.executable, '-B', '-c', "from slug import slug; assert slug('Hello World') == 'hello-world'"]
            checked = subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=False)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            saved = P.run_cli(ROOT, workspace, 'evidence', '--expected-task-id', state['task_id'],
                              '--expected-revision', str(state['revision']), '--kind', 'verification',
                              '--summary', 'Actual normal input check passed after authorized docs/tests writes',
                              '--result', 'pass', '--command', json.dumps(command), '--check-id', 'normal',
                              '--package-id', 'slug', '--actor', 'journey-worker')
            self.assertEqual(saved['work_packages'][0]['status'], 'active')
            self.assertFalse(saved['work_packages'][0].get('scope_drift', False))
            self.assertEqual(saved['work_packages'][0]['evidence'][-1]['result'], 'pass')
            P.write(workspace / 'undeclared.py', '# Undeclared synthetic path must remain outside this package.\n')
            with self.assertRaisesRegex(ValueError, 'scope drift|active or complete'):
                P.run_cli(ROOT, workspace, 'evidence', '--expected-task-id', saved['task_id'],
                          '--expected-revision', str(saved['revision']), '--kind', 'verification',
                          '--summary', 'Attempted evidence after undeclared write must be rejected',
                          '--result', 'pass', '--check-id', 'normal', '--package-id', 'slug',
                          '--actor', 'journey-worker')

    def test_trace_candidates_are_deduplicated_and_semantic_metrics_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trace.jsonl'
            items = [
                {'type': 'commandExecution', 'id': 'c1', 'command': 'cat 合同.md', 'exitCode': 0},
                {'type': 'commandExecution', 'id': 'c2', 'command': 'python3 longtask_state.py route', 'exitCode': 1},
                {'type': 'fileChange', 'id': 'f1', 'status': 'completed', 'changes': [{'path': '/fixture/slug.py'}]},
            ]
            rows = [{'direction': 'server_to_client', 'elapsed_ms': 100 + i,
                     'message': {'method': 'item/completed', 'params': {'item': item}}}
                    for i, item in enumerate(items + items[:1])]
            path.write_text('\n'.join(json.dumps(row) for row in rows))
            result = P.trace_observations({'latency_ms': 200}, path, '/fixture')
            self.assertEqual(result['unique_observed_tool_ids'], ['c1', 'c2', 'f1'])
            self.assertEqual(result['material_read_candidate_ids'], ['c1'])
            self.assertEqual(result['state_cli_candidate_ids'], ['c2'])
            self.assertEqual(result['nonzero_exit_candidate_ids'], ['c2'])
            self.assertEqual(result['first_application_change_event']['item_id'], 'f1')
            for key in ('semantic_material_bytes', 'unjustified_rechecks', 'wrong_permission_questions',
                        'corrective_calls', 'first_effective_advancement'):
                self.assertIsNone(result[key])

    def test_application_event_is_unknown_for_shell_only_write(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trace.jsonl'
            trace.write_text(json.dumps({'direction': 'server_to_client', 'elapsed_ms': 1,
                'message': {'method': 'item/completed', 'params': {'item': {
                    'type': 'commandExecution', 'id': 'shell', 'command': 'echo code > slug.py', 'exitCode': 0}}}}))
            result = P.trace_observations({}, trace)
            self.assertIsNone(result['first_application_change_event'])

    def test_product_probe_catches_real_failure_and_supplemental_constraint(self):
        cases = {case['id']: case for case in P.load_cases()[1]}
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'fixture'
            P.fixture(workspace, ROOT, cases['narrow-low-risk'])
            stages = [{'status': 'collected'}]
            self.assertEqual(P.acceptance(workspace, cases['narrow-low-risk'], stages)['status'], 'fail')
            (workspace / 'slug.py').write_text("def slug(text):\n    return '-'.join(text.lower().split())\n")
            self.assertEqual(P.acceptance(workspace, cases['narrow-low-risk'], stages)['status'], 'pass')
            continuation_stages = [{'status': 'collected'}, {'status': 'collected'}]
            result = P.acceptance(workspace, cases['inserted-constraint'], continuation_stages)
            self.assertEqual(result['status'], 'fail')
            self.assertFalse(result['checks']['None-TypeError'])
            (workspace / 'slug.py').write_text("def slug(text):\n    if not isinstance(text,str): raise TypeError('str required')\n    return '-'.join(text.lower().split())\n")
            self.assertEqual(P.acceptance(workspace, cases['inserted-constraint'], continuation_stages)['status'], 'pass')

    def test_failed_host_cannot_pass_from_valid_product(self):
        case = P.load_cases()[1][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'slug.py').write_text("def slug(text): return '-'.join(text.lower().split())\n")
            self.assertEqual(P.acceptance(root, case, [{'status': 'unable_to_verify'}])['status'], 'unable_to_verify')

    def test_planning_then_authorized_continuation_requires_both_stages(self):
        case = next(c for c in P.load_cases()[1] if c['id'] == 'planning-only')
        self.assertEqual([phase['id'] for phase in case['phases']], ['plan', 'continue'])
        self.assertIn('禁止实现slug.py', case['phases'][0]['instruction'])
        self.assertIn('新增实施授权', case['phases'][1]['instruction'])
        self.assertEqual(case['acceptance'], 'application')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = P.inventory(root)
            (root / 'plan.md').write_text('synthetic bounded plan')
            after = P.inventory(root)
            first_stage = {'status': 'collected', 'phase': 'plan', 'before': before, 'after': after}
            self.assertNotIn('slug.py', first_stage['before'])
            self.assertNotIn('slug.py', first_stage['after'])
            self.assertEqual(P.acceptance(root, case, [first_stage])['status'], 'unable_to_verify')
            (root / 'slug.py').write_text("def slug(text): return '-'.join(text.lower().split())\n")
            self.assertEqual(P.acceptance(root, case, [first_stage])['status'], 'unable_to_verify')
            second_stage = {'status': 'collected', 'phase': 'continue'}
            self.assertEqual(P.acceptance(root, case, [first_stage, second_stage])['status'], 'pass')

    def test_supplemental_changed_goal_preserves_original_bytes_and_only_checks_new_boundary(self):
        case = next(c for c in P.load_cases()[1] if c['id'] == 'changed-goal')
        self.assertTrue(case['supplemental'])
        self.assertEqual([phase['id'] for phase in case['phases']], ['prepare', 'replace'])
        self.assertIn('明确撤销', case['phases'][1]['instruction'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'fixture'
            P.fixture(root, ROOT, case)
            original = (root / 'slug.py').read_bytes()
            before = P.inventory(root)
            (root / 'plan.md').write_text('synthetic preparation, implementation not yet authorized')
            first_stage = {'status': 'collected', 'phase': 'prepare', 'before': before,
                           'after': P.inventory(root)}
            self.assertEqual(first_stage['before']['slug.py'], first_stage['after']['slug.py'])
            self.assertEqual(P.acceptance(root, case, [first_stage])['status'], 'unable_to_verify')
            P.write(root / 'docs/变更说明.md', '原实现目标撤销；当前仅文档交付。')
            second_stage = {'status': 'collected', 'phase': 'replace',
                            'before': P.inventory(root), 'after': P.inventory(root)}
            stages = [first_stage, second_stage]
            grade = P.acceptance(root, case, stages)
            self.assertEqual(grade['status'], 'pass')
            self.assertEqual((root / 'slug.py').read_bytes(), original)
            self.assertIn('semantics require independent review', grade['reason'])
            # Making the old application pass violates the new fixed byte boundary.
            (root / 'slug.py').write_text("def slug(text): return '-'.join(text.lower().split())\n")
            grade = P.acceptance(root, case, stages)
            self.assertEqual(grade['status'], 'fail')
            self.assertFalse(grade['checks']['original_application_bytes_preserved'])

    def failure_sample(self, root, corrupt=None):
        """Mock transport failure only, exercising the full retained evidence path."""
        source = root / 'source'
        source.mkdir()
        (source / 'SKILL.md').write_text('source-explicit synthetic skill')
        inputs, snapshots = P.bindings(), {}
        out = root / 'private'
        out.mkdir()
        for name in P.BINDINGS:
            target = out / 'inputs' / name
            P.write(target, (ROOT / name).read_bytes())
            snapshots[name] = str(target)
        args = Namespace(timeout=1, retention_until='2099-01-01T00:00:00Z')
        case = next(c for c in P.load_cases()[1] if c['id'] == 'new-project')
        def failed_collector(command, **kwargs):
            raw = Path(command[command.index('--out') + 1])
            raw.mkdir()
            trace = raw / 'trace.jsonl'
            trace.write_text('')
            summary = {'status': 'unable_to_verify', 'failure_class': 'TimeoutError',
                       'collector_run_id': 'observed-run-failure', 'thread_id': 'observed-thread-failure',
                       'last_turn_id': 'observed-turn-failure', 'trace_path': str(trace),
                       'trace_sha256': P.digest(trace), 'latency_ms': 1000,
                       'evaluated_archive_sha256': command[command.index('--evaluated-archive-sha256') + 1]}
            if corrupt == 'summary-json':
                (raw / 'summary.json').write_text('{')
            elif corrupt == 'summary-type':
                P.write_json(raw / 'summary.json', [])
            elif corrupt == 'summary-status':
                P.write_json(raw / 'summary.json', {'status': 'invented'})
            else:
                if corrupt == 'trace-json':
                    trace.write_text('{')
                    summary['trace_sha256'] = P.digest(trace)
                elif corrupt == 'trace-type':
                    trace.write_text('[]\n')
                    summary['trace_sha256'] = P.digest(trace)
                P.write_json(raw / 'summary.json', summary)
            return Namespace(returncode=1, stdout='', stderr='timeout preserved')
        with mock.patch.object(P.subprocess, 'run', side_effect=failed_collector):
            sample = P.collect_case(args, case, source, out, inputs, snapshots)
        data = {'schema_version': 1, 'suite': 'source_explicit_progress_journeys',
                'inputs_sha256': inputs, 'samples': [sample]}
        return data

    def test_timeout_is_retained_as_observed_failure_and_bindings_verify(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.failure_sample(Path(directory))
            sample = data['samples'][0]
            self.assertEqual(sample['stages'][0]['failure_class'], 'TimeoutError')
            self.assertEqual(sample['grade']['status'], 'unable_to_verify')
            self.assertEqual(P.verify(data), [])
            self.assertEqual(P.verify(data, private=True), [])

    def test_present_invalid_summary_or_trace_is_retained_and_privately_verifiable(self):
        for corrupt in ('summary-json', 'summary-type', 'summary-status', 'trace-json', 'trace-type'):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                data = self.failure_sample(Path(directory), corrupt=corrupt)
                sample = data['samples'][0]
                stage = sample['stages'][0]
                self.assertEqual(stage['status'], 'unable_to_verify')
                self.assertTrue(stage['artifact_error'])
                self.assertEqual(sample['grade']['status'], 'unable_to_verify')
                self.assertTrue(stage['summary_sha256'])
                failure = json.loads(Path(stage['failure_path']).read_text())
                self.assertEqual(failure['stderr'], 'timeout preserved')
                self.assertEqual(failure['exit_code'], 1)
                self.assertEqual(P.verify(data), [])
                self.assertEqual(P.verify(data, private=True), [])

    def test_result_symlink_preflight_fails_before_any_collector_call(self):
        import sys
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / 'outside'
            outside.mkdir()
            (root / 'link').symlink_to(outside, target_is_directory=True)
            argv = ['run_progress_evals.py', '--collect', '--case', 'new-project',
                    '--out', str(root / 'private'), '--results', str(root / 'link/result.json'),
                    '--retention-until', '2099-01-01T00:00:00Z']
            with mock.patch.object(sys, 'argv', argv), mock.patch.object(P.subprocess, 'run') as run:
                with self.assertRaises(OSError):
                    P.main()
                run.assert_not_called()
            self.assertFalse((outside / 'result.json').exists())
            self.assertFalse((root / 'private').exists())

    def test_collector_without_summary_preserves_actual_failure_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('source explicit fixture')
            out = root / 'private'
            out.mkdir()
            args = Namespace(timeout=1, retention_until='2099-01-01T00:00:00Z')
            case = P.load_cases()[1][0]
            with mock.patch.object(P.subprocess, 'run', return_value=Namespace(
                    returncode=7, stdout='actual output', stderr='actual transport failure')):
                sample = P.collect_case(args, case, source, out, P.bindings(),
                                        {'scripts/collect_host_trace.py': str(ROOT / 'scripts/collect_host_trace.py')})
            stage = sample['stages'][0]
            self.assertEqual(stage['status'], 'unable_to_verify')
            self.assertEqual(sample['grade']['status'], 'unable_to_verify')
            self.assertEqual(stage['collector_exit_code'], 7)
            actual = json.loads(Path(stage['failure_path']).read_text())
            self.assertEqual(actual['stderr'], 'actual transport failure')
            self.assertEqual(P.digest(stage['failure_path']), stage['failure_sha256'])

    def test_failed_or_outside_application_filechange_does_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / 'trace.jsonl'
            rows = [{'direction': 'server_to_client', 'elapsed_ms': 1,
                     'message': {'method': 'item/completed', 'params': {'item': item}}} for item in (
                {'id': 'failed', 'type': 'fileChange', 'status': 'failed', 'changes': [{'path': '/fixture/slug.py'}]},
                {'id': 'outside', 'type': 'fileChange', 'status': 'completed', 'changes': [{'path': '/outside/slug.py'}]})]
            trace.write_text('\n'.join(json.dumps(row) for row in rows))
            self.assertIsNone(P.trace_observations({}, trace, '/fixture')['first_application_change_event'])

    def test_case_runner_inputs_host_identity_and_results_cannot_be_rebound(self):
        for mutation in ('inputs', 'case', 'thread', 'source', 'grade'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                data = self.failure_sample(Path(directory))
                sample = data['samples'][0]
                identity = sample['collection_identity']
                if mutation == 'inputs':
                    data['inputs_sha256']['scripts/run_progress_evals.py'] = '0' * 64
                elif mutation == 'case':
                    identity['case_target_sha256'] = '0' * 64
                elif mutation == 'thread':
                    sample['stages'][0]['thread_id'] = 'replacement'
                elif mutation == 'source':
                    identity['source_sha256']['SKILL.md'] = '0' * 64
                elif mutation == 'grade':
                    sample['grade']['status'] = 'pass'
                self.assertTrue(P.verify(data))

    def test_private_changed_bytes_paths_sources_and_extra_source_files_rejected(self):
        for mutation in ('trace', 'summary', 'input', 'source', 'extra-source', 'initial', 'final', 'identity'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                data = self.failure_sample(Path(directory))
                sample = data['samples'][0]
                identity = sample['collection_identity']
                targets = {'trace': sample['stages'][0]['trace_path'],
                           'summary': sample['stages'][0]['summary_path'],
                           'input': identity['collection_snapshot_paths']['scripts/run_progress_evals.py'],
                           'source': str(Path(identity['source_path']) / 'SKILL.md'),
                           'extra-source': str(Path(identity['source_path']) / 'scripts/new.py'),
                           'initial': str(Path(identity['initial_fixture_path']) / '合同.md'),
                           'final': str(Path(identity['fixture_path']) / '合同.md'),
                           'identity': sample['collection_identity_path']}
                path = Path(targets[mutation])
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('changed actual bytes')
                self.assertTrue(P.verify(data, private=True))

    def test_duplicate_actual_runs_or_threads_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.failure_sample(Path(directory))
            data['samples'].append(copy.deepcopy(data['samples'][0]))
            self.assertTrue(any('reuse' in error for error in P.verify(data)))

    def test_semantic_success_without_bound_independent_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            data = self.failure_sample(Path(directory))
            sample = data['samples'][0]
            sample['semantic_status'] = 'pass'
            self.assertTrue(any('Semantic success' in error for error in P.verify(data)))
            sample['semantic_judgments'] = [{'status': 'pass', 'reviewer': 'claimed-reviewer'}]
            self.assertTrue(any('Semantic judgment' in error for error in P.verify(data)))

    def test_semantic_status_conservatively_aggregates_all_retained_judgments(self):
        for statuses, expected in (([], 'unknown'), (['pass'], 'pass'),
                                   (['pass', 'pass'], 'pass'), (['pass', 'fail'], 'fail'),
                                   (['unable_to_verify', 'fail'], 'fail'),
                                   (['pass', 'unable_to_verify'], 'unable_to_verify')):
            with self.subTest(statuses=statuses):
                self.assertEqual(P.semantic_status([{'status': status} for status in statuses]), expected)

    def test_bound_actual_private_failed_judgment_cannot_be_labelled_semantic_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.failure_sample(root)
            sample = data['samples'][0]
            identity = sample['collection_identity']
            payload = {'status': 'fail', 'reviewer': 'synthetic-test-independent-reviewer',
                       'evidence': ['Actual bound synthetic test artifact records failed judgment'],
                       'binding': {'collection_identity_sha256': sample['collection_identity_sha256'],
                                   'source_manifest_sha256': identity['evaluated_archive_sha256'],
                                   'case_target_sha256': identity['case_target_sha256'],
                                   'trace_sha256': [stage['trace_sha256'] for stage in identity['stages']]}}
            artifact = root / 'actual-semantic-judgment.json'
            P.write_json(artifact, payload)
            judgment = {**payload, 'artifact_path': str(artifact), 'artifact_sha256': P.digest(artifact)}
            sample['semantic_judgments'] = [judgment]
            sample['semantic_status'] = 'pass'
            sample['sample_sha256'] = P.canonical({key: value for key, value in sample.items() if key != 'sample_sha256'})
            for private in (False, True):
                self.assertTrue(any('Semantic status' in error for error in P.verify(data, private=private)))
            sample['semantic_status'] = 'fail'
            sample['sample_sha256'] = P.canonical({key: value for key, value in sample.items() if key != 'sample_sha256'})
            self.assertEqual(P.verify(data), [])
            self.assertEqual(P.verify(data, private=True), [])


if __name__ == '__main__':
    unittest.main()
