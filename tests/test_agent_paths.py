"""端到端：覆盖题目要求验收的六类场景 + 两条不同的执行路径。"""
import unittest
from pathlib import Path

from src.agent import Agent
from src.answer import verify_answer
from src.docs_store import DocStore
from src.models import load_rules

ROOT = Path(__file__).resolve().parents[1]


def run(question, history=None, faults=None, **kwargs):
    rules = load_rules()
    store = DocStore(ROOT / "data" / "docs")
    agent = Agent(store, rules, faults=faults, **kwargs)
    return agent.run(question, history), store


class AgentPathTest(unittest.TestCase):
    def test_clear_answer_with_citation(self):
        res, store = run("员工加班后调休的有效期是多久？")
        self.assertEqual(res.status, "answered")
        self.assertIn("6 个月", res.answer)
        self.assertEqual([c.doc_id for c in res.citations], ["D04"])
        self.assertTrue(verify_answer(res.answer, [c.as_dict() for c in res.citations], store)["ok"])
        self.assertEqual(res.tool_calls, 2)

    def test_document_conflict_asks_instead_of_choosing(self):
        res, store = run("报销期限是多久？")
        self.assertEqual(res.status, "needs_clarification")
        cited = {c.doc_id: c.quote for c in res.citations}
        self.assertIn("D01", cited)
        self.assertIn("D02", cited)
        self.assertIn("30 天", cited["D01"])
        self.assertIn("15 天", cited["D02"])
        # 不能擅自给出单一结论
        text = res.answer
        self.assertNotRegex(text, r"报销期限(是|为)\s*15\s*天")
        self.assertTrue(verify_answer(text, [c.as_dict() for c in res.citations], store)["ok"])

    def test_out_of_scope_reports_gap(self):
        res, _ = run("公司可以帮我办理日本签证吗？")
        self.assertEqual(res.status, "insufficient_info")
        self.assertIsNone(res.answer)
        self.assertEqual(res.citations, [])
        self.assertTrue(res.gaps)

    def test_paraphrase_same_answer(self):
        base, _ = run("员工加班后调休的有效期是多久？")
        para, _ = run("加班换来的补休，最晚多久要休掉？")
        self.assertEqual(para.status, "answered")
        self.assertIn("6 个月", para.answer)
        self.assertEqual(base.citations[0].doc_id, para.citations[0].doc_id)

    def test_contextual_followup_uses_history(self):
        history = [
            {"role": "user", "content": "正式员工的年假有多少天？"},
            {"role": "assistant", "content": "根据 D05，年假按工龄分为 5/10/15 天。"},
        ]
        res, _ = run("那要提前几天申请？", history=history)
        self.assertEqual(res.status, "answered")
        self.assertEqual(res.citations[0].doc_id, "D05")
        self.assertIn("3 个工作日", res.citations[0].quote)

    def test_tool_failure_recovers(self):
        res, _ = run("报销期限是多久？", faults={"search_docs": 1})
        self.assertEqual(res.status, "needs_clarification")
        steps = res.steps
        self.assertFalse(steps[0]["observation"]["ok"])
        # 观察到失败后换了更短的关键词重试，而不是直接作答
        self.assertEqual(steps[1]["action"], "search_docs")
        self.assertNotEqual(steps[0]["args"]["query"], steps[1]["args"]["query"])

    def test_tool_failure_terminal_is_honest(self):
        res, _ = run("报销期限是多久？", faults={"search_docs": 2})
        self.assertEqual(res.status, "failed")
        self.assertIsNone(res.answer)
        self.assertEqual(res.citations, [])
        self.assertIn("upstream_timeout", res.reason + str(res.steps))

    def test_limits_return_incomplete(self):
        res, _ = run("报销期限是多久？", max_steps=2)
        self.assertEqual(res.status, "incomplete")
        self.assertIsNone(res.answer)
        self.assertIn("step_limit", res.reason)
        res2, _ = run("报销期限是多久？", max_tool_calls=1)
        self.assertEqual(res2.status, "incomplete")
        self.assertIn("tool_call_limit", res2.reason)

    def test_two_paths_diverge_on_observation(self):
        """同一问题在不同工具返回下走出不同动作序列，证明决策依赖观察而非固定脚本。"""
        res_a, _ = run("报销期限是多久？")
        self.assertEqual(
            [s["action"] for s in res_a.steps],
            ["search_docs", "read_doc", "read_doc", "check_consistency", "ask_user"],
        )
        res_b, _ = run("报销期限是多久？", faults={"search_docs": 1})
        self.assertEqual(res_b.steps[0]["action"], "search_docs")
        self.assertFalse(res_b.steps[0]["observation"]["ok"])
        # A 路径第 2 步就是读原文；B 路径第 2 步是先重试检索
        self.assertEqual(res_a.steps[1]["action"], "read_doc")
        self.assertEqual(res_b.steps[1]["action"], "search_docs")
        self.assertEqual(res_b.steps[1]["args"].get("query"), "报销期限")


if __name__ == "__main__":
    unittest.main()
