"""前置校验：真实HTTP后端协议用离线传输验证，绝不伪称真实模型验收。"""
import io
import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.app import run_job
from excel_match_agent.deepseek import DeepSeekBackend
from excel_match_agent.source_preflight import check_source, validate_decision, ContentPreflightError
from excel_match_agent.tests.test_markdown_agent import FakeModel, TEXT, RULES, TRACE


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'source.md').write_text(TEXT,encoding='utf-8')
        self.job = {'parse_strategy':'prompt','markdownMD5':'demo-md','markdown_path':'source.md',
                    'GZKJG':RULES,'output_dir':'runs','rule':{'指标名称示例':'指标甲累计','指标名称取值逻辑':'指标加列名'}}

    def tearDown(self):
        self.temp.cleanup()

    def decision(self,kind='has_relevant_data',text=TEXT):
        return {'decision':kind,'reason':'源文有同一指标的有效零值' if kind=='has_relevant_data' else '源文仅有其他业务数据',
                'related_indicator_names':['指标甲累计'] if kind=='has_relevant_data' else [],'evidence':[text]}

    def test_real_backend_receives_actual_body_and_gzk_then_validates_evidence(self):
        calls = []
        def transport(request,timeout):
            body=json.loads(request.data);calls.append(body)
            context=json.loads(body['messages'][1]['content'].split('\n',1)[1])
            self.assertEqual(context['source_text'],TEXT)
            self.assertEqual(context['GZKJG'],RULES)
            self.assertLessEqual(timeout,120)
            return io.BytesIO(json.dumps({'choices':[{'finish_reason':'stop','message':{
                'content':json.dumps(self.decision())}}]}).encode())
        backend=DeepSeekBackend('https://example.invalid','user-model',require_api_key=False,transport=transport)
        result=check_source(backend,TEXT,RULES,self.root/'preflight',{})
        self.assertTrue(result['allowed'])
        self.assertEqual(calls[0]['model'],'user-model')
        self.assertTrue((self.root/'source-preflight.json').is_file())

    def test_bad_evidence_or_non_gzk_name_cannot_reject_or_pass(self):
        for changes in ({'evidence':['捏造99']},{'related_indicator_names':['非GZK指标']},
                        {'decision':'has_relevant_data','related_indicator_names':[]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                validate_decision(json.dumps({**self.decision(),**changes}),TEXT,RULES)
        with self.assertRaises(ValueError):
            validate_decision('{"decision":"uncertain","decision":"no_valid_data"}',TEXT,RULES)

    def test_multiline_excel_evidence_omitting_row_prefixes(self):
        text = ('工作表 Sheet2 第5行: 第一产业（季报） | 万元 | 10208 | -3.9\n'
                '工作表 Sheet2 第6行: 第二产业（季报） | 万元 | 83147 | -3\n'
                '工作表 Sheet2 第7行: 第三产业（季报） | 万元 | 617968 | 5.5')
        quote = ('第一产业（季报） | 万元 | 10208 | -3.9\n'
                 '第二产业（季报） | 万元 | 83147 | -3\n'
                 '第三产业（季报） | 万元 | 617968 | 5.5')
        value = {**self.decision(), 'evidence':[quote]}
        self.assertEqual(validate_decision(json.dumps(value),text,RULES),value)
        invalid = [quote.replace('83147','99999'),
                   '\n'.join(reversed(quote.splitlines())),
                   quote.replace('第一产业','其他产业'),
                   '第一产业（季报） | 万元 | 10208 | -3.9 | 第二产业（季报） | 万元 | 83147 | -3',
                   quote+'\n不存在的指标 | 99']
        for bad in invalid:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_decision(json.dumps({**value,'evidence':[bad]}),text,RULES)

    def test_negative_preflight_stops_prompt_before_generation_without_rebuild(self):
        for kind,code in [('no_valid_data','SOURCE_NO_VALID_DATA'),('no_gzk_data','SOURCE_GZK_NO_RELEVANT_DATA')]:
            model=FakeModel()
            model.check_source=lambda *args:self.decision(kind)
            result=run_job(self.job,self.root,model)
            self.assertEqual(result['failure_kind'],code,result)
            self.assertEqual(result['failure_stage'],'preflight')
            self.assertEqual(result['attempts'],0)
            self.assertEqual(model.calls,[])
            self.assertFalse((Path(result['output_dir'])/'node-output.json').exists())

    def test_valid_zero_and_uncertainty_continue_to_existing_review(self):
        for kind in ('has_relevant_data','uncertain'):
            model=FakeModel();model.check_source=lambda *args:self.decision(kind)
            result=run_job(self.job,self.root,model)
            self.assertEqual(result['status'],'candidate_pass',result)
            self.assertEqual(model.generations,1)

    def test_empty_body_stops_without_calling_model(self):
        class Forbidden:
            def check_source(self,*args):
                raise AssertionError('空正文不调用模型')
        with self.assertRaises(ContentPreflightError) as caught:
            check_source(Forbidden(),'  ',RULES,self.root/'preflight',{})
        self.assertEqual(caught.exception.code,'SOURCE_NO_VALID_DATA')

    def test_excel_preflight_stops_before_parser_generation(self):
        (self.root/'source.csv').write_text('名称,数值\n无关,1\n',encoding='utf-8')
        (self.root/'SKILL.md').write_text('按实际源表生成',encoding='utf-8')
        class Gate:
            def check_source(self,context,*args):
                return {'decision':'no_gzk_data','reason':'仅有无关指标1，没有规则库对应数据',
                        'related_indicator_names':[],'evidence':['无关 | 1']}
            def generate(self,*args):
                raise AssertionError('前置拦截后不生成')
        result=run_job({'source_path':'source.csv','skill_path':'SKILL.md','GZKJG':RULES,
                        'output_dir':'runs'},self.root,Gate())
        self.assertEqual(result['failure_kind'],'SOURCE_GZK_NO_RELEVANT_DATA',result)
        self.assertEqual(result['attempts'],0)

    def test_markdown_missing_binary_and_empty_have_explicit_errors(self):
        for content,code in [(b'','MARKDOWN_EMPTY'),(b'%PDF-fake','MARKDOWN_INVALID_CONTENT')]:
            (self.root/'source.md').write_bytes(content)
            result=run_job(self.job,self.root,FakeModel())
            self.assertEqual(result['failure_kind'],code,result)
            self.assertEqual(result['attempts'],0)


if __name__=='__main__':
    unittest.main()
