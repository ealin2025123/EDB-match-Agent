"""真实HTTP/持久任务/平台脚本测试；假Agent只提供合成结果，不冒充真实模型验收。"""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from excel_match_agent.service import (APIError, TaskService, approved_delivery, create_server,
    init_config, json_text, normalize_request, prepare_job, read_config, transport_reply)


RULES = [{'rule_indicator_code':str(i),'rule_indicator_name':name,'rule_constant_name':'万元',
          'rule_unit_ratio':'','indicator_release_frequency':'月'} for i,name in ((1,'合成甲累计'),(2,'合成乙累计'))]
REQUEST = {'pageTitle':'合成来源\\统计','fileMd5':'demo-source','markdownMD5':'demo-markdown',
           'fileType':'xlsx','title':'2026年上半年合成表','publictime':'2026-07-01','GZKJG':RULES}


def passed_result(folder, strategy='excel'):
    folder = folder/'runs'/'synthetic-run'
    folder.mkdir(parents=True,exist_ok=True)
    rows = [{'indicator_code':r['rule_indicator_code'],'indicator_name':r['rule_indicator_name'],
             'indicator_data':'0','end_date':'2026-06-30'} for r in RULES]
    output = {'OUTDATA':rows}
    if strategy != 'prompt':
        output.update(unitArray={'indicator_unit':['万元'],'KB_unit':['万元'],'convert_ratio':[1]})
        output['codeOutputResult'] = copy.deepcopy(output)
        asset = folder/'platform/platform-parser.py'
        asset.parent.mkdir()
        code = '# synthetic test asset, no business generation\ndef main(params):\n    return {}\n'
    else:
        asset = folder/'prompt.md'
        code = 'Synthetic test prompt only.'
    asset.write_bytes(code.encode())
    (folder/'node-output.json').write_text(json_text(output),encoding='utf-8')
    digest = hashlib.sha256(code.encode()).hexdigest()
    if strategy != 'prompt':
        (asset.parent/'platform-validation.json').write_text(json_text({'status':'verified',
            'real_download_tested':True,'platform_code_sha256':digest}),encoding='utf-8')
    history = [{'asset':str(asset),'asset_sha256':digest}]
    if strategy == 'excel':
        parser = folder/'attempt-1/parser.py'
        parser.parent.mkdir()
        parser.write_text('# synthetic parser',encoding='utf-8')
        parsed = [{k:row[k] for k in ('indicator_name','indicator_data','end_date')} |
                  {'indicator_unit':'万元','source':{'sheet':'synthetic','cell':'A1'}} for row in rows]
        (parser.parent/'ExcelData.json').write_text(json_text({'ExcelData':parsed}),encoding='utf-8')
        history[0].update(attempt=1,code_path=str(parser))
    if strategy == 'html':
        parser = folder/'attempt-1/parser.py'
        parser.parent.mkdir()
        parser.write_bytes(code.encode())
        parsed = [{k:row[k] for k in ('indicator_name','indicator_data','end_date')} |
                  {'indicator_unit':'万元'} for row in rows]
        (parser.parent/'parsed-data.json').write_text(json_text(parsed),encoding='utf-8')
        history[0].update(attempt=1,asset=str(parser))
    return {'status':'candidate_pass','production_ready':True,'output_dir':str(folder),
            'parse_strategy':strategy,'platform_parser_path':str(asset),
            'verified_asset':str(parser if strategy=='html' else asset),
            'history':history, 'attempts':1,'rebuilds':0,
            'name_review':{'name_status':'correct'} if strategy=='excel' else {'status':'correct'},
            'elapsed_seconds':1,**output}


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile = self.root/'job.json'
        self.profile.write_text(json_text({'source_path':'old.xlsx','markdown_path':'old.md',
            'start_file':'old.txt','gzk_file':'old.json','pagetitle':'old','filemd5':'old',
            'upstream_metadata':{'title':'wrong'},'skill_path':'../skills/demo.md','max_attempts':1,
            'continue_from_run':'old-run','resume_from_run':'old-run'}),encoding='utf-8')
        self.config = {'runtime_dir':str(self.root/'runtime'),'api_token':'test-only-service-token-'+'x'*20,
                       'profiles':{k:str(self.profile) for k in ('excel','html','prompt')}}
        for strategy in ('html','prompt'):
            path=self.root/(strategy+'-job.json')
            path.write_text(json_text({'parse_strategy':strategy}),encoding='utf-8')
            self.config['profiles'][strategy]=str(path)
        self.service = None
        self.server = None
        self.server_thread = None
        self.release = threading.Event()
        self.calls = 0

    def tearDown(self):
        self.release.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=3)
        if self.service:
            self.service.close()
        self.temp.cleanup()

    def start(self, blocking=False, strategy='excel', runner=None):
        def fake(request_path, result_path):
            self.calls += 1
            if blocking:
                self.release.wait(5)
            return passed_result(request_path.parent,strategy)
        self.service = TaskService(self.config,runner=runner or fake)
        return self.service

    def ready(self, task_id):
        end = time.monotonic()+8
        while time.monotonic()<end:
            result = self.service.query(task_id,.1)
            if result['status'] in {'ready','failed','interrupted'}:
                return result
        self.fail('worker did not finish')

    def test_config_is_separate_and_never_overwrites_token(self):
        p=self.root/'service/config.json'
        init_config(p)
        original=p.read_bytes()
        with self.assertRaises(FileExistsError):init_config(p)
        self.assertEqual(p.read_bytes(),original)
        config=read_config(p)
        self.assertGreaterEqual(len(config['api_token']),24)
        self.assertEqual(config['host'],'127.0.0.1')

    def test_prepare_job_cannot_reuse_old_source_or_resume(self):
        request=normalize_request(REQUEST)
        job,base=prepare_job(request,self.profile,self.root/'task')
        for key in ('source_path','markdown_path','start_file','gzk_file','pagetitle','filemd5',
                    'upstream_metadata','continue_from_run','resume_from_run'):
            self.assertNotIn(key,job)
        self.assertEqual(job['fileMd5'],'demo-source')
        self.assertEqual(job['max_attempts'],3)
        self.assertEqual(base,self.root)
        self.assertTrue(job['platform_verify_download'])

    def test_validation_covers_missing_type_source_rules_and_alias(self):
        for changes in ({'fileType':''},{'GZKJG':[]},{'fileMd5':'../escape'},
                        {'markdownMD5':'','fileType':'html'},{'pageTitle':''},{'rule_row':True},
                        {'rule_row':1},{'GZKJG':[None]},{'fileType':'exe'},
                        {'api_key':'secret'},{'pageTitle':'x','pagetitle':'y'}):
            with self.subTest(changes=changes),self.assertRaises(APIError):
                normalize_request({**REQUEST,**changes})
        good=normalize_request({**REQUEST,'GZKJG':json.dumps(RULES)})
        self.assertEqual(good['GZKJG'],RULES)
        for extension,strategy in [('html','html'),('pdf','prompt'),('csv','excel')]:
            self.assertEqual(normalize_request({**REQUEST,'fileType':extension})['parse_strategy'],strategy)

    def test_profile_type_mismatch_rejected_before_acceptance(self):
        self.start()
        with self.assertRaises(APIError) as c:
            self.service.submit({**REQUEST,'profile':'html'})
        self.assertEqual(c.exception.code,'PROFILE_TYPE_MISMATCH')
        self.assertEqual(self.calls,0)

    def test_idempotency_deduplicates_queued_running_and_completed(self):
        self.start(blocking=True)
        first,reused=self.service.submit(REQUEST)
        self.assertFalse(reused)
        second,reused=self.service.submit(REQUEST)
        self.assertTrue(reused)
        self.assertEqual(first['job_id'],second['job_id'])
        self.release.set()
        result=self.ready(first['job_id'])
        self.assertEqual(result['status'],'ready',result)
        again,reused=self.service.submit(REQUEST)
        self.assertTrue(reused)
        self.assertEqual(again['job_id'],first['job_id'])
        self.assertEqual(self.calls,1)

    def test_workflow_id_conflict_and_queue_limit(self):
        self.config['max_pending_jobs']=1
        self.start(blocking=True)
        request={**REQUEST,'workflow_run_id':'workflow-stable-1'}
        self.service.submit(request)
        with self.assertRaises(APIError) as c:self.service.submit({**request,'title':'different'})
        self.assertEqual(c.exception.status,409)
        with self.assertRaises(APIError) as c:self.service.submit({**request,'workflow_run_id':'another'})
        self.assertEqual(c.exception.status,429)

    def test_profile_frozen_at_acceptance(self):
        self.start(blocking=True)
        result,_=self.service.submit(REQUEST)
        self.profile.write_text('{"changed":true}',encoding='utf-8')
        snapshot=json.loads((Path(self.config['runtime_dir'])/result['job_id']/'request.json').read_text(encoding='utf-8'))
        self.assertNotIn('changed',snapshot['job'])

    def test_different_branches_keep_their_final_contract(self):
        self.start(strategy='prompt')
        first,_=self.service.submit({**REQUEST,'fileType':'pdf'})
        result=self.ready(first['job_id'])
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(set(result['result']),{'OUTDATA'})
        self.assertEqual(result['asset']['type'],'prompt')

    def test_failures_and_pending_webpage_never_export_candidate_code(self):
        self.start(runner=lambda *args:{'status':'markdown_verified_pending_webpage','failure_kind':'WEBPAGE_VERIFICATION_PENDING'})
        task,_=self.service.submit(REQUEST)
        result=self.ready(task['job_id'])
        self.assertEqual(result['status'],'failed')
        self.assertNotIn('asset',result)
        self.assertEqual(result['error']['code'],'WEBPAGE_VERIFICATION_PENDING')

    def test_asset_and_output_tampering_are_rejected(self):
        result=passed_result(self.root)
        self.assertEqual(approved_delivery(result)['status'],'ready')
        asset=Path(result['platform_parser_path'])
        original=asset.read_bytes();asset.write_bytes(original+b'# changed')
        with self.assertRaises(ValueError):approved_delivery(result)
        asset.write_bytes(original)
        Path(result['output_dir'],'node-output.json').write_text('{"OUTDATA":[]}',encoding='utf-8')
        with self.assertRaises(ValueError):approved_delivery(result)

    def test_legacy_excel_status_requires_real_audit_and_platform_proof(self):
        result=passed_result(self.root)
        result['production_ready']=False
        result.pop('parse_strategy')
        self.assertEqual(approved_delivery(result)['status'],'ready')
        result['name_review']={'name_status':'uncertain'}
        with self.assertRaises(ValueError):approved_delivery(result)

    def test_html_details_preserve_raw_values_separate_from_matched_output(self):
        result = passed_result(self.root,'html')
        root = Path(result['output_dir'])
        raw = [{'indicator_name':'合成原始甲','indicator_data':'48.82','indicator_unit':'亿元',
                'end_date':'2026-06-30'},
               {'indicator_name':'合成原始乙','indicator_data':'0','indicator_unit':'%',
                'end_date':'2026-06-30'}]
        parsed = Path(result['verified_asset']).parent/'parsed-data.json'
        parsed.write_text(json_text(raw),encoding='utf-8')
        original = (root/'node-output.json').read_bytes()
        delivery = approved_delivery(result)
        self.assertEqual(delivery['result']['excel_data'],raw)
        self.assertEqual(delivery['result']['OUTDATA'],result['OUTDATA'])
        self.assertEqual((root/'node-output.json').read_bytes(),original)
        # 接出只传递原始明细，不从已匹配/换算结果反推，也不再次换算。
        nodes = Path(__file__).resolve().parents[1]/'workflow_nodes'
        record = runpy.run_path(str(nodes/'DC_Agent接入.py'))['_record'](delivery)
        output = runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']({'agent':[record]})
        self.assertEqual(output['excel_data'],raw)
        self.assertEqual(output['OUTDATA'],result['OUTDATA'])

    def test_html_details_require_final_attempt_and_valid_source(self):
        result = passed_result(self.root,'html')
        old = Path(result['verified_asset'])
        parser = old.parent.parent/'attempt-2/parser.py'
        parser.parent.mkdir()
        parser.write_bytes(old.read_bytes())
        final_rows = [{'indicator_name':'最终轮次','indicator_data':'-3','indicator_unit':'%',
                       'end_date':'2026-06-30'}]
        parsed = parser.parent/'parsed-data.json'
        parsed.write_text(json_text(final_rows),encoding='utf-8')
        result['attempts'] = 2
        result['verified_asset'] = str(parser)
        result['history'].append({**result['history'][0],'attempt':2,'asset':str(parser)})
        self.assertEqual(approved_delivery(result)['result']['excel_data'],final_rows)
        for invalid,code in (([], 'EMPTY_EXTRACTION'),
                             ([{**final_rows[0],'end_date':'empty'}], 'END_DATE_EMPTY')):
            parsed.write_text(json_text(invalid),encoding='utf-8')
            delivery = approved_delivery(result)
            self.assertEqual(delivery['error']['code'],code)
            self.assertNotIn('asset',delivery)
        parsed.write_text(json_text(final_rows),encoding='utf-8')
        parser.write_bytes(parser.read_bytes()+b'# changed')
        with self.assertRaisesRegex(ValueError,'最终审核资产'):
            approved_delivery(result)

    def test_old_ready_html_query_adds_details_without_reexecution_or_history_changes(self):
        def runner(request_path,result_path):
            self.calls += 1
            result = passed_result(request_path.parent,'html')
            result_path.write_text(json_text(result),encoding='utf-8')
            return result
        self.start(strategy='html',runner=runner)
        task,_ = self.service.submit({**REQUEST,'fileType':'html'})
        ready = self.ready(task['job_id'])
        self.assertEqual(ready['status'],'ready')
        legacy = {k:v for k,v in ready.items() if k not in {'job_id','created_at','updated_at','elapsed_seconds'}}
        legacy['result'].pop('excel_data')
        before = json_text(legacy)
        with self.service.condition:
            self.service.db.execute('UPDATE tasks SET response=? WHERE id=?',(before,task['job_id']))
            self.service.db.commit()
        refreshed = self.service.query(task['job_id'])
        self.assertEqual(len(refreshed['result']['excel_data']),2)
        stored = self.service.db.execute('SELECT response FROM tasks WHERE id=?',(task['job_id'],)).fetchone()[0]
        self.assertEqual(stored,before)
        self.assertEqual(self.calls,1)

    def test_real_subprocess_worker_reports_agent_error_without_model(self):
        # 非HTTP下载地址在网络及模型调用之前失败；验证独立进程真实接入原Agent。
        job=json.loads(self.profile.read_text(encoding='utf-8'))
        job['download_base_url']='invalid://offline-test'
        self.profile.write_text(json_text(job),encoding='utf-8')
        self.service=TaskService(self.config)
        task,_=self.service.submit(REQUEST)
        result=self.ready(task['job_id'])
        self.assertEqual(result['status'],'failed',result)
        self.assertEqual(result['error']['code'],'SOURCE_DOWNLOAD_CONFIG_INVALID',result)
        self.assertTrue((Path(self.config['runtime_dir'])/task['job_id']/'agent-result.json').is_file())

    def test_restart_marks_running_interrupted_without_regeneration(self):
        self.start()
        first,_=self.service.submit({**REQUEST,'workflow_run_id':'stable'})
        self.ready(first['job_id'])
        self.service.close();self.service=None
        with sqlite3.connect(self.root/'runtime/tasks.sqlite3') as db:
            db.execute("UPDATE tasks SET state='running',response=NULL WHERE id=?",(first['job_id'],))
        db.close()
        self.start()
        result=self.service.query(first['job_id'])
        self.assertEqual(result['status'],'interrupted')
        self.assertEqual(result['error']['code'],'SERVICE_RESTARTED')
        replay,reused=self.service.submit({**REQUEST,'workflow_run_id':'stable'})
        self.assertTrue(reused)
        self.assertEqual(replay['status'],'interrupted')
        self.assertEqual(self.calls,1)

    def test_second_service_cannot_share_runtime(self):
        self.start()
        with self.assertRaises(RuntimeError):TaskService(self.config)

    def http(self,method,path,payload=None,token=True,raw=None):
        body=json.dumps(payload).encode() if raw is None and payload is not None else raw
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+self.config['api_token']
        req=urllib.request.Request(self.base+path,data=body,method=method,headers=headers)
        try:
            with urllib.request.urlopen(req,timeout=5) as reply:
                return reply.status,json.load(reply)
        except urllib.error.HTTPError as exc:
            return exc.code,json.load(exc)

    def start_http(self,strategy='excel'):
        self.start(strategy=strategy)
        self.server=create_server(self.service,'127.0.0.1',0)
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.server_thread.start()

    def test_real_http_auth_validation_and_post_query(self):
        self.start_http()
        self.assertEqual(self.http('GET','/health',token=False)[0],200)
        self.assertEqual(self.http('POST','/v1/match/jobs',REQUEST,token=False)[0],401)
        self.assertEqual(self.http('POST','/v1/match/jobs',raw=b'{"fileType":"xlsx","fileType":"pdf"}')[0],422)
        self.assertEqual(self.http('POST','/v1/match/jobs',{**REQUEST,'GZKJG':[]})[0],422)
        status,task=self.http('POST','/v1/match/jobs',REQUEST)
        self.assertEqual(status,202)
        status,result=self.http('GET','/v1/match/jobs/'+task['job_id']+'?wait_seconds=2')
        self.assertEqual(status,200)
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(self.http('GET','/v1/match/jobs/'+task['job_id']+'?wait_seconds=999')[0],422)
        self.assertNotIn(self.config['api_token'],json.dumps(result))
        self.assertNotIn(str(self.root),json.dumps(result))

    def test_two_platform_scripts_work_over_real_http(self):
        self.start_http()
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope=runpy.run_path(str(nodes/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL=self.base,SERVICE_TOKEN=self.config['api_token'],NODE_WAIT_SECONDS=4)
        response=scope['main'](REQUEST)
        item=response['agent'][0]
        self.assertEqual(item['status'],'ready',item)
        self.assertTrue(all(isinstance(v,str) for v in item.values()))
        output=runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main'](response)
        self.assertEqual(len(output['OUTDATA']),2)
        self.assertEqual(len(output['excel_data']),2)
        self.assertNotIn('source',output['excel_data'][0])
        decoded_response=json.loads(json.dumps(response))
        decoded_response['agent'][0]['result_json']=json.loads(item['result_json'])
        output_main=runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']
        self.assertEqual(output_main(decoded_response),output)
        for invalid in (None,[],123):
            with self.subTest(result_json=invalid),self.assertRaisesRegex(ValueError,'result_json'):
                output_main({'agent':[{**item,'result_json':invalid}]})
        again=scope['main'](response)
        self.assertEqual(again['agent'][0]['job_id'],item['job_id'])
        self.assertEqual(self.calls,1)
        response['agent'][0]['PythonCode']+='tampered'
        with self.assertRaises(ValueError):runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main'](response)

    def test_http_ready_supports_old_html_node_with_missing_pythoncode(self):
        self.start_http(strategy='html')
        _, task = self.http('POST','/v1/match/jobs',{**REQUEST,'fileType':'html'})
        core = self.ready(task['job_id'])
        _, reply = self.http('GET','/v1/match/jobs/'+task['job_id'])
        self.assertIn('_asset_transfer',reply['result'])
        self.assertNotIn('_asset_transfer',core['result'])
        item = self.legacy_record(reply)
        nodes = Path(__file__).resolve().parents[1]/'workflow_nodes'
        output = runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']({'agent':[item]})
        self.assertEqual(output['python_code'],reply['asset']['content'])
        self.assertEqual(output['OUTDATA'],core['result']['OUTDATA'])
        self.assertNotIn('_asset_transfer',output)
        # 已完成POST复用也携带备份，无新建任务、无二次模型执行。
        _, reused = self.http('POST','/v1/match/jobs',{**REQUEST,'fileType':'html'})
        self.assertTrue(reused['reused'])
        self.assertEqual(reused['job_id'],task['job_id'])
        self.assertIn('_asset_transfer',reused['result'])
        self.assertEqual(self.calls,1)
        stored = json.loads(self.service.db.execute('SELECT response FROM tasks WHERE id=?',
                            (task['job_id'],)).fetchone()[0])
        self.assertNotIn('_asset_transfer',stored['result'])

    @staticmethod
    def legacy_record(reply):
        # 旧接入脚本只序列化result，DC字段映射将独立代码字段丢为None。
        return {'status':reply['status'],'job_id':reply['job_id'],
                'parse_strategy':reply['parse_strategy'],'code_sha256':reply['asset']['sha256'],
                'PythonCode':None,'PromptCode':None,'result_json':json.dumps(reply['result'])}

    def test_transport_backup_recovery_and_tampering_all_branches(self):
        nodes = Path(__file__).resolve().parents[1]/'workflow_nodes'
        extract = runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']
        for strategy in ('excel','html','prompt'):
            with self.subTest(strategy=strategy):
                delivery = {**approved_delivery(passed_result(self.root/strategy,strategy)),
                            'job_id':'synthetic-'+strategy}
                before = copy.deepcopy(delivery)
                reply = transport_reply(delivery)
                self.assertEqual(delivery,before)
                self.assertEqual(transport_reply(reply),reply)
                item = self.legacy_record(reply)
                recovered = extract({'agent':[item]})
                self.assertEqual(recovered['python_code'],delivery['asset']['content'])
                self.assertEqual(recovered['OUTDATA'],delivery['result']['OUTDATA'])
                self.assertNotIn('_asset_transfer',recovered)
                for field,value in (('job_id','wrong-job'),('sha256','0'*64),
                                    ('parse_strategy','wrong'),('content_b64','dGFtcGVyZWQ=')):
                    bad = copy.deepcopy(item)
                    altered = json.loads(bad['result_json'])
                    altered['_asset_transfer'][field] = value
                    bad['result_json'] = json.dumps(altered)
                    with self.assertRaises(ValueError):
                        extract({'agent':[bad]})
                bad = {**item,('PromptCode' if strategy=='prompt' else 'PythonCode'):'tampered'}
                with self.assertRaisesRegex(ValueError,'哈希校验失败'):
                    extract({'agent':[bad]})
                delivery['asset']['content'] += 'tampered'
                with self.assertRaises(APIError):
                    transport_reply(delivery)
        for state in ('queued','running','failed','interrupted'):
            reply = {'status':state}
            self.assertEqual(transport_reply(reply),reply)

    def test_pending_node_resumes_the_same_job(self):
        self.start(blocking=True)
        self.server=create_server(self.service,'127.0.0.1',0)
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope=runpy.run_path(str(nodes/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL=self.base,SERVICE_TOKEN=self.config['api_token'],NODE_WAIT_SECONDS=.01,PENDING_MODE='return')
        response=scope['main'](REQUEST)
        self.assertIn(response['agent'][0]['status'],{'queued','running'})
        self.release.set();self.ready(response['agent'][0]['job_id'])
        scope['main'].__globals__['NODE_WAIT_SECONDS']=3
        result=scope['main'](response)
        self.assertEqual(result['agent'][0]['status'],'ready')
        self.assertEqual(self.calls,1)

    def test_prompt_platform_output_does_not_add_table_fields(self):
        self.start_http(strategy='prompt')
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope=runpy.run_path(str(nodes/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL=self.base,SERVICE_TOKEN=self.config['api_token'],NODE_WAIT_SECONDS=4)
        response=scope['main']({**REQUEST,'fileType':'pdf'})
        item=response['agent'][0]
        self.assertTrue(item['PromptCode'])
        self.assertEqual(item['PythonCode'],'')
        result=runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main'](response)
        # 接出当前保留资产回写字段，但不向Prompt业务结果加入单位/表格字段。
        self.assertEqual(set(result),{'OUTDATA','python_code','python_file'})
        self.assertEqual(result['OUTDATA'],json.loads(item['result_json'])['OUTDATA'])
        self.assertEqual(result['python_code'],item['PromptCode'])

    def test_preflight_error_and_gzk_zero_reach_platform_agent_record(self):
        self.start_http()
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope=runpy.run_path(str(nodes/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL=self.base,SERVICE_TOKEN=self.config['api_token'])
        response=scope['main']({**REQUEST,'GZKJG':[]})
        self.assertEqual(response['agent'][0]['error_code'],'GZKJG_EMPTY')
        self.assertIn('0条',response['agent'][0]['message'])
        self.assertEqual(self.calls,0)
        reason='实际正文只有无关贷款指标，与本次GZK无关'
        delivery=approved_delivery({'status':'needs_review','failure_kind':'SOURCE_GZK_NO_RELEVANT_DATA',
            'failure_stage':'preflight','notifications':[{'code':'SOURCE_GZK_NO_RELEVANT_DATA','message':reason}]})
        item=scope['_record'](delivery)
        self.assertIn(reason,item['message'])
        self.assertIn('处理建议',item['message'])
        self.assertEqual(item['stage'],'preflight')

    def test_active_job_deduplicates_even_after_ttl(self):
        self.start(blocking=True)
        first,_=self.service.submit(REQUEST)
        with self.service.condition:
            self.service.db.execute('UPDATE tasks SET created=? WHERE id=?',(time.time()-10000,first['job_id']))
            self.service.db.commit()
        second,reused=self.service.submit(REQUEST)
        self.assertTrue(reused)
        self.assertEqual(first['job_id'],second['job_id'])

    def test_default_pending_raises_retry_and_reuses_active_job(self):
        self.start(blocking=True)
        self.server=create_server(self.service,'127.0.0.1',0)
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        self.server_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.server_thread.start()
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        scope=runpy.run_path(str(nodes/'DC_Agent接入.py'))
        scope['main'].__globals__.update(SERVICE_BASE_URL=self.base,SERVICE_TOKEN=self.config['api_token'],NODE_WAIT_SECONDS=.01)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError,'DC_AGENT_RETRY'):
                scope['main'](REQUEST)
        with self.service.condition:
            self.assertEqual(self.service.db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0],1)

    def test_md_unit_array_adapter_preserves_rows_and_api_contract(self):
        result=passed_result(self.root,'prompt')
        rows=result['OUTDATA']
        for i,row in enumerate(rows):
            row.update(indicator_unit='亿元' if i==0 else '%',KB_unit='万元' if i==0 else '%')
        Path(result['output_dir'],'node-output.json').write_text(json_text({'OUTDATA':rows}),encoding='utf-8')
        delivery=approved_delivery(result)
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        record=runpy.run_path(str(nodes/'DC_Agent接入.py'))['_record'](delivery)
        output=runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']({'agent':[record],'platform_chain':'md_array'})
        self.assertEqual(output['OUTDATA'],rows)
        self.assertEqual(output['unitArray'],{'indicator_unit':['亿元','%'],'KB_unit':['万元','%'],'convert_ratio':[None,None]})
        self.assertEqual(set(delivery['result']),{'OUTDATA'})

    def test_new_path_cannot_inherit_previous_task_rule(self):
        original={'parse_strategy':'html','rule_source':'workbook','rule_row':1197,'rule':{'路径':'旧来源'},
                  'path_code':'wrong','rule_overrides':{'数据源路径代码':'wrong'}}
        profile=Path(self.config['profiles']['html'])
        profile.write_text(json_text(original),encoding='utf-8')
        job,_=prepare_job(normalize_request({**REQUEST,'fileType':'html'}),profile,self.root/'task')
        for field in ('rule_row','rule','path_code','rule_overrides'):
            self.assertNotIn(field,job)
        self.assertEqual(job['rule_source'],'workbook')

    def test_table_audit_frequency_is_from_gzk_not_blank_sjpllb(self):
        delivery=approved_delivery(passed_result(self.root))
        nodes=Path(__file__).resolve().parents[1]/'workflow_nodes'
        record=runpy.run_path(str(nodes/'DC_Agent接入.py'))['_record'](delivery)
        output=runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main'](
            {'agent':[record],'GZKJG':RULES,'platform_chain':'table_audit'})
        self.assertTrue(all(row['indicator_release_frequency']=='月' for row in output['OUTDATA']))
        self.assertEqual(output['OUTDATA'],output['codeOutputResult']['OUTDATA'])
        self.assertNotIn('indicator_release_frequency',delivery['result']['OUTDATA'][0])


if __name__ == '__main__':
    unittest.main()
