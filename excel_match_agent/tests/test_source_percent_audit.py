import unittest

from excel_match_agent.app import audit_sources


class SourcePercentAuditTests(unittest.TestCase):
    def check(self, raw, value, unit='%'):
        rows = [{'indicator_data': value, 'indicator_unit': unit,
                 'source': {'sheet': 'Sheet1', 'cell': 'A1'}}]
        audit_sources(rows, {'sheets': [{'name': 'Sheet1', 'rows': [[raw]]}]})

    def test_explicit_percent_source_preserves_value(self):
        for raw, value in [('10.2%', '10.2'), ('1.0%', '1.0'),
                           ('1.8%', '1.8'), ('-2.5％', '-2.5'), ('0%', 0)]:
            with self.subTest(raw=raw):
                self.check(raw, value)

    def test_wrong_value_unit_or_implicit_scaling_rejected(self):
        for raw, value, unit in [('10.2%', '10.3', '%'), ('10.2%', '.102', '%'),
                                 ('10.2%', '10.2', '万元'), (.102, '10.2', '%'),
                                 ('增长10.2%', '10.2', '%')]:
            with self.subTest(raw=raw, value=value, unit=unit), self.assertRaises(ValueError):
                self.check(raw, value, unit)

    def test_numeric_source_keeps_strict_comparison(self):
        self.check(3085274, 3085274, '万元')
        with self.assertRaises(ValueError):
            self.check(3085274, 3085275, '万元')
