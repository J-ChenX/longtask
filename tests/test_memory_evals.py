"""Behavior-result contracts, not mocked model success or compression savings."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('memory_eval', ROOT / 'scripts/run_memory_evals.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class MemoryEvalTests(unittest.TestCase):
    def test_corpus_preserves_repeats_controls_and_real_compaction(self):
        manifest, cases = M.load_cases()
        self.assertEqual(manifest['repetitions'], 2)
        self.assertEqual(cases[1]['phases'], ['investigate', 'compact', 'recover'])
        self.assertIn('without_save', cases[1]['arms'])
        self.assertNotIn('without_save', cases[0]['arms'])

    def test_missing_usage_is_unknown_not_zero(self):
        sample = {'case_id': 'x', 'arm': 'previous', 'grade': {'status': 'unable_to_verify'},
                  'stages': [{'observations': {'latency_ms': 12, 'tool_calls': 0, 'compactions': 0, 'usage': None}}]}
        groups = M.aggregate([sample])
        self.assertIsNone(groups[0]['metrics']['input_tokens_mean'])
        self.assertEqual(groups[0]['unable_to_verify'], 1)
        self.assertEqual(groups[0]['passed'], 0)

    def test_missing_host_phase_cannot_pass_from_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'report.json').write_text('{}')
            self.assertEqual(M.grade('investigation-resume', root,
                [{'status': 'unable_to_verify'}])['status'], 'unable_to_verify')

    def test_actual_compaction_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'report.json').write_text('{}')
            stage = {'status': 'collected', 'observations': {'compactions': 0}}
            self.assertEqual(M.grade('investigation-resume', root, [stage])['status'], 'unable_to_verify')

    def test_repeated_failed_experiment_fails_actual_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'module.md').write_text('contract')
            report = {'source_refs': ['module.md'], 'known_failure': 'legacy fails on fixture-v1',
                      'hypothesis_status': 'inferred', 'missing_acceptance': ['empty-file'],
                      'runtime_mode': 'offline', 'next_action': 'verify empty-file'}
            (root / 'report.json').write_text(json.dumps(report))
            stages = [{'status': 'collected', 'observations': {'compactions': 1}},
                      {'status': 'collected', 'observations': {'compactions': 0,
                       'command_observations': [{'executed_scripts': ['probe_legacy.py'], 'exit_code': 3},
                                                {'executed_scripts': ['verify_runtime.py'], 'exit_code': 0}]}}]
            result = M.grade('investigation-resume', root, stages)
            self.assertEqual(result['status'], 'fail')
            self.assertFalse(result['checks']['no_unchanged_failure_retry'])

    def test_unknown_any_sample_blocks_full_group_usage(self):
        def sample(usage):
            return {'case_id': 'x', 'arm': 'candidate', 'grade': {'status': 'pass'},
                    'stages': [{'observations': {'latency_ms': 10, 'tool_calls': 1, 'compactions': 0, 'usage': usage}}]}
        result = M.aggregate([sample({'inputTokens': 20}), sample(None)])
        self.assertIsNone(result[0]['metrics']['input_tokens_mean'])

    def test_stale_inputs_and_duplicate_threads_rejected(self):
        s = {'stages': [{'status': 'collected', 'thread_id': 'same',
                        'observations': {'latency_ms': 1, 'tool_calls': 0, 'compactions': 0, 'usage': None}}],
             'case_id': 'x', 'arm': 'candidate', 'grade': {'status': 'pass'}}
        data = {'schema_version': 1, 'suite': 'source_explicit_memory_consumption',
                'inputs_sha256': {}, 'samples': [s, s], 'groups': M.aggregate([s, s])}
        errors = M.verify(data)
        self.assertTrue(any('stale' in e for e in errors))
        self.assertTrue(any('reuse' in e for e in errors))



class SourceAblationTests(unittest.TestCase):
    def test_b_and_d_remove_interfaces_in_valid_source_and_keep_cas(self):
        import ast
        for arm in ('without_overview', 'without_save'):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / 'ablated'
                result = M.make_ablation(ROOT, target, arm)
                text = (target / 'scripts/longtask_state.py').read_text()
                ast.parse(text)
                self.assertTrue(result['changed_sources'])
                self.assertIn('expected_task_id', text)
                self.assertIn('expected_revision', text)
                self.assertEqual(M.digest(result['diff_path']), result['diff_sha256'])

    def test_chinese_empty_array_is_scored_as_same_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'contract.md').write_text('contract')
            M.write_json(root / 'report.json', {'package_id': 'p73',
                'module_contract': {'delimiter': ';', 'empty_behavior': '空文件必须输出空数组 []',
                                    'verification': 'python3 scripts/verify_runtime.py'},
                'source_refs': ['contract.md'], 'risks': []})
            self.assertEqual(M.grade('frontier-knowledge', root, [{'status': 'collected'}])['status'], 'pass')

    def test_external_environment_is_outside_workspace_and_snapshot_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'fixture'
            external = Path(directory) / 'environment.json'
            manifest = M.fixture(workspace, ROOT, 'investigation-resume', external)
            before = M.digest(external)
            M.write_json(external, {'mode': 'offline', 'fixture_version': 'fixture-v1'})
            self.assertNotEqual(before, M.digest(external))
            self.assertNotIn('environment.json', manifest)
            self.assertIn(str(external), (workspace / 'scripts/verify_runtime.py').read_text())

    def test_refresh_keeps_collection_binding_and_rejects_modified_trace(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner, corpus, trace = root / 'runner.py', root / 'cases.json', root / 'trace.jsonl'
            runner.write_text('original runner')
            corpus.write_text('original corpus')
            trace.write_text('{}\n')
            _, cases = M.load_cases()
            case = cases[0]
            collection = {'scripts/run_memory_evals.py': M.digest(runner), 'evals/memory_cases.json': M.digest(corpus)}
            sample = {'case_id': case['id'], 'arm': 'no_skill', 'fixture_path': str(root),
                      'collection_inputs_sha256': collection.copy(), 'collection_runner_path': str(runner),
                      'collection_cases_path': str(corpus), 'case_target_sha256': M.canonical({k:case[k] for k in ('id','phases','goal') if k in case}),
                      'stages': [{'trace_path': str(trace), 'trace_sha256': '0' * 64}]}
            with self.assertRaisesRegex(ValueError, 'trace_path binding mismatch'):
                M.refresh({'samples': [sample]})
            self.assertEqual(sample['collection_inputs_sha256'], collection)

class FrozenEvidenceTests(unittest.TestCase):
    def test_phase_source_drift_keeps_start_digest_and_marks_unverifiable(self):
        from argparse import Namespace
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('frozen source')
            original = M.source_manifest(source)
            out = root / 'out'
            out.mkdir()
            args = Namespace(candidate=source, retention_until='2099-01-01T00:00:00Z', timeout=2)
            case = M.load_cases()[1][0]
            def fixture(workspace, source, case_id, external):
                workspace.mkdir()
                M.write_json(external, {'mode': 'online'})
                (workspace / '.longtask').mkdir()
                M.write_json(workspace / '.longtask/state.json', {'status':'planned'})
                return {}
            def backend(command, **kwargs):
                raw = Path(command[command.index('--out')+1])
                raw.mkdir()
                trace = raw / 'trace.jsonl'
                trace.write_text('')
                summary = {'status': 'collected', 'failure_class': None, 'collector_run_id': 'run',
                           'thread_id': 'thread', 'last_turn_id': 'turn', 'session_id': None,
                           'trace_path': str(trace), 'trace_sha256': M.digest(trace),
                           'configured_model': 'observed-config', 'model_rerouting_events': [],
                           'codex_version': 'observed-version', 'usage_updates': [], 'tool_ids': [],
                           'latency_ms': 1, 'compaction_events': [],
                           'evaluated_archive_sha256': command[command.index('--evaluated-archive-sha256')+1]}
                M.write_json(raw / 'summary.json', summary)
                (source / 'SKILL.md').write_text('changed during phase')
                return Namespace(returncode=0, stderr='')
            with mock.patch.object(M, 'fixture', side_effect=fixture), mock.patch.object(M.subprocess, 'run', side_effect=backend):
                sample = M.collect(args, case, 'candidate', 1, source, out)
            self.assertEqual(sample['source_sha256'], original)
            self.assertEqual(sample['evaluated_archive_sha256'], M.canonical(original))
            self.assertEqual(sample['stages'][0]['evaluated_archive_sha256'], M.canonical(original))
            self.assertEqual(sample['grade']['status'], 'unable_to_verify')
            self.assertEqual(sample['input_drift'][0]['boundary'], 'after')

    def test_refresh_rejects_changed_summary_runner_report_or_source(self):
        from unittest import mock
        for changed in ('summary', 'runner', 'report', 'source'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                paths = {name:root/name for name in ('summary','runner','corpus','trace','report')}
                for path in paths.values():
                    path.write_text('{}\n')
                source = root / 'source'
                source.mkdir()
                (source / 'SKILL.md').write_text('source')
                _, cases = M.load_cases()
                case = cases[0]
                sample = {'case_id':case['id'], 'arm':'candidate', 'fixture_path':str(root),
                          'collection_inputs_sha256':{'scripts/run_memory_evals.py':M.digest(paths['runner']),
                                                      'evals/memory_cases.json':M.digest(paths['corpus'])},
                          'collection_runner_path':str(paths['runner']), 'collection_cases_path':str(paths['corpus']),
                          'case_target_sha256':M.canonical({k:case[k] for k in ('id','phases','goal') if k in case}),
                          'source_path':str(source), 'source_sha256':M.source_manifest(source),
                          'source_manifest_sha256':M.canonical(M.source_manifest(source)),
                          'artifact_snapshot':{'path':str(paths['report']), 'exists':True,'sha256':M.digest(paths['report'])},
                          'stages':[{'trace_path':str(paths['trace']),'trace_sha256':M.digest(paths['trace']),
                                     'summary_path':str(paths['summary']),'summary_sha256':M.digest(paths['summary'])}]}
                before = json.loads(json.dumps(sample))
                (source/'SKILL.md' if changed=='source' else paths[changed]).write_text('replacement')
                with mock.patch.object(M, 'observations') as observations, mock.patch.object(M, 'grade') as grade:
                    with self.assertRaisesRegex(ValueError, 'binding mismatch'):
                        M.refresh({'samples':[sample]})
                    observations.assert_not_called()
                    grade.assert_not_called()
                self.assertEqual(sample, before)

class SourceSnapshotSafetyTests(unittest.TestCase):
    def test_symlink_to_unrelated_sibling_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            (source / 'scripts').mkdir(parents=True)
            (source / 'SKILL.md').write_text('public source')
            sibling = root / 'unrelated.py'
            sibling.write_text('synthetic unrelated content')
            (source / 'scripts/linked.py').symlink_to(sibling)
            with self.assertRaisesRegex(ValueError, 'symlink is forbidden'):
                M.copy_source_snapshot(source, root / 'snapshot')
            self.assertFalse((root / 'snapshot').exists())

    def test_private_root_files_and_state_are_outside_source_closure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            source.mkdir()
            (source / 'SKILL.md').write_text('public source')
            (source / '.env').write_text('synthetic private marker')
            (source / '.longtask').mkdir()
            (source / '.longtask/state.json').write_text('{}')
            (source / 'unrelated.json').write_text('synthetic unrelated root file')
            target = root / 'snapshot'
            M.copy_source_snapshot(source, target)
            self.assertEqual(M.source_manifest(source), M.source_manifest(target))
            self.assertEqual([str(p.relative_to(target)) for p in target.rglob('*')], ['SKILL.md'])

class RecoveryReferenceTests(unittest.TestCase):
    def test_structured_relative_and_exact_external_refs_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / 'fixture'
            workspace.mkdir()
            (workspace / 'module.md').write_text('synthetic contract')
            external = root / 'environment.json'
            external.write_text('{}')
            self.assertTrue(M.valid_reference({'path':'module.md', 'section':'调查', 'supports':'observed'}, workspace, external))
            self.assertTrue(M.valid_reference({'path':str(external), 'section':None}, workspace, external))
            self.assertFalse(M.valid_reference(str(external), workspace))
            self.assertFalse(M.valid_reference({'path':'../environment.json'}, workspace, external))
            self.assertFalse(M.valid_reference({'path':str(root/'other.json')}, workspace, external))
            self.assertFalse(M.valid_reference({'path':'missing.md'}, workspace, external))

class ArtifactAndWriteBoundaryTests(unittest.TestCase):
    def test_nonobject_report_or_contract_is_retained_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for report in ([], {'module_contract': []}):
                M.write_json(root/'report.json', report)
                result = M.grade('frontier-knowledge', root, [{'status':'collected'}])
                self.assertEqual(result['status'], 'fail')

    def test_missing_risks_fails_but_explicit_empty_risks_are_legal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'contract.md').write_text('contract')
            report = {'package_id':'p73', 'module_contract':{'delimiter':';','empty_behavior':'[]','verification':'verify_runtime.py'},
                      'source_refs':['contract.md']}
            M.write_json(root/'report.json', report)
            self.assertEqual(M.grade('frontier-knowledge',root,[{'status':'collected'}])['status'],'fail')
            report['risks'] = []
            M.write_json(root/'report.json', report)
            self.assertEqual(M.grade('frontier-knowledge',root,[{'status':'collected'}])['status'],'pass')

    def test_unauthorized_recovery_write_fails_even_with_correct_report(self):
        audit = M.phase_write_audit('recover', {'product.py':'before'}, {'product.py':'after','report.json':'report'})
        self.assertEqual(audit['status'],'fail')
        self.assertEqual(audit['unexpected_paths'],['product.py'])
        self.assertEqual(M.phase_write_audit('investigate',{}, {'docs/modules/导入.md':'x','.longtask/state.json':'y'})['status'],'pass')
        self.assertEqual(M.phase_write_audit('compact',{}, {'report.json':'x'})['status'],'fail')
        self.assertEqual(M.phase_write_audit('investigate',{}, {},'old','new')['status'],'fail')

class ChapterReferenceTests(unittest.TestCase):
    def test_string_markdown_anchor_must_be_unique_but_structured_label_is_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'module.md').write_text('# 模块\n## 当前合同\n## 重复\n## 重复\n')
            self.assertTrue(M.valid_reference('module.md#当前合同', root))
            self.assertFalse(M.valid_reference('module.md#不存在', root))
            self.assertFalse(M.valid_reference('module.md#重复', root))
            self.assertTrue(M.valid_reference({'path':'module.md','section':'自然语言定位说明'}, root))

class ObservedRuntimeModeFormatTests(unittest.TestCase):
    def test_observed_structured_current_mode_meets_untyped_original_goal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'module.md').write_text('contract')
            M.write_json(root/'report.json', {'known_failure':{'name':'legacy','condition':'fixture-v1'},
                'hypothesis_status':{'status':'inferred'}, 'missing_acceptance':['empty-file'],
                'runtime_mode':{'current':'offline','previous':'online'}, 'next_action':['empty-file'],
                'source_refs':[{'path':'module.md','section':'plain language'}]})
            stages = [{'status':'collected','observations':{'compactions':1}},
                      {'status':'collected','observations':{'compactions':0,'command_observations':[{'executed_scripts':['verify_runtime.py'],'exit_code':0}]}}]
            self.assertEqual(M.grade('investigation-resume',root,stages)['status'],'pass')

class UnsafeReportTests(unittest.TestCase):
    def test_report_link_targets_are_never_read_or_hashed(self):
        from unittest import mock
        for kind in ('symlink', 'hardlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root/'fixture'
                workspace.mkdir()
                sibling = root/'synthetic-unrelated.json'
                sibling.write_text('{"synthetic_private_marker":true}')
                report = workspace/'report.json'
                if kind=='symlink':
                    report.symlink_to(sibling)
                else:
                    import os
                    os.link(sibling, report)
                with mock.patch.object(Path,'read_text',side_effect=AssertionError('Unsafe read_text call')):
                    content, snapshot = M.read_artifact(report)
                    self.assertIsNone(content)
                    self.assertFalse(snapshot['safe'])
                    self.assertIsNone(snapshot['sha256'])
                    self.assertTrue(snapshot['exists'])
                    grade = M.grade('frontier-knowledge',workspace,[{'status':'collected'}])
                    self.assertEqual(grade['status'],'fail')
                    self.assertFalse(grade['checks']['safe_artifact'])
                inventory = M.fixture_inventory(workspace)
                self.assertTrue(inventory['report.json'].startswith(('symlink:','unsafe-')))
                self.assertEqual(M.phase_write_audit('recover',{},inventory)['status'],'fail')

    def test_missing_report_snapshot_is_bound_without_guessing_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            content, snapshot = M.read_artifact(Path(directory)/'report.json')
            self.assertIsNone(content)
            self.assertFalse(snapshot['exists'])
            self.assertTrue(snapshot['safe'])
            self.assertIsNone(snapshot['sha256'])

class ReferenceHardlinkSafetyTests(unittest.TestCase):
    def test_markdown_reference_hardlink_does_not_read_target(self):
        from unittest import mock
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            workspace=root/'fixture'
            workspace.mkdir()
            sibling=root/'synthetic-unrelated.md'
            sibling.write_text('# synthetic-private-marker\n')
            os.link(sibling,workspace/'alias.md')
            with mock.patch.object(Path,'read_text',side_effect=AssertionError('Unsafe anchor reader')):
                self.assertFalse(M.valid_reference('alias.md#synthetic-private-marker',workspace))

class ControlledInputFingerprintTests(unittest.TestCase):
    def test_only_run_paths_and_timestamps_normalize_while_mode_remains_material(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            case=M.load_cases()[1][0]
            first=root/'first'
            second=root/'second'
            env1=root/'one.json'
            env2=root/'two.json'
            M.fixture(first,ROOT,case['id'],env1)
            M.fixture(second,ROOT,case['id'],env2)
            a=M.controlled_fixture_input(first,case,env1)
            b=M.controlled_fixture_input(second,case,env2)
            self.assertEqual(a['sha256'],b['sha256'])
            M.write_json(env2,{'mode':'offline','fixture_version':'fixture-v1'})
            self.assertNotEqual(a['sha256'],M.controlled_fixture_input(second,case,env2)['sha256'])

class HistoricalAppendTests(unittest.TestCase):
    def test_historical_sample_is_excluded_and_append_explicitly_rejected(self):
        sample = {'comparison_stratum': 'historical_noncomparable', 'case_id':'x', 'arm':'no_skill',
                  'grade':{'status':'pass'}, 'stages':[]}
        self.assertEqual(M.aggregate([sample]), [])
        with self.assertRaisesRegex(ValueError, 'Historical samples require independent new collection'):
            M.validated_append_samples({'samples':[sample]})

class ComparisonBindingTests(unittest.TestCase):
    def test_controlled_fingerprint_tampering_is_rejected(self):
        payload={'initial_environment':{'mode':'online'}}
        config={'model':'configured-default'}
        sample={'comparison_stratum':'matched_current_fixture',
                'controlled_fixture_input':{'payload':payload,'sha256':M.canonical(payload)},
                'stages':[{'host_configuration_observed':config}]}
        sample['controlled_input_sha256']=M.canonical({'fixture':M.canonical(payload),'host_configuration':config})
        self.assertEqual(M.comparison_errors(sample), [])
        payload['initial_environment']['mode']='offline'
        self.assertTrue(M.comparison_errors(sample))

    def test_source_manifest_must_match_preserved_caller_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source';source.mkdir()
            (source/'SKILL.md').write_text('actual source')
            runner=root/'runner.py';runner.write_text('actual runner')
            corpus=root/'cases.json';corpus.write_text('{}')
            report=root/'report.json';report.write_text('{}')
            _,artifact=M.read_artifact(report)
            manifest=M.source_manifest(source)
            sample={'stages':[], 'collection_inputs_sha256':{'scripts/run_memory_evals.py':M.digest(runner),'evals/memory_cases.json':M.digest(corpus)},
                    'collection_runner_path':str(runner),'collection_cases_path':str(corpus),
                    'source_path':str(source),'source_sha256':manifest,'source_manifest_sha256':M.canonical(manifest),
                    'evaluated_archive_sha256':'0'*64,'artifact_snapshot':artifact}
            self.assertIn('Collector caller digest differs from original source manifest',M.evidence_errors(sample))

class PrivateControlledReconstructionTests(unittest.TestCase):
    def make_sample(self, root):
        runner=root/'collection-runner.py'
        runner.write_text("import json\nfrom pathlib import Path\ndef fixture(workspace,source,case_id,external):\n workspace.mkdir();(workspace/'.longtask').mkdir()\n (workspace/'.longtask/state.json').write_text(json.dumps({'task_id':'synthetic'}))\n external.write_text(json.dumps({'mode':'online','fixture_version':'fixture-v1'}))\n return {}\n")
        source=root/'source-candidate';source.mkdir();(source/'SKILL.md').write_text('synthetic source')
        spec=importlib.util.spec_from_file_location('fixture_generator',runner);generator=importlib.util.module_from_spec(spec);spec.loader.exec_module(generator)
        workspace=root/'initial';external=root/'initial-environment.json';generator.fixture(workspace,source,'frontier-knowledge',external)
        case=M.load_cases()[1][0];fixture=M.controlled_fixture_input(workspace,case,external)
        trace=root/'trace.jsonl';trace.write_text(json.dumps({'direction':'client_to_server','message':{'id':1,'method':'thread/start'}})+'\n'+json.dumps({'direction':'server_to_client','message':{'id':1,'result':{'model':'actual-configured-model','sandbox':{'type':'workspaceWrite','networkAccess':False}}}})+'\n')
        config=M.host_configuration(trace)
        return {'case_id':case['id'],'comparison_stratum':'matched_current_fixture',
                'collection_runner_path':str(runner),'collection_inputs_sha256':{'scripts/run_memory_evals.py':M.digest(runner)},
                'controlled_fixture_input':fixture,'controlled_input_sha256':M.canonical({'fixture':fixture['sha256'],'host_configuration':config}),
                'stages':[{'trace_path':str(trace),'host_configuration_observed':config}],
                'environment':{'path':str(external)},'fixture_inputs_sha256':{}}

    def test_resigned_invented_environment_fails_real_generator_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            sample=self.make_sample(Path(directory))
            self.assertEqual(M.private_comparison_errors(sample),[])
            fixture=sample['controlled_fixture_input'];fixture['payload']['initial_environment']['mode']='invented-mode';fixture['sha256']=M.canonical(fixture['payload'])
            sample['controlled_input_sha256']=M.canonical({'fixture':fixture['sha256'],'host_configuration':sample['stages'][0]['host_configuration_observed']})
            self.assertEqual(M.comparison_errors(sample),[])
            self.assertIn('Controlled initial fixture differs from bound frozen generator',M.private_comparison_errors(sample))

    def test_resigned_invented_model_fails_actual_transport_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            sample=self.make_sample(Path(directory));sample['stages'][0]['host_configuration_observed']['model']='invented-model'
            sample['controlled_input_sha256']=M.canonical({'fixture':sample['controlled_fixture_input']['sha256'],'host_configuration':sample['stages'][0]['host_configuration_observed']})
            self.assertEqual(M.comparison_errors(sample),[])
            self.assertIn('Controlled host configuration differs from bound actual trace',M.private_comparison_errors(sample))

    def test_refresh_rejects_invented_model_before_rebinding_or_regrading(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            sample=self.make_sample(Path(directory));case=M.load_cases()[1][0]
            sample['case_target_sha256']=M.canonical({key:case[key] for key in ('id','phases','goal','investigation_goal') if key in case})
            sample['grade']={'status':'original'};sample['stages'][0]['host_configuration_observed']['model']='invented-model'
            sample['controlled_input_sha256']=M.canonical({'fixture':sample['controlled_fixture_input']['sha256'],'host_configuration':sample['stages'][0]['host_configuration_observed']})
            data={'samples':[sample],'inputs_sha256':{'original':'untouched'}}
            # Other original bindings have their own regressions; this test exercises
            # refresh ordering with a real immutable transport and fixture generator.
            with mock.patch.object(M,'evidence_errors',return_value=[]):
                with self.assertRaisesRegex(ValueError,'Controlled host configuration differs'):
                    M.refresh(data)
            self.assertEqual(sample['grade'],{'status':'original'})
            self.assertEqual(data['inputs_sha256'],{'original':'untouched'})


if __name__ == '__main__':
    unittest.main()
