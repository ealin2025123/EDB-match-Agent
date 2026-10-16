import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook, load_workbook
from excel_match_agent.app import run_job
from excel_match_agent.deepseek import DeepSeekBackend, validate_decision
from excel_match_agent.rule_derivation import FIELDS, POLICY
from excel_match_agent.tests.test_deepseek import answer
from excel_match_agent.tests.test_runtime import TestExecutor


class RuleDerivationTests(unittest.TestCase):
    def test_generate_requires_all_four_rules_but_missing_source_evidence_can_request_review(self):
        decision = {"decision": "generate", "python_code": "def main(params): return []", "explanation": "x"}
        with self.assertRaisesRegex(ValueError, "四项推导规则"):
            validate_decision(json.dumps(decision), require_derivation=True)
        decision['derived_rules'] = {name: '真实表头依据' for name in FIELDS}
        decision['derived_rules'][FIELDS[0]] = ''
        with self.assertRaises(ValueError):
            validate_decision(json.dumps(decision), require_derivation=True)
        pending = {"decision": "needs_review", "python_code": "", "explanation": "源文件及名称未提供统计期间依据"}
        self.assertEqual(validate_decision(json.dumps(pending), require_derivation=True)['decision'], 'needs_review')

    def test_only_example_and_name_logic_reach_deepseek_other_excelrule_fields_are_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n', encoding='utf-8')
            (folder / 'SKILL.md').write_text('按规则解析；四字段根据源表推导', encoding='utf-8')
            book = Workbook()
            sheet = book.active
            sheet.append(['路径', '文件名称', '指标名称示例', '指标取值逻辑', *FIELDS])
            sheet.append(['本次路径', 'source.csv', 'GDP', 'A列指标，B列数值', '不应传给模型的旧规则', None, None, None])
            book.save(folder / 'excelrule.xlsx')
            book.close()
            requests = []
            def transport(request, timeout):
                payload = json.loads(request.data)
                requests.append(payload)
                if len(requests) == 1:
                    context = json.loads(payload['messages'][1]['content'].split('\n', 1)[1])
                    self.assertEqual(context['rule_derivation_policy'], POLICY)
                    rule = context['business_rules'][0]['rules'][0]['fields']
                    self.assertEqual(rule, {'指标名称示例': 'GDP', '指标名称取值逻辑': 'A列指标，B列数值'})
                    self.assertTrue(all(name not in rule for name in FIELDS))
                    self.assertNotIn('不应传给模型的旧规则', json.dumps(payload, ensure_ascii=False))
                    return answer()
                return answer(message={'content': json.dumps({'name_status': 'correct', 'explanation': '离线夹具：名称与源表相同',
                                                               'issues': [], 'candidate_reviews': []})})
            backend = DeepSeekBackend('https://example.invalid', 'test', require_api_key=False, transport=transport)
            result = run_job({'source_path': 'source.csv', 'pagetitle': '本次路径', 'skill_path': 'SKILL.md',
                              'excelrule_path': 'excelrule.xlsx', 'output_dir': 'runs', 'GZKJG': [
                                  {'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'},
                                  {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'}]}, folder, backend, TestExecutor())
            self.assertEqual(result['status'], 'candidate_pass')
            derived = json.loads((Path(result['output_dir']) / 'attempt-1/derived-rules.json').read_text(encoding='utf-8'))
            self.assertEqual(set(derived), set(FIELDS))
            unchanged = load_workbook(folder / 'excelrule.xlsx', data_only=True)
            self.assertEqual(unchanged.active['E2'].value, '不应传给模型的旧规则')
            unchanged.close()
