"""离线规划器（Mock）：不依赖外部模型，用可解释的启发式完成同样的 ReAct 循环。

它的作用只有一个：在没有 API Key 的环境中跑通「决策 → 工具 → 观察 → 再决策」，
并验证引用、冲突、上限等工程约束。它**不代表**真实模型的决策能力，真实链路由
planner_llm.LLMPlanner 承担（同一套工具与同一条循环）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .answer import Answerer
from .models import Action, Context
from .retrieval import Retriever


class MockPlanner:
    name = "mock"

    def __init__(self, retriever: Retriever, rules: Dict[str, Any], answerer: Answerer):
        self.retriever = retriever
        self.rules = rules
        self.answerer = answerer
        self.stopwords = rules["stopwords"]
        self.query = ""
        self.expanded = False
        self.candidates: List[str] = []

    # ---------- 主循环中的一次决策 ----------
    def decide(self, ctx: Context) -> Action:
        last = ctx.last_observation
        if last is None:
            self.query = self._augment(ctx.question, ctx.history)
            return Action(
                "search_docs",
                {"query": self.query, "top_k": 5},
                "首轮：先用问题关键词检索制度库，确认是否存在可依据的文档",
            )
        handler = {
            "search_docs": self._after_search,
            "read_doc": self._after_read,
            "check_consistency": self._after_consistency,
            "validate_citations": self._after_validation,
        }.get(last["action"])
        if handler is None:
            return self._fail(f"未预期的观察来源：{last['action']}", "状态异常，终止并报告")
        return handler(ctx, last)

    # ---------- 各阶段 ----------
    def _after_search(self, ctx: Context, last: Dict[str, Any]) -> Action:
        if not last["ok"]:
            # 参数错误（如检索词为空）重试同一类参数没有意义，直接如实报告；
            # 只有上游故障才值得换策略重试一次。
            if str(last.get("error", "")).startswith("bad_arguments"):
                return self._fail(
                    f"检索请求参数不合法（{last['error']}），无法从知识库取证",
                    "检索词为空或非法：没有可检索的依据，明确报告而不是把参数问题说成服务故障",
                    gaps=["检索词为空或非法，本次未能完成检索"],
                )
            fails = ctx.tool_failures.get("search_docs", 0)
            if fails <= 1:
                return Action(
                    "search_docs",
                    {"query": self._core(self.query or ctx.question), "top_k": 3},
                    f"检索工具报错（{last['error']}），先用更短的核心词重试一次，排除查询过长导致的问题",
                )
            return self._fail(
                "检索工具连续失败，知识库不可达，无法获得任何制度依据",
                "两次检索均失败，没有可用依据：明确报告工具故障，不猜测答案",
                gaps=["检索工具不可用（upstream_timeout），本次未能完成检索"],
            )

        hits = last["data"].get("hits", [])
        if not hits:
            if not self.expanded:
                self.expanded = True
                alt = self._broaden(self.query or ctx.question)
                if alt and alt != self.query:
                    return Action(
                        "search_docs",
                        {"query": alt, "top_k": 5},
                        "首轮检索零命中，改用更宽的关键词再检索一次，确认是否只是表述差异",
                    )
            return Action(
                "finish",
                self.answerer.compose_insufficient(ctx.question),
                "两轮检索都没有命中任何制度或 FAQ，判定为资料缺口，拒绝凭常识作答",
            )

        best = hits[0]["score"]
        self.candidates = [h["doc_id"] for h in hits if h["score"] >= 0.45 * best][:2]
        unread = [d for d in self.candidates if d not in ctx.read_docs and d not in ctx.failed_docs]
        if unread:
            return Action(
                "read_doc",
                {"doc_id": unread[0]},
                f"命中 {len(self.candidates)} 篇候选（{'、'.join(self.candidates)}）：摘要不足以判断是否冲突，先读原文",
            )
        return Action(
            "check_consistency",
            {"question": self.query, "doc_ids": [d for d in self.candidates if d in ctx.read_docs]},
            "候选原文已读完，核查同一事项在不同文档中的取值是否一致",
        )

    def _after_read(self, ctx: Context, last: Dict[str, Any]) -> Action:
        if not last["ok"]:
            fails = ctx.tool_failures.get("read_doc", 0)
            if fails <= 1 and last["args"].get("doc_id") not in ctx.failed_docs:
                return Action(
                    "read_doc",
                    {"doc_id": last["args"].get("doc_id", "")},
                    f"读取原文失败（{last['error']}），重试一次；若仍失败则换下一篇候选",
                )
            ctx.failed_docs[last["args"].get("doc_id", "")] = last["error"] or "read_failed"
            unread = [d for d in self.candidates if d not in ctx.read_docs and d not in ctx.failed_docs]
            if unread:
                return Action("read_doc", {"doc_id": unread[0]},
                              "该文档读取失败且已重试，改用下一篇候选继续核对")
            if not ctx.read_docs:
                return self._fail("所有候选文档都无法读取", "没有可引用原文，终止并报告读取失败")
            return Action("check_consistency",
                          {"question": self.query, "doc_ids": list(ctx.read_docs)},
                          "在可读的候选文档之间继续做一致性核查")

        unread = [d for d in self.candidates if d not in ctx.read_docs and d not in ctx.failed_docs]
        if unread:
            return Action(
                "read_doc",
                {"doc_id": unread[0]},
                f"已读 {last['data'].get('doc_id')}，还有候选未读；读全后才好判断是否与其它制度冲突",
            )
        doc_ids = [d for d in self.candidates if d in ctx.read_docs]
        if len(doc_ids) < 2:
            return Action(
                "finish",
                self.answerer.compose_clear(self.query, list(ctx.read_docs.values())),
                "只有一篇候选文档命中且无其它制度可比对，直接依据该文档原文回答",
            )
        return Action(
            "check_consistency",
            {"question": self.query, "doc_ids": doc_ids},
            "候选原文已读完，执行取值一致性核查，确认是否存在冲突",
        )

    def _after_consistency(self, ctx: Context, last: Dict[str, Any]) -> Action:
        if not last["ok"]:
            if str(last.get("error", "")).startswith("bad_arguments"):
                # 参数不合法（如可读候选不足两篇）：重试同样的参数必然再失败，改为直接作答或报告
                docs = list(ctx.read_docs.values())
                if docs:
                    return Action(
                        "finish",
                        self.answerer.compose_clear(self.query, docs),
                        "一致性核查因候选不足两篇无法执行，改为依据已读原文作答并在结论中说明",
                    )
                return self._fail("可用于比对的文档不足两篇且没有已读原文",
                                  "缺少可比对原文，终止并报告")
            fails = ctx.tool_failures.get("check_consistency", 0)
            if fails <= 1:
                return Action(
                    "check_consistency",
                    {"question": self.query, "doc_ids": list(ctx.read_docs)},
                    f"核查工具报错（{last['error']}），重试一次",
                )
            return self._fail(
                "一致性核查工具不可用，无法排除文档冲突",
                "核查失败时直接给结论有漏判冲突的风险，按规则终止并报告失败",
            )

        data = last["data"]
        conflicts = data.get("conflicts", [])
        docs = list(ctx.read_docs.values())
        if conflicts:
            unresolved = [c for c in conflicts if not c.get("resolution")]
            if unresolved:
                return Action(
                    "ask_user",
                    self.answerer.compose_clarification(unresolved[0], ctx.question),
                    "发现冲突且两份制度都没有写适用范围或优先级：不能擅自选一个，向用户确认适用制度",
                )
            resolved = conflicts[0]
            return Action(
                "finish",
                self.answerer.compose_resolved(resolved, resolved["resolution"], docs),
                "冲突可用文档内的适用范围/优先条款定分：给出结论并同时披露两份依据",
            )
        if not docs:
            return self._fail("没有可用的原文", "缺少原文，终止")
        return Action(
            "finish",
            self.answerer.compose_clear(self.query, docs, data.get("claims", [])),
            "候选文档对同一事项取值一致，基于原文直接作答",
        )

    def _after_validation(self, ctx: Context, last: Dict[str, Any]) -> Action:
        return self._fail(
            "引用校验未通过：" + "；".join(last.get("issues", ["quote_not_found"])),
            "引用无法在原文中逐字匹配，拒绝输出答案，明确标记失败",
            gaps=["引用校验失败，未能给出可核验的答案"],
        )

    # ---------- 小工具 ----------
    def _fail(self, reason: str, basis: str, gaps: Optional[List[str]] = None) -> Action:
        return Action(
            "finish",
            {
                "status": "failed",
                "answer": None,
                "citations": [],
                "gaps": gaps or [reason],
                "reason": reason,
            },
            basis,
        )

    def _clean(self, text: str) -> str:
        out = text
        for sw in sorted(self.stopwords, key=len, reverse=True):
            out = out.replace(sw, " ")
        out = re.sub(r"[^\w\u4e00-\u9fff]+", " ", out)
        return re.sub(r"\s+", " ", out).strip()

    def _augment(self, question: str, history: List[Dict[str, str]]) -> str:
        """追问：把上一轮用户问题的主体词带入本轮检索，解决「那…呢」的指代。"""
        if not history:
            return question
        markers = self.rules.get("followup_markers", [])
        if not any(m in question for m in markers):
            return question
        last_user = ""
        for turn in reversed(history):
            if turn.get("role") == "user":
                last_user = turn.get("content", "")
                break
        if not last_user:
            return question
        return f"{question} {self._clean(last_user)[:40]}".strip()

    def _core(self, text: str) -> str:
        core = self._clean(text)
        return core[:12] if core else text[:12]

    def _broaden(self, text: str) -> str:
        core = self._clean(text)
        for tail in ("吗", "呢", "吧"):
            core = core.rstrip(tail)
        return core[:16]
