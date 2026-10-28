"""引用与答案支撑校验：错误引用、指标加料都必须被拦住。"""
import unittest
from pathlib import Path

from src.answer import verify_answer
from src.docs_store import DocStore

ROOT = Path(__file__).resolve().parents[1]


class CitationGuardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = DocStore(ROOT / "data" / "docs")

    def test_verbatim_quote_passes(self):
        res = verify_answer(
            "根据 D01：「员工应在费用发生之日起 30 天内提交报销申请」。",
            [{"doc_id": "D01", "section": "报销期限",
              "quote": "员工应在费用发生之日起 30 天内提交报销申请"}],
            self.store,
        )
        self.assertTrue(res["ok"], res["issues"])

    def test_fabricated_quote_is_rejected(self):
        res = verify_answer(
            "根据 D01，报销期限是 45 天。",
            [{"doc_id": "D01", "section": "报销期限",
              "quote": "员工应在费用发生之日起 45 天内提交报销申请"}],
            self.store,
        )
        self.assertFalse(res["ok"])
        self.assertTrue(any("未在原文中逐字出现" in i for i in res["issues"]))

    def test_number_must_be_supported_by_quote(self):
        res = verify_answer(
            "根据 D01，报销期限为 45 天。",
            [{"doc_id": "D01", "section": "报销期限",
              "quote": "员工应在费用发生之日起 30 天内提交报销申请"}],
            self.store,
        )
        self.assertFalse(res["ok"])
        self.assertTrue(any("找不到依据" in i for i in res["issues"]))

    def test_unknown_doc_id_is_rejected(self):
        res = verify_answer("根据 D99。", [{"doc_id": "D99", "section": "x", "quote": "不存在的原文片段"}], self.store)
        self.assertFalse(res["ok"])


if __name__ == "__main__":
    unittest.main()
