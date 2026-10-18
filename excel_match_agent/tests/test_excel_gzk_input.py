import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.excel_gzk_input import load_excel_gzk_file
from excel_match_agent.app import normalize_rules


class ExcelGzkInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / '规则库.txt'
        self.rows = [{'rule_indicator_name': '地区生产总值', 'rule_indicator_code': 123,
                      'rule_constant_name': '万元', 'indicator_release_frequency': '季'}]

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, text):
        self.path.write_text(text, encoding='utf-8-sig')
        return load_excel_gzk_file(self.path)

    def test_original_json_formats_remain_supported(self):
        for value in (self.rows, {'GZKJG': self.rows}, json.dumps(self.rows)):
            with self.subTest(value=value):
                self.assertEqual(normalize_rules(self.read(json.dumps(value))), normalize_rules(self.rows))

    def test_platform_duplicate_display_is_read_once(self):
        rows = json.dumps(self.rows, ensure_ascii=False)
        value = self.read('输出:\ncodeOutputResult：{"GZKJG":' + rows + '}\nGZKJG：' + rows)
        self.assertEqual(value, self.rows)
        self.assertEqual(len(value), 1)

    def test_direct_field_ascii_colon_and_whitespace(self):
        self.assertEqual(self.read('输出:\n  GZKJG: \n' + json.dumps(self.rows)), self.rows)

    def test_empty_rules_are_passed_to_existing_empty_gzk_check(self):
        self.assertEqual(self.read('输出:\nGZKJG：[]'), [])

    def test_conflicting_duplicate_values_rejected(self):
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.read('codeOutputResult：{"GZKJG":[]}\nGZKJG：' + json.dumps(self.rows))

    def test_malformed_first_field_not_hidden_by_second(self):
        with self.assertRaises(ValueError):
            self.read('codeOutputResult：{bad}\nGZKJG：[]')

    def test_script_is_never_executed(self):
        target = Path(self.tmp.name) / 'must-not-exist'
        text = 'scriptCode：open(' + repr(str(target)) + ',"w").write("bad")\nGZKJG：[]'
        self.assertEqual(self.read(text), [])
        self.assertFalse(target.exists())

    def test_wrong_export_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, '未找到'):
            self.read('输入:\ninputParams：{"GZK":{}}')


if __name__ == '__main__':
    unittest.main()
