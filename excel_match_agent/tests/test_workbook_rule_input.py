from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from excel_match_agent.markdown_inputs import rule_row


class WorkbookRuleTests(unittest.TestCase):
    def setUp(self):
        headers=['数据来源编码','数据源路径代码','路径','指标名称示例','指标名称取值逻辑','所属模块','数据目标表','文件名称','无关人员字段']
        values=[1020000741,2.08499399225206e18,'来源\\统计信息','GDP','登记正则','地方经济','usrDFJJYXSJB','文章标题','不应传给模型']
        cells=[SimpleNamespace(value=value,row=1197,hyperlink=SimpleNamespace(target='https://example.org/article') if h=='文件名称' else None) for h,value in zip(headers,values)]
        self.iterator_calls=[]
        owner=self
        class Sheet:
            title='Sheet1';max_row=1291
            def __getitem__(self,index):return [SimpleNamespace(value=h) for h in headers]
            def iter_rows(self,**kwargs):
                owner.iterator_calls.append(kwargs)
                return iter([cells])
        self.sheet=Sheet()
        class Book:
            worksheets=[owner.sheet]
            def __getitem__(self,name):return owner.sheet
            def close(self):pass
        self.book=Book()
        self.job={'rule_source':'workbook','excelrule_path':'rules.xlsx','rule_sheet':'Sheet1','rule_row':1197,
                  'routing_reference_path':'nonexistent-routing-reference-fixture.xlsx',
                  'pageTitle':'来源\\统计信息','rule':{'指标名称示例':'旧快照'}}

    def read(self,**updates):
        with patch('openpyxl.load_workbook',return_value=self.book):
            return rule_row({**self.job,**updates},Path)

    def test_workbook_mode_overrides_stale_snapshot_and_maps_headers(self):
        result=self.read()
        self.assertEqual(result['指标名称示例'],'GDP')
        self.assertEqual(result['目标模块'],'地方经济')
        self.assertEqual(result['目标表'],'usrDFJJYXSJB')
        self.assertEqual(result['source_url'],'https://example.org/article')
        self.assertEqual(result['规则表行'],1197)
        self.assertNotIn('无关人员字段',result)
        self.assertEqual(self.iterator_calls,[{'min_row':1197,'max_row':1197}])

    def test_precision_lost_code_is_not_guessed(self):
        result=self.read()
        self.assertEqual(result['数据源路径代码'],'')
        self.assertIn('规则读取警告',result)

    def test_explicit_identifier_preserved_as_text(self):
        result=self.read(rule_overrides={'数据源路径代码':'2084993992252060000'})
        self.assertEqual(result['数据源路径代码'],'2084993992252060000')
        self.assertEqual(result['数据来源编码'],'1020000741')

    def test_mismatched_start_path_rejected(self):
        with self.assertRaisesRegex(ValueError,'不一致'):self.read(pageTitle='另一来源')

    def test_bad_row_numbers_rejected(self):
        for number in (1,1292,1197.5,True,'1197.5'):
            with self.subTest(number=number),self.assertRaises(ValueError):self.read(rule_row=number)

    def test_business_logic_cannot_be_hidden_in_identifier_overrides(self):
        with self.assertRaises(ValueError):self.read(rule_overrides={'指标名称取值逻辑':'改规则'})

    def test_existing_inline_mode_unchanged(self):
        with patch('openpyxl.load_workbook') as loader:
            result=rule_row({'rule':{'指标名称示例':'inline'}},Path)
        self.assertEqual(result,{'指标名称示例':'inline'})
        loader.assert_not_called()
