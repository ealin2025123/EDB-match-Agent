import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook
from excel_match_agent.app import run_job
from excel_match_agent.deepseek import DeepSeekBackend
from excel_match_agent.name_reference import attach_name_reference, comparison_rules, DEFAULT_WORKBOOK_NAME
from excel_match_agent.tests.test_deepseek import answer, csv_preflight_answer
from excel_match_agent.tests.test_runtime import CODE, TestExecutor


class NameReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        (self.folder / 'SKILL.md').write_text('依据源表及拼接组成生成完整名称', encoding='utf-8')
        book = Workbook()
        sheet = book.active
        sheet.title = 'path-code'
        for parts in [('GDP', '', 'GDP'), ('GDP2', '', 'GDP2'), ('地区生产总值', '累计', '地区生产总值累计')]:
            sheet.append(parts)
        book.save(self.folder / 'names.xlsx')
        book.close()
        self.job = {'source_path': 'source.csv', 'skill_path': 'SKILL.md', 'output_dir': 'runs',
                    'context_files': [{'path': 'names.xlsx', 'sheet': 'path-code', 'role': 'name_reference', 'final_name_column': 'C'}],
                    'GZKJG': [{'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'},
                              {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'},
                              {'rule_indicator_name': '地区生产总值', 'rule_indicator_code': '3'}]}

    def tearDown(self):
        self.temp.cleanup()

    def backend(self, codes, review):
        self.requests = []
        self.generated = 0
        def transport(request, timeout):
            payload = json.loads(request.data)
            preflight = csv_preflight_answer(payload)
            if preflight is not None:
                return preflight
            self.requests.append(payload)
            context = json.loads(payload['messages'][1]['content'].split('\n', 1)[1])
            if 'parsed_data' not in context:
                code = codes[min(self.generated, len(codes) - 1)]
                self.generated += 1
                return answer(code)
            return answer(message={'content': json.dumps(review(context))})
        return DeepSeekBackend('https://example.invalid', 'fixture', require_api_key=False, transport=transport)

    def decision(self, context, target, decision):
        item = context['near_name_candidates'][0]
        reason = '离线夹具：拼接表C3由指标及累计组成，源表B4为对应数值，确认同义后采用GZK最终名'
        named = target or item['candidates'][0]['rule_indicator_name']
        result = {'name_status': 'incorrect' if decision == 'same_indicator' else 'correct', 'explanation': reason,
                  'issues': [], 'candidate_reviews': [{
                      'indicator_name': item['indicator_name'], 'source': item['source'], 'decision': decision,
                      'target_indicator_name': target, 'reason': reason,
                      'compatibility': {'rule_indicator_name': named, 'unit': 'unknown', 'frequency': 'unknown',
                                        'reason': '测试未提供指标频率及标准单位，不从日期猜测；源表及拼接证据充分'}}]}
        if decision == 'same_indicator':
            result['issues'] = [{'indicator_name': item['indicator_name'], 'source': item['source'],
                                 'target_indicator_name': target, 'reason': reason}]
        return result

    def test_gzk_exact_hit_still_repairs_wrong_source_identity_using_concat_evidence(self):
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n地区生产总值,2,unit,2026-06-30\n', encoding='utf-8')
        self.job['GZKJG'].append({'rule_indicator_name': '地区财政收入累计', 'rule_indicator_code': '4'})
        wrong = CODE.replace('row[0]', '{"地区生产总值": "地区财政收入累计"}.get(row[0], row[0])')
        def review(context):
            item = context['near_name_candidates'][0]
            self.assertTrue(item['exact_gzk_match'])
            target = next(c for c in item['concatenation_candidates'] if c['rule_indicator_name'] == '地区生产总值累计')
            reference = target.get('name_reference')
            if reference is None:
                reference = next(r['name_reference'] for r in context['name_comparison_rules']
                                 if r['rule_indicator_name'] == target['name_reference_lookup'])
            self.assertEqual(reference[0]['cell'], 'C3')
            self.assertEqual(reference[0]['components'], ['地区生产总值', '累计'])
            return self.decision(context, '地区生产总值', 'same_indicator' if item['indicator_name'] == '地区财政收入累计' else 'already_canonical')
        result = run_job(self.job, self.folder, self.backend([wrong, CODE], review), TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(result['attempts'], 2)
        self.assertEqual(result['metrics']['matched_indicator_count'], 3)
        self.assertEqual(result['last_evaluation']['matching_stage'], 'after_name_review')
        stages = [s['stage'] for s in result['stage_timings']]
        self.assertLess(stages.index('name_precheck'), stages.index('name_review'))
        self.assertLess(stages.index('name_review'), stages.index('match'))
        self.assertIn('3', [r['indicator_code'] for r in result['OUTDATA']])
        self.assertNotIn('4', [r['indicator_code'] for r in result['OUTDATA']])

    def test_correct_concat_name_rebuilds_to_gzk_final_name_and_does_not_loop_back(self):
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n地区生产总值累计,2,unit,2026-06-30\n', encoding='utf-8')
        repaired = CODE.replace('row[0]', '{"地区生产总值累计": "地区生产总值"}.get(row[0], row[0])')
        def review(context):
            item = context['near_name_candidates'][0]
            if item['indicator_name'] == '地区生产总值累计':
                self.assertTrue(item['exact_concat_match'])
                self.assertFalse(item['exact_gzk_match'])
                return self.decision(context, '地区生产总值', 'same_indicator')
            self.assertEqual(context['required_name_alignments'][0]['target_basis'], 'GZKJG')
            return self.decision(context, '地区生产总值', 'already_canonical')
        result = run_job(self.job, self.folder, self.backend([CODE, repaired], review), TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(self.generated, 2)
        self.assertEqual(result['OUTDATA'][2]['indicator_name'], '地区生产总值')
        self.assertEqual(result['OUTDATA'][2]['indicator_data'], '2')

    def test_correct_gzk_name_with_no_unresolved_issue_outputs_without_regeneration(self):
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n地区生产总值,2,unit,2026-06-30\n', encoding='utf-8')
        result = run_job(self.job, self.folder, self.backend([CODE], lambda ctx: self.decision(ctx, '地区生产总值', 'already_canonical')), TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(self.generated, 1)
        self.assertEqual(result['metrics']['matched_indicator_count'], 3)

    def test_source_indicator_without_a_gzk_counterpart_is_retained_when_two_others_match(self):
        self.job['GZKJG'] = self.job['GZKJG'][:2]
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n地区生产总值累计,2,unit,2026-06-30\n', encoding='utf-8')
        result = run_job(self.job, self.folder, self.backend([CODE], lambda ctx: self.decision(ctx, '', 'different_indicator')), TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(self.generated, 1)
        self.assertEqual(result['last_evaluation']['unmatched'][0]['indicator_name'], '地区生产总值累计')

    def test_auto_path_code_lookup_retains_full_name_cell_and_components(self):
        path = self.folder / 'excelname拼接规则'
        path.mkdir()
        book = Workbook()
        sheet = book.active
        sheet.title = '123'
        sheet['A1'] = '规上工业'
        sheet['B1'] = '同比'
        sheet['K1'] = '规上工业同比'
        book.save(path / DEFAULT_WORKBOOK_NAME)
        book.close()
        business = [{'rules': [{'fields': {'数据源路径代码': '123'}}]}]
        attach_name_reference(business, {}, lambda p: self.folder / p, self.folder)
        names = comparison_rules(business, [])
        self.assertEqual(names[0]['rule_indicator_name'], '规上工业同比')
        self.assertEqual(names[0]['name_reference'][0]['cell'], 'K1')
        self.assertEqual(names[0]['name_reference'][0]['sheet'], '123')
        self.assertNotIn('rule_indicator_code', names[0])
        business = [{'rules': [{'fields': {'数据源路径代码': 'missing'}}]}]
        attach_name_reference(business, {}, lambda p: self.folder / p, self.folder)
        self.assertEqual(comparison_rules(business, []), [])
        self.assertEqual(business[-1]['role'], 'name_reference_missing')
