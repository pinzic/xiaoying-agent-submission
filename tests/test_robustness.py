"""鲁棒性：替换文档内容后，答案与动作是否随之改变（对应审核要点「替换文档内容，核对答案与动作」）。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from src.agent import Agent
from src.docs_store import DocStore
from src.models import load_rules

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "docs"


class RobustnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.docs_dir = self.tmp / "docs"
        shutil.copytree(DOCS, self.docs_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_agent(self, question):
        agent = Agent(DocStore(self.docs_dir), load_rules())
        return agent.run(question)

    def test_removing_conflicting_doc_switches_action(self):
        # 环境 A：D01 + D02 并存 → 必须追问
        res_a = self.run_agent("报销期限是多久？")
        self.assertEqual(res_a.status, "needs_clarification")

        # 环境 B：删掉 D02 → 只剩 D01，动作从「追问」变成「直接作答」
        (self.docs_dir / "D02.md").unlink()
        res_b = self.run_agent("报销期限是多久？")
        self.assertEqual(res_b.status, "answered")
        self.assertEqual([c.doc_id for c in res_b.citations], ["D01"])
        self.assertIn("30 天", res_b.answer)

    def test_editing_doc_changes_answer(self):
        # 把 D01 的 30 天改成 45 天，答案必须跟着变；旧引用必须失效
        target = self.docs_dir / "D01.md"
        target.write_text(target.read_text(encoding="utf-8").replace("30 天", "45 天"), encoding="utf-8")
        (self.docs_dir / "D02.md").unlink()
        res = self.run_agent("报销期限是多久？")
        self.assertEqual(res.status, "answered")
        self.assertIn("45 天", res.answer)
        self.assertNotIn("30 天", res.answer)

    def test_editing_conflicting_value_changes_conflict_payload(self):
        target = self.docs_dir / "D02.md"
        target.write_text(target.read_text(encoding="utf-8").replace("15 天", "7 天"), encoding="utf-8")
        res = self.run_agent("报销期限是多久？")
        self.assertEqual(res.status, "needs_clarification")
        quotes = " ".join(c.quote for c in res.citations)
        self.assertIn("7 天", quotes)
        self.assertIn("30 天", quotes)

    def test_editing_scope_in_doc_changes_resolution(self):
        # 把 D06 的「不享受年假」改成「每年享受 3 天年假」，结论要改为 3 天
        target = self.docs_dir / "D06.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace("实习生不享受年假", "实习生每年享受 3 天年假"),
            encoding="utf-8",
        )
        res = self.run_agent("实习生有年假吗？")
        self.assertEqual(res.status, "answered")
        self.assertIn("3 天", res.answer)
        self.assertIn("D06", [c.doc_id for c in res.citations])

    def test_scope_alone_suffices_after_removing_precedence(self):
        """去掉 D06 的优先条款后，仅凭适用范围仍可定分——两个信号是相互独立的。"""
        from src.conflict import detect
        from src.models import load_rules

        target = self.docs_dir / "D06.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace("本规定与公司其他假期制度不一致时，以本规定为准。", ""),
            encoding="utf-8",
        )
        res = detect("实习生有年假吗？", ["D06", "D05"], DocStore(self.docs_dir), load_rules())
        self.assertEqual(len(res["conflicts"]), 1)
        resolution = res["conflicts"][0]["resolution"]
        self.assertIsNotNone(resolution)
        self.assertEqual(resolution["type"], "scope")


if __name__ == "__main__":
    unittest.main()
