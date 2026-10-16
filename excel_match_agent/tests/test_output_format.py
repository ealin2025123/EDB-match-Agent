import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.output_format import FIELDS, export_output, workflow_output


class OutputFormatTests(unittest.TestCase):
    def test_example_shape_values_and_field_order(self):
        row = {field: "" for field in FIELDS}
        row.update(indicator_data=0, indicator_code=123, indicator_name="中文指标", indicator_unit="亿元")
        output = workflow_output({"OUTDATA": [row], "unitArray": {"indicator_unit": ["亿元"], "KB_unit": ["万元"]}})
        self.assertEqual(list(output), ["unitArray", "codeOutputResult", "OUTDATA"])
        self.assertEqual(output["codeOutputResult"]["OUTDATA"], output["OUTDATA"])
        self.assertEqual(output["codeOutputResult"]["unitArray"], output["unitArray"])
        self.assertEqual(list(output["OUTDATA"][0]), list(FIELDS))
        self.assertEqual(output["OUTDATA"][0]["indicator_data"], 0)
        self.assertIs(type(output["OUTDATA"][0]["indicator_data"]), int)
        self.assertEqual(output["OUTDATA"][0]["indicator_code"], "123")

    def test_mixed_value_types_match_workflow_wire_format(self):
        values = [1423939, 6.3, 22863, 4.7, 223925, 5, 1177151, 6.6, 51476, 18.3, "3", "6.30", "0"]
        expected = [1423939, "6.3", 22863, "4.7", 223925, 5, 1177151, "6.6", 51476, "18.3", "3", "6.30", "0"]
        rows = [{**{field: "" for field in FIELDS}, "indicator_data": value} for value in values]
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            export_output(folder, {"OUTDATA": rows, "unitArray": {"indicator_unit": ["万元", "%"], "KB_unit": ["万元", "%"]}})
            output = json.loads((folder / "node-output.json").read_text(encoding="utf-8"))
            actual = [row['indicator_data'] for row in output['OUTDATA']]
            self.assertEqual(actual, expected)
            self.assertEqual([type(v) for v in actual], [type(v) for v in expected])
            self.assertEqual(output['OUTDATA'], output['codeOutputResult']['OUTDATA'])

    def test_non_finite_and_boolean_values_are_rejected(self):
        for value in (True, float('nan'), float('inf'), float('-inf')):
            row = {**{field: "" for field in FIELDS}, "indicator_data": value}
            with self.assertRaises(ValueError):
                workflow_output({"OUTDATA": [row], "unitArray": {"indicator_unit": [], "KB_unit": []}})

    def test_text_exports_same_three_sections_without_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            export_output(folder, {"OUTDATA": [], "unitArray": {"indicator_unit": ["-"], "KB_unit": ["-"]}})
            text = (folder / "node-output.txt").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("输出:\nunitArray："))
            self.assertIn("\ncodeOutputResult：", text)
            self.assertIn("\nOUTDATA：", text)
            self.assertNotIn("run_id", text)
            self.assertEqual(set(json.loads((folder / "node-output.json").read_text(encoding="utf-8"))),
                             {"unitArray", "codeOutputResult", "OUTDATA"})
