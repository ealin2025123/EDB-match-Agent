import json
import unittest

from excel_match_agent.name_review import validate_review


class NameReviewTests(unittest.TestCase):
    def test_distant_source_title_requires_explicit_real_cell_evidence(self):
        rows = [{'indicator_name': '新建同比', 'source': {'sheet': 's', 'cell': 'B20'}}]
        raw = [['投资完成额', None]] + [[None, None] for _ in range(18)] + [['新建', 4]]
        book = {'sheets': [{'name': 's', 'rows': raw}]}
        def response(reason, target='投资完成额新建同比'):
            return json.dumps({'name_status': 'incorrect', 'explanation': '标题限定', 'issues': [
                {**rows[0], 'target_indicator_name': target, 'reason': reason}]})
        self.assertEqual(validate_review(response('A1 为该子表标题投资完成额'), rows, book)['name_status'], 'incorrect')
        for reason in ('标题在前面但无坐标', 'A2 为该子表标题投资完成额', 'A999 为标题'):
            with self.assertRaises(ValueError):
                validate_review(response(reason), rows, book)
        with self.assertRaises(ValueError):
            validate_review(response('A1为投资完成额', '虚构限定新建同比'), rows, book)

    rows = [{"indicator_name": "GDP", "source": {"sheet": "source", "cell": "B2"}}]

    def test_correct_cannot_include_unresolved_name_errors(self):
        with self.assertRaises(ValueError):
            validate_review(json.dumps({"name_status": "correct", "explanation": "x", "issues": [{}]}), self.rows)

    def test_incorrect_needs_source_evidence(self):
        with self.assertRaises(ValueError):
            validate_review(json.dumps({"name_status": "incorrect", "explanation": "low rate", "issues": []}), self.rows)

    def test_fabricated_source_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_review(json.dumps({"name_status": "incorrect", "explanation": "x", "issues": [
                {"indicator_name": "GDP", "source": {"sheet": "source", "cell": "B999"}, "reason": "x"}]}), self.rows)

    def test_review_can_identify_numeric_source_omitted_by_parser(self):
        book = {"sheets": [{"name": "source", "rows": [["name", "value"], ["GDP", 1], ["Other", 0]]}]}
        result = validate_review(json.dumps({"name_status": "incorrect", "explanation": "错误去重丢失有效源指标", "issues": [
            {"indicator_name": "Other", "source": {"sheet": "source", "cell": "B3"}, "reason": "源值0被错误丢弃"}]}), self.rows, book)
        self.assertEqual(result["name_status"], "incorrect")

    def test_omission_evidence_cannot_point_to_text_or_invent_replacement_name_for_existing_cell(self):
        book = {"sheets": [{"name": "source", "rows": [["name", "value"], ["GDP", 1], ["Other", "——"]]}]}
        for cell in ("B3", "B2", "B999"):
            with self.assertRaises(ValueError):
                validate_review(json.dumps({"name_status": "incorrect", "explanation": "x", "issues": [
                    {"indicator_name": "Other", "source": {"sheet": "source", "cell": cell}, "reason": "x"}]}), self.rows, book)


if __name__ == "__main__":
    unittest.main()
