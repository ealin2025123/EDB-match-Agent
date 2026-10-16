import json
from pathlib import Path
import tempfile
import unittest

from excel_match_agent.app import run_job
from excel_match_agent.name_alignment import build_name_candidates, required_alignments
from excel_match_agent.name_review import validate_review
from excel_match_agent.tests.test_runtime import CODE, TestExecutor


ALIASES = {"规上工业增加值同比": "规模以上工业增加值增速", "社会消费品零售额累计": "社会消费品零售总额绝对量"}
ALIGNED_CODE = CODE.replace('row[0]', repr(ALIASES) + '.get(row[0], row[0])')


class AlignmentBackend:
    """离线语义判定夹具；真实任务仍由 DeepSeek 判断。"""
    def __init__(self, repair=True, falsely_pass_second=False):
        self.calls = 0
        self.repair = repair
        self.falsely_pass_second = falsely_pass_second
        self.contexts = []

    def generate(self, context, folder):
        self.contexts.append(json.loads(json.dumps(context)))
        self.calls += 1
        return {"decision": "generate", "python_code": ALIGNED_CODE if self.calls > 1 and self.repair else CODE,
                "explanation": "离线夹具：第二次才通过代码对齐GZK，非直接改写数据"}

    def review_names(self, context, folder):
        issues, decisions = [], []
        for candidate in context["near_name_candidates"]:
            name, source = candidate["indicator_name"], candidate["source"]
            same = name in ALIASES and not (self.calls > 1 and self.falsely_pass_second)
            target = ALIASES[name] if same else ""
            reason = "名称简称/等义后缀，源表指标含义相同" if same else "工业总产值与工业增加值口径不同，不能强配"
            decisions.append({"indicator_name": name, "source": source, "decision": "same_indicator" if same else "different_indicator",
                              "target_indicator_name": target, "reason": reason,
                              "compatibility": {"rule_indicator_name": target or candidate["candidates"][0]["rule_indicator_name"],
                                                "unit": "convertible" if name == "社会消费品零售额累计" else "compatible",
                                                "frequency": "unknown", "reason": "离线夹具：%同单位；零售亿元/万元可换算；源未明确提供发布频率，不从日期猜测"}})
            if same:
                issues.append({"indicator_name": name, "source": source, "target_indicator_name": target, "reason": reason})
        return {"name_status": "incorrect" if issues else "correct", "explanation": "按名称核对同义项及不同口径",
                "issues": issues, "candidate_reviews": decisions}


class NameAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        (self.folder / "source.csv").write_text(
            "name,value,unit,date\n规上工业增加值同比,0,%,2026-06-30\n"
            "社会消费品零售额累计,123,亿元,2026-06-30\n工业总产值累计,456,万元,2026-06-30\n", encoding="utf-8")
        (self.folder / "SKILL.md").write_text("按列解析，名称规范化，保留源数值", encoding="utf-8")
        self.rules = [{"rule_indicator_name": name, "rule_indicator_code": str(i), "rule_constant_name": "%" if i == 1 else "万元",
                       "indicator_release_frequency": "月", "rule_unit_ratio": ""}
                      for i, name in enumerate(ALIASES.values(), 1)]
        self.job = {"source_path": "source.csv", "skill_path": "SKILL.md", "GZKJG": self.rules, "output_dir": "runs"}

    def tearDown(self):
        self.temp.cleanup()

    def test_synonyms_rebuild_code_execute_again_and_output_exact_gzk_names(self):
        backend = AlignmentBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual({r["indicator_name"] for r in result["OUTDATA"]}, set(ALIASES.values()))
        self.assertEqual(result["OUTDATA"][0]["indicator_data"], "0")
        self.assertEqual(result["OUTDATA"][0]["indicator_unit"], "%")
        self.assertEqual(result["OUTDATA"][1]["indicator_unit"], "亿元")
        self.assertEqual(result["OUTDATA"][1]["KB_unit"], "万元")
        self.assertEqual(result["OUTDATA"][1]["indicator_data"], "123")
        self.assertEqual(len(backend.contexts[1]["required_name_alignments"]), 2)
        output = Path(result["output_dir"])
        original = json.loads((output / "attempt-1/ExcelData.json").read_text(encoding="utf-8"))["ExcelData"]
        repaired = json.loads((output / "attempt-2/ExcelData.json").read_text(encoding="utf-8"))["ExcelData"]
        self.assertEqual(original[0]["indicator_name"], "规上工业增加值同比")
        self.assertEqual(repaired[0]["indicator_name"], "规模以上工业增加值增速")
        self.assertEqual([r["indicator_data"] for r in original], [r["indicator_data"] for r in repaired])
        self.assertEqual(repaired[2]["indicator_name"], "工业总产值累计")
        self.assertEqual(len(result["last_evaluation"]["unmatched"]), 1)
        self.assertTrue((output / "node-output.json").exists())

    def test_confirmed_target_cannot_be_ignored_even_if_later_review_says_correct(self):
        result = run_job(self.job, self.folder, AlignmentBackend(repair=False, falsely_pass_second=True), TestExecutor())
        self.assertEqual(result["failure_kind"], "ATTEMPTS_EXHAUSTED")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())
        self.assertIn("尚未通过解析代码重跑落实", str(result["history"]))

    def test_two_existing_matches_do_not_bypass_an_unresolved_synonym(self):
        (self.folder / "source.csv").write_text(
            "name,value,unit,date\nGDP,1,亿元,2026-06-30\nGDP2,2,亿元,2026-06-30\n"
            "规上工业增加值同比,0,%,2026-06-30\n", encoding="utf-8")
        self.job["GZKJG"] = [self.rules[0], {"rule_indicator_name": "GDP", "rule_indicator_code": "100"},
                             {"rule_indicator_name": "GDP2", "rule_indicator_code": "200"}]
        result = run_job(self.job, self.folder, AlignmentBackend(), TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(result["history"][0]["evaluation"]["metrics"]["matched_indicator_count"], 2)
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["metrics"]["matched_indicator_count"], 3)

    def test_unresolved_synonyms_do_not_export_at_retry_limit(self):
        self.job["max_attempts"] = 1
        result = run_job(self.job, self.folder, AlignmentBackend(repair=False), TestExecutor())
        self.assertEqual(result["failure_kind"], "ATTEMPTS_EXHAUSTED")
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_candidate_ranking_uses_names_and_carries_auxiliary_units_and_frequency(self):
        rows = [{"indicator_name": "完全不同名称", "indicator_unit": "亿元", "indicator_release_frequency": "季",
                 "source": {"sheet": "s", "cell": "B2"}}]
        baseline = build_name_candidates(rows, self.rules)
        changed = [{**r, "rule_constant_name": "", "indicator_release_frequency": "年", "rule_unit_ratio": 999} for r in self.rules]
        modified = build_name_candidates(rows, changed)
        self.assertEqual([(v["rule_indicator_name"], v["name_similarity"]) for v in baseline[0]["candidates"]],
                         [(v["rule_indicator_name"], v["name_similarity"]) for v in modified[0]["candidates"]])
        self.assertEqual(baseline[0]["indicator_unit"], "亿元")
        self.assertEqual(baseline[0]["indicator_release_frequency"], "季")
        self.assertEqual(modified[0]["candidates"][0]["indicator_release_frequency"], "年")
        self.assertEqual(modified[0]["candidates"][0]["rule_constant_name"], "")
        self.assertEqual(len(baseline), 1)
        self.assertEqual(len(baseline[0]["candidates"]), 2)

    def test_candidate_review_cannot_be_skipped_or_point_to_fake_gzk_target(self):
        rows = [{"indicator_name": "规上工业增加值同比", "source": {"sheet": "s", "cell": "B2"}}]
        candidates = build_name_candidates(rows, self.rules)
        missing = {"name_status": "correct", "issues": [], "explanation": "原名本身合理"}
        with self.assertRaisesRegex(ValueError, "未复核"):
            validate_review(json.dumps(missing), rows, rules=self.rules, candidates=candidates)
        target = "虚构目标"
        issue = {**rows[0], "target_indicator_name": target, "reason": "x"}
        review = {"name_status": "incorrect", "issues": [issue], "explanation": "x",
                  "candidate_reviews": [{**issue, "decision": "same_indicator"}]}
        with self.assertRaisesRegex(ValueError, "真实GZK"):
            validate_review(json.dumps(review), rows, rules=self.rules, candidates=candidates)

    def test_pending_same_indicator_cannot_claim_correct(self):
        rows = [{"indicator_name": "规上工业增加值同比", "source": {"sheet": "s", "cell": "B2"}}]
        review = {"name_status": "correct", "issues": [], "explanation": "相似但未改代码",
                  "candidate_reviews": [{**rows[0], "decision": "same_indicator", "target_indicator_name": next(iter(ALIASES.values())),
                                         "compatibility": {"rule_indicator_name": next(iter(ALIASES.values())), "unit": "compatible",
                                                           "frequency": "compatible", "reason": "测试夹具：百分比/月频相同"},
                                         "reason": "简称同义"}]}
        with self.assertRaisesRegex(ValueError, "重建解析代码"):
            validate_review(json.dumps(review), rows, rules=self.rules, candidates=build_name_candidates(rows, self.rules))

    def test_incompatible_unit_or_frequency_cannot_be_confirmed_as_same_indicator(self):
        rows = [{"indicator_name": "规上工业增加值同比", "source": {"sheet": "s", "cell": "B2"}}]
        target = next(iter(ALIASES.values()))
        issue = {**rows[0], "target_indicator_name": target, "reason": "相近名称"}
        for unit, frequency in [("incompatible", "compatible"), ("compatible", "incompatible")]:
            review = {"name_status": "incorrect", "issues": [issue], "explanation": "x",
                      "candidate_reviews": [{**issue, "decision": "same_indicator",
                                             "compatibility": {"rule_indicator_name": target, "unit": unit,
                                                               "frequency": frequency, "reason": "明确金额/百分比或月/季序列不兼容"}}]}
            with self.assertRaisesRegex(ValueError, "单位或频率不兼容"):
                validate_review(json.dumps(review), rows, rules=self.rules, candidates=build_name_candidates(rows, self.rules))

    def test_incompatible_near_name_is_left_unmatched_without_rewriting_code(self):
        (self.folder / "source.csv").write_text(
            "name,value,unit,date\nGDP,1,亿元,2026-06-30\nGDP2,2,亿元,2026-06-30\n"
            "规上工业增加值同比,3,万元,2026-06-30\n", encoding="utf-8")
        self.job["GZKJG"] = [self.rules[0], {"rule_indicator_name": "GDP", "rule_indicator_code": "100"},
                             {"rule_indicator_name": "GDP2", "rule_indicator_code": "200"}]
        class IncompatibleBackend(AlignmentBackend):
            def review_names(self, context, folder):
                candidate = context["near_name_candidates"][0]
                return {"name_status": "correct", "issues": [], "explanation": "名称相近但源为金额、GZK为增速，单位维度不兼容",
                        "candidate_reviews": [{"indicator_name": candidate["indicator_name"], "source": candidate["source"],
                                               "decision": "different_indicator", "target_indicator_name": "", "reason": "金额与增速不是同一指标",
                                               "compatibility": {"rule_indicator_name": self_target, "unit": "incompatible",
                                                                 "frequency": "unknown", "reason": "源万元与GZK%无法单位换算；源频率未证实"}}]}
        self_target = next(iter(ALIASES.values()))
        backend = IncompatibleBackend()
        result = run_job(self.job, self.folder, backend, TestExecutor())
        self.assertEqual(result["status"], "candidate_pass")
        self.assertEqual(backend.calls, 1)
        self.assertEqual(result["metrics"]["matched_indicator_count"], 2)
        self.assertEqual(len(result["last_evaluation"]["unmatched"]), 1)

    def test_name_repair_cannot_drop_an_explicitly_confirmed_source_cell(self):
        self.job["required_source_cells"] = [{"sheet": "source", "cell": "B4"}]
        class DroppingBackend(AlignmentBackend):
            def generate(self, context, folder):
                response = super().generate(context, folder)
                response["python_code"] = response["python_code"].replace('sheet["rows"][1:]', 'sheet["rows"][1:3]')
                return response
        result = run_job(self.job, self.folder, DroppingBackend(), TestExecutor())
        self.assertEqual(result["failure_kind"], "ATTEMPTS_EXHAUSTED")
        self.assertIn("遗漏已确认", str(result["history"]))
        self.assertFalse((Path(result["output_dir"]) / "node-output.json").exists())

    def test_already_canonical_cannot_use_a_concat_only_name_as_final_gzk(self):
        rows = [{"indicator_name": "中间拼接名", "source": {"sheet": "s", "cell": "B2"}}]
        comparison = self.rules + [{"rule_indicator_name": "中间拼接名"}]
        candidate = {"indicator_name": "中间拼接名", "source": rows[0]['source']}
        review = {"name_status": "correct", "explanation": "x", "issues": [], "candidate_reviews": [{
            **candidate, "decision": "already_canonical", "target_indicator_name": "中间拼接名", "reason": "x",
            "compatibility": {"rule_indicator_name": "中间拼接名", "unit": "unknown", "frequency": "unknown", "reason": "x"}}]}
        with self.assertRaisesRegex(ValueError, "真实GZK"):
            validate_review(json.dumps(review), rows, rules=comparison, candidates=[candidate], gzk_rules=self.rules)

    def test_canonical_priority_never_allows_missing_required_source(self):
        with self.assertRaisesRegex(ValueError, "尚未通过解析代码重跑落实"):
            validate_review(json.dumps({"name_status": "correct", "explanation": "x", "issues": [], "candidate_reviews": []}),
                            [], rules=self.rules, candidates=[], required=[{"source": {"sheet": "s", "cell": "B2"},
                                                                          "target_indicator_name": "中间拼接名"}], gzk_rules=self.rules)

    def test_gzk_target_supersedes_intermediate_concat_target_without_looping_back(self):
        source = {"sheet": "s", "cell": "B2"}
        target = self.rules[0]['rule_indicator_name']
        def event(name):
            return {"evaluation": {"name_review": {"candidate_reviews": [{"source": source,
                "decision": "same_indicator", "target_indicator_name": name, "reason": "规则及源证据"}]}}}
        targets = required_alignments([event('中间拼接名'), event(target), event('中间拼接名')], self.rules)
        self.assertEqual(targets[0]['target_indicator_name'], target)
        self.assertEqual(targets[0]['target_basis'], 'GZKJG')

    def test_source_parent_extension_needs_actual_nearby_evidence(self):
        row = {"indicator_name": "占比累计", "source": {"sheet": "s", "cell": "B3"}}
        target = "高技术产业占比累计"
        issue = {**row, "target_indicator_name": target, "reason": "源A2给出真实父级"}
        review = {"name_status": "incorrect", "explanation": "缺少父级", "issues": [issue],
                  "candidate_reviews": [{**issue, "decision": "same_indicator", "compatibility": {
                      "rule_indicator_name": target, "unit": "compatible", "frequency": "unknown", "reason": "均为比例，源频率未知"}}]}
        workbook = {"sheets": [{"name": "s", "rows": [["表头"], ["高技术产业"], ["占比", 3]]}]}
        result = validate_review(json.dumps(review), [row], workbook, self.rules, [row], gzk_rules=self.rules)
        self.assertEqual(result['name_status'], 'incorrect')
        workbook['sheets'][0]['rows'][1] = ['其他产业']
        with self.assertRaisesRegex(ValueError, '源表父级'):
            validate_review(json.dumps(review), [row], workbook, self.rules, [row], gzk_rules=self.rules)

    def test_review_can_find_source_scope_error_outside_recalled_candidates(self):
        row = {"indicator_name": "第二产业同比", "source": {"sheet": "s", "cell": "B3"}}
        target = "固定资产投资第二产业同比"
        issue = {**row, "target_indicator_name": target, "reason": "源A1为投资子表，与GDP口径不同"}
        review = {"name_status": "incorrect", "explanation": "字面命中但源口径错误", "issues": [issue],
                  "candidate_reviews": [{**issue, "decision": "same_indicator", "compatibility": {
                      "rule_indicator_name": target, "unit": "compatible", "frequency": "unknown", "reason": "投资分产业增速"}}]}
        workbook = {"sheets": [{"name": "s", "rows": [["固定资产投资"], ["按产业分"], ["第二产业", 14.1]]}]}
        result = validate_review(json.dumps(review), [row], workbook, self.rules, [], gzk_rules=self.rules)
        self.assertEqual(result['name_status'], 'incorrect')
        review['candidate_reviews'][0]['source'] = {'sheet': 's', 'cell': 'B99'}
        with self.assertRaisesRegex(ValueError, '真实待审核记录'):
            validate_review(json.dumps(review), [row], workbook, self.rules, [], gzk_rules=self.rules)
