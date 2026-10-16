import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from excel_match_agent.app import DockerExecutor, ExecutionUnavailable, run_job
from excel_match_agent.tests.test_runtime import FixedBackend


class DockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.executor = DockerExecutor("docker")

    def tearDown(self):
        self.tmp.cleanup()

    def test_isolation_flags_and_no_key_forwarding(self):
        args = self.executor.build_command(self.folder, "test-container")
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertEqual(args[args.index("--memory") + 1], "256m")
        self.assertEqual(args[args.index("--pull=never")], "--pull=never")
        self.assertEqual(args.count("--mount"), 2)
        self.assertNotIn("--env", args)
        self.assertNotIn("DEEPSEEK_API_KEY", " ".join(args))

    def test_success_cleans_its_container(self):
        self.executor.prepared = True
        with patch("excel_match_agent.app.command", return_value='{"ExcelData": []}'), patch("excel_match_agent.app.subprocess.run") as cleanup:
            result = self.executor.execute(self.folder / "parser.py", {}, self.folder)
        self.assertEqual(result, [])
        args = cleanup.call_args.args[0]
        self.assertEqual(args[:3], ["docker", "rm", "-f"])
        self.assertTrue(args[3].startswith("excel-parser-"))

    def test_timeout_still_cleans_its_container(self):
        self.executor.prepared = True
        with patch("excel_match_agent.app.command", side_effect=TimeoutError("test timeout")), patch("excel_match_agent.app.subprocess.run") as cleanup:
            with self.assertRaises(TimeoutError):
                self.executor.execute(self.folder / "parser.py", {}, self.folder)
        cleanup.assert_called_once()

    def test_missing_runtime_is_explicit(self):
        with patch("excel_match_agent.app.command", side_effect=RuntimeError("daemon unavailable")):
            with self.assertRaises(ExecutionUnavailable):
                self.executor.prepare(self.folder)

    def test_parser_must_belong_to_current_attempt(self):
        with self.assertRaises(ValueError):
            self.executor.execute(self.folder / "other.py", {}, self.folder)

    def test_missing_runtime_stops_before_model_call(self):
        (self.folder / "source.csv").write_text("name,value,unit,date\nGDP,0,亿元,2026-06-30\n", encoding="utf-8")
        (self.folder / "SKILL.md").write_text("按列抽取", encoding="utf-8")
        job = {"source_path": "source.csv", "skill_path": "SKILL.md", "output_dir": "runs",
               "GZKJG": [{"rule_indicator_name": "GDP", "rule_indicator_code": "123", "rule_constant_name": "亿元"}]}
        backend = FixedBackend()
        with patch.object(self.executor, "prepare", side_effect=ExecutionUnavailable("test missing Docker")):
            result = run_job(job, self.folder, backend, self.executor)
        self.assertEqual(result["failure_kind"], "EXECUTION_BACKEND_UNAVAILABLE")
        self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()
