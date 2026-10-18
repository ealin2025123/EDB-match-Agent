import copy
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.html_platform_export import export_html_platform_parser, load_html_adapter, configure_endpoint
from excel_match_agent.platform_export import PlatformExportError
from excel_match_agent.tests import test_markdown_agent as fixtures
from excel_match_agent.tests.test_markdown_agent import FakeModel

CORE = '''def main(params):
    numbers = params['Markdown'].strip().splitlines()
    return {'code':0,'msg':'success','ExcelData':[{'indicator_name':name,'indicator_data':value,'indicator_unit':'亿元','end_date':'2026-06-30'} for name,value in zip(['甲','乙'],numbers)]}
'''
ROWS = [dict(indicator_name=name, indicator_data=value, indicator_unit='亿元', end_date='2026-06-30') for name,value in [('甲','12'),('乙','3')]]
RULES = [dict(rule_indicator_name=name, rule_indicator_code=str(i), rule_constant_name='万元') for i,name in enumerate(['甲','乙'],1)]


class HtmlPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.folder=Path(self.tmp.name)
        self.core=self.folder/'core.py';self.core.write_text(CORE,encoding='utf-8')
        self.calls=[]

    def tearDown(self):self.tmp.cleanup()

    def runner(self, path, params, folder, label, timeout):
        calls=self.calls
        class Response:
            status=200;headers={'Content-Type':'text/plain'}
            def read(self,limit):return b'12\n3'
            def close(self):pass
        class Opener:
            def open(self,request,timeout):
                calls.append(request.full_url)
                assert not request.has_header('Authorization')
                return Response()
        with patch('urllib.request.build_opener',return_value=Opener()):
            return runpy.run_path(str(path))['main'](params)

    def export(self,runner=None,**job):
        return export_html_platform_parser(self.core,ROWS,{'markdownMD5':'test-id','title':'2026年1-6月'},self.folder,
            {'download_base_url':'http://example.test/api/file/download',**job},RULES,runner=runner or self.runner)

    def test_actual_adapter_path_contract_and_verbatim_core(self):
        result=self.export()
        self.assertEqual(self.calls,['http://example.test/api/file/download/test-id']*2)
        code=Path(result['platform_parser_path']).read_text(encoding='utf-8')
        self.assertIn(CORE,code)
        self.assertEqual(Path(result['platform_parser_path']).read_bytes(),Path(result['platform_parser_text_path']).read_bytes())
        self.assertTrue(result['platform_download_verified'])
        self.assertEqual(json.loads(Path(result['platform_validation_path']).read_text())['distinct_indicators'],2)

    def test_wrong_download_rows_cannot_be_published(self):
        def wrong(*args):return {'code':0,'ExcelData':ROWS[:1]}
        with self.assertRaises(PlatformExportError):self.export(runner=wrong)
        self.assertFalse((self.folder/'platform/platform-parser.py').exists())
        report=json.loads((self.folder/'platform/platform-validation.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'failed')

    def test_export_cannot_skip_real_download(self):
        with self.assertRaises(PlatformExportError):self.export(platform_verify_download=False)
        self.assertEqual(self.calls,[])

    def test_missing_md5_never_falls_back_to_original(self):
        with self.assertRaises(PlatformExportError):
            export_html_platform_parser(self.core,ROWS,{'fileMd5':'other'},self.folder,{},RULES,runner=self.runner)
        self.assertEqual(self.calls,[])

    def test_global_helper_collision_fails_before_execution(self):
        self.core.write_text(CORE+'\ndef _decode_text(value):\n    return value\n',encoding='utf-8')
        with self.assertRaisesRegex(PlatformExportError,'名称冲突'):self.export()
        self.assertEqual(self.calls,[])

    def test_adapter_asset_hash_and_original_responses(self):
        prefix,adapter,proof=load_html_adapter()
        self.assertEqual(proof['provider'],'deepseek')
        self.assertIn('def main(params)',prefix)
        configured=configure_endpoint(adapter,'http://example.test/download/')
        self.assertIn("_DOWNLOAD_BASE_URL = 'http://example.test/download'",configured)
        self.assertNotIn('?markdownMd5=',configured)
        with self.assertRaises(Exception):configure_endpoint(adapter,'http://name:password@example.test/')


class HtmlPipelineExportTests(unittest.TestCase):
    setUp = fixtures.MarkdownTests.setUp
    tearDown = fixtures.MarkdownTests.tearDown
    run_model = fixtures.MarkdownTests.run_model
    def test_export_is_automatic_for_html_and_keeps_pending_webpage(self):
        delivered={'platform_parser_path':'verified.py','platform_download_verified':True}
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}), \
             patch('excel_match_agent.markdown_agent.export_html_platform_parser',return_value=delivered) as export:
            result=self.run_model(FakeModel(html=True),parse_strategy='html',export_platform_parser=True,
                                  markdown_only_regression=True,webpage_defer_authorization='用户明确允许')
        self.assertEqual(result['status'],'markdown_verified_pending_webpage')
        self.assertEqual(result['platform_parser_path'],'verified.py')
        self.assertFalse(result['production_ready'])
        export.assert_called_once()

    def test_export_failure_does_not_regenerate_business_asset(self):
        model=FakeModel(html=True)
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}), \
             patch('excel_match_agent.markdown_agent.export_html_platform_parser',side_effect=PlatformExportError('download failed')):
            result=self.run_model(model,parse_strategy='html',export_platform_parser=True,
                                  markdown_only_regression=True,webpage_defer_authorization='用户明确允许')
        self.assertEqual(result['failure_kind'],'PLATFORM_EXPORT_FAILED')
        self.assertEqual(model.generations,1)
        folder=Path(result['output_dir'])
        self.assertFalse((folder/'node-output.json').exists())
        self.assertFalse((folder/'regression-node-output.json').exists())

    def test_prompt_never_uses_html_exporter(self):
        with patch('excel_match_agent.markdown_agent.export_html_platform_parser') as export:
            result=self.run_model(export_platform_parser=True)
        export.assert_not_called()
        self.assertEqual(result['status'],'candidate_pass')
