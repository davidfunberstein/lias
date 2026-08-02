"""Tests for DB upsert functions (LIAS/db.py) using temp SQLite."""
import unittest
import tempfile
from pathlib import Path

from LIAS import db


class TestDBUpserts(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = Path(self._tmp.name)
        db.init_db(self.db_path)
        self._orig_get_conn = db.get_conn
        db.get_conn = lambda dp=None: self._orig_get_conn(self.db_path)

    def tearDown(self):
        db.get_conn = self._orig_get_conn
        try:
            conn = getattr(db._local, "conn_" + str(self.db_path), None)
            if conn:
                conn.close()
                delattr(db._local, "conn_" + str(self.db_path))
        except Exception:
            pass
        self.db_path.unlink(missing_ok=True)

    def test_upsert_client_creates(self):
        cid = db.upsert_client("בר")
        self.assertIsInstance(cid, int)
        self.assertGreater(cid, 0)

    def test_upsert_client_idempotent(self):
        cid1 = db.upsert_client("בר")
        cid2 = db.upsert_client("בר")
        self.assertEqual(cid1, cid2)

    def test_upsert_client_different_names(self):
        cid1 = db.upsert_client("בר")
        cid2 = db.upsert_client("כהן")
        self.assertNotEqual(cid1, cid2)

    def test_upsert_case_creates(self):
        cid = db.upsert_client("בר")
        case_id = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        self.assertIsInstance(case_id, int)
        self.assertGreater(case_id, 0)

    def test_upsert_case_idempotent(self):
        cid = db.upsert_client("בר")
        id1 = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        id2 = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        self.assertEqual(id1, id2)

    def test_upsert_case_unique_by_portal_and_number(self):
        cid = db.upsert_client("בר")
        id1 = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        id2 = db.upsert_case(cid, "BDR", "ה-ט 12345-01-22")
        self.assertNotEqual(id1, id2)

    def test_upsert_sub_case_creates(self):
        cid = db.upsert_client("בר")
        case_id = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        sub_id = db.upsert_sub_case(case_id, "sub-1")
        self.assertIsInstance(sub_id, int)
        self.assertGreater(sub_id, 0)

    def test_upsert_sub_case_idempotent(self):
        cid = db.upsert_client("בר")
        case_id = db.upsert_case(cid, "NET", "ה-ט 12345-01-22")
        id1 = db.upsert_sub_case(case_id, "sub-1")
        id2 = db.upsert_sub_case(case_id, "sub-1")
        self.assertEqual(id1, id2)

    def test_full_hierarchy(self):
        cid = db.upsert_client("דוד פונברשטיין")
        case_id = db.upsert_case(cid, "BDR", "1355021-2")
        sub_id = db.upsert_sub_case(case_id, "1355021-2")
        conn = self._orig_get_conn(self.db_path)
        row = conn.execute(
            "SELECT c.display_name, ca.portal, ca.case_number "
            "FROM sub_cases s JOIN cases ca ON ca.case_id=s.case_id "
            "JOIN clients c ON c.client_id=ca.client_id "
            "WHERE s.sub_case_id=?", (sub_id,)
        ).fetchone()
        self.assertEqual(row["display_name"], "דוד פונברשטיין")
        self.assertEqual(row["portal"], "BDR")


if __name__ == "__main__":
    unittest.main()
