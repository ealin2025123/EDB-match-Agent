import hashlib
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook
from excel_match_agent.app import workbook


class WorkbookContextTests(unittest.TestCase):
    def test_style_only_trailing_columns_are_omitted_without_changing_coordinates_or_source(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.xlsx"
            book = Workbook()
            sheet = book.active
            sheet["A1"] = "name"
            sheet["C5"] = 0
            sheet["C5"].number_format = "0.00"
            sheet["IV76"].number_format = "0.00"
            sheet.merge_cells("A2:D2")
            book.save(path)
            book.close()
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = workbook(path)["sheets"][0]
            self.assertEqual(result["original_column_count"], 256)
            self.assertEqual(result["effective_column_count"], 3)
            self.assertEqual(result["rows"][4][2], 0)
            self.assertEqual(len(result["rows"]), 76)
            self.assertIn("A2:D2", result["merged_ranges"])
            self.assertEqual(result["number_formats"]["C5"], "0.00")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

    def test_blank_gap_before_nonempty_column_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source.csv"
            path.write_text("name,,value,\nA,,0,\n", encoding="utf-8")
            result = workbook(path)["sheets"][0]
            self.assertEqual(result["rows"][1][1], "")
            self.assertEqual(result["rows"][1][2], "0")
