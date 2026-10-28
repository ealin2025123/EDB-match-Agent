"""代码/结果缓存离线验证；不调用真实模型，不代表DC实测。"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from excel_match_agent.app import run_job
from excel_match_agent.code_cache import store_success, lookup, key, current_scope, group_folder
from excel_match_agent.service import TaskService, normalize_request, json_text
from excel_match_agent.tests.test_runtime import FixedBackend, TestExecutor, CODE
from excel_match_agent.tests.test_markdown_agent import FakeModel, RULES, TEXT
from excel_match_agent.tests.test_service import REQUEST, passed_result


class CodeCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'SKILL.md').write_text('离线测试：仅按源表生成',encoding='utf-8')
        (self.root/'source.csv').write_text('name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n',encoding='utf-8')
        self.cache = self.root/'cache'
        self.job = {'source_path':'source.csv','skill_path':'SKILL.md','pageTitle':'测试来源\\统计',
                    'output_dir':'runs','code_cache_dir':str(self.cache),
                    'GZKJG':[{'rule_indicator_name':'GDP','rule_indicator_code':'1'},
                             {'rule_indicator_name':'GDP2','rule_indicator_code':'2'}]}

    def first_success(self):
        result = run_job(self.job,self.root,FixedBackend(),TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertTrue(store_success(self.cache,result))
        return result

    def test_new_source_reuses_code_but_executes_current_values_and_dates(self):
        self.first_success()
        (self.root/'source.csv').write_text('name,value,unit,date\nGDP,99,亿元,2026-09-30\nGDP2,2,亿元,2026-09-30\n',encoding='utf-8')
        backend = FixedBackend()
        preflights = []
        def gate(context,*args):
            preflights.append(context)
            return {'decision':'has_relevant_data','reason':'真实本次源值', 'related_indicator_names':['GDP'],
                    'evidence':['GDP | 99 | 亿元 | 2026-09-30']}
        backend.check_source = gate
        result = run_job(self.job,self.root,backend,TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertTrue(result['cache_hit'])
        self.assertEqual((backend.calls,backend.review_calls,len(preflights)),(0,1,1))
        self.assertEqual(result['OUTDATA'][0]['indicator_data'],'99')
        self.assertEqual(result['OUTDATA'][0]['end_date'],'2026-09-30')

    def test_rule_order_is_irrelevant_but_rule_changes_invalidate(self):
        self.first_success()
        reversed_job = {**self.job,'GZKJG':list(reversed(self.job['GZKJG']))}
        backend = FixedBackend()
        result = run_job(reversed_job,self.root,backend,TestExecutor())
        self.assertTrue(result['cache_hit'])
        self.assertEqual(backend.calls,0)
        changed = copy.deepcopy(self.job)
        changed['GZKJG'][0]['rule_indicator_code'] = '999'
        backend = FixedBackend()
        result = run_job(changed,self.root,backend,TestExecutor())
        self.assertFalse(result['cache_hit'])
        self.assertEqual(backend.calls,1)

    def test_path_skill_and_strategy_isolation(self):
        first = self.first_success()
        context = json.loads((Path(first['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
        self.assertIsNotNone(lookup(self.cache,context,'excel'))
        self.assertIsNone(lookup(self.cache,{**context,'pageTitle':'另一路径'},'excel'))
        self.assertIsNone(lookup(self.cache,{**context,'skill':[{'content':'不同Skill'}]},'excel'))
        self.assertIsNone(lookup(self.cache,context,'html'))

    def test_same_path_versions_share_one_group_without_overwriting_task_history(self):
        first = self.first_success()
        original = (Path(first['output_dir'])/'run-result.json').read_bytes()
        changed = copy.deepcopy(self.job)
        changed['GZKJG'][0]['rule_indicator_code'] = '999'
        second = run_job(changed,self.root,FixedBackend(),TestExecutor())
        self.assertTrue(store_success(self.cache,second))
        groups = list(self.cache.glob('*/group.json'))
        self.assertEqual(len(groups),1)
        index = json.loads(groups[0].read_text(encoding='utf-8'))
        self.assertEqual(index['pageTitle'],self.job['pageTitle'])
        self.assertEqual(len(index['versions']),2)
        self.assertEqual((Path(first['output_dir'])/'run-result.json').read_bytes(),original)
        first_context = json.loads((Path(first['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
        second_context = json.loads((Path(second['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
        self.assertIsNotNone(lookup(self.cache,first_context,'excel'))
        self.assertIsNotNone(lookup(self.cache,second_context,'excel'))

    def test_grouping_normalizes_path_separator_padding_but_keeps_other_paths_separate(self):
        self.assertEqual(group_folder(self.cache,{'pageTitle':' 来源 / 统计 '}),
                         group_folder(self.cache,{'pageTitle':'来源\\统计'}))
        self.assertNotEqual(group_folder(self.cache,{'pageTitle':'来源\\统计'}),
                            group_folder(self.cache,{'pageTitle':'其他来源\\统计'}))

    def test_corrupt_cache_is_ignored_and_generation_still_works(self):
        self.first_success()
        path = next(p for p in self.cache.glob('*/*.json') if p.name != 'group.json')
        item = json.loads(path.read_text(encoding='utf-8'))
        item['response']['python_code'] += '\n# modified'
        path.write_text(json.dumps(item),encoding='utf-8')
        backend = FixedBackend()
        result = run_job(self.job,self.root,backend,TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertFalse(result['cache_hit'])
        self.assertEqual(backend.calls,1)

    def test_cached_empty_parser_is_repaired_within_original_budget(self):
        self.first_success()
        path = next(p for p in self.cache.glob('*/*.json') if p.name != 'group.json')
        item = json.loads(path.read_text(encoding='utf-8'))
        code = 'def main(params): return []'
        item['response']['python_code'] = code
        item['sha256'] = hashlib.sha256(code.encode()).hexdigest()
        Path(item['library_path']).write_text(code,encoding='utf-8')
        item['library_sha256'] = hashlib.sha256(Path(item['library_path']).read_bytes()).hexdigest()
        path.write_text(json.dumps(item),encoding='utf-8')
        backend = FixedBackend()
        result = run_job(self.job,self.root,backend,TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertEqual((result['attempts'],backend.calls),(2,1))
        self.assertFalse(result['cache_hit'])
        self.assertTrue(result['cache_attempted'])
        self.assertEqual(backend.contexts[0]['output_repair']['code'],'EMPTY_EXTRACTION')

    def test_cache_never_bypasses_empty_rules_or_saves_failed_result(self):
        self.first_success()
        backend = FixedBackend()
        result = run_job({**self.job,'GZKJG':[]},self.root,backend,TestExecutor())
        self.assertEqual(result['failure_kind'],'GZKJG_EMPTY')
        self.assertEqual(backend.calls,0)
        self.assertFalse(store_success(self.cache,result))

    def test_prompt_success_asset_is_reused_without_regenerating_prompt(self):
        (self.root/'source.md').write_text(TEXT,encoding='utf-8')
        job = {'parse_strategy':'prompt','markdownMD5':'synthetic','markdown_path':'source.md',
               'GZKJG':RULES,'rule':{'指标名称取值逻辑':'指标加列名'},'output_dir':'runs','code_cache_dir':str(self.cache)}
        first = run_job(job,self.root,FakeModel())
        self.assertEqual(first['status'],'candidate_pass')
        self.assertTrue(store_success(self.cache,first))
        backend = FakeModel()
        result = run_job(job,self.root,backend)
        self.assertEqual(result['status'],'candidate_pass')
        self.assertTrue(result['cache_hit'])
        self.assertEqual(backend.generations,0)
        self.assertTrue(any(context.get('Markdown') for _,context in backend.calls))

    def test_html_success_core_is_reused_without_regenerating(self):
        (self.root/'source.md').write_text(TEXT,encoding='utf-8')
        job = {'parse_strategy':'html','markdownMD5':'synthetic','markdown_path':'source.md',
               'GZKJG':RULES,'rule':{'指标名称取值逻辑':'按真实源文'},'output_dir':'runs',
               'export_platform_parser':False,'code_cache_dir':str(self.cache)}
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'fixture','rules':[]}), patch(
                'excel_match_agent.markdown_agent.page_evidence',return_value={'status':'verified'}):
            first = run_job(job,self.root,FakeModel(html=True),TestExecutor())
            self.assertEqual(first['status'],'candidate_pass')
            self.assertTrue(store_success(self.cache,first))
            backend = FakeModel(html=True)
            result = run_job(job,self.root,backend,TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertTrue(result['cache_hit'])
        self.assertEqual(backend.generations,0)

    def test_new_source_does_not_use_fixed_date_code_from_old_source(self):
        first = self.first_success()
        context = json.loads((Path(first['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
        path = next(p for p in self.cache.glob('*/*.json') if p.name != 'group.json')
        item = json.loads(path.read_text(encoding='utf-8'))
        code = CODE.replace('row[3]',repr('2026-06-30'))
        item['response']['python_code'] = code
        item['sha256'] = hashlib.sha256(code.encode()).hexdigest()
        Path(item['library_path']).write_text(code,encoding='utf-8')
        item['library_sha256'] = hashlib.sha256(Path(item['library_path']).read_bytes()).hexdigest()
        path.write_text(json.dumps(item),encoding='utf-8')
        self.assertIsNotNone(lookup(self.cache,context,'excel'))
        self.assertIsNone(lookup(self.cache,{**context,'source_sha256':'different-source'},'excel'))


class ResultCacheTests(unittest.TestCase):
    def test_ready_result_survives_ttl_and_rule_reordering_without_new_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'SKILL.md').write_text('离线测试Skill',encoding='utf-8')
            profile = root/'profile.json'
            profile.write_text(json.dumps({'skill_path':'SKILL.md'}),encoding='utf-8')
            config = {'runtime_dir':str(root/'runtime'),'profiles':{'excel':str(profile)},'api_token':'test-only-token'}
            calls = []
            def runner(request_path,result_path):
                calls.append(1)
                payload = json.loads(request_path.read_text(encoding='utf-8'))
                result = passed_result(request_path.parent)
                parser = Path(result['history'][0]['code_path'])
                parser.write_text(CODE,encoding='utf-8')
                published = Path(result['platform_parser_path'])
                published.write_text(CODE+'\n# synthetic platform fixture\n',encoding='utf-8')
                validation = published.parent/'platform-validation.json'
                proof = json.loads(validation.read_text(encoding='utf-8'))
                proof['platform_code_sha256'] = hashlib.sha256(published.read_bytes()).hexdigest()
                validation.write_text(json.dumps(proof),encoding='utf-8')
                context = current_scope(payload['job'],payload['job_base'])
                Path(result['output_dir'],'input-context.json').write_text(json.dumps(context),encoding='utf-8')
                result_path.write_text(json.dumps(result),encoding='utf-8')
                return result
            service = TaskService(config,runner=runner)
            try:
                submitted,_ = service.submit(REQUEST)
                for _ in range(30):
                    ready = service.query(submitted['job_id'],.1)
                    if ready['status']=='ready':
                        break
                self.assertEqual(ready['status'],'ready')
                with service.condition:
                    service.db.execute('update tasks set created=? where id=?',(time.time()-10000,submitted['job_id']))
                    service.db.commit()
                response,reused = service.submit({**REQUEST,'GZKJG':list(reversed(REQUEST['GZKJG']))})
                self.assertTrue(reused)
                self.assertTrue(response['cache_hit'])
                self.assertEqual(response['cache_kind'],'result')
                self.assertEqual(response['job_id'],submitted['job_id'])
                self.assertEqual(len(calls),1)
                self.assertEqual(service.db.execute('select count(*) from tasks').fetchone()[0],1)
                (root/'SKILL.md').write_text('已更改的Skill规则',encoding='utf-8')
                fresh,reused = service.submit(REQUEST)
                self.assertFalse(reused)
                self.assertNotEqual(fresh['job_id'],submitted['job_id'])
            finally:
                service.close()


if __name__ == '__main__':
    unittest.main()
