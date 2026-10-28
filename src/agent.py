"""ReAct 循环：决策 → 工具 → 观察 → 再决策 / 结束，带步数与工具调用上限。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from .answer import Answerer, verify_answer
from .models import Action, Citation, Context, RunResult
from .planner_mock import MockPlanner
from .retrieval import Retriever
from .tools import Tools

TERMINAL_ACTIONS = ("finish", "ask_user")


class Agent:
    def __init__(self, store, rules: Dict[str, Any], planner=None,
                 max_steps: int = 8, max_tool_calls: int = 6,
                 faults: Optional[Dict[str, int]] = None):
        self.store = store
        self.rules = rules
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.retriever = Retriever(store, rules)
        self.answerer = Answerer(store, rules)
        self.tools = Tools(store, self.retriever, rules, faults)
        self.planner = planner or MockPlanner(self.retriever, rules, self.answerer)

    # ---------- 主循环 ----------
    def run(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> RunResult:
        ctx = Context(question=question, history=list(history or []))
        plan_name = getattr(self.planner, "name", "unknown")

        while True:
            if len(ctx.steps) >= self.max_steps:
                return self._abort(ctx, plan_name,
                                   f"step_limit_reached: 已执行 {len(ctx.steps)} 步，超出上限 {self.max_steps}",
                                   f"达到执行步数上限（{self.max_steps} 步）时任务仍未完成")

            action = self.planner.decide(ctx)

            if action.name == "__invalid__":
                self._record(ctx, "invalid_action", action.args, action.decision_basis,
                             {"ok": False, "summary": "规划器输出无法解析为合法动作，未执行工具"})
                ctx.last_observation = {"action": "__invalid__", "ok": False, "args": {},
                                        "error": action.args.get("error", "invalid action")}
                continue

            if action.name in TERMINAL_ACTIONS:
                result = self._finalize(ctx, action, plan_name)
                if result is not None:
                    return result
                continue  # 引用校验未通过，已把问题回灌给规划器重试

            if ctx.tool_calls >= self.max_tool_calls:
                return self._abort(ctx, plan_name,
                                   f"tool_call_limit_reached: 已调用 {ctx.tool_calls} 次工具",
                                   f"达到工具调用上限（{self.max_tool_calls} 次）时任务仍未完成")

            result = self.tools.call(action.name, action.args)
            ctx.tool_calls += 1
            self._observe(ctx, action, result)
            self._record(ctx, action.name, action.args, action.decision_basis,
                         {"ok": result.ok, "summary": result.summary,
                          "data": self._compact(action.name, result.data),
                          **({"error": result.error} if result.error else {})})

    # ---------- 工具结果 → 上下文 ----------
    def _observe(self, ctx: Context, action: Action, result) -> None:
        if not result.ok:
            ctx.tool_failures[action.name] = ctx.tool_failures.get(action.name, 0) + 1
        if action.name == "read_doc":
            if result.ok:
                ctx.read_docs[result.data["doc_id"]] = result.data
            elif "not_found" in (result.error or ""):
                ctx.failed_docs[str(action.args.get("doc_id", ""))] = result.error or "not_found"
        ctx.last_observation = {
            "action": action.name,
            "args": action.args,
            "ok": result.ok,
            "summary": result.summary,
            "data": result.data,
            "error": result.error,
        }

    def _record(self, ctx: Context, action: str, args: Any, basis: str, observation: Dict[str, Any]) -> None:
        ctx.steps.append({
            "step": len(ctx.steps) + 1,
            "action": action,
            "args": args,
            "decision_basis": basis,
            "observation": observation,
        })

    # ---------- 收尾 ----------
    def _finalize(self, ctx: Context, action: Action, plan_name: str) -> Optional[RunResult]:
        payload = dict(action.args)
        status = payload.get("status") or ("needs_clarification" if action.name == "ask_user" else "answered")
        citations = [
            Citation(doc_id=c.get("doc_id", ""), section=c.get("section", ""), quote=c.get("quote", ""))
            for c in payload.get("citations", [])
        ]
        validation = verify_answer(payload.get("answer"), [c.as_dict() for c in citations], self.store)
        if status in ("answered", "needs_clarification") and not citations:
            validation["ok"] = False
            validation["issues"].append("给出结论但没有任何引用，无法核验依据")

        # 引用不合格：把问题回灌给规划器，允许它重新取证一次
        if not validation["ok"] and status in ("answered", "needs_clarification") and ctx.citation_retries == 0:
            ctx.citation_retries = 1
            self._record(ctx, "validate_citations", [c.as_dict() for c in citations],
                         "输出前逐条核对引用是否为原文片段、答案中的量化结论是否有原文支撑",
                         {"ok": False, "summary": "引用校验未通过，拒绝输出并要求重新取证",
                          "issues": validation["issues"]})
            ctx.last_observation = {"action": "validate_citations", "args": {}, "ok": False,
                                    "issues": validation["issues"], "error": "citation_invalid"}
            return None

        if not validation["ok"] and status in ("answered", "needs_clarification"):
            status = "failed"
            payload["answer"] = None
            payload["citations"] = []
            payload["gaps"] = list(payload.get("gaps", [])) + ["引用校验失败，未能给出可核验的答案"]
            payload["reason"] = "citation_validation_failed: " + "；".join(validation["issues"])
            citations = []

        self._record(ctx, action.name, payload, action.decision_basis,
                     {"ok": True, "summary": f"任务结束：{status}"})
        ctx.steps[-1]["final"] = True
        return RunResult(
            status=status,
            answer=payload.get("answer"),
            citations=citations,
            gaps=list(payload.get("gaps", [])),
            question=ctx.question,
            history=ctx.history,
            steps=ctx.steps,
            planner=plan_name,
            tool_calls=ctx.tool_calls,
            reason=payload.get("reason"),
            validation=validation,
        )

    def _abort(self, ctx: Context, plan_name: str, reason: str, message: str) -> RunResult:
        self._record(ctx, "abort", {}, "达到执行上限，按约定返回明确的未完成状态，不输出未经验证的答案",
                     {"ok": False, "summary": reason})
        ctx.steps[-1]["final"] = True
        return RunResult(status="incomplete", answer=None, citations=[],
                         gaps=[message], question=ctx.question, history=ctx.history,
                         steps=ctx.steps, planner=plan_name, tool_calls=ctx.tool_calls,
                         reason=reason)

    # ---------- 观察压缩（Trace 只留摘要，完整数据留在上下文） ----------
    @staticmethod
    def _compact(name: str, data: Dict[str, Any]) -> Dict[str, Any]:
        if not data:
            return {}
        if name == "search_docs":
            return {
                "query": data.get("query"),
                "hits": [{k: h.get(k) for k in ("doc_id", "title", "section", "score", "coverage", "effective_date")}
                         for h in data.get("hits", [])],
            }
        if name == "read_doc":
            out = {k: data.get(k) for k in ("doc_id", "title", "version", "effective_date", "scope", "sections")}
            out["chars"] = len(data.get("text", ""))
            return out
        if name == "check_consistency":
            return {
                "patterns": data.get("patterns"),
                "conflicts": data.get("conflicts", []),
                "claims": data.get("claims", []),
                "hints": data.get("hints", []),
            }
        return data
