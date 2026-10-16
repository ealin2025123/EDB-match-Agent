import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.app import LocalPythonExecutor, run_job
from excel_match_agent.tests.test_runtime import CODE, FixedBackend


class LocalPythonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-中文-")
        self.folder = Path(self.tmp.name)
        self.parser = self.folder / "parser.py"
        self.params = {"workbook": {"sheets": [{"name": "中文表", "rows": [
            ["name", "value", "unit", "date"], ["GDP", "0", "亿元", "2026-06-30"]]}]}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_real_subprocess_preserves_unicode_and_zero(self):
        self.parser.write_text(CODE, encoding="utf-8")
        rows = LocalPythonExecutor().execute(self.parser, self.params, self.folder)
        self.assertEqual(rows[0]["indicator_unit"], "亿元")
        self.assertEqual(rows[0]["indicator_data"], "0")
        self.assertEqual(rows[0]["source"]["sheet"], "中文表")

    def test_child_environment_does_not_receive_credentials(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "placeholder", "OTHER_SECRET": "placeholder", "PYTHONPATH": "wrong"}), \
                patch("excel_match_agent.app.command", return_value='{"ExcelData": []}') as command:
            LocalPythonExecutor().execute(self.parser, {}, self.folder)
        args, kwargs = command.call_args.args, command.call_args.kwargs
        self.assertEqual(args[0][:4], [sys.executable, "-I", "-X", "utf8"])
        for key in ("DEEPSEEK_API_KEY", "OTHER_SECRET", "PYTHONPATH"):
            self.assertNotIn(key, kwargs["env"])

    def test_infinite_parser_is_terminated(self):
        self.parser.write_text("def main(params):\n    while True: pass\n", encoding="utf-8")
        with self.assertRaises(TimeoutError):
            LocalPythonExecutor(timeout=1).execute(self.parser, {}, self.folder)

    def test_forbidden_file_write_is_rejected_before_call(self):
        self.parser.write_text('def main(params):\n    open("unexpected.txt", "w").write("bad")\n    return []', encoding="utf-8")
        with self.assertRaises(RuntimeError):
            LocalPythonExecutor().execute(self.parser, {}, self.folder)
        self.assertFalse((self.folder / "unexpected.txt").exists())

    def test_parser_logs_do_not_corrupt_json_protocol(self):
        self.parser.write_text('def main(params):\n    print("解析进度")\n    return []', encoding="utf-8")
        self.assertEqual(LocalPythonExecutor().execute(self.parser, {}, self.folder), [])

    def test_parser_must_be_current_attempt(self):
        with self.assertRaises(ValueError):
            LocalPythonExecutor().execute(self.folder / "other.py", {}, self.folder)

    def test_default_job_retries_with_real_python_and_never_looks_for_docker(self):
        (self.folder / "source.csv").write_text("name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n", encoding="utf-8")
        (self.folder / "SKILL.md").write_text("按列抽取", encoding="utf-8")
        job = {"source_path": "source.csv", "skill_path": "SKILL.md", "output_dir": "runs",
               "GZKJG": [{"rule_indicator_name": "GDP", "rule_indicator_code": "123", "rule_constant_name": "万元"},
                         {"rule_indicator_name": "GDP2", "rule_indicator_code": "124", "rule_constant_name": "亿元"}]}
        backend = FixedBackend([CODE.replace('row[0]', '"wrong"'), CODE])
        with patch("excel_match_agent.app.locate_docker", side_effect=AssertionError("must not use Docker")):
            result = run_job(job, self.folder, backend=backend)
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["execution_backend"], "local_python")
        self.assertFalse(result["os_sandbox"])
        saved = json.loads((Path(result["output_dir"]) / "node-output.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["OUTDATA"][0]["indicator_unit"], "亿元")
        self.assertEqual(saved["OUTDATA"][0]["KB_unit"], "万元")


if __name__ == "__main__":
    unittest.main()
