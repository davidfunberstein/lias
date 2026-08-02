"""Tests for case-card aggregation: arkaa distinction, status, party data, merging."""
import unittest
from ui_modules.db import _arkaa, _case_cards, GROUPS


def _make_row(sub_case_id, sub_number, portal="NET", court="", doc_type="בקשה",
              submission_date="01/01/2024", download_status="COMPLETED",
              local_path="", client_id=1, case_title="", **kw):
    return {
        "sub_case_id": sub_case_id, "sub_number": sub_number,
        "portal": portal, "court": court, "doc_type": doc_type,
        "submission_date": submission_date, "download_status": download_status,
        "local_path": local_path, "client_id": client_id,
        "case_title": case_title, "document_id": kw.get("document_id", sub_case_id),
        "logical_name": "doc", "physical_name": "doc.pdf",
        "submitter_est": "", "pages": 1, "client_name": "test",
    }


class TestArkaa(unittest.TestCase):

    def test_bdr_azori(self):
        self.assertEqual(_arkaa("BDR", "1355021-2"), "בית דין רבני אזורי")

    def test_bdr_gadol_from_court(self):
        self.assertEqual(
            _arkaa("BDR", "1355021-2", "בית הדין הרבני הגדול"),
            "בית דין רבני גדול",
        )

    def test_bdr_azori_from_court(self):
        self.assertEqual(
            _arkaa("BDR", "1355021-2", "בית הדין הרבני האזורי"),
            "בית דין רבני אזורי",
        )

    def test_eca(self):
        self.assertEqual(_arkaa("ECA", "12345"), "הוצאה לפועל")

    def test_net_shalom(self):
        self.assertEqual(_arkaa("NET", "תא 1234-01-22", "בית משפט שלום"), "שלום — אזרחי")

    def test_net_family(self):
        self.assertEqual(_arkaa("NET", "תלהמ 1234", ""), "ענייני משפחה")

    def test_net_avoda(self):
        self.assertEqual(_arkaa("NET", "בל 1234", ""), "בית הדין לעבודה")

    def test_fallback_net(self):
        self.assertEqual(_arkaa("NET", "xyz", ""), "בתי משפט (NET)")


class TestCaseCardMerge(unittest.TestCase):

    def test_bdr_duplicate_merged(self):
        rows = [
            _make_row(1, "1355021-2 החזקת ילדים – הסדרי שהות - 11-01-2022", "BDR"),
            _make_row(1, "1355021-2 החזקת ילדים – הסדרי שהות - 11-01-2022", "BDR",
                      document_id=2),
            _make_row(2, "1355021-2 החזקת ילדים – הסדרי שהות - תל-אביב", "BDR"),
        ]
        cards = _case_cards(rows)
        matching = [c for c in cards if "1355021-2" in c["sub_number"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["docs"], 3)

    def test_bdr_different_cases_not_merged(self):
        rows = [
            _make_row(1, "1355021-2 החזקת ילדים – הסדרי שהות - 11-01-2022", "BDR"),
            _make_row(2, "1355021-3 חלוקת רכוש - כריכה - 11-01-2022", "BDR"),
        ]
        cards = _case_cards(rows)
        self.assertEqual(len(cards), 2)

    def test_merged_sub_number_drops_suffix(self):
        rows = [
            _make_row(1, "1355021-4 מזונות ילדים - 11-01-2022", "BDR"),
            _make_row(2, "1355021-4 מזונות ילדים - פתח תקוה", "BDR"),
        ]
        cards = _case_cards(rows)
        matching = [c for c in cards if "1355021-4" in c["sub_number"]]
        self.assertEqual(len(matching), 1)
        self.assertNotIn("11-01-2022", matching[0]["sub_number"])
        self.assertNotIn("פתח תקוה", matching[0]["sub_number"])

    def test_net_cases_not_merged(self):
        rows = [
            _make_row(1, "תמש 330-04-22", "NET"),
            _make_row(2, "ה-ט 46544-01-22", "NET"),
        ]
        cards = _case_cards(rows)
        self.assertEqual(len(cards), 2)


class TestCaseCardStatus(unittest.TestCase):

    def test_portal_status_propagated(self):
        rows = [_make_row(1, "תמש 330-04-22", "NET")]
        cards = _case_cards(rows)
        self.assertIn("portal_status", cards[0])

    def test_status_field_type(self):
        rows = [_make_row(1, "תמש 330-04-22", "NET")]
        cards = _case_cards(rows)
        self.assertIsInstance(cards[0].get("portal_status", ""), str)


class TestCaseCardParties(unittest.TestCase):

    def test_parties_from_local_path(self):
        rows = [_make_row(1, "תמש 330-04-22", "NET",
                          local_path="downloads/אלון כהן - דני לוי/תמש 330-04-22/doc.pdf")]
        cards = _case_cards(rows)
        self.assertGreaterEqual(len(cards[0]["parties"]), 2)

    def test_empty_parties_for_no_path(self):
        rows = [_make_row(1, "תמש 330-04-22", "NET")]
        cards = _case_cards(rows)
        self.assertIsInstance(cards[0]["parties"], list)


if __name__ == "__main__":
    unittest.main()
