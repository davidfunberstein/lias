"""Tests for _normalize_party (core/download.py)."""
import unittest
from core.download import _normalize_party


class TestNormalizeParty(unittest.TestCase):

    def test_plain_name_unchanged(self):
        self.assertEqual(_normalize_party("דוד פונברשטיין"), "דוד פונברשטיין")

    def test_strip_vaach_with_geresh(self):
        self.assertEqual(_normalize_party("איז'ק יצחקי ואח'"), "איז'ק יצחקי")

    def test_strip_vaach_with_hebrew_geresh(self):
        self.assertEqual(_normalize_party("איז'ק יצחקי ואח׳"), "איז'ק יצחקי")

    def test_strip_vaach_no_quote(self):
        self.assertEqual(_normalize_party("כהן ואח"), "כהן")

    def test_strip_baam(self):
        result = _normalize_party('בזק בע"מ')
        self.assertEqual(result, "בזק")

    def test_strip_baam_smart_quotes(self):
        result = _normalize_party("בזק בע”מ")
        self.assertEqual(result, "בזק")

    def test_collapse_whitespace(self):
        self.assertEqual(_normalize_party("דוד   פונברשטיין"), "דוד פונברשטיין")

    def test_strip_leading_trailing(self):
        self.assertEqual(_normalize_party("  דוד  "), "דוד")

    def test_empty_string(self):
        self.assertEqual(_normalize_party(""), "")

    def test_vaach_mid_string_not_stripped(self):
        self.assertEqual(_normalize_party("ואח דוד"), "ואח דוד")

    def test_combined_suffix_and_whitespace(self):
        result = _normalize_party("  כהן  ואח'  ")
        self.assertEqual(result, "כהן")


if __name__ == "__main__":
    unittest.main()
