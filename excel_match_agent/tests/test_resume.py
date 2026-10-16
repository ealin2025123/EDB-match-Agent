import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.app import run_job, dump, NameReviewProtocolError, ModelOutputTruncated
from excel_match_agent.tests.test_runtime import CODE, FixedBackend, TestExecutor
from excel_match_agent.audit_saved_run import invalidate_after_audit


class ProtocolResumeTests(unittest.TestCase):
    def test_incomplete_review_replays_without_a_new_generation(self):
        class Truncated(FixedBackend):
            def generate(self, context, folder):
                response = super().generate(context, folder)
                response['derived_rules'] = {k: 'fixture rule' for k in (
                    '指标单位取值逻辑', '指标单位位置', '截止日期取值逻辑', '截止日期位置')}
                dump(folder / 'model-response-raw.json', {'content': json.dumps(response)})
                dump(folder / 'model-metadata.json', {'provider': 'deepseek', 'finish_reason': 'stop'})
                return response
            def review_names(self, context, folder):
                dump(folder / 'model-metadata.json', {'finish_reason': 'length'})
                raise ModelOutputTruncated('fixture review truncated')
        fresh = {k: v for k, v in self.job.items() if k != 'resume_from_run'}
        old = run_job(fresh, self.base, Truncated(), TestExecutor())
        self.assertEqual(old['failure_kind'], 'MODEL_OUTPUT_TRUNCATED')
        backend = FixedBackend()
        result = run_job({**fresh, 'resume_from_run': old['output_dir'], 'resume_incomplete_review': True},
                         self.base, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass', result.get('notifications'))
        self.assertEqual((result['attempts'], result['rebuilds'], backend.calls), (1, 0, 0))
        proof = json.loads((Path(result['output_dir']) / 'recovery/provenance.json').read_text(encoding='utf-8'))
        self.assertTrue(proof['review_restarted'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / 'source.csv').write_text('name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n', encoding='utf-8')
        (self.base / 'SKILL.md').write_text('离线恢复测试夹具', encoding='utf-8')
        self.job = {'source_path': 'source.csv', 'skill_path': 'SKILL.md', 'output_dir': 'runs',
                    'GZKJG': [{'rule_indicator_name': n, 'rule_indicator_code': str(i), 'rule_constant_name': '亿元'}
                              for i, n in enumerate(['GDP', 'GDP2'], 100)]}
        class BrokenProtocol(FixedBackend):
            def review_names(self, context, folder):
                issues = [{**{k: r[k] for k in ['indicator_name', 'source']}, 'reason': '离线夹具：错误名称',
                           'target_indicator_name': 'GDP' if r['source']['cell'] == 'B2' else 'GDP2'}
                          for r in context['parsed_data']]
                review = {'name_status': 'incorrect', 'explanation': '离线夹具', 'issues': issues,
                          'candidate_reviews': [{**issue, 'decision': 'same_indicator', 'compatibility': {
                              'rule_indicator_name': issue['target_indicator_name'], 'unit': 'compatible',
                              'frequency': 'unknown', 'reason': '离线夹具'}} for issue in issues]}
                dump(folder / 'model-response-invalid.json', {'content': json.dumps(review), 'validation_error': '离线模拟协议故障'})
                raise NameReviewProtocolError('离线模拟协议故障')
        old = run_job(self.job, self.base, BrokenProtocol([CODE.replace('row[0]', '"wrong"')]), TestExecutor())
        self.previous = Path(old['output_dir'])
        self.job['resume_from_run'] = str(self.previous)

    def tearDown(self):
        self.temp.cleanup()

    def test_resume_preserves_prior_generation_and_remaining_budget(self):
        backend = FixedBackend()
        result = run_job(self.job, self.base, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual((result['attempts'], result['rebuilds'], backend.calls), (2, 1, 1))
        proof = json.loads((Path(result['output_dir']) / 'recovery/provenance.json').read_text(encoding='utf-8'))
        self.assertTrue(proof['code_reexecuted'])
        self.assertFalse(proof['semantic_response_edited'])

    def test_resume_rejects_different_input(self):
        self.job['GZKJG'][0]['rule_indicator_code'] = 'changed'
        backend = FixedBackend()
        result = run_job(self.job, self.base, backend, TestExecutor())
        self.assertEqual(result['failure_kind'], 'INPUT_OR_RUNTIME_ERROR')
        self.assertEqual(backend.calls, 0)
        self.assertIn('恢复输入', str(result['notifications']))

    def test_resume_rejects_different_publication_metadata(self):
        self.job['publictime'] = '2026-08-14 00:00:00'
        backend = FixedBackend()
        result = run_job(self.job, self.base, backend, TestExecutor())
        self.assertEqual(backend.calls, 0)
        self.assertEqual(result['failure_kind'], 'INPUT_OR_RUNTIME_ERROR')
        self.assertIn('publictime', str(result['notifications']))

    def test_resume_cannot_reset_exhausted_attempts(self):
        record = json.loads((self.previous / 'run-result.json').read_text(encoding='utf-8'))
        record['attempts'] = 3
        dump(self.previous / 'run-result.json', record)
        backend = FixedBackend()
        result = run_job(self.job, self.base, backend, TestExecutor())
        self.assertEqual(backend.calls, 0)
        self.assertIn('没有剩余重建次数', str(result['notifications']))

    def test_negative_recheck_withdraws_old_output_and_resumes_same_budget(self):
        record = json.loads((self.previous / 'run-result.json').read_text(encoding='utf-8'))
        raw = json.loads((self.previous / 'attempt-1/name-review/model-response-invalid.json').read_text(encoding='utf-8'))
        review = json.loads(raw['content'])
        record.update(status='candidate_pass', failure_kind=None)
        dump(self.previous / 'run-result.json', record)
        (self.previous / 'node-output.json').write_text('{}', encoding='utf-8')
        audit = self.previous / 'semantic-recheck/negative-name-audit'
        audit.mkdir(parents=True)
        dump(audit / 'combined-review.json', review)
        invalidate_after_audit(self.previous, review)
        self.assertFalse((self.previous / 'node-output.json').exists())
        self.assertTrue((self.previous / 'semantic-recheck/before-audit-node-output.json').exists())
        self.job['resume_negative_audit'] = True
        backend = FixedBackend()
        result = run_job(self.job, self.base, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual((result['attempts'], result['rebuilds'], backend.calls), (2, 1, 1))
