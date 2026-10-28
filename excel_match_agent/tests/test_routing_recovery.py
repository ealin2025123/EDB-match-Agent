"""路由编码修复的离线工程测试，不调用模型或修改源规则。"""
import hashlib
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from excel_match_agent.markdown_inputs import rule_row,regex_rules
from excel_match_agent.service import approved_delivery


class RoutingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.headers = ['路径','数据源路径代码','指标名称示例','指标名称取值逻辑','文件名称','人员']
        self.page = '滕州市人民政府\\政府信息公开\\财政信息'
        self.code = '2085389831646609410'
        self.old = [self.page,2.0853898316466e18,'一般公共预算收入累计值','登记正则','文章标题','不传给模型']
        self.good = [self.page,self.code,'一般公共预算收入累计值','登记正则','文章标题','不传给模型']
        self.write('old.xlsx','Sheet1',[self.old])
        self.write('full.xlsx','HTML',[self.good])
        self.job = {'rule_source':'workbook','excelrule_path':'old.xlsx','pageTitle':self.page,
                    'routing_reference_path':'full.xlsx','rule_sheet':'Sheet1'}

    def write(self,name,sheet,rows):
        book = Workbook()
        ws = book.active
        ws.title = sheet
        ws.append(self.headers)
        for row in rows:
            ws.append(row)
        book.save(self.root/name)
        book.close()

    def read(self,**updates):
        return rule_row({**self.job,**updates},lambda value:(self.root/value).resolve())

    def test_exact_text_reference_recovers_identifier_and_preserves_books(self):
        original = {p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob('*.xlsx')}
        result = self.read()
        self.assertEqual(result['数据源路径代码'],self.code)
        self.assertEqual(result['编码修复依据']['rows'],[2])
        self.assertEqual(result['编码修复依据']['sheet'],'HTML')
        self.assertEqual(result['指标名称取值逻辑'],'登记正则')
        self.assertNotIn('人员',result)
        self.assertEqual(original,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.glob('*.xlsx')})

    def test_missing_reference_never_guesses_lost_digits(self):
        result = self.read(routing_reference_path='missing.xlsx')
        self.assertEqual(result['数据源路径代码'],'')
        with self.assertRaisesRegex(ValueError,'不能推算末尾数字'):
            regex_rules({},result,lambda value:(self.root/value).resolve())

    def test_different_path_is_not_used(self):
        wrong = self.good.copy()
        wrong[0] = '另一来源'
        self.write('full.xlsx','HTML',[wrong])
        self.assertEqual(self.read()['数据源路径代码'],'')

    def test_different_name_logic_or_example_is_not_used(self):
        for column in (2,3):
            wrong = self.good.copy()
            wrong[column] = '另一业务规则'
            self.write('full.xlsx','HTML',[wrong])
            self.assertEqual(self.read()['数据源路径代码'],'')

    def test_two_distinct_text_codes_remain_unresolved(self):
        other = self.good.copy()
        other[1] = '2085389831646609999'
        self.write('full.xlsx','HTML',[self.good,other])
        self.assertEqual(self.read()['数据源路径代码'],'')

    def test_long_numeric_reference_is_also_rejected(self):
        wrong = self.good.copy()
        wrong[1] = 2.0853898316466e18
        self.write('full.xlsx','HTML',[wrong])
        self.assertEqual(self.read()['数据源路径代码'],'')

    def test_same_exact_code_duplicates_are_grounded_together(self):
        self.write('full.xlsx','HTML',[self.good,self.good])
        result = self.read()
        self.assertEqual(result['数据源路径代码'],self.code)
        self.assertEqual(result['编码修复依据']['rows'],[2,3])

    def test_explicit_verified_override_stays_authoritative(self):
        result = self.read(rule_overrides={'数据源路径代码':'1234567890123456789'},rule_override_provenance='测试已核实文本')
        self.assertEqual(result['数据源路径代码'],'1234567890123456789')
        self.assertEqual(result['编码修复依据'],'测试已核实文本')

    def test_invalid_input_public_message_contains_specific_reason(self):
        message = '正则任务缺少数据源路径代码'
        response = approved_delivery({'status':'needs_review','failure_kind':'INVALID_INPUT','failure_stage':'regex_lookup',
                     'notifications':[{'code':'INVALID_INPUT','message':message}]})
        self.assertIn(message,response['error']['message'])
        self.assertEqual(response['error']['stage'],'regex_lookup')
        self.assertIn('精确文本',response['error']['suggestion'])


if __name__ == '__main__':
    unittest.main()
