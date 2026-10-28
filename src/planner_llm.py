"""真实模型规划器：OpenAI 兼容的 /chat/completions 接口 + JSON 动作协议。

不使用框架的 function calling，是为了让「工具协议 / 循环 / 上限 / 引用校验」全部显式可见，
也便于在没有 SDK 的环境里跑（只用标准库 urllib）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .models import Action, Context

SYSTEM_PROMPT = """你是企业内部的制度与产品问答助手，服务对象是公司员工。你必须只依据检索到的文档回答。

## 可用工具
1. search_docs {"query": "关键词", "top_k": 5}
   - 返回命中文档的 ID、标题、版本、生效日期、适用范围和片段摘要（不是全文）。
2. read_doc {"doc_id": "D01"}  可选 "section"
   - 读取原文。引用必须逐字来自这里返回的文本。
3. check_consistency {"question": "用户问题", "doc_ids": ["D01","D02"]}
   - 对已读文档做同一事项的取值核查，返回冲突、原文引用、是否可用适用范围/优先条款定分。

## 输出格式
每次只输出一个 JSON 对象，不要输出解释性文字或 Markdown 代码块：
{"action": "search_docs", "args": {...}, "decision_basis": "一句话说明为什么做这个动作"}

结束任务时用：
{"action": "finish", "args": {"status": "answered", "answer": "回答正文", "citations": [{"doc_id": "D04", "section": "调休有效期", "quote": "原文逐字片段"}], "gaps": [], "reason": "结论依据"}, "decision_basis": "..."}
或需要用户补充信息时：
{"action": "ask_user", "args": {"status": "needs_clarification", "answer": "要向用户提出的问题", "citations": [...], "gaps": [...], "reason": "..."}, "decision_basis": "..."}

## 硬性规则
1. 每条 citation 的 quote 必须是 read_doc 原文的逐字片段（可含标点），不得改写、概括或拼接。
2. 只有读过原文才能引用；不能引用只出现在检索摘要里的内容。
3. 若两篇文档对同一事项给出不同数值，且都没有写明适用范围差异或「以谁为准」，必须 ask_user，
   在 answer 中列出两份文档的 ID、版本、原值和原文，并请用户确认适用哪一份；禁止自行选一个。
4. 若差异能由文档内的适用范围或优先条款解释，可以作答，但必须在 answer 中说明冲突与定分依据。
5. 检索零命中：可换一次更宽的关键词；仍无命中则 finish，status=insufficient_info，answer=null。
6. 工具报错：最多重试一次；仍失败则 finish，status=failed，说明工具不可用，禁止猜测。
7. answer 中出现的所有天数/月份/金额等数字，必须能在 citation 原文中找到。
8. 不要编造文档 ID 或原文。信息不足时，把缺口写进 gaps。"""


class LLMPlanner:
    name = "llm"

    def __init__(self, rules: Dict[str, Any], model: Optional[str] = None,
                 base_url: Optional[str] = None, api_key: Optional[str] = None,
                 timeout: int = 60):
        self.rules = rules
        self.model = model or os.environ.get("LLM_MODEL", "")
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.timeout = timeout

    @classmethod
    def available(cls) -> bool:
        return bool(os.environ.get("LLM_API_KEY") and os.environ.get("LLM_BASE_URL")
                    and os.environ.get("LLM_MODEL"))

    # ---------- 决策 ----------
    def decide(self, ctx: Context) -> Action:
        try:
            text = self._chat(self._build_user(ctx))
        except Exception as exc:  # noqa: BLE001
            return Action("__invalid__", {"error": f"model_call_failed: {exc}"},
                          "模型调用失败，本轮无法决策")
        data = _extract_json(text)
        if not data or "action" not in data:
            return Action("__invalid__", {"error": f"unparsable_output: {text[:200]}"},
                          "模型输出不是合法 JSON 动作")
        name = str(data.get("action", ""))
        if name not in ("search_docs", "read_doc", "check_consistency", "finish", "ask_user"):
            return Action("__invalid__", {"error": f"unknown_action: {name}"},
                          "模型输出了未定义的动作")
        args = data.get("args") or {}
        if not isinstance(args, dict):
            return Action("__invalid__", {"error": "args_not_object"}, "动作参数不是对象")
        return Action(name, args, str(data.get("decision_basis", ""))[:200])

    # ---------- 提示词 ----------
    def _build_user(self, ctx: Context) -> str:
        lines: List[str] = []
        if ctx.history:
            lines.append("## 对话历史")
            for turn in ctx.history[-6:]:
                lines.append(f"{turn.get('role', 'user')}: {turn.get('content', '')}")
        lines.append("## 当前问题")
        lines.append(ctx.question)
        lines.append("")
        lines.append("## 已执行的步骤")
        if not ctx.steps:
            lines.append("（尚无，先检索）")
        for step in ctx.steps:
            obs = step.get("observation", {})
            data = obs.get("data") or {}
            lines.append(
                f"{step['step']}. action={step['action']} args={json.dumps(step.get('args', {}), ensure_ascii=False)}"
                f" -> ok={obs.get('ok')} summary={obs.get('summary')}"
            )
            if data.get("hits"):
                lines.append("   hits=" + json.dumps(data["hits"], ensure_ascii=False))
            if data.get("conflicts") is not None:
                lines.append("   consistency=" + json.dumps(
                    {"conflicts": data.get("conflicts"), "claims": data.get("claims", [])[:8]},
                    ensure_ascii=False))
            if data.get("doc_id") and data.get("sections"):
                lines.append(f"   已读原文：{data['doc_id']} 章节={data['sections']}")
        lines.append("")
        lines.append(f"## 预算\n已用步数 {len(ctx.steps)}/{getattr(ctx, 'max_steps', 8)}，"
                     f"已用工具调用 {ctx.tool_calls}。请给出下一步动作（只输出 JSON）。")
        return "\n".join(lines)

    def _chat(self, user: str) -> str:
        if not self.api_key or not self.base_url or not self.model:
            raise RuntimeError("缺少 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body["choices"][0]["message"]["content"]


def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}
