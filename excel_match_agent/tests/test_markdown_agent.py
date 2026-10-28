"""离线契约/故障测试；假模型仅用于测试，不用于真实业务生成。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.app import run_job
from excel_match_agent.markdown_agent import (strategy_for, task_rules, restore_generated_asset,
                                             continue_generation_context, checked_generation, restore_generation_for_repair)
from excel_match_agent.markdown_inputs import decode_markdown, normalize_task, exported_value, regex_rules
from excel_match_agent.markdown_protocol import (validate_review, validate_rows, validate_extraction,
                                                match_rows, node_output, has_value, coverage_names)

ROWS = [dict(indicator_code="1", indicator_name="指标甲累计", indicator_data="0", indicator_unit="亿元", end_date="2026-08-31"),
        dict(indicator_code="2", indicator_name="指标乙同比", indicator_data="-9.0", indicator_unit="%", end_date="2026-08-31")]
RULES = [dict(rule_indicator_code="1", rule_indicator_name="指标甲累计", rule_constant_name="万元", indicator_release_frequency="月"),
         dict(rule_indicator_code="2", rule_indicator_name="指标乙同比", rule_constant_name="%", indicator_release_frequency="月")]
TEXT = "# 2026年1-8月测试\n指标甲累计0亿元。指标乙同比下降9.0%。"
TRACE = '<!--\n' + '\n'.join(x + '：' for x in ('规则表行','数据源路径编码','数据来源编码','采集数据源','路径','老采集站点名称','目标表','目标模块','源文件','MD5')) + '\n-->\n仅输出JSON。'


def review(rows=ROWS):
    return {"status": "correct", "explanation": "源文证据均符合", "row_checks": [
        {"index": i, "result": "valid", "source_quote": TEXT, "target_name": "", "reason": "源文有值"} for i, _ in enumerate(rows)],
        "coverage": [{"indicator_name": r["rule_indicator_name"], "result": "present" if any(v["indicator_name"]==r["rule_indicator_name"] for v in rows) else "not_in_source", "source_quote": TEXT, "reason": "核对源文"} for r in RULES]}


class FakeModel:
    def __init__(self, rows=None, reviews=None, html=False):
        self.rows = copy.deepcopy(ROWS if rows is None else rows)
        self.reviews = reviews
        self.html = html
        self.generations = 0
        self.calls = []

    def _complete(self, system, context, folder, schema, validator):
        self.calls.append((system, copy.deepcopy(context)))
        if folder.name == "generation":
            self.generations += 1
            if self.html:
                rows = [{k:v for k,v in r.items() if k != "indicator_code"} for r in self.rows]
                answer = {"decision": "generate", "python_code": "def main(params):\n    return " + repr({"ExcelData": rows}), "explanation": "test only"}
            else:
                answer = {"decision": "generate", "prompt_code": TRACE, "explanation": "test only"}
        elif folder.name == "extraction":
            # PromptCode确实作为system，执行上下文确实包含正文/GZK，不能只给MD5。
            assert system == TRACE and context["Markdown"].replace('\r\n','\n') == TEXT and context["GZK"]["GZKJG"]
            answer = {"out_data": self.rows}
        else:
            answer = self.reviews(self.generations) if self.reviews else review(self.rows)
        raw = json.dumps(answer, ensure_ascii=False)
        (folder / "model-response-raw.json").write_text(raw, encoding="utf-8")
        return validator(raw)


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        (self.base / "source.md").write_text(TEXT, encoding="utf-8")
        self.job = {"parse_strategy":"prompt", "markdownMD5":"sample-id", "markdown_path":"source.md",
                    "GZKJG":RULES, "rule":{"数据源路径代码":"123", "指标名称取值逻辑":"指标列+列名"},
                    "output_dir":"runs", "publictime":"2026-09-01", "title":"2026年1-8月测试",
                    "export_platform_parser":False}

    def tearDown(self):
        self.temp.cleanup()

    def run_model(self, model=None, **job):
        return run_job({**self.job, **job}, self.base, backend=model or FakeModel())

    def test_prompt_real_execution_boundary_and_txt_contract(self):
        result = self.run_model()
        self.assertEqual(result["status"], "candidate_pass")
        output=json.loads((Path(result["output_dir"])/"node-output.json").read_text(encoding="utf-8"))
        self.assertEqual(set(output), {"OUTDATA"})
        row=output["OUTDATA"][0]
        self.assertNotIn("SJPLLB",row)
        self.assertIn("convert_ratio",row)
        self.assertEqual(row["indicator_release_frequency"],"月")
        self.assertEqual(row["indicator_data"],"0")
        self.assertEqual(row["KB_unit"],"万元")
        self.assertEqual(row["indicator_unit"],"亿元")

    def test_no_match_rate_threshold(self):
        # 名称匹配率50%仍应通过；另外两条为源文真实但无规则编码的HTML指标。
        from excel_match_agent.markdown_protocol import match_rows
        rows=[{k:v for k,v in r.items() if k!='indicator_code'} for r in ROWS]
        rows += [{**rows[0],"indicator_name":"其他甲"},{**rows[1],"indicator_name":"其他乙"}]
        result=match_rows(rows,RULES,{})
        self.assertEqual(result["metrics"]["matched_indicator_count"],2)
        self.assertEqual(result["metrics"]["match_rate"],.5)

    def test_maximum_two_rebuilds(self):
        model=FakeModel(rows=ROWS[:1])
        result=self.run_model(model)
        self.assertEqual(model.generations,3)
        self.assertEqual(result["rebuilds"],2)
        self.assertEqual(result["failure_kind"],"MAX_REBUILDS_EXHAUSTED")
        self.assertFalse((Path(result["output_dir"])/"node-output.json").exists())

    def test_omission_triggers_rebuild_despite_two_matches(self):
        rules=RULES+[dict(rule_indicator_code="3",rule_indicator_name="丙",rule_constant_name="%")]
        def checks(attempt):
            item=review()
            item['coverage'].append(dict(indicator_name="丙",result="missing",source_quote=TEXT,reason="测试遗漏"))
            item['status']='incorrect'
            return item
        model=FakeModel(reviews=checks)
        result=self.run_model(model,GZKJG=rules)
        self.assertEqual(result['attempts'],3)
        self.assertTrue(result['requires_human_review'])

    def test_empty_gzk_never_calls_model(self):
        model=FakeModel()
        result=self.run_model(model,GZKJG=[])
        self.assertEqual(result['failure_kind'],'GZKJG_EMPTY')
        self.assertEqual(model.calls,[])

    def test_bad_code_pair_rejected(self):
        rows=copy.deepcopy(ROWS);rows[0]['indicator_code']='2'
        result=self.run_model(FakeModel(rows=rows))
        self.assertEqual(result['failure_kind'],'MAX_REBUILDS_EXHAUSTED')

    def test_invalid_review_protocol_is_not_asset_rebuild(self):
        result=self.run_model(FakeModel(reviews=lambda _: {'status':'correct'}))
        self.assertEqual(result['failure_kind'],'MODEL_PROTOCOL_FAILED')
        self.assertEqual(result['attempts'],1)

    def test_uncertain_stops_without_rebuild(self):
        def checks(_):
            value=review();value['status']='uncertain';return value
        result=self.run_model(FakeModel(reviews=checks))
        self.assertEqual(result['failure_kind'],'BUSINESS_AMBIGUITY')
        self.assertEqual(result['attempts'],1)

    def test_html_real_subprocess_and_common_output(self):
        (self.base/'page.html').write_text('<html><body>'+TEXT+'</body></html>',encoding='utf-8')
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}):
            result=self.run_model(FakeModel(html=True),parse_strategy='html',webpage_path='page.html',source_url='https://example.org/source')
        self.assertEqual(result['status'],'candidate_pass',result)
        self.assertEqual(set(result['output_keys']),{'OUTDATA','unitArray','codeOutputResult'})
        self.assertIn('SJPLLB',result['OUTDATA'][0])
        self.assertNotIn('indicator_release_frequency',result['OUTDATA'][0])

    def test_authorized_markdown_regression_never_exports_formal_success(self):
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}), \
             patch('excel_match_agent.markdown_agent.page_evidence') as page:
            result=self.run_model(FakeModel(html=True),parse_strategy='html',markdown_only_regression=True,
                                  webpage_defer_authorization='用户明确允许先回归Markdown并保留待核验')
        page.assert_not_called()
        self.assertEqual(result['status'],'markdown_verified_pending_webpage')
        self.assertFalse(result['production_ready'])
        self.assertTrue(result['markdown_regression_passed'])
        folder=Path(result['output_dir'])
        self.assertFalse((folder/'node-output.json').exists())
        self.assertTrue((folder/'regression-node-output.json').exists())
        self.assertNotIn('OUTDATA',result)

    def test_markdown_regression_requires_explicit_authorization_trace(self):
        model=FakeModel(html=True)
        result=self.run_model(model,parse_strategy='html',markdown_only_regression=True)
        self.assertEqual(result['failure_kind'],'INVALID_INPUT')
        self.assertEqual(model.calls,[])

    def test_prompt_cannot_silently_defer_webpage(self):
        result=self.run_model(markdown_only_regression=True,webpage_defer_authorization='not applicable')
        self.assertEqual(result['failure_kind'],'INVALID_INPUT')

    def test_markdown_not_original_file_fallback(self):
        job={**self.job};job.pop('markdownMD5')
        result=run_job({**job,'fileMd5':'other'},self.base,backend=FakeModel())
        self.assertEqual(result['failure_kind'],'MARKDOWN_MD5_MISSING')

    def test_alias_conflict(self):
        with self.assertRaises(ValueError):
            normalize_task({'markdownMD5':'a','markdownMd5':'b'},lambda x:x)

    def test_decode_accepts_html_tables_but_not_html_document(self):
        self.assertIn('<table>',decode_markdown(b'<table><tr><td>1</td></tr></table>'))
        self.assertIn('[title]',decode_markdown(b'[title](https://example.org)\n1'))
        for raw in (b'',b'<html>bad</html>',b'{"error":"bad"}',b'%PDF-data',b'PKfile'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):decode_markdown(raw)

    def test_pdf_prompt_routing_and_explicit_override(self):
        self.assertEqual(strategy_for({'fileType':'pdf'}),'prompt')
        self.assertEqual(strategy_for({'fileType':'html'}),'html')
        self.assertEqual(strategy_for({'fileType':'pdf','parse_strategy':'excel'}),'excel')

    def test_extraction_schema_and_precision(self):
        self.assertEqual(validate_extraction(json.dumps({'out_data':ROWS}))['out_data'][1]['indicator_data'],'-9.0')
        with self.assertRaises(ValueError):validate_extraction('{"OUTDATA":[]}')
        for value in ('NaN','Infinity','--'):
            row={**ROWS[0],'indicator_data':value}
            with self.assertRaises(ValueError):validate_rows([row],'prompt')

    def test_review_must_cover_all_names_and_records(self):
        value=review();value['coverage'].pop()
        with self.assertRaises(ValueError):validate_review(json.dumps(value),ROWS,TEXT,coverage_names(RULES,{}))
        value=review();value['row_checks'][0]['source_quote']='伪造0'
        with self.assertRaises(ValueError):validate_review(json.dumps(value),ROWS,TEXT,coverage_names(RULES,{}))

    def test_numeric_evidence_preserves_sign(self):
        self.assertTrue(has_value('-9.0','下降9.0%'))
        self.assertFalse(has_value('-9.0','增长9.0%'))
        self.assertFalse(has_value('9.0','19.0%'))

    def test_different_scope_does_not_block_valid_matches(self):
        name = '外币各项贷款余额'
        quote = '本外币各项贷款余额765.33亿元'
        value = review()
        value['coverage'].append({'indicator_name':name,'result':'not_in_source',
                                 'source_quote':quote,'reason':'原文为本外币，不能代表外币单项'})
        names = coverage_names(RULES,{}) + [name]
        self.assertEqual(validate_review(json.dumps(value),ROWS,TEXT+quote,names)['status'],'correct')
        model = FakeModel(reviews=lambda _: value)
        extra = dict(rule_indicator_code='3',rule_indicator_name=name,rule_constant_name='万元',indicator_release_frequency='月')
        (self.base/'source.md').write_text(TEXT+quote,encoding='utf-8')
        # HTML假执行器不依赖Prompt测试中固定正文的断言。
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}):
            model.html = True
            result=self.run_model(model,parse_strategy='html',GZKJG=RULES+[extra],
                                  markdown_only_regression=True,webpage_defer_authorization='用户授权Markdown回归')
        self.assertTrue(result.get('markdown_regression_passed'),result)
        self.assertEqual(result['matched_indicator_count'],2)
        self.assertEqual(model.generations,1)

    def test_continuation_inherits_budget_and_does_not_execute_tentative_asset(self):
        origin=self.base/'origin';origin.mkdir()
        context={'Markdown':'same source','rule':{'id':'123'},'history':[],'skill':['new policy']}
        (origin/'input-context.json').write_text(json.dumps({**context,'skill':['old policy']}),encoding='utf-8')
        (origin/'run-result.json').write_text(json.dumps({'attempts':2,'history':[{'attempt':1}]}),encoding='utf-8')
        for i in (1,2):
            folder=origin/f'attempt-{i}'/'generation';folder.mkdir(parents=True)
            (folder/'model-request.json').write_text('{}',encoding='utf-8')
        asset={'decision':'needs_review','python_code':'tentative','explanation':'different scope'}
        (folder/'model-response-raw.json').write_text(json.dumps({'content':json.dumps(asset)}),encoding='utf-8')
        (folder/'model-metadata.json').write_text(json.dumps({'provider':'deepseek','finish_reason':'stop'}),encoding='utf-8')
        self.assertEqual(continue_generation_context(origin,context,'html'),2)
        self.assertEqual(context['previous_asset'],'tentative')
        self.assertEqual(context['history'],[{'attempt':1}])
        with self.assertRaises(ValueError):
            continue_generation_context(origin,{**context,'Markdown':'changed'},'html')

    def test_generation_duplicate_decision_repaired_once_without_rebuild(self):
        class Model(FakeModel):
            repairs=0
            def _complete(self,system,context,folder,schema,validator):
                if folder.name=='generation':
                    self.generations+=1
                    raw='{"decision":"generate","python_code":"bad","explanation":"test","decision":"needs_review"}'
                    (folder/'model-response-raw.json').write_text(json.dumps({'content':raw}),encoding='utf-8')
                    return validator(raw)
                if folder.name=='protocol-repair' and folder.parent.name=='generation':
                    self.repairs+=1
                    assert 'generation_protocol_feedback' in context
                    return validator(json.dumps({'decision':'generate','prompt_code':TRACE,'explanation':'repaired'}))
                return super()._complete(system,context,folder,schema,validator)
        model=Model()
        result=self.run_model(model)
        self.assertEqual(result['status'],'candidate_pass',result)
        self.assertEqual(result['attempts'],1)
        self.assertEqual(result['rebuilds'],0)
        self.assertEqual(model.generations,1)
        self.assertEqual(model.repairs,1)

    def test_generation_repeated_invalid_repair_is_protocol_failure(self):
        class Model(FakeModel):
            repairs=0
            def _complete(self,system,context,folder,schema,validator):
                if folder.name in {'generation','protocol-repair'}:
                    raw='{"decision":"generate","decision":"needs_review"}'
                    (folder/'model-response-raw.json').write_text(json.dumps({'content':raw}),encoding='utf-8')
                    if folder.name=='generation':self.generations+=1
                    else:self.repairs+=1
                    return validator(raw)
                return super()._complete(system,context,folder,schema,validator)
        model=Model()
        result=self.run_model(model)
        self.assertEqual(result['failure_kind'],'MODEL_PROTOCOL_FAILED',result)
        self.assertEqual(model.repairs,1)
        self.assertEqual(model.generations,1)
        self.assertFalse((Path(result['output_dir'])/'node-output.json').exists())

    def test_generation_truncation_never_format_repaired(self):
        from excel_match_agent.app import ModelOutputTruncated
        class Model:
            calls=0
            def _complete(self,*args):
                self.calls+=1
                raise ModelOutputTruncated('truncated')
        model=Model()
        with self.assertRaises(ModelOutputTruncated):
            checked_generation(model,'test',{},self.base/'generation','html')
        self.assertEqual(model.calls,1)

    def test_protocol_recovery_checks_input_finish_and_budget(self):
        origin=self.base/'protocol-origin';origin.mkdir()
        context={'Markdown':'source','history':[]}
        (origin/'input-context.json').write_text(json.dumps(context),encoding='utf-8')
        (origin/'run-result.json').write_text(json.dumps({'attempts':3}),encoding='utf-8')
        gen=origin/'attempt-3/generation';gen.mkdir(parents=True)
        for name,value in [('model-request.json',{}),('model-response-invalid.json',{}),
                           ('model-metadata.json',{'provider':'deepseek','finish_reason':'stop'})]:
            (gen/name).write_text(json.dumps(value),encoding='utf-8')
        self.assertEqual(restore_generation_for_repair(origin,context),(gen,3))
        with self.assertRaises(ValueError):restore_generation_for_repair(origin,{**context,'Markdown':'changed'})
        (gen/'protocol-repair').mkdir()
        with self.assertRaises(ValueError):restore_generation_for_repair(origin,context)

    def test_repaired_generation_is_recoverable_with_original_budget(self):
        origin=self.base/'repaired-origin';origin.mkdir()
        context={'Markdown':'same','history':[]}
        (origin/'input-context.json').write_text(json.dumps(context),encoding='utf-8')
        (origin/'run-result.json').write_text(json.dumps({'attempts':3,'prior_generation_calls':3}),encoding='utf-8')
        folder=origin/'attempt-3';gen=folder/'generation';fixed=gen/'protocol-repair';fixed.mkdir(parents=True)
        (gen/'model-request.json').write_text('{}',encoding='utf-8')
        (gen/'model-response-raw.json').write_text(json.dumps({'content':'{"decision":"generate","decision":"generate"}'}),encoding='utf-8')
        code='def main(params):\n    return []'
        asset={'decision':'generate','python_code':code,'explanation':'format repaired'}
        (fixed/'model-response-raw.json').write_text(json.dumps({'content':json.dumps(asset)}),encoding='utf-8')
        (fixed/'model-metadata.json').write_text(json.dumps({'provider':'deepseek','finish_reason':'stop'}),encoding='utf-8')
        (folder/'parser.py').write_text(code,encoding='utf-8')
        self.assertEqual(restore_generated_asset(origin,context,'html'),(asset,gen,3))
        self.assertEqual(continue_generation_context(origin,context,'html'),3)
        self.assertEqual(context['previous_asset'],code)

    def test_html_output_types_align_workflow_but_core_and_prompt_stay_strings(self):
        rows=[{k:v for k,v in row.items() if k!='indicator_code'} for row in ROWS]
        evaluation=match_rows(rows,RULES,{})
        html=node_output(evaluation,RULES,'html')
        self.assertIsInstance(html['OUTDATA'][0]['indicator_data'],int)
        self.assertEqual(html['OUTDATA'][1]['indicator_data'],'-9.0')
        self.assertEqual(rows[0]['indicator_data'],'0')
        self.assertEqual(html['OUTDATA'],html['codeOutputResult']['OUTDATA'])
        self.assertEqual(node_output(evaluation,RULES,'prompt')['OUTDATA'][0]['indicator_data'],'0')

    def test_https_comment_is_not_a_windows_path(self):
        class UrlModel(FakeModel):
            def _complete(self, system, context, folder, schema, validator):
                if folder.name=='generation':
                    answer={'decision':'generate','python_code':'# https://example.org/source\ndef main(params):\n    return '+repr({'ExcelData':[{k:v for k,v in row.items() if k!='indicator_code'} for row in ROWS]}),'explanation':'test'}
                    return validator(json.dumps(answer))
                return super()._complete(system,context,folder,schema,validator)
        with patch('excel_match_agent.markdown_agent.regex_rules',return_value={'format':'test','rules':[]}):
            result=self.run_model(UrlModel(html=True),parse_strategy='html',markdown_only_regression=True,
                                  webpage_defer_authorization='用户授权Markdown回归')
        self.assertEqual(result['status'],'markdown_verified_pending_webpage',result)
        self.assertEqual(result['attempts'],1)

    def test_recovery_counts_interrupted_generation_and_requires_exact_input(self):
        origin=self.base/'origin';first=origin/'attempt-1';generation=first/'generation';generation.mkdir(parents=True)
        second=origin/'attempt-2/generation';second.mkdir(parents=True)
        for path in (generation,second):(path/'model-request.json').write_text('{}',encoding='utf-8')
        context={'Markdown':'exact source','rule':{'path':'123'}}
        (origin/'input-context.json').write_text(json.dumps(context),encoding='utf-8')
        code='def main(params):\n    return []'
        answer={'decision':'generate','python_code':code,'explanation':'test'}
        (generation/'model-response-raw.json').write_text(json.dumps({'content':json.dumps(answer)}),encoding='utf-8')
        (generation/'model-metadata.json').write_text(json.dumps({'provider':'deepseek','finish_reason':'stop'}),encoding='utf-8')
        (first/'parser.py').write_text(code,encoding='utf-8')
        restored,path,used=restore_generated_asset(origin,context,'html')
        self.assertEqual(restored['python_code'],code)
        self.assertEqual(used,2)
        with self.assertRaises(ValueError):restore_generated_asset(origin,{**context,'Markdown':'changed'},'html')

    def test_export_decoder_does_not_duplicate_outer_fields(self):
        path=self.base/'export.txt';path.write_text('输出:\ncodeOutputResult：{"GZKJG":[]}\nGZKJG：[]',encoding='utf-8')
        self.assertEqual(exported_value(path,'codeOutputResult'),{'GZKJG':[]})

    def test_raw_gzk_adapter_keeps_names_codes_frequencies(self):
        rules=task_rules({'GZK':{'ZBDM':[1,2],'ZBMC':['甲','乙'],'ZBDW':['万元','%'],'indicator_release_frequency':['季','月']}},lambda x:x)
        self.assertEqual([r['rule_indicator_code'] for r in rules],['1','2'])
        self.assertEqual(rules[0]['indicator_release_frequency'],'季')

    def test_plain_gzkjg_export_supported(self):
        path=self.base/'rules.txt';path.write_text('输出:\nGZKJG：'+json.dumps(RULES,ensure_ascii=False),encoding='utf-8')
        self.assertEqual(task_rules({'gzk_file':str(path)},Path),RULES)

    def test_duplicate_json_fields_rejected(self):
        with self.assertRaises(ValueError):validate_extraction('{"out_data":[],"out_data":[]}')

    def test_regex_new_multiline_exact_task_id(self):
        # 工作簿替身保留真实C/F/K列布局，验证不混入近似任务且全部行读取。
        class Sheet:
            values=[(None,None,'123',None,None,'甲',None,None,None,None,'[{"selector_type":"regex","rules":["a(1)","a(2)"]}]'),
                    (None,None,'123',None,None,'乙',None,None,None,None,'[{"selector_type":"regex","rules":["b(3)"]}]'),
                    (None,None,'1234',None,None,'错误任务',None,None,None,None,'[]')]
        class Book:
            sheetnames=['汇总']
            def __getitem__(self,key):return Sheet()
            def close(self):pass
        path=self.base/'new.xlsx';path.touch()
        with patch('openpyxl.load_workbook',return_value=Book()):
            result=regex_rules({'regex_path':str(path),'legacy_regex_path':str(self.base/'absent.xlsx')},{'数据源路径代码':'123'},Path)
        self.assertEqual([r['indicator_name'] for r in result['rules']],['甲','甲','乙'])

    def test_regex_present_but_empty_never_falls_back(self):
        class Sheet:
            values=[(None,None,'123',None,None,'甲',None,None,None,None,'[]')]
        class Book:
            sheetnames=['汇总']
            def __getitem__(self,key):return Sheet()
            def close(self):pass
        path=self.base/'new.xlsx';path.touch()
        with patch('openpyxl.load_workbook',return_value=Book()),self.assertRaises(ValueError):
            regex_rules({'regex_path':str(path)},{'数据源路径代码':'123'},Path)


if __name__=='__main__':unittest.main()
