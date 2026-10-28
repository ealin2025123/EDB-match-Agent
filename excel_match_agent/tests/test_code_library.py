"""按来源路径命名的代码库验证；仅使用离线合成代码。"""
import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.app import run_job
from excel_match_agent.code_cache import library_path, library_root, lookup, store_success
from excel_match_agent.tests.test_runtime import FixedBackend, TestExecutor, CODE


class CodeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'SKILL.md').write_text('按本次源表',encoding='utf-8')
        (self.root/'source.csv').write_text('name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n',encoding='utf-8')
        self.cache = self.root/'service-runtime/code-cache'
        self.job = {'source_path':'source.csv','skill_path':'SKILL.md','pageTitle':'来源\\公开/统计',
                    'GZKJG':[{'rule_indicator_name':'GDP','rule_indicator_code':'1'},
                             {'rule_indicator_name':'GDP2','rule_indicator_code':'2'}],
                    'output_dir':'runs','code_cache_dir':str(self.cache)}

    def success(self, code=CODE):
        result = run_job(self.job,self.root,FixedBackend([code]),TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        context = json.loads((Path(result['output_dir'])/'input-context.json').read_text(encoding='utf-8'))
        return result,context

    def test_file_layout_and_skill_path_names(self):
        result,context = self.success()
        self.assertTrue(store_success(self.cache,result))
        self.assertEqual(library_root(self.cache),self.root/'code')
        self.assertTrue(all((self.root/'code'/kind).is_dir() for kind in ('html','excel','prompt')))
        for kind,extension in (('excel','.py'),('html','.py'),('prompt','.md')):
            path = library_path(self.cache,context,kind)
            self.assertEqual(path,self.root/'code'/kind/('来源_公开_统计'+extension))
        self.assertEqual(library_path(self.cache,context,'excel').read_text(encoding='utf-8'),CODE)

    def test_visible_python_is_complete_platform_asset_core_stays_internal(self):
        result,context = self.success()
        full = Path(result['output_dir'])/'platform-parser.py'
        raw = (CODE+'\n# synthetic complete-platform adapter\n').encode('utf-8')
        full.write_bytes(raw)
        result['platform_parser_path'] = str(full)
        self.assertTrue(store_success(self.cache,result))
        self.assertEqual(library_path(self.cache,context,'excel').read_bytes(),raw)
        cached = lookup(self.cache,context,'excel')
        self.assertEqual(cached['response']['python_code'],CODE)

    def test_missing_library_file_causes_generation_not_hidden_cache_execution(self):
        first,context = self.success()
        store_success(self.cache,first)
        library_path(self.cache,context,'excel').unlink()
        backend = FixedBackend()
        result = run_job(self.job,self.root,backend,TestExecutor())
        self.assertEqual(result['status'],'candidate_pass')
        self.assertEqual(backend.calls,1)
        self.assertFalse(result['cache_hit'])

    def test_hand_edits_are_not_executed_as_approved_cache_or_overwritten(self):
        result,context = self.success()
        store_success(self.cache,result)
        path = library_path(self.cache,context,'excel')
        edited = CODE+'\n# local user change\n'
        path.write_text(edited,encoding='utf-8')
        self.assertIsNone(lookup(self.cache,context,'excel'))
        with self.assertRaisesRegex(ValueError,'手工改动'):
            store_success(self.cache,result)
        self.assertEqual(path.read_text(encoding='utf-8'),edited)

    def test_different_paths_with_same_filename_are_not_merged(self):
        self.job['pageTitle'] = 'A\\B'
        first,context = self.success()
        store_success(self.cache,first)
        original = library_path(self.cache,context,'excel').read_bytes()
        self.job['pageTitle'] = 'A_B'
        second,other = self.success()
        self.assertEqual(library_path(self.cache,context,'excel'),library_path(self.cache,other,'excel'))
        with self.assertRaisesRegex(ValueError,'不同路径'):
            store_success(self.cache,second)
        self.assertEqual(library_path(self.cache,context,'excel').read_bytes(),original)

    def test_new_success_updates_path_file_and_old_task_does_not_roll_it_back(self):
        first,context = self.success()
        store_success(self.cache,first)
        # 改变Skill使新代码走生成流程，同时仍属于同一路径代码文件。
        (self.root/'SKILL.md').write_text('新版Skill规则',encoding='utf-8')
        second,_ = self.success(CODE+'\n# newly generated valid version\n')
        store_success(self.cache,second)
        path = library_path(self.cache,context,'excel')
        newest = path.read_bytes()
        store_success(self.cache,first)
        self.assertEqual(path.read_bytes(),newest)

    def test_invalid_windows_characters_cannot_escape_branch_directory(self):
        path = library_path(self.cache,{'pageTitle':'来源/统计:数据?'},'excel')
        self.assertEqual(path.name,'来源_统计_数据_.py')
        self.assertEqual(path.parent,self.root/'code/excel')

    def test_delivery_filename_matches_skill_path_naming(self):
        from excel_match_agent.service import approved_delivery
        from excel_match_agent.tests.test_service import passed_result
        result = passed_result(self.root)
        Path(result['output_dir'],'input-context.json').write_text(json.dumps({'pageTitle':self.job['pageTitle']}),encoding='utf-8')
        delivery = approved_delivery(result)
        self.assertEqual(delivery['asset']['filename'],'来源_公开_统计.py')


if __name__ == '__main__':
    unittest.main()
