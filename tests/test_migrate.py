"""Tests for migrate_csv functions: is_me, pick_client, _map_status."""
import unittest
from LIAS.migrate_csv import is_me, pick_client, _map_status


LAWYER_WORDS = {"דוד", "פונברשטיין"}


class TestIsMe(unittest.TestCase):

    def test_exact_match(self):
        self.assertTrue(is_me("דוד פונברשטיין", LAWYER_WORDS))

    def test_last_name_only(self):
        self.assertTrue(is_me("פונברשטיין", LAWYER_WORDS))

    def test_first_name_only(self):
        self.assertTrue(is_me("דוד", LAWYER_WORDS))

    def test_different_person(self):
        self.assertFalse(is_me("חנה פונברשטיין", LAWYER_WORDS))

    def test_completely_different(self):
        self.assertFalse(is_me("בר", LAWYER_WORDS))

    def test_empty_name(self):
        self.assertFalse(is_me("", LAWYER_WORDS))

    def test_empty_lawyer_words(self):
        self.assertFalse(is_me("דוד פונברשטיין", set()))

    def test_non_hebrew_ignored(self):
        self.assertFalse(is_me("David F", LAWYER_WORDS))

    def test_superset_not_match(self):
        self.assertFalse(is_me("דוד פונברשטיין כהן", LAWYER_WORDS))


class TestPickClient(unittest.TestCase):

    def test_filters_lawyer_returns_other(self):
        result = pick_client(["פונברשטיין", "בר"], LAWYER_WORDS)
        self.assertEqual(result, "בר")

    def test_both_are_lawyer(self):
        result = pick_client(["פונברשטיין", "פונברשטיין"], LAWYER_WORDS)
        self.assertEqual(result, "פונברשטיין")

    def test_multiple_others(self):
        result = pick_client(["פונברשטיין", "בר", "כהן"], LAWYER_WORDS)
        self.assertIn("בר", result)
        self.assertIn("כהן", result)
        self.assertIn(" נ' ", result)

    def test_single_name(self):
        result = pick_client(["בר"], LAWYER_WORDS)
        self.assertEqual(result, "בר")

    def test_empty_list(self):
        result = pick_client([], LAWYER_WORDS)
        self.assertIsNone(result)

    def test_no_lawyer_words_joins_all(self):
        result = pick_client(["בר", "כהן"], set())
        self.assertIn("בר", result)
        self.assertIn("כהן", result)

    def test_two_non_lawyer_names(self):
        result = pick_client(["בר", "כהן"], LAWYER_WORDS)
        self.assertEqual(result, "בר נ' כהן")

    def test_deduplicates(self):
        result = pick_client(["בר", "בר"], LAWYER_WORDS)
        self.assertEqual(result, "בר")

    def test_family_member_not_filtered(self):
        result = pick_client(["דוד פונברשטיין", "חנה פונברשטיין"], LAWYER_WORDS)
        self.assertEqual(result, "חנה פונברשטיין")


class TestMapStatus(unittest.TestCase):

    def test_success(self):
        self.assertEqual(_map_status("Success"), "COMPLETED")

    def test_local_sync(self):
        self.assertEqual(_map_status("Local Sync"), "COMPLETED")

    def test_missing(self):
        self.assertEqual(_map_status("Missing"), "MISSING")

    def test_failed(self):
        self.assertEqual(_map_status("Failed - timeout"), "ERROR")

    def test_empty(self):
        self.assertEqual(_map_status(""), "PENDING")

    def test_none(self):
        self.assertEqual(_map_status(None), "PENDING")

    def test_unknown(self):
        self.assertEqual(_map_status("something else"), "PENDING")

    def test_whitespace(self):
        self.assertEqual(_map_status("  Success  "), "COMPLETED")


if __name__ == "__main__":
    unittest.main()
