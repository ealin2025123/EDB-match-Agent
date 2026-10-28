import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.app import run_job
from excel_match_agent.platform_export import export_platform_parser, PlatformExportError, load_adapter
from excel_match_agent.tests.test_runtime import CODE, FixedBackend, TestExecutor


class PlatformExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.rows = [{'indicator_name': n, 'indicator_data': i, 'indicator_unit': '万元', 'end_date': '2026-06-30',
                      'source': {'sheet': 's', 'cell': 'B' + str(i + 2)}} for i, n in enumerate(['GDP', 'GDP2'])]
        self.rules = [{'rule_indicator_name': r['indicator_name'], 'rule_indicator_code': str(i + 1)}
                      for i, r in enumerate(self.rows)]
        (self.folder / 'input-context.json').write_text(json.dumps({'GZKJG': self.rules}), encoding='utf-8')
        self.code = self.folder / 'parser.py'
        self.code.write_text(CODE.replace('return [', 'return {"code":0,"ExcelData":[').replace('2)]', '2)]}'), encoding='utf-8')
        self.params = {'workbook': {'sheets': [{'name': 's', 'rows': [['name', 'value', 'unit', 'date']]
            + [[r['indicator_name'], r['indicator_data'], r['indicator_unit'], r['end_date']] for r in self.rows]}]},
            'fileMd5': 'abc', 'source_name': 'actual.csv'}

    def tearDown(self):
        self.temp.cleanup()

    def test_full_file_replays_same_core_in_clean_subprocess(self):
        result = export_platform_parser(self.code, self.rows, self.params, self.folder, {'platform_verify_download': False})
        full = Path(result['platform_parser_path'])
        self.assertTrue(full.read_text(encoding='utf-8').startswith(self.code.read_text(encoding='utf-8')))
        self.assertEqual(full.read_bytes(), Path(result['platform_parser_text_path']).read_bytes())
        report = json.loads(Path(result['platform_validation_path']).read_text(encoding='utf-8'))
        self.assertEqual(report['parsed_count'], 2)
        self.assertFalse(report['real_download_tested'])

    def test_changed_download_records_cannot_be_delivered_as_success(self):
        calls = []
        def runner(path, params, folder, label, timeout):
            calls.append((label, params))
            rows = json.loads(json.dumps(self.rows))
            if label == 'download':
                rows[0]['indicator_unit'] = '亿元'
            return {'code': 0, 'ExcelData': rows}
        with self.assertRaisesRegex(PlatformExportError, '真实下载解析'):
            export_platform_parser(self.code, self.rows, self.params, self.folder,
                {'upstream_metadata': {'workbook': {}, 'source_name': 'wrong'}}, runner=runner)
        self.assertNotIn('workbook', calls[1][1])
        self.assertNotIn('source_name', calls[1][1])
        self.assertFalse((self.folder / 'platform/platform-validation.json').exists())

    def test_adapter_tampering_is_rejected(self):
        asset = self.folder / 'assets'
        asset.mkdir()
        (asset / 'platform-adapter.py.txt').write_text('tampered', encoding='utf-8')
        (asset / 'platform-adapter.provenance.json').write_text('{"provider":"deepseek","sha256":"wrong"}', encoding='utf-8')
        with patch('excel_match_agent.platform_export.ASSETS', asset), self.assertRaises(PlatformExportError):
            load_adapter()

    def test_platform_failure_withholds_node_output_without_another_generation(self):
        (self.folder / 'source.csv').write_text('name,value,unit,date\nGDP,0,万元,2026-06-30\nGDP2,1,万元,2026-06-30\n', encoding='utf-8')
        (self.folder / 'SKILL.md').write_text('offline fixture', encoding='utf-8')
        job = {'source_path': 'source.csv', 'skill_path': 'SKILL.md', 'output_dir': 'runs',
               'GZKJG': self.rules, 'export_platform_parser': True}
        backend = FixedBackend()
        with patch('excel_match_agent.app.export_platform_parser', side_effect=PlatformExportError('test download failure')):
            result = run_job(job, self.folder, backend, TestExecutor())
        self.assertEqual(result['failure_kind'], 'PLATFORM_EXPORT_FAILED')
        self.assertEqual((backend.calls, result['attempts'], result['rebuilds']), (1, 1, 0))
        self.assertFalse((Path(result['output_dir']) / 'node-output.json').exists())
