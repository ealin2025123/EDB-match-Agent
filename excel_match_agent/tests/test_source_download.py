from email.message import Message
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from openpyxl import Workbook
from excel_match_agent.app import run_job, workbook
from excel_match_agent.source_download import download_source, download_url, SourceDownloadError
from excel_match_agent.tests.test_runtime import FixedBackend, TestExecutor


CSV = b'name,value,unit,date\nGDP,0,unit,2026-06-30\nGDP2,1,unit,2026-06-30\n'


class Response(io.BytesIO):
    def __init__(self, data, **headers):
        super().__init__(data)
        self.status = 200
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k.replace('_', '-')] = str(v)


class SourceDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_xlsx_bytes_fix_raw_utf8_header_name_and_preserve_values(self):
        book = Workbook()
        book.active['B2'] = 0
        memory = io.BytesIO()
        book.save(memory)
        book.close()
        raw = memory.getvalue()
        requests = []
        def transport(request, timeout):
            requests.append(request)
            return Response(raw, Content_Disposition='attachment; filename="' + '上街2026年6月.xls'.encode('utf-8').decode('latin-1') + '"')
        path, info = download_source('ABC123', self.folder, transport=transport)
        self.assertEqual(path.name, '上街2026年6月.xlsx')
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(workbook(path)['sheets'][0]['rows'][1][1], 0)
        self.assertEqual(info['content_md5'], hashlib.md5(raw).hexdigest())
        self.assertIsNone(requests[0].get_header('Authorization'))
        self.assertEqual(requests[0].full_url, 'https://files.example.invalid/api/file/download/ABC123')

    def test_rule_filename_used_when_server_only_returns_md5(self):
        identifier = hashlib.md5(CSV).hexdigest()
        path, info = download_source(identifier, self.folder, filename_hint='统计2026年6月.csv', transport=lambda *a, **kw: Response(
            CSV, Content_Disposition=f'attachment; filename="{identifier}"', Content_Length=len(CSV)))
        self.assertEqual(path.name, '统计2026年6月.csv')
        self.assertTrue(info['md5_matches_identifier'])

    def test_rfc5987_name_and_csv_type_without_extension(self):
        path, info = download_source('id', self.folder, transport=lambda *a, **kw: Response(
            CSV, Content_Disposition="attachment; filename*=UTF-8''%E6%8A%A5%E8%A1%A82026%E5%B9%B46%E6%9C%88", Content_Type='text/csv'))
        self.assertEqual(path.name, '报表2026年6月.csv')

    def test_header_path_cannot_escape_task_source_directory(self):
        path, _ = download_source('id', self.folder / 'source', transport=lambda *a, **kw: Response(
            CSV, Content_Disposition='attachment; filename="../../outside.csv"'))
        self.assertEqual(path.parent, self.folder / 'source')
        self.assertFalse((self.folder / 'outside.csv').exists())

    def test_oversized_and_truncated_downloads_remove_partial_file(self):
        for headers, limit in [({}, 5), ({'Content_Length': len(CSV) + 1}, 5000)]:
            with self.subTest(headers=headers):
                with self.assertRaises(SourceDownloadError):
                    download_source('id', self.folder, filename_hint='source.csv', max_bytes=limit,
                                    transport=lambda *a, **kw: Response(CSV, **headers))
                self.assertEqual(list(self.folder.iterdir()), [])

    def test_error_page_empty_file_and_http_error_are_not_model_input(self):
        for data, headers in [(b'', {}), (b'{"error":"not found"}', {'Content_Type': 'application/json'}),
                              (b'<html>login</html>', {'Content_Type': 'text/html'})]:
            with self.assertRaises(SourceDownloadError):
                download_source('id', self.folder, filename_hint='source.csv', transport=lambda *a, **kw: Response(data, **headers))
        def missing(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, 'not found', {}, io.BytesIO(b'private body'))
        with self.assertRaisesRegex(SourceDownloadError, 'HTTP 404') as caught:
            download_source('id', self.folder, transport=missing)
        self.assertNotIn('private body', str(caught.exception))

    def test_invalid_url_and_identifier_rejected_before_request(self):
        for base, identifier in [('http://x/download', '../x'), ('http://x/download?key=x', 'id'),
                                 ('http://user:password@x/download', 'id'), ('file:///tmp', 'id')]:
            with self.assertRaises(SourceDownloadError):
                download_url(base, identifier)

    def test_pipeline_downloads_then_executes_and_matches_without_source_path(self):
        (self.folder / 'SKILL.md').write_text('按列读取', encoding='utf-8')
        job = {'filemd5': 'id', 'pagetitle': 'test path', 'skill_path': 'SKILL.md', 'output_dir': 'runs',
               'GZKJG': [{'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'},
                         {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'}]}
        def fixture(filemd5, directory, **kwargs):
            return download_source(filemd5, directory, **kwargs, transport=lambda *a, **kw: Response(
                CSV, Content_Disposition='attachment; filename="source.csv"'))
        backend = FixedBackend()
        with patch('excel_match_agent.app.download_source', side_effect=fixture):
            result = run_job(job, self.folder, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(result['source_acquisition']['origin'], 'download')
        self.assertEqual(backend.contexts[0]['source_name'], 'source.csv')
        self.assertEqual(result['OUTDATA'][0]['indicator_data'], '0')
        self.assertTrue((Path(result['output_dir']) / 'source-acquisition.json').exists())
        self.assertIn('download', [s['stage'] for s in result['stage_timings']])

    def test_download_failure_or_empty_gzk_never_calls_model(self):
        backend = FixedBackend()
        job = {'filemd5': 'missing', 'skill_path': 'unused.md', 'output_dir': 'runs',
               'GZKJG': [{'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'}]}
        with patch('excel_match_agent.app.download_source', side_effect=SourceDownloadError('timeout')) as download:
            result = run_job(job, self.folder, backend, TestExecutor())
            self.assertEqual(result['failure_kind'], 'SOURCE_DOWNLOAD_FAILED')
            self.assertEqual(backend.calls, 0)
            job['GZKJG'] = []
            result = run_job(job, self.folder, backend, TestExecutor())
            self.assertEqual(result['failure_kind'], 'GZKJG_EMPTY')
            self.assertEqual(download.call_count, 1)

    def test_pagetitle_selects_rule_but_actual_filename_comes_from_download(self):
        rules = Workbook()
        sheet = rules.active
        sheet.append(['路径', '文件名称'])
        sheet.append(['本次路径', '统计2026年6月.csv'])
        rules.save(self.folder / 'excelrule.xlsx')
        rules.close()
        (self.folder / 'SKILL.md').write_text('按列读取', encoding='utf-8')
        job = {'filemd5': 'id', 'pagetitle': '本次路径', 'excelrule_path': 'excelrule.xlsx',
               'skill_path': 'SKILL.md', 'output_dir': 'runs',
               'GZKJG': [{'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'},
                         {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'}]}
        def fixture(filemd5, directory, **kwargs):
            self.assertIsNone(kwargs['filename_hint'])
            return download_source(filemd5, directory, **kwargs, transport=lambda *a, **kw: Response(
                CSV, Content_Disposition='attachment; filename="actual-2026-06.csv"'))
        backend = FixedBackend()
        with patch('excel_match_agent.app.download_source', side_effect=fixture):
            result = run_job(job, self.folder, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(backend.contexts[0]['source_name'], 'actual-2026-06.csv')
        self.assertEqual(backend.contexts[0]['business_rules'][0]['rules'][0]['row'], 2)

    def test_existing_local_file_keeps_local_entry_and_does_not_download(self):
        (self.folder / 'source.csv').write_bytes(CSV)
        (self.folder / 'SKILL.md').write_text('按列读取', encoding='utf-8')
        job = {'source_path': 'source.csv', 'filemd5': 'id', 'skill_path': 'SKILL.md', 'output_dir': 'runs',
               'GZKJG': [{'rule_indicator_name': 'GDP', 'rule_indicator_code': '1'},
                         {'rule_indicator_name': 'GDP2', 'rule_indicator_code': '2'}]}
        with patch('excel_match_agent.app.download_source', side_effect=AssertionError('local source must not download')):
            result = run_job(job, self.folder, FixedBackend(), TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(result['source_acquisition']['origin'], 'local')
