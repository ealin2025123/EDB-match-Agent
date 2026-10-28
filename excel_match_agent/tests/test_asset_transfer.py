import base64
import copy
import hashlib
import json
from pathlib import Path
import runpy
import unittest


class AssetTransferTests(unittest.TestCase):
    def setUp(self):
        nodes = Path(__file__).resolve().parents[1]/'workflow_nodes'
        self.record = runpy.run_path(str(nodes/'DC_Agent接入.py'))['_record']
        self.output = runpy.run_path(str(nodes/'DC_匹配结果输出.py'))['main']
        self.code = '# 中文\r\ndef main(params):\r\n    return {}\r\n'
        rows = [{'indicator_code':str(n)} for n in (1,2)]
        units = {'indicator_unit':[],'KB_unit':[],'convert_ratio':[]}
        self.reply = {'status':'ready','job_id':'job-for-test','parse_strategy':'html',
            'asset':{'type':'python','content':self.code,'filename':'parser.py',
                     'sha256':hashlib.sha256(self.code.encode()).hexdigest()},
            'result':{'OUTDATA':rows,'unitArray':units,
                      'codeOutputResult':{'OUTDATA':rows,'unitArray':units}}}

    def test_missing_mapped_code_recovers_exact_bytes(self):
        item = self.record(self.reply)
        item['PythonCode'] = None
        result = self.output({'agent':[item]})
        self.assertEqual(result['python_code'],self.code)
        self.assertNotIn('_asset_transfer',result)
        self.assertNotIn('_asset_transfer',self.reply['result'])

    def test_corrupt_or_wrong_task_backup_fails(self):
        for change in ('body','job_id','parse_strategy','sha256'):
            item = self.record(self.reply)
            item['PythonCode'] = None
            payload = json.loads(item['result_json'])
            transfer = payload['_asset_transfer']
            if change == 'body':
                transfer['content_b64'] = base64.b64encode(b'tampered').decode()
            else:
                transfer[change] = 'wrong'
            item['result_json'] = payload
            with self.subTest(change=change),self.assertRaises(ValueError):
                self.output({'agent':[item]})

    def test_nonempty_tampered_primary_is_not_silently_replaced(self):
        item = self.record(self.reply)
        item['PythonCode'] += 'tampered'
        with self.assertRaises(ValueError):
            self.output({'agent':[item]})

    def test_legacy_primary_code_still_works(self):
        item = self.record(self.reply)
        payload = json.loads(item['result_json'])
        payload.pop('_asset_transfer')
        item['result_json'] = json.dumps(payload)
        self.assertEqual(self.output({'agent':[item]})['python_code'],self.code)


if __name__ == '__main__':
    unittest.main()
