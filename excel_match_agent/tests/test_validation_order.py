"""离线工程回归；不调用真实DeepSeek，不代表DC实测。"""
import hashlib
import io
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from excel_match_agent.app import run_job
from excel_match_agent.error_reporting import public_error
from excel_match_agent.service import approved_delivery, normalize_request, APIError, TaskService
from excel_match_agent.output_checks import DATE_REPAIR
from excel_match_agent.tests.test_runtime import CODE, FixedBackend, TestExecutor
from excel_match_agent.tests.test_service import REQUEST
from excel_match_agent.tests.test_markdown_agent import FakeModel, RULES, ROWS, TEXT


class ValidationOrderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.csv'
        self.source.write_text('name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n', encoding='utf-8')
        (self.root / 'SKILL.md').write_text('测试夹具：从真实源单元格读取', encoding='utf-8')
        self.job = {'source_path':'source.csv', 'skill_path':'SKILL.md', 'output_dir':'runs',
                    'GZKJG':[{'rule_indicator_name':'GDP','rule_indicator_code':'1'},
                             {'rule_indicator_name':'GDP2','rule_indicator_code':'2'}]}
        self.original = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def run_model(self, backend):
        result = run_job(self.job, self.root, backend, TestExecutor())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original)
        return result

    def test_invalid_rules_stop_before_source_or_model(self):
        for rules in ([], None, [None], [{}], [{'rule_indicator_name':'GDP'}]):
            self.job['GZKJG'] = rules
            backend = FixedBackend()
            with self.subTest(rules=rules), patch('excel_match_agent.app.workbook', side_effect=AssertionError('不得读取源表')):
                result = self.run_model(backend)
                self.assertIn(result['failure_kind'], {'GZKJG_EMPTY','GZKJG_INVALID','RULE_FIELDS_INVALID'})
                self.assertEqual(backend.calls, 0)
                self.assertEqual(result['attempts'], 0)
                self.assertIn('指标规则为空或无效', result['notifications'][-1]['message'])

    def test_actual_excel_preflight_runs_after_reading_and_before_generation(self):
        for decision, expected in (('no_valid_data','SOURCE_NO_VALID_DATA'), ('no_gzk_data','SOURCE_GZK_NO_RELEVANT_DATA')):
            backend = FixedBackend()
            def gate(context, *args):
                self.assertIn('GDP | 0', context['source_text'])
                self.assertEqual(context['GZKJG'][0]['rule_indicator_name'], 'GDP')
                return {'decision':decision,'reason':'离线夹具业务否定结论','related_indicator_names':[],
                        'evidence':['GDP | 0 | 亿元 | 2026-06-30']}
            backend.check_source = gate
            result = self.run_model(backend)
            self.assertEqual(result['failure_kind'], expected)
            self.assertEqual(result['failure_stage'], 'preflight')
            self.assertEqual(backend.calls, 0)

    def test_uncertain_preflight_continues_and_zero_is_preserved(self):
        backend = FixedBackend()
        backend.check_source = lambda *args: {'decision':'uncertain', 'reason':'证据不足继续解析',
            'related_indicator_names':[], 'evidence':['GDP | 0 | 亿元 | 2026-06-30']}
        result = self.run_model(backend)
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result['OUTDATA'][0]['indicator_data'], '0')

    def test_empty_extraction_can_rebuild_then_pass(self):
        backend = FixedBackend(['def main(params): return []', CODE])
        result = self.run_model(backend)
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(backend.contexts[1]['output_repair']['code'], 'EMPTY_EXTRACTION')
        first = Path(result['output_dir']) / 'attempt-1/ExcelData.json'
        self.assertEqual(json.loads(first.read_text(encoding='utf-8'))['ExcelData'], [])

    def test_empty_extraction_stops_after_three_generations(self):
        backend = FixedBackend(['def main(params): return []'])
        result = self.run_model(backend)
        self.assertEqual(result['failure_kind'], 'EMPTY_EXTRACTION')
        self.assertEqual((backend.calls, result['rebuilds'], backend.review_calls), (3,2,0))
        self.assertFalse((Path(result['output_dir'])/'node-output.json').exists())
        self.assertTrue((Path(result['output_dir'])/'human-review.json').exists())

    def test_blank_empty_null_dates_use_rebuild_budget_and_never_pass(self):
        for date in ('""', '" empty "', 'None'):
            backend = FixedBackend([CODE.replace('row[3]', date)])
            self.job.update(publictime='2026-07-09')
            result = self.run_model(backend)
            self.assertEqual(result['failure_kind'], 'END_DATE_EMPTY')
            self.assertEqual(result['failure_stage'], 'date_validation')
            self.assertEqual((backend.calls, backend.review_calls), (3,0))
            self.assertEqual(backend.contexts[1]['output_repair']['code'], 'END_DATE_EMPTY')
            self.assertFalse((Path(result['output_dir'])/'node-output.json').exists())

    def test_date_repair_receives_source_filename_title_and_priority(self):
        backend = FixedBackend([CODE.replace('row[3]', '""'), CODE])
        self.job.update(title='2026年上半年经济数据', publictime='2026-07-09')
        result = self.run_model(backend)
        self.assertEqual(result['status'], 'candidate_pass')
        context = backend.contexts[1]
        self.assertEqual(context['title'], self.job['title'])
        self.assertEqual(context['source_name'], 'source.csv')
        self.assertEqual(context['date_validation_policy'], DATE_REPAIR)
        self.assertTrue(all(r['end_date']=='2026-06-30' for r in result['OUTDATA']))

    def test_only_publictime_does_not_fill_dates(self):
        self.source.write_text('name,value,unit,date\nGDP,0,亿元,\nGDP2,1,亿元,\n',encoding='utf-8')
        self.original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.job['publictime'] = '2026-07-09'
        result = self.run_model(FixedBackend())
        self.assertEqual(result['failure_kind'], 'END_DATE_EMPTY')
        self.assertEqual(result['attempts'], 3)

    def test_html_prompt_share_empty_and_missing_date_gates(self):
        (self.root/'source.md').write_text(TEXT,encoding='utf-8')
        for strategy in ('html','prompt'):
            job = {'parse_strategy':strategy,'markdownMD5':'synthetic','markdown_path':'source.md',
                   'GZKJG':RULES,'rule':{'数据源路径代码':'123','指标名称取值逻辑':'按真实源表'},
                   'output_dir':'runs','export_platform_parser':False}
            if strategy == 'html':
                job.update(markdown_only_regression=True,webpage_defer_authorization='离线测试夹具允许只核验Markdown')
            for rows, expected in (([], 'EMPTY_EXTRACTION'),
                                   ([{**r,'end_date':''} for r in ROWS], 'END_DATE_EMPTY')):
                model = FakeModel(rows=rows,html=strategy=='html')
                with self.subTest(strategy=strategy,expected=expected), patch('excel_match_agent.markdown_agent.regex_rules', return_value={'format':'fixture','rules':[]}):
                    result = run_job(job,self.root,model,TestExecutor())
                self.assertEqual(result['failure_kind'],expected,result)
                self.assertEqual(model.generations,3)
                self.assertFalse((Path(result['output_dir'])/'node-output.json').exists())

    def test_every_failure_has_public_reason_stage_advice_without_raw_secret(self):
        for code in ('EMPTY_EXTRACTION','END_DATE_EMPTY','NAME_REVIEW_PROTOCOL_ERROR','INPUT_OR_RUNTIME_ERROR'):
            error = approved_delivery({'failure_kind':code,'notifications':[{'code':code,'message':'secret api_key=private C:\\private\\file'}]})
            self.assertEqual(error['status'], 'failed')
            self.assertEqual(error['error']['code'], code)
            self.assertTrue(error['error']['stage'])
            self.assertTrue(error['error']['suggestion'])
            self.assertNotIn('private', str(error))
            self.assertNotIn('Agent未正式通过', str(error))

    def test_old_failed_snapshot_shows_reason_without_reexecuting_or_modifying_record(self):
        folder = self.root/'saved-task'
        folder.mkdir()
        saved = folder/'agent-result.json'
        saved.write_text(json.dumps({'status':'needs_review','failure_kind':'END_DATE_EMPTY'}),encoding='utf-8')
        original = saved.read_bytes()
        row = {'id':'saved-task','state':'failed','created':1,'updated':2,
               'response':json.dumps({'status':'failed','error':{'code':'END_DATE_EMPTY','message':'旧笼统提示'}})}
        service = SimpleNamespace(root=self.root,runner=Mock(side_effect=AssertionError('禁止重跑')))
        result = TaskService._snapshot(service,row)
        self.assertEqual(result['error']['code'],'END_DATE_EMPTY')
        self.assertIn('统计截止日期',result['error']['message'])
        self.assertTrue(result['error']['suggestion'])
        self.assertEqual(saved.read_bytes(),original)
        service.runner.assert_not_called()

    def test_delivery_cannot_export_empty_date_even_if_upstream_marked_pass(self):
        from excel_match_agent.tests.test_service import passed_result, json_text
        result = passed_result(self.root)
        result['OUTDATA'][0]['end_date'] = ''
        result['codeOutputResult']['OUTDATA'][0]['end_date'] = ''
        output = {k:result[k] for k in ('OUTDATA','unitArray','codeOutputResult')}
        Path(result['output_dir'],'node-output.json').write_text(json_text(output),encoding='utf-8')
        delivery = approved_delivery(result)
        self.assertEqual(delivery['status'],'failed')
        self.assertEqual(delivery['error']['code'],'END_DATE_EMPTY')

    def test_dc_both_scripts_show_structured_failure_and_suggestion(self):
        root = Path(__file__).resolve().parents[1]/'workflow_nodes'
        task = '00000000-0000-0000-0000-000000000001'
        for script in ('DC_Agent接入.py','DC_Agent接入_粘贴版.py'):
            scope = runpy.run_path(str(root/script))
            runtime = scope['main'].__globals__
            runtime.update(SERVICE_BASE_URL='http://agent.example.invalid:8767',SERVICE_TOKEN='synthetic-test-token',PENDING_MODE='return')
            reply = {'status':'failed','job_id':task,'error':public_error({'failure_kind':'END_DATE_EMPTY'})}
            runtime['_http'] = Mock(return_value=reply)
            item = scope['main']({'job_id':task})['agent'][0]
            for key in ('status','stage','job_id','error_code','message'):
                self.assertTrue(item[key])
            self.assertIn('处理建议', item['message'])
            output = runpy.run_path(str(root/'DC_匹配结果输出.py'))
            with self.assertRaisesRegex(ValueError, 'END_DATE_EMPTY') as caught:
                output['main']({'agent':[item]})
            self.assertIn(task, str(caught.exception))
            self.assertIn('date_validation', str(caught.exception))

    def test_dc_http_422_shows_rule_error_instead_of_generic_message(self):
        root = Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope = runpy.run_path(str(root/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL='http://agent.example.invalid:8767', SERVICE_TOKEN='synthetic-test-token')
        error = {'error':{'code':'GZKJG_EMPTY','message':'指标规则为空或无效', 'stage':'input_validation','suggestion':'检查GZKJG映射'}}
        scope['main'].__globals__['_http'] = Mock(side_effect=urllib.error.HTTPError('http://example.invalid',422,'invalid',{},io.BytesIO(json.dumps(error).encode())))
        item = scope['main'](REQUEST)['agent'][0]
        self.assertEqual(item['error_code'], 'GZKJG_EMPTY')
        self.assertEqual(item['stage'], 'input_validation')
        self.assertIn('检查GZKJG映射', item['message'])


if __name__ == '__main__':
    unittest.main()
