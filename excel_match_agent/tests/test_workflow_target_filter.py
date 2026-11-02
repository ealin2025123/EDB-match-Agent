from pathlib import Path
import json
import runpy
import unittest


class TargetTableFilterTests(unittest.TestCase):
    def setUp(self):
        self.main = runpy.run_path(str(Path(__file__).resolve().parents[1]/'workflow_nodes/DC_地方经济前置过滤.py'))['main']

    def test_exact_table_flat_and_wrapped(self):
        for value in ('usrDFJJYXSJB', {'alias':'数据目标表','value':' usrDFJJYXSJB '}):
            self.assertTrue(self.main({'data_target_table':value})['continue_workflow'])

    def test_full_start_event_and_json_string(self):
        event={'sourceDataInfo':{'fields':{'data_target_table':{'value':'usrDFJJYXSJB'}}}}
        self.assertTrue(self.main(event)['continue_workflow'])
        self.assertTrue(self.main(json.dumps(event))['continue_workflow'])

    def test_other_table_missing_or_wrong_type_skip(self):
        for value in ('usrOTHER', '', None, 123, {'value':[]}):
            result=self.main({'data_target_table':value})
            self.assertFalse(result['continue_workflow'])
            self.assertTrue(result['skip_workflow'])
        self.assertFalse(self.main({'title':'地方经济','labels':['地方经济']})['continue_workflow'])

    def test_conflicts_skip(self):
        result=self.main({'data_target_table':'usrDFJJYXSJB',
                          'sourceDataInfo':{'fields':{'data_target_table':{'value':'other'}}}})
        self.assertFalse(result['continue_workflow'])
        self.assertIn('冲突',result['filter_reason'])
