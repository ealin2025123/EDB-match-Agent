"""DC调用诊断：区分连接故障、HTTP拒绝、错误JSON及/v3响应混用。"""
import io
import json
from pathlib import Path
import runpy
import socket
import unittest
from unittest.mock import Mock, patch
import urllib.error

from excel_match_agent.tests.test_service import REQUEST

TASK = '00000000-0000-0000-0000-000000000001'


class DCDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.scope=runpy.run_path(str(Path(__file__).resolve().parents[1]/'workflow_nodes/DC_Agent接入.py'))
        self.g=self.scope['main'].__globals__
        self.g.update(SERVICE_BASE_URL='http://agent.example.invalid:8767',SERVICE_TOKEN='TEST_ONLY_SERVICE_TOKEN',
                      NODE_WAIT_SECONDS=3,PENDING_MODE='return')

    def call_error(self,exc):
        self.g['_http']=Mock(side_effect=exc)
        return self.scope['main'](REQUEST)['agent'][0]

    def test_timeout_is_not_confused_with_model_failure(self):
        item=self.call_error(urllib.error.URLError(TimeoutError('timed out')))
        self.assertEqual(item['error_code'],'SERVICE_TIMEOUT')
        self.assertEqual(item['stage'],'submit_job')
        self.assertEqual(item['job_id'],'')
        self.assertIn('不代表服务端一定未受理',item['message'])

    def test_dns_and_refusal_have_different_error_codes(self):
        for reason,code in [(socket.gaierror(-2,'not found'),'SERVICE_DNS_FAILED'),
                            (ConnectionRefusedError(111,'refused'),'SERVICE_CONNECTION_REFUSED')]:
            with self.subTest(code=code):
                self.assertEqual(self.call_error(urllib.error.URLError(reason))['error_code'],code)

    def test_http_401_remains_http_error_and_does_not_echo_body(self):
        item=self.call_error(urllib.error.HTTPError('http://example.invalid',401,'refused',{},
                            io.BytesIO(b'<html>PRIVATE_VALUE_NOT_TO_ECHO</html>')))
        self.assertEqual(item['error_code'],'HTTP_401')
        self.assertNotIn('PRIVATE_VALUE_NOT_TO_ECHO',json.dumps(item))

    def test_service_422_business_reason_is_preserved(self):
        body=json.dumps({'error':{'code':'GZKJG_EMPTY','message':'规则库0条'}}).encode()
        item=self.call_error(urllib.error.HTTPError('http://example.invalid',422,'invalid',{},io.BytesIO(body)))
        self.assertEqual(item['error_code'],'GZKJG_EMPTY')
        self.assertIn('规则库0条',item['message'])
        self.assertIn('处理建议',item['message'])

    def test_json_without_job_id_is_a_protocol_error(self):
        self.g['_http']=Mock(return_value={'agent':[]})
        item=self.scope['main'](REQUEST)['agent'][0]
        self.assertEqual(item['error_code'],'SERVICE_RESPONSE_JOB_ID_MISSING')

    def test_old_v3_ready_response_is_not_delivered(self):
        self.g['_http']=Mock(return_value={'job_id':TASK,'status':'ready','PythonCode':'old'})
        item=self.scope['main'](REQUEST)['agent'][0]
        self.assertEqual(item['error_code'],'SERVICE_RESPONSE_CONTRACT_MISMATCH')
        self.assertEqual(item['PythonCode'],'')
        self.assertEqual(item['job_id'],TASK)

    def test_malformed_http_body_does_not_leak_content(self):
        opener=Mock();opener.open.return_value=io.BytesIO(b'<html>PRIVATE_VALUE_NOT_TO_ECHO</html>')
        with patch('urllib.request.build_opener',return_value=opener):
            item=self.scope['main'](REQUEST)['agent'][0]
        self.assertEqual(item['error_code'],'SERVICE_RESPONSE_NOT_JSON')
        self.assertNotIn('PRIVATE_VALUE_NOT_TO_ECHO',json.dumps(item))

    def test_query_timeout_preserves_known_job_id(self):
        self.g['_http']=Mock(side_effect=[{'status':'queued','job_id':TASK},urllib.error.URLError(TimeoutError())])
        item=self.scope['main'](REQUEST)['agent'][0]
        self.assertEqual(item['status'],'pending')
        self.assertEqual(item['stage'],'query_job')
        self.assertEqual(item['job_id'],TASK)
        self.assertEqual(item['error_code'],'SERVICE_TIMEOUT')

    def test_query_cannot_replace_task_id(self):
        self.g['_http']=Mock(return_value={'status':'running','job_id':'00000000-0000-0000-0000-000000000002'})
        item=self.scope['main']({'job_id':TASK})['agent'][0]
        self.assertEqual(item['error_code'],'SERVICE_RESPONSE_JOB_ID_MISMATCH')
        self.assertEqual(item['job_id'],TASK)

    def test_empty_optional_fields_and_string_gzk_do_not_break_submission(self):
        http=Mock(return_value={'job_id':TASK,'status':'failed','error':{'code':'DEMO','message':'test'}})
        self.g['_http']=http
        self.scope['main']({**REQUEST,'rule_row':'','profile':'','GZKJG':'',
                            'GZK':json.dumps({'GZKJG':REQUEST['GZKJG']})})
        payload=http.call_args.args[2]
        self.assertNotIn('rule_row',payload)
        self.assertNotIn('profile',payload)
        self.assertEqual(payload['GZKJG'],REQUEST['GZKJG'])

    def test_blank_wait_keeps_resume_job_id(self):
        self.g.update(NODE_WAIT_SECONDS=.01)
        self.g['_http']=Mock(side_effect=AssertionError('无等待预算不调用'))
        item=self.scope['main']({'job_id':TASK})['agent'][0]
        self.assertEqual(item['job_id'],TASK)


if __name__=='__main__':
    unittest.main()
