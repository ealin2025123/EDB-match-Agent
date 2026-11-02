import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.existing_parser import locate, run_if_present
from excel_match_agent.service import approved_delivery, transport_reply
from excel_match_agent.tests.test_service import RULES


ROWS = [{'indicator_name':r['rule_indicator_name'],'indicator_data':'0',
         'indicator_unit':'万元','end_date':'2026-06-30'} for r in RULES]


class ExistingParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = self.root/'library'
        self.job = {'parse_strategy':'excel','pageTitle':'合成来源/统计',
                    'fileMd5':'synthetic','existing_parser_root':str(self.library),
                    'GZKJG':RULES,'output_dir':'runs'}

    def asset(self, strategy='excel', content='def main(params):\n    return {}\n', version='v1'):
        p=self.library/strategy/version/('合成来源_统计'+('.md' if strategy=='prompt' else '.py'))
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content,encoding='utf-8')
        return p

    def test_separators_exact_branch_duplicates_and_conflict(self):
        p=self.asset()
        self.assertEqual(locate(self.library,'合成来源\\统计','excel'),p)
        self.assertEqual(locate(self.library,'合成来源/统计','html'),p)
        self.asset(version='v2')
        self.assertEqual(locate(self.library,'合成来源/统计','excel'),p)
        self.asset(content='different',version='v3')
        with self.assertRaises(ValueError):locate(self.library,'合成来源/统计','excel')

    def test_hit_executes_once_without_backend_or_rule_lookup(self):
        self.asset()
        calls=[]
        def runner(*args):calls.append(args);return {'code':0,'ExcelData':ROWS}
        result=run_if_present(self.job,self.root,runner=runner)
        self.assertEqual(len(calls),1)
        self.assertEqual(result['generation_calls'],0)
        self.assertEqual(result['rebuilds'],0)
        delivery=approved_delivery(result)
        self.assertEqual(delivery['status'],'ready')
        self.assertEqual(delivery['approval_basis'],'user_confirmed_library')
        self.assertEqual(delivery['result']['excel_data'],ROWS)
        self.assertIn('_asset_transfer',transport_reply({**delivery,'job_id':'demo'})['result'])

    def test_failure_stops_without_generation_and_miss_falls_back(self):
        self.assertIsNone(run_if_present(self.job,self.root))
        self.asset()
        with patch('excel_match_agent.platform_export.run_platform',side_effect=ValueError('synthetic failure')) as runner:
            result=run_if_present(self.job,self.root)
        self.assertEqual(runner.call_count,1)
        self.assertEqual(approved_delivery(result)['status'],'failed')
        self.assertEqual(result['generation_calls'],0)
        self.assertEqual(result['rebuilds'],0)

    def test_empty_missing_date_and_low_matches_rejected_without_rebuild(self):
        self.asset()
        for rows in ([],[{**ROWS[0],'end_date':'empty'}],[ROWS[0]]):
            with self.subTest(rows=rows):
                result=run_if_present(self.job,self.root,runner=lambda *a:{'ExcelData':rows})
                self.assertNotEqual(result['status'],'candidate_pass')
                self.assertEqual(result['rebuilds'],0)

    def test_html_pipeline_and_tampered_asset(self):
        self.asset('html')
        result=run_if_present({**self.job,'parse_strategy':'html'},self.root,
                              runner=lambda *a:{'ExcelData':ROWS})
        self.assertEqual(approved_delivery(result)['status'],'ready')
        Path(result['verified_asset']).write_text('tampered',encoding='utf-8')
        with self.assertRaises(ValueError):approved_delivery(result)

    def test_prompt_uses_existing_prompt_once_never_generates(self):
        self.asset('prompt','existing trusted prompt')
        (self.root/'source.md').write_text('synthetic source',encoding='utf-8')
        class Backend:
            calls=0
            def _complete(self,system,context,folder,schema,validator):
                self.calls+=1
                assert system=='existing trusted prompt'
                return validator(json.dumps({'out_data':[{**r,'indicator_code':str(rule['rule_indicator_code'])}
                    for r,rule in zip(ROWS,RULES)]}))
        backend=Backend()
        result=run_if_present({**self.job,'parse_strategy':'prompt','markdownMD5':'demo',
                               'markdown_path':'source.md'},self.root,backend=backend)
        self.assertEqual(backend.calls,1)
        self.assertEqual(result['generation_calls'],0)
        delivery=approved_delivery(result)
        self.assertEqual(delivery['status'],'ready')
        self.assertEqual(set(delivery['result']),{'OUTDATA'})

    def test_app_dispatch_bypasses_generation(self):
        from excel_match_agent.app import run_job
        self.asset()
        with patch('excel_match_agent.platform_export.run_platform',return_value={'ExcelData':ROWS}), \
             patch('excel_match_agent.supervisor.run_supervised',side_effect=AssertionError('generation invoked')):
            result=run_job(self.job,self.root)
        self.assertEqual(result['execution_mode'],'trusted_library')

    def test_real_subprocess_executes_full_parser_without_generation(self):
        self.asset(content='def main(params):\n    assert params["fileMd5"] == "synthetic"\n    return '+repr({'code':0,'ExcelData':ROWS})+'\n')
        result=run_if_present(self.job,self.root)
        self.assertEqual(result['status'],'candidate_pass')
        self.assertEqual(approved_delivery(result)['status'],'ready')
        self.assertEqual(result['generation_calls'],0)

    def test_html_input_reuses_excel_folder_parser_without_generation(self):
        self.asset()
        with patch('excel_match_agent.platform_export.run_platform',return_value={'ExcelData':ROWS}) as runner:
            result=run_if_present({**self.job,'parse_strategy':'html'},self.root)
        self.assertEqual(runner.call_count,1)
        self.assertEqual(result['generation_calls'],0)
        self.assertEqual(approved_delivery(result)['status'],'ready')
