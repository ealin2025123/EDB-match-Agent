from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from excel_match_agent.app import run_job
from excel_match_agent.supervisor import run_supervised, time_budget
from excel_match_agent.tests.test_runtime import FixedBackend, TestExecutor


class DeadlineTests(unittest.TestCase):
    def test_budget_is_optional_and_can_exceed_previous_cap(self):
        self.assertIsNone(time_budget({}))
        self.assertIsNone(time_budget({"total_timeout": None}))
        self.assertEqual(time_budget({"total_timeout": 600}), 600)
        for value in (0, -1, True, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                time_budget({"total_timeout": value})

    def test_review_cannot_export_success_after_budget_expires(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "source.csv").write_text("name,value,unit,date\nA,1,亿元,2026-06-30\nB,2,亿元,2026-06-30\n", encoding="utf-8")
            (folder / "SKILL.md").write_text("read all rows", encoding="utf-8")
            class SlowReview(FixedBackend):
                def review_names(self, context, folder):
                    time.sleep(0.15)
                    return super().review_names(context, folder)
            result = run_job({"source_path": "source.csv", "skill_path": "SKILL.md", "output_dir": "runs",
                              "total_timeout": 0.1, "GZKJG": [
                                  {"rule_indicator_name": "A", "rule_indicator_code": "1"},
                                  {"rule_indicator_name": "B", "rule_indicator_code": "2"}]},
                             folder, SlowReview(), TestExecutor())
            self.assertEqual(result["failure_kind"], "TOTAL_TIMEOUT")
            self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_supervisor_kills_stalled_worker_and_removes_racing_success(self):
        with tempfile.TemporaryDirectory() as temp:
            class Stalled:
                returncode = None
                def poll(self): return None
                def wait(self, timeout): self.returncode = -1
            worker = Stalled()
            def launch(*args, **kwargs):
                output = next(Path(temp).iterdir())
                (output / "node-output.json").write_text("{}")
                return worker
            began = time.monotonic()
            with patch("excel_match_agent.supervisor.subprocess.Popen", side_effect=launch), \
                 patch("excel_match_agent.app.terminate_tree") as terminate:
                result = run_supervised({"total_timeout": 0.1}, Path(temp), output_root=Path(temp))
            self.assertEqual(result["failure_kind"], "TOTAL_TIMEOUT")
            terminate.assert_called_once_with(worker, kill_timeout=1)
            self.assertLess(time.monotonic() - began, 2)
            self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_real_worker_startup_obeys_very_short_budget_without_api(self):
        with tempfile.TemporaryDirectory() as temp:
            began = time.monotonic()
            result = run_supervised({"total_timeout": 0.001, "GZKJG": []}, Path(temp), output_root=Path(temp))
            self.assertEqual(result["failure_kind"], "TOTAL_TIMEOUT")
            self.assertLess(time.monotonic() - began, 4)
            self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_unlimited_supervisor_still_returns_preflight_error_without_api(self):
        with tempfile.TemporaryDirectory() as temp:
            result = run_supervised({"total_timeout": None, "GZKJG": []}, Path(temp), output_root=Path(temp))
            self.assertEqual(result["failure_kind"], "GZKJG_EMPTY")
            self.assertIsNone(result["total_timeout_seconds"])
            self.assertFalse(result["overall_deadline_enabled"])
