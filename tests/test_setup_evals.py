"""Synthetic evidence tests prove binding/aggregation only, never model quality."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_evals', ROOT / 'scripts/run_setup_evals.py')
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


class SetupEvalTests(unittest.TestCase):
    def fixture(self, root):
        corpus = S.load_object(S.CORPUS)
        source = root / 'source'
        source.mkdir()
        (source / 'SKILL.md').write_text('synthetic skill fixture only')
        source_hashes = S.helper().source_manifest(source)
        inputs = S.bindings()
        samples = []
        for case in corpus['cases']:
            cid = case['id']
            stages = []
            for phase in case['phases']:
                trace = root / f'{cid}-{phase["id"]}.jsonl'
                trace.write_text(json.dumps({'synthetic': True, 'case': cid, 'phase': phase['id']}) + '\n')
                role = phase['role']
                stages.append({'phase_id': phase['id'], 'actor_id': cid + '-' + role,
                               'context_id': cid + '-' + role, 'status': 'collected',
                               'trace': {'path': str(trace), 'sha256': S.helper().digest(trace)}})
            document = root / (cid + '.md')
            document.write_text('Synthetic document. No actual model evaluation.')
            sample = {'id': cid + '-synthetic-run', 'case_id': cid,
                      'case_sha256': S.canonical(case), 'source_sha256': S.canonical(source_hashes),
                      'inputs_sha256': inputs, 'harness': 'synthetic-unit-fixture', 'model': None,
                      'collection_status': 'collected', 'stages': stages,
                      'documents': [{'path': str(document), 'sha256': S.helper().digest(document)}]}
            judgment = {'reviewer': 'synthetic-independent-test-reviewer', 'independent': True,
                        'sample_sha256': S.canonical(sample),
                        'dimensions': {dimension: {'status': 'pass', 'reason': 'Synthetic rubric binding test',
                                                  'evidence': [str(document) + ':1 synthetic only']}
                                       for dimension in case['dimensions']}}
            record = root / (cid + '-judgment.json')
            record.write_text(json.dumps(judgment, ensure_ascii=False))
            judgment['record'] = {'path': str(record), 'sha256': S.helper().digest(record)}
            sample['judgments'] = [judgment]
            samples.append(sample)
        results = {'schema_version': 1, 'suite': 'setup_document_quality',
                   'inputs_sha256': inputs, 'evaluated_source_sha256': source_hashes, 'samples': samples}
        return source, corpus, results

    def rebind_judgment(self, sample, status=None):
        """Synthetic mutation helper; not an evidence refresh API."""
        judgment = sample['judgments'][0]
        judgment['sample_sha256'] = S.canonical({k: v for k, v in sample.items() if k != 'judgments'})
        if status:
            for grade in judgment['dimensions'].values():
                grade['status'] = status
        record = Path(judgment['record']['path'])
        record.write_text(json.dumps({k: v for k, v in judgment.items() if k != 'record'}))
        judgment['record']['sha256'] = S.helper().digest(record)

    def test_representative_corpus_covers_contract_and_bad_dimensions_are_rejected(self):
        corpus = S.load_object(S.CORPUS)
        self.assertEqual(S.validate_cases(corpus), [])
        for mutation in ('missing-dimension', 'missing-scenario', 'unknown', 'duplicate', 'unsafe-fixture', 'missing-prompt'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(corpus)
                if mutation == 'missing-dimension':
                    for case in bad['cases']:
                        case['dimensions'] = [d for d in case['dimensions'] if d != 'SU01']
                elif mutation == 'missing-scenario':
                    bad['cases'].pop()
                elif mutation == 'unknown':
                    bad['cases'][0]['dimensions'].append('SU13')
                elif mutation == 'duplicate':
                    bad['cases'].append(copy.deepcopy(bad['cases'][0]))
                elif mutation == 'unsafe-fixture':
                    bad['cases'][0]['fixture_files']['../secret'] = 'invalid'
                else:
                    bad['cases'][0]['phases'][0]['prompt'] = ''
                self.assertTrue(S.validate_cases(bad))

    def test_consumer_isolation_and_multi_turn_contract_cannot_be_removed(self):
        corpus = S.load_object(S.CORPUS)
        for mutation in ('consumer-phase', 'consumer-role', 'multi-turn-phase'):
            with self.subTest(mutation=mutation):
                bad = copy.deepcopy(corpus)
                cid = 'multi-turn-scope' if mutation == 'multi-turn-phase' else 'independent-consumer'
                case = next(c for c in bad['cases'] if c['id'] == cid)
                if mutation == 'consumer-role':
                    case['phases'][-1]['role'] = 'author'
                else:
                    case['phases'].pop()
                self.assertTrue(S.validate_cases(bad))

    def test_bound_fixture_is_internally_consistent_without_authenticity_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            source, corpus, data = self.fixture(Path(directory))
            for private in (False, True):
                errors, report = S.verify(data, corpus, source, private)
                self.assertEqual(errors, [])
                self.assertEqual(report['behavior_status'], 'pass')
                self.assertEqual(report['release_gate'], 'not_evaluated')
                self.assertIn('verification_required', report['host_and_reviewer_authenticity'])

    def test_missing_samples_or_semantic_judgments_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            source, corpus, data = self.fixture(Path(directory))
            data['samples'].pop()
            errors, report = S.verify(data, corpus, source)
            self.assertEqual(errors, [])
            self.assertEqual(report['behavior_status'], 'unable_to_verify')
            data['samples'][0]['judgments'] = []
            errors, report = S.verify(data, corpus, source)
            self.assertEqual(report['cases'][data['samples'][0]['case_id']]['SU01'], 'unable_to_verify')

    def test_negative_and_unknown_samples_stay_in_all_dimension_aggregation(self):
        for status, expected in (('fail', 'fail'), ('unable_to_verify', 'unable_to_verify')):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                source, corpus, data = self.fixture(Path(directory))
                grade = data['samples'][0]['judgments'][0]['dimensions']['SU01']
                grade['status'] = status
                self.rebind_judgment(data['samples'][0])
                errors, report = S.verify(data, corpus, source, True)
                self.assertEqual(errors, [])
                self.assertEqual(report['dimensions']['SU01'], expected)
                self.assertEqual(report['behavior_status'], expected)

    def test_failed_repeat_cannot_be_hidden_by_successful_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, corpus, data = self.fixture(root)
            repeat = copy.deepcopy(data['samples'][0])
            repeat['id'] += '-failed-repeat'
            for stage in repeat['stages']:
                stage['context_id'] += '-repeat'
                trace = root / (repeat['id'] + stage['phase_id'] + '.jsonl')
                trace.write_text('Synthetic repeat trace only')
                stage['trace'] = {'path': str(trace), 'sha256': S.helper().digest(trace)}
            record = root / 'repeat-judgment.json'
            repeat['judgments'][0]['record']['path'] = str(record)
            self.rebind_judgment(repeat, 'fail')
            data['samples'].append(repeat)
            errors, report = S.verify(data, corpus, source, True)
            self.assertEqual(errors, [])
            self.assertEqual(report['cases'][repeat['case_id']]['SU01'], 'fail')
            self.assertEqual(report['sample_count'], len(corpus['cases']) + 1)

    def test_stale_source_inputs_targets_and_sample_bindings_reject(self):
        for mutation in ('source', 'inputs', 'target', 'sample', 'grading'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source, corpus, data = self.fixture(Path(directory))
                if mutation == 'source':
                    (source / 'SKILL.md').write_text('changed source')
                elif mutation == 'inputs':
                    data['inputs_sha256'] = {}
                elif mutation == 'target':
                    corpus['cases'][0]['phases'][0]['prompt'] += ' Changed target'
                elif mutation == 'sample':
                    data['samples'][0]['harness'] = 'replacement'
                else:
                    data['samples'][0]['judgments'][0]['dimensions'].pop('SU01')
                errors, report = S.verify(data, corpus, source)
                self.assertTrue(errors)
                self.assertNotEqual(report['behavior_status'], 'pass')

    def test_author_self_grading_or_consumer_context_reuse_cannot_pass(self):
        for mutation in ('author-grader', 'consumer-grader', 'same-consumer', 'same-run'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source, corpus, data = self.fixture(Path(directory))
                sample = data['samples'][-1]
                if mutation == 'author-grader':
                    sample['judgments'][0]['reviewer'] = sample['stages'][0]['actor_id']
                    self.rebind_judgment(sample)
                elif mutation == 'consumer-grader':
                    sample['judgments'][0]['reviewer'] = sample['stages'][1]['actor_id']
                    self.rebind_judgment(sample)
                elif mutation == 'same-consumer':
                    sample['stages'][1]['context_id'] = sample['stages'][0]['context_id']
                    self.rebind_judgment(sample)
                else:
                    data['samples'].append(copy.deepcopy(sample))
                errors, report = S.verify(data, corpus, source, True)
                self.assertTrue(errors)
                self.assertNotEqual(report['behavior_status'], 'pass')

    def test_valid_failure_survives_incomplete_collection_but_invalid_failure_is_diagnostic(self):
        for mutation in ('collection-unknown', 'missing-phase', 'changed-document'):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                source, corpus, data = self.fixture(Path(directory))
                sample = data['samples'][-1]
                sample['judgments'][0]['dimensions']['SU01']['status'] = 'fail'
                sample['collection_status'] = 'unable_to_verify'
                if mutation == 'missing-phase':
                    sample['stages'].pop()
                self.rebind_judgment(sample)
                if mutation == 'changed-document':
                    Path(sample['documents'][0]['path']).write_text('Changed evidence bytes')
                errors, report = S.verify(data, corpus, source, True)
                valid = mutation != 'changed-document'
                self.assertEqual(bool(errors), not valid)
                self.assertEqual(report['cases'][sample['case_id']]['SU01'],
                                 'fail' if valid else 'unable_to_verify')
                self.assertEqual(report['cases'][sample['case_id']]['SU02'], 'unable_to_verify')
                self.assertEqual(report['reported_failures'], [{'sample_id': sample['id'],
                    'case_id': sample['case_id'], 'dimension': 'SU01', 'evidence_valid': valid}])
                self.assertNotEqual(report['behavior_status'], 'pass')

    def test_private_trace_document_or_saved_judgment_drift_rejects(self):
        for field in ('trace', 'document', 'judgment'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                source, corpus, data = self.fixture(Path(directory))
                sample = data['samples'][0]
                artifact = {'trace': sample['stages'][0]['trace'], 'document': sample['documents'][0],
                            'judgment': sample['judgments'][0]['record']}[field]
                Path(artifact['path']).write_text('changed private bytes')
                self.assertTrue(S.verify(data, corpus, source, True)[0])

    def test_incomplete_collection_retains_unknown_and_cannot_claim_collected(self):
        with tempfile.TemporaryDirectory() as directory:
            source, corpus, data = self.fixture(Path(directory))
            sample = data['samples'][-1]
            sample['stages'].pop()
            sample['collection_status'] = 'unable_to_verify'
            sample['judgments'] = []
            errors, report = S.verify(data, corpus, source, True)
            self.assertEqual(errors, [])
            self.assertEqual(report['behavior_status'], 'unable_to_verify')
            sample['collection_status'] = 'collected'
            self.assertTrue(S.verify(data, corpus, source)[0])


if __name__ == '__main__':
    unittest.main()
