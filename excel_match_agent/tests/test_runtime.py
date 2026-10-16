import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.app import run_job, audit_sources, normalize_job
from excel_match_agent.worker import check_code, invoke

CODE = '''def main(params):
    sheet = params["workbook"]["sheets"][0]
    return [{"indicator_name": row[0], "indicator_data": row[1], "indicator_unit": row[2],
             "end_date": row[3], "source": {"sheet": sheet["name"], "cell": "B" + str(i)}}
            for i, row in enumerate(sheet["rows"][1:], 2)]
'''


class TestExecutor:
    def execute(self, path, params, folder):
        return invoke(path.read_text(encoding="utf-8"), params)


class FixedBackend:
    def __init__(self, codes=None):
        self.calls = 0
        self.codes = codes or [CODE]
        self.contexts = []
        self.review_calls = 0

    def generate(self, context, folder):
        self.contexts.append(json.loads(json.dumps(context)))
        self.calls += 1
        return {"decision": "generate", "python_code": self.codes[min(self.calls - 1, len(self.codes) - 1)],
                "explanation": "offline test fixture"}

    def review_names(self, context, folder):
        self.review_calls += 1
        issues = [{"indicator_name": r["indicator_name"], "source": r["source"],
                   "reason": "测试夹具：源名称GDP被错误替换为wrong"}
                  for r in context["parsed_data"] if r["indicator_name"] == "wrong"]
        return {"name_status": "incorrect" if issues else "correct", "issues": issues,
                "candidate_reviews": [{"indicator_name": c["indicator_name"], "source": c["source"],
                                       "decision": "different_indicator", "target_indicator_name": "",
                                       "compatibility": {"rule_indicator_name": c["candidates"][0]["rule_indicator_name"],
                                                         "unit": "unknown", "frequency": "unknown",
                                                         "reason": "离线夹具：无单位/频率矛盾结论，名称本身不同"},
                                       "reason": "离线夹具：Other无对应GZK；wrong由独立源名称issue修复"}
                                      for c in context.get("near_name_candidates", [])],
                "explanation": "测试夹具：依据源表审核名称，忽略匹配比例和单位"}


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        (self.folder / "source.csv").write_text("name,value,unit,date\nGDP,0,亿元,2026-06-30\nGDP2,1,亿元,2026-06-30\n", encoding="utf-8")
        (self.folder / "SKILL.md").write_text("从CSV字段读取指标，不改变源值。", encoding="utf-8")
        self.job = {"source_path": "source.csv", "skill_path": "SKILL.md", "output_dir": "runs",
                    "GZKJG": [{"rule_indicator_name": "GDP", "rule_indicator_code": 123, "rule_constant_name": "亿元"},
                              {"rule_indicator_name": "GDP2", "rule_indicator_code": 124, "rule_constant_name": "亿元"}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_orchestration_writes_code_and_node_values(self):
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        output = Path(result["output_dir"])
        self.assertTrue((output / "attempt-1" / "parser.py").exists())
        self.assertEqual(json.loads((output / "node-output.json").read_text(encoding="utf-8"))["OUTDATA"][0]["indicator_data"], "0")
        self.assertEqual(backend.calls, 1)

    def test_upstream_lowercase_fields_reach_model_and_parser(self):
        self.job.update(pagetitle="本次业务路径", filemd5="upstream-file-id")
        backend = FixedBackend()
        class CapturingExecutor(TestExecutor):
            def execute(self, path, params, folder):
                self.params = params
                return super().execute(path, params, folder)
        executor = CapturingExecutor()
        result = run_job(self.job, self.folder, backend, executor)
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.contexts[0]["pageTitle"], "本次业务路径")
        self.assertEqual(backend.contexts[0]["fileMd5"], "upstream-file-id")
        self.assertEqual(executor.params["fileMd5"], "upstream-file-id")
        self.assertEqual(result["source_identity"]["supplied_filemd5"], "upstream-file-id")

    def test_conflicting_aliases_are_rejected(self):
        with self.assertRaises(ValueError):
            normalize_job({"pagetitle": "A", "pageTitle": "B"})

    def test_start_node_metadata_preserves_publication_and_source_dates_separately(self):
        timestamp = '2026-08-14 00:00:00'
        metadata = {'publictime': timestamp, 'release_frequency': '年', 'parseFileMd5': 'parsed-id',
                    'fileMd5': 'source-id', 'title': '2026年1-6月全区主要经济指标'}
        self.job.update(publictime=timestamp, fileMd5='source-id', upstream_metadata=metadata)
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(backend.contexts[0]['upstream_metadata'], metadata)
        self.assertEqual(backend.contexts[0]['publictime'], timestamp)
        self.assertEqual(result['source_identity']['supplied_filemd5'], 'source-id')
        self.assertTrue(all(r['publictime'] == timestamp for r in result['OUTDATA']))
        self.assertTrue(all(r['end_date'] == '2026-06-30' for r in result['OUTDATA']))

    def test_empty_rules_skip_source_and_model(self):
        backend = FixedBackend()
        self.job.update(GZKJG=[], source_path="missing.csv", skill_path="missing.md")
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["failure_kind"], "GZKJG_EMPTY")
        self.assertEqual(backend.calls, 0)

    def test_name_failure_repairs_code_and_reexecutes(self):
        backend = FixedBackend([CODE.replace('row[0]', '"wrong"'), CODE])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.contexts[1]["history"][0]["evaluation"]["failure_kind"], "NAME_CONSTRUCTION_ERROR")

    def test_needs_review_does_not_export_success(self):
        class ReviewBackend:
            def generate(self, context, folder):
                return {"decision": "needs_review", "python_code": "", "explanation": "规则范围不明"}
        result = run_job(self.job, self.folder, ReviewBackend(), TestExecutor())
        self.assertEqual(result["failure_kind"], "BUSINESS_CONTEXT_REQUIRED")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_fabricated_value_is_rejected(self):
        backend = FixedBackend([CODE.replace('row[1]', '"999"')])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["failure_kind"], "ATTEMPTS_EXHAUSTED")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_io_and_reflection_not_allowed(self):
        for code in ('import os\ndef main(params): return []',
                     'def main(params): return open("secret").read()',
                     'def main(params): return params.__class__'):
            with self.assertRaises(ValueError):
                check_code(code)

    def test_unit_difference_does_not_trigger_model_repair(self):
        self.job["GZKJG"][0]["rule_constant_name"] = "万元"
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result["OUTDATA"][0]["indicator_unit"], "亿元")
        self.assertEqual(result["OUTDATA"][0]["KB_unit"], "万元")
        self.assertEqual(result["notifications"], [])

    def test_missing_unit_does_not_override_name_match(self):
        self.job["GZKJG"][0]["rule_constant_name"] = ""
        backend = FixedBackend([CODE.replace('row[2]', '""')])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.calls, 1)

    def test_low_name_rate_repairs_even_with_unit_difference(self):
        self.job["GZKJG"][0]["rule_constant_name"] = "万元"
        backend = FixedBackend([CODE.replace('row[0]', '"wrong"'), CODE])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.calls, 2)

    def test_backend_unavailable_stops_without_rewriting_parser(self):
        from excel_match_agent.app import BackendUnavailable
        class Offline:
            def generate(self, context, folder):
                raise BackendUnavailable("network timeout")
        result = run_job(self.job, self.folder, Offline(), TestExecutor())
        self.assertEqual(result["failure_kind"], "MODEL_BACKEND_UNAVAILABLE")
        self.assertEqual(result["attempts"], 1)

    def test_token_truncation_stops_without_repeating_same_configuration(self):
        from excel_match_agent.app import ModelOutputTruncated
        class TruncatedBackend:
            calls = 0
            def generate(self, context, folder):
                self.calls += 1
                raise ModelOutputTruncated("increase output budget")
        backend = TruncatedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["failure_kind"], "MODEL_OUTPUT_TRUNCATED")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(backend.calls, 1)
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_low_rate_passes_with_two_correct_matching_indicators(self):
        with (self.folder / "source.csv").open("a", encoding="utf-8") as source:
            for number in range(10):
                source.write(f"Other{number},2,亿元,2026-06-30\n")
        self.job["match_threshold"] = 0.99  # 历史字段不再影响结果。
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertAlmostEqual(result["metrics"]["match_rate"], 2 / 12)
        self.assertNotIn("match_threshold", result["metrics"])
        self.assertEqual(result["metrics"]["matched_indicator_count"], 2)
        self.assertEqual(backend.calls, 1)
        self.assertEqual(backend.review_calls, 1)
        self.assertEqual(result["notifications"][0]["code"], "PARTIAL_MATCH")

    def test_partial_match_is_warning_not_failure(self):
        with (self.folder / "source.csv").open("a", encoding="utf-8") as source:
            source.write("Other,2,亿元,2026-06-30\n")
        result = run_job(self.job, self.folder, FixedBackend(), TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertAlmostEqual(result["metrics"]["match_rate"], 2 / 3)
        self.assertEqual(len(result["last_evaluation"]["unmatched"]), 1)
        self.assertTrue((Path(result["output_dir"]) / "node-output.json").exists())

    def test_even_full_match_repairs_wrong_source_name(self):
        self.job["GZKJG"].append({"rule_indicator_name": "wrong", "rule_indicator_code": "456"})
        backend = FixedBackend([CODE.replace('row[0]', '"wrong"'), CODE])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(result["history"][0]["evaluation"]["metrics"]["match_rate"], 1)
        self.assertEqual(backend.calls, 2)
        self.assertEqual(backend.review_calls, 2)
        self.assertEqual(result["OUTDATA"][0]["indicator_name"], "GDP")

    def test_uncertain_name_cannot_export_success(self):
        class UncertainBackend(FixedBackend):
            def review_names(self, context, folder):
                return {"name_status": "uncertain", "explanation": "源表缺少区分总量与子项的表头", "issues": []}
        result = run_job(self.job, self.folder, UncertainBackend(), TestExecutor())
        self.assertEqual(result["failure_kind"], "NAME_REVIEW_UNCERTAIN")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_one_correct_match_rebuilds_until_limit_even_when_names_correct(self):
        self.job["GZKJG"] = self.job["GZKJG"][:1]
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["failure_kind"], "INSUFFICIENT_MATCHES")
        self.assertEqual(backend.calls, 3)
        self.assertIn('INSUFFICIENT_MATCHES', str(backend.contexts[1]['history']))
        self.assertEqual(result['attempts'], 3)
        self.assertEqual(result['rebuilds'], 2)
        self.assertTrue(result['requires_human_review'])
        handoff = json.loads((Path(result['output_dir']) / 'human-review.json').read_text(encoding='utf-8'))
        self.assertEqual(handoff['rebuilds'], 2)
        self.assertEqual(handoff['failure_kind'], 'INSUFFICIENT_MATCHES')
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_fewer_than_two_matches_without_name_issues_rebuilds_and_then_succeeds(self):
        first = CODE.replace('sheet["rows"][1:]', 'sheet["rows"][1:2]')
        backend = FixedBackend([first, CODE])
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result['status'], 'candidate_pass')
        self.assertEqual(backend.calls, 2)
        before = backend.contexts[1]['history'][0]['evaluation']
        self.assertEqual(before['name_review']['name_status'], 'correct')
        self.assertEqual(before['near_name_candidates'], [])
        self.assertEqual(before['metrics']['matched_indicator_count'], 1)
        self.assertTrue(before['retryable'])
        self.assertIn('拼接表', before['repair_request']['reason'])
        self.assertEqual(result['metrics']['matched_indicator_count'], 2)
        self.assertEqual(result['rebuilds'], 1)
        self.assertFalse(result['requires_human_review'])

    def test_zero_matches_rebuilds_from_rules_even_when_names_correct(self):
        self.job["GZKJG"][0]["rule_indicator_name"] = "Other"
        self.job["GZKJG"][1]["rule_indicator_name"] = "Other2"
        backend = FixedBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["failure_kind"], "INSUFFICIENT_MATCHES")
        self.assertEqual(result["last_evaluation"]["metrics"]["matched_indicator_count"], 0)
        self.assertEqual(backend.calls, 3)

    def test_two_records_for_same_indicator_do_not_count_as_two_indicators(self):
        self.job["GZKJG"][1]["rule_indicator_code"] = self.job["GZKJG"][0]["rule_indicator_code"]
        result = run_job(self.job, self.folder, FixedBackend(), TestExecutor())
        self.assertEqual(result["last_evaluation"]["metrics"]["matched_count"], 2)
        self.assertEqual(result["failure_kind"], "INSUFFICIENT_MATCHES")

    def test_unresolved_name_error_does_not_pass_at_retry_limit(self):
        self.job["max_attempts"] = 1
        result = run_job(self.job, self.folder, FixedBackend([CODE.replace('row[0]', '"wrong"')]), TestExecutor())
        self.assertEqual(result["failure_kind"], "ATTEMPTS_EXHAUSTED")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())


if __name__ == "__main__":
    unittest.main()
