import unittest
from pathlib import Path

from src.conflict import detect
from src.docs_store import DocStore
from src.models import load_rules

ROOT = Path(__file__).resolve().parents[1]


class ConflictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()
        cls.store = DocStore(ROOT / "data" / "docs")

    def test_unresolved_conflict_has_no_resolution(self):
        res = detect("报销期限是多久？", ["D01", "D02"], self.store, self.rules)
        self.assertEqual(len(res["conflicts"]), 1)
        c = res["conflicts"][0]
        self.assertEqual(c["key"], "reimbursement.deadline")
        self.assertIsNone(c["resolution"], "没有适用范围/优先条款时不得定分")
        self.assertEqual(set(c["doc_a"]["values"] + c["doc_b"]["values"]), {"30", "15"})
        self.assertIn("生效日期更新", c["hint"])

    def test_scope_match_resolves_conflict(self):
        res = detect("实习生有年假吗？", ["D06", "D05"], self.store, self.rules)
        self.assertEqual(len(res["conflicts"]), 1)
        c = res["conflicts"][0]
        self.assertIsNotNone(c["resolution"])
        self.assertEqual(c["resolution"]["doc_id"], "D06")
        self.assertIn("scope", c["resolution"]["type"])

    def test_generic_audience_does_not_resolve(self):
        # 问题没说明是正式员工还是实习生，即使 D06 有「以本规定为准」也不能替用户选
        res = detect("年假有几天？", ["D05", "D06"], self.store, self.rules)
        self.assertEqual(len(res["conflicts"]), 1)
        self.assertIsNone(res["conflicts"][0]["resolution"])

    def test_single_doc_has_no_conflict(self):
        res = detect("报销期限是多久？", ["D01"], self.store, self.rules)
        self.assertEqual(res["conflicts"], [])
        self.assertTrue(res["claims"])
        self.assertEqual({c["value"] for c in res["claims"]}, {"30"})

    def test_multi_value_claim_is_kept(self):
        res = detect("年假有几天？", ["D05"], self.store, self.rules)
        self.assertEqual({c["value"] for c in res["claims"]}, {"5", "10", "15"})


if __name__ == "__main__":
    unittest.main()
