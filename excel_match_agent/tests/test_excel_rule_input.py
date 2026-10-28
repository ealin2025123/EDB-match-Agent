from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from excel_match_agent.app import business_context
from excel_match_agent.excel_rule_input import excel_rule_context


class ExcelRuleInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.primary = self.root / 'rules.xlsx'
        self.fallback = self.root / 'full.xlsx'
        self.page = '临沧市人民政府\\政务公开\\统计信息'
        self.headers = ['路径', '数据源路径代码', '指标名称示例', '指标名称取值逻辑', '截止日期位置', '文件名称', '人员']
        self.row = [self.page, '2094652937900003330', '生产总值地区生产总值累计', 'sheet名+A列+行2行3', '旧日期不要传', '旧季度样例.xls', '隐私不要传']
        self.write(self.primary, [['其他路径', '123', '其他地区', '其他规则']])
        self.write(self.fallback, [self.row], preamble=True)
        self.job = {'excelrule_path': str(self.primary), 'excelrule_fallback_path': str(self.fallback), 'pageTitle': self.page}

    def write(self, path, rows, preamble=False):
        book = Workbook()
        sheet = book.active
        sheet.title = 'EXcel文件'
        if preamble:
            sheet.append(['全局说明，仅作为表内数据'])
        sheet.append(self.headers)
        for row in rows:
            sheet.append(row)
        book.save(path)
        book.close()

    def read(self, **updates):
        return excel_rule_context({**self.job, **updates}, Path, self.root)

    def test_fallback_keeps_row_provenance_and_only_name_business_fields(self):
        result = self.read()
        self.assertEqual(result['rules'][0]['row'], 3)
        self.assertEqual(result['rules'][0]['routing']['数据源路径代码'], self.row[1])
        self.assertEqual(result['rules'][0]['fields'], {'指标名称示例': self.row[2], '指标名称取值逻辑': self.row[3]})
        self.assertNotIn('旧日期不要传', str(result))
        self.assertNotIn('旧季度样例.xls', str(result))
        self.assertNotIn('隐私不要传', str(result))

    def test_old_unique_rule_remains_preferred(self):
        self.write(self.primary, [self.row])
        self.fallback.unlink()
        self.assertEqual(self.read()['path'], str(self.primary))

    def test_path_separator_padding_does_not_prevent_matching(self):
        self.assertEqual(self.read(pageTitle=' 临沧市人民政府 \\ 政务公开 \\ 统计信息 ')['rules'][0]['row'], 3)

    def test_explicit_row_never_falls_back_to_another_workbook(self):
        with self.assertRaisesRegex(ValueError, '实际找到0行'):
            self.read(rule_row=50)

    def test_ambiguous_primary_never_uses_fallback(self):
        other = self.row.copy()
        other[3] = '另一种冲突名称逻辑'
        self.write(self.primary, [self.row, other])
        with self.assertRaisesRegex(ValueError, '实际找到2行'):
            self.read()

    def test_fallback_must_also_have_exactly_one_path(self):
        other = self.row.copy()
        other[3] = '另一种冲突名称逻辑'
        for rows, count in (([], 0), ([self.row, other], 2)):
            self.write(self.fallback, rows, preamble=True)
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, f'实际找到{count}行'):
                self.read()

    def test_complete_rule_and_empty_duplicate_choose_complete_without_md5(self):
        blank = self.row.copy()
        blank[2:4] = [None,None]
        self.write(self.fallback,[blank,self.row],preamble=True)
        item = self.read()['rules'][0]
        self.assertEqual(item['row'],4)
        self.assertEqual(item['duplicate_rows'],[3])
        self.assertEqual(item['fields']['指标名称取值逻辑'],self.row[3])

    def test_identical_primary_rows_collapse_and_remain_preferred(self):
        self.write(self.primary,[self.row,self.row])
        result = self.read()
        self.assertEqual(result['path'],str(self.primary))
        self.assertEqual(result['rules'][0]['duplicate_rows'],[3])

    def test_exact_source_md5_selects_template_and_is_not_business_input(self):
        self.headers.append('MD5')
        other = self.row.copy()
        other[3] = '其他文件的冲突规则'
        self.write(self.fallback,[other+['OTHER'],self.row+['CURRENT']],preamble=True)
        item = self.read(fileMd5='current')['rules'][0]
        self.assertEqual(item['row'],4)
        self.assertEqual(item['selection_reason'],'source_file_md5_match')
        self.assertNotIn('_source_md5',item)
        self.assertNotIn('CURRENT',str(item))

    def test_new_source_md5_still_collapses_empty_duplicate(self):
        self.headers.append('MD5')
        blank = self.row.copy()
        blank[2:4] = [None,None]
        self.write(self.fallback,[self.row+['OLD'],blank+[None]],preamble=True)
        self.assertEqual(self.read(fileMd5='NEW')['rules'][0]['row'],3)

    def test_same_md5_with_conflicting_rules_still_rejected(self):
        self.headers.append('MD5')
        other = self.row.copy()
        other[3] = '同一文件但规则冲突'
        self.write(self.fallback,[self.row+['SAME'],other+['SAME']],preamble=True)
        with self.assertRaisesRegex(ValueError,'实际找到2行'):
            self.read(fileMd5='SAME')

    def test_empty_row_with_different_path_code_cannot_be_silently_merged(self):
        blank = self.row.copy()
        blank[1:4] = ['other-code',None,None]
        self.write(self.fallback,[self.row,blank],preamble=True)
        with self.assertRaisesRegex(ValueError,'实际找到2行'):
            self.read()

    def test_default_fallback_is_connected_to_business_context(self):
        target = self.root / 'excel_match_agent' / '完整excelrule表' / 'EDB 代码映射迁移进展.xlsx'
        target.parent.mkdir(parents=True)
        target.write_bytes(self.fallback.read_bytes())
        job = dict(self.job)
        job.pop('excelrule_fallback_path')
        with patch('excel_match_agent.app.ROOT', self.root):
            result = business_context(job, Path)
        self.assertEqual(result[0]['path'], str(target))


if __name__ == '__main__':
    unittest.main()
