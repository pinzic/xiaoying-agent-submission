import unittest
from pathlib import Path

from src.docs_store import DocStore
from src.models import load_rules
from src.retrieval import Retriever

ROOT = Path(__file__).resolve().parents[1]


class RetrievalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules()
        cls.store = DocStore(ROOT / "data" / "docs")
        cls.retriever = Retriever(cls.store, cls.rules)

    def ids(self, query):
        return [h.doc_id for h in self.retriever.search(query)]

    def test_exact_question_hits_expected_doc(self):
        self.assertEqual(self.ids("员工加班后调休的有效期是多久？")[0], "D04")

    def test_paraphrase_still_hits(self):
        # 「补休」是「调休」的口语说法，规则里的同义词扩展要能兜住
        self.assertIn("D04", self.ids("加班换来的补休，最晚多久要休掉？"))
        # 长句改写后仍应命中报销制度
        hit_ids = self.ids("费用发生后最晚几天内要把报销单交上去？")
        self.assertIn("D01", hit_ids)
        self.assertIn("D02", hit_ids)

    def test_out_of_scope_returns_nothing(self):
        self.assertEqual(self.ids("公司可以帮我办理日本签证吗？"), [])
        # 含有一个相同词（补贴），但整体主题无关，也要被门槛挡住
        self.assertEqual(self.ids("我想问下旅游补贴"), [])

    def test_hit_carries_metadata_and_section(self):
        hit = self.retriever.search("调休有效期")[0]
        self.assertEqual(hit.doc_id, "D04")
        self.assertEqual(hit.section, "调休有效期")
        self.assertTrue(hit.version.startswith("v"))
        self.assertTrue(hit.effective_date)
        self.assertLessEqual(len(hit.snippet), 200)

    def test_section_level_chunking(self):
        hit = self.retriever.search("退款政策")[0]
        self.assertEqual(hit.doc_id, "P03")
        self.assertIn("7 天", hit.snippet)


if __name__ == "__main__":
    unittest.main()
