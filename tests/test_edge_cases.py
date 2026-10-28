"""边界与健壮性：把排查中发现的缺陷固化成回归用例。

这里每一条都曾是可复现的错误行为，修复后由本文件守住。
"""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from src.agent import Agent
from src.docs_store import DocStore
from src.models import DOCS_DIR, load_rules
from src.retrieval import Retriever


def run(question, history=None, faults=None, max_steps=8, max_tool_calls=6):
    rules = load_rules()
    store = DocStore(DOCS_DIR)
    agent = Agent(store, rules, max_steps=max_steps, max_tool_calls=max_tool_calls, faults=faults)
    return agent.run(question, history or [])


class TieredValueOrdering(unittest.TestCase):
    """同一制度按人群分档时，取值必须按数值升序输出，而不是字典序。"""

    def test_tiered_values_are_numerically_sorted(self):
        res = run("正式员工的年假有多少天？")
        self.assertEqual(res.status, "answered")
        self.assertIn("5 天、10 天、15 天", res.answer,
                      "字典序会把 5/10/15 排成 10、15、5")

    def test_every_tier_value_has_a_citation(self):
        res = run("正式员工的年假有多少天？")
        joined = "".join(c.quote for c in res.citations)
        for value in ("5 天", "10 天", "15 天"):
            self.assertIn(value, joined, f"分档取值 {value} 缺少原文引用")


class EmptyCorpus(unittest.TestCase):
    """空语料库不能让检索器崩溃（曾经 ZeroDivisionError）。"""

    def test_search_on_empty_corpus_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DocStore(Path(tmp))
            retriever = Retriever(store, load_rules())
            self.assertEqual(retriever.search("报销期限"), [])
            self.assertEqual(retriever.avg_len, 0.0)

    def test_agent_on_empty_corpus_reports_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            rules = load_rules()
            store = DocStore(Path(tmp))
            agent = Agent(store, rules)
            res = agent.run("报销期限是多久？")
            self.assertEqual(res.status, "insufficient_info")
            self.assertEqual(res.citations, [])


class ArgumentErrorIsNotUpstreamFailure(unittest.TestCase):
    """参数错误不能被当成「服务超时」重试，也不能被描述成工具不可用。"""

    def test_bad_arguments_is_reported_honestly(self):
        # 直接构造参数错误：检索词为空
        rules = load_rules()
        store = DocStore(DOCS_DIR)
        agent = Agent(store, rules)
        # 绕过 CLI 校验，用空问题驱动，观察是否会被误报成 upstream_timeout
        res = agent.run("")
        self.assertEqual(res.status, "failed")
        joined = " ".join(res.gaps) + " " + (res.reason or "")
        self.assertNotIn("upstream_timeout", joined,
                         "空检索词是参数问题，不应被描述为上游超时")
        self.assertIn("参数不合法", joined)

    def test_cli_rejects_empty_question(self):
        from src.cli import main
        with contextlib.redirect_stderr(io.StringIO()):  # argparse 的用法输出会污染测试流
            with self.assertRaises(SystemExit) as ctx:
                main(["--question", "   "])
        self.assertEqual(ctx.exception.code, 2)


class Limits(unittest.TestCase):
    """上限必须返回明确的未完成状态，且不输出未验证的答案。"""

    def test_step_limit_is_explicit(self):
        res = run("报销期限是多久？", max_steps=2)
        self.assertEqual(res.status, "incomplete")
        self.assertIsNone(res.answer)
        self.assertIn("step_limit_reached", res.reason or "")

    def test_tool_call_limit_is_explicit(self):
        res = run("报销期限是多久？", max_tool_calls=1)
        self.assertEqual(res.status, "incomplete")
        self.assertIn("tool_call_limit_reached", res.reason or "")

    def test_abort_step_is_marked_final_and_trace_is_serialisable(self):
        res = run("报销期限是多久？", max_steps=2)
        self.assertTrue(res.steps[-1].get("final"))
        json.dumps(res.as_dict(), ensure_ascii=False)  # trace 必须可完整序列化


if __name__ == "__main__":
    unittest.main()
