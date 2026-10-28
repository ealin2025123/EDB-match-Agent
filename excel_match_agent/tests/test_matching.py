import copy
import json
from pathlib import Path
import unittest
from excel_match_agent.matching import NeedsReviewError, demo, evaluate, normalize_rules, run_loop


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.row = {"indicator_name": "GDP", "indicator_data": 0, "indicator_unit": "亿元",
                    "end_date": "2026-06-30", "source": {"sheet": "数据", "cell": "C4"}}
        self.rules = [{"rule_indicator_name": "GDP", "rule_indicator_code": "1", "rule_constant_name": "亿元"}]
        self.expected = [["GDP", "2026-06-30"]]

    def evaluate(self, rows=None, rules=None, expected=True):
        return evaluate(rows if rows is not None else [self.row], rules if rules is not None else self.rules,
                        expected_keys=self.expected if expected else None)

    def test_zero_is_valid(self):
        self.assertEqual(self.evaluate()["status"], "candidate_pass")
        self.assertFalse(self.evaluate()["production_ready"])

    def test_empty(self):
        result = self.evaluate([])
        self.assertEqual(result["metrics"]["match_rate"], 0)
        self.assertEqual(result["OUTDATA"][0]["indicator_code"], "empty")

    def test_unknown_scope_does_not_certify_coverage(self):
        result = self.evaluate(expected=False)
        self.assertEqual(result["status"], "candidate_pass")
        self.assertIsNone(result["metrics"]["expected_coverage"])
        self.assertFalse(result["production_ready"])

    def test_false_high_match(self):
        self.expected.append(["投资", "2026-06-30"])
        self.assertEqual(self.evaluate()["status"], "needs_review")

    def test_ambiguous_rules(self):
        self.assertEqual(self.evaluate(rules=self.rules * 2)["status"], "needs_review")

    def test_duplicate_rows(self):
        self.assertEqual(self.evaluate([self.row] * 2)["status"], "needs_review")

    def test_invalid_fields(self):
        for field, value in [("end_date", "2026-02-30"), ("indicator_data", False),
                             ("indicator_data", "NaN"), ("source", None)]:
            row = copy.deepcopy(self.row)
            row[field] = value
            self.assertEqual(self.evaluate([row])["status"], "needs_review")

    def test_no_fuzzy_match(self):
        self.row["indicator_name"] = "gdp"
        self.assertEqual(self.evaluate()["metrics"]["matched_count"], 0)

    def test_repair_feedback(self):
        result = demo()
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["history"][0]["result"]["metrics"]["match_rate"], 0)
        self.assertEqual(result["history"][1]["result"]["metrics"]["match_rate"], 1)

    def test_error_budget(self):
        def execute(*args):
            raise ValueError("bad parser")
        result = run_loop({"GZKJG": self.rules}, lambda context: "candidate", execute)
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["status"], "execution_error")

    def test_empty_rules_never_generate_or_execute(self):
        def forbidden(*args):
            self.fail("空规则库不能调用生成器或执行器")
        result = run_loop({"GZKJG": []}, forbidden, forbidden)
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(result["failure_kind"], "GZKJG_EMPTY")

    def test_invalid_rules_are_not_empty_rules(self):
        result = run_loop({}, None, None)
        self.assertEqual(result["failure_kind"], "GZKJG_INVALID")
        self.assertEqual(result["attempts"], 0)

    def test_placeholder_not_counted(self):
        result = self.evaluate([])
        self.assertEqual(len(result["OUTDATA"]), 1)
        self.assertEqual(result["metrics"]["valid_outdata_count"], 0)

    def test_invalid_values_excluded_from_output(self):
        self.row["indicator_data"] = "NaN"
        result = self.evaluate()
        self.assertEqual(result["metrics"]["matched_count"], 1)
        self.assertEqual(result["metrics"]["valid_outdata_count"], 0)
        self.assertFalse(result["retryable"])

    def test_name_mismatch_retries_and_reports(self):
        self.row["indicator_name"] = "gdp"
        result = run_loop({"GZKJG": self.rules}, lambda context: "candidate",
                          lambda candidate, params: [self.row])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["stop_reason"], "attempts_exhausted")
        self.assertEqual(result["notifications"][0]["code"], "NAME_MISMATCH")

    def test_quality_problem_does_not_blindly_rewrite_names(self):
        self.row["end_date"] = ""
        result = run_loop({"GZKJG": self.rules}, lambda context: "candidate",
                          lambda candidate, params: [self.row])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["failure_kind"], "DATA_QUALITY")

    def test_generator_business_diagnosis_stops_repairs(self):
        def generate(context):
            raise NeedsReviewError("源名称正确，但规则库缺少该指标")
        result = run_loop({"GZKJG": self.rules}, generate, None)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["failure_kind"], "BUSINESS_CONTEXT_REQUIRED")

    def test_upstream_field_aliases(self):
        raw = [{"indicator_code": "001", "indicator_name": "GDP", "constant_name": "亿元",
                "indicator_release_frequency": "季"}]
        result = self.evaluate(rules=raw)
        self.assertEqual(result["metrics"]["matched_count"], 1)
        self.assertEqual(result["OUTDATA"][0]["indicator_code"], "001")
        self.assertNotIn("rule_indicator_name", raw[0])
        self.assertEqual(normalize_rules(raw)[0]["indicator_release_frequency"], "季")

    def test_conflicting_aliases_stop_before_generation(self):
        rules = [dict(self.rules[0], indicator_name="other")]
        result = run_loop({"GZKJG": rules}, None, None)
        self.assertEqual(result["failure_kind"], "GZKJG_INVALID")
        self.assertEqual(result["attempts"], 0)

    def test_unit_difference_not_silently_converted(self):
        self.rules[0]["rule_constant_name"] = "万元"
        self.row["indicator_data"] = "1.23456789"
        result = self.evaluate()
        self.assertEqual(result["OUTDATA"][0]["indicator_data"], "1.23456789")
        self.assertFalse(result["unit_differences"][0]["conversion_applied"])
        self.assertEqual(result["notifications"][0]["code"], "UNIT_CONVERSION_REQUIRED")
        self.assertFalse(result["production_ready"])

    def test_exact_upstream_node_example(self):
        path = Path(__file__).parent / "fixtures" / "upstream-node-format.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        snapshot = copy.deepcopy(original)
        for payload in (original, json.dumps(original, ensure_ascii=False), {"GZKJG": original},
                        {"GZKJG": json.dumps(original)}, json.dumps({"GZKJG": original})):
            with self.subTest(payload_type=type(payload).__name__):
                rules = normalize_rules(payload)
                self.assertEqual(len(rules), 14)
                self.assertEqual(rules[0]["rule_indicator_code"], "9000000001")
                self.assertEqual(rules[0]["rule_unit_ratio"], "")
                self.assertEqual(rules[6]["indicator_release_frequency"], "季")
                row = dict(self.row, indicator_name=original[0]["rule_indicator_name"], indicator_unit="万元")
                result = evaluate([row], payload)
                self.assertEqual(result["metrics"]["matched_count"], 1)
                self.assertEqual(result["OUTDATA"][0]["indicator_code"], "9000000001")
                self.assertEqual(result["OUTDATA"][0]["unit_ratio"], "")
        self.assertEqual(original, snapshot)

    def test_json_text_enters_run_loop(self):
        result = run_loop({"GZKJG": json.dumps(self.rules)}, lambda context: "candidate",
                          lambda candidate, params: [self.row])
        self.assertEqual(result["status"], "candidate_pass")

    def test_empty_json_is_empty_rule_library(self):
        for payload in ("[]", {"GZKJG": []}, '{"GZKJG": []}'):
            result = run_loop({"GZKJG": payload}, None, None)
            self.assertEqual(result["failure_kind"], "GZKJG_EMPTY")
            self.assertEqual(result["attempts"], 0)

    def test_invalid_json_not_treated_as_empty(self):
        for payload in ("[", "null", "{}", "[null]", {"GZKJG": "["}):
            result = run_loop({"GZKJG": payload}, None, None)
            self.assertEqual(result["failure_kind"], "GZKJG_INVALID")
            self.assertEqual(result["attempts"], 0)

    def test_invalid_code_types_rejected(self):
        for code in (True, 9000000001.0):
            with self.assertRaises(ValueError):
                normalize_rules([dict(self.rules[0], rule_indicator_code=code)])


if __name__ == "__main__":
    unittest.main()
