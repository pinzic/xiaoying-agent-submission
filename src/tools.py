"""工具层：检索、读原文、冲突核查。业务工具用本地函数实现（题目允许 Mock）。

故障注入（AGENT_FAULTS / CLI --fault）用于验证「工具失败」场景：
    首次调用某工具时按配置返回异常，观察 Agent 是否降级重试或如实报告失败。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .conflict import detect
from .docs_store import DocStore
from .models import ToolResult
from .retrieval import Retriever

TOOL_SPECS = [
    {
        "name": "search_docs",
        "description": "在制度/FAQ 库中按关键词检索，返回命中文档的 ID、标题、版本、生效日期、适用范围与片段摘要（不是全文）。",
        "args": {"query": "检索关键词", "top_k": "返回条数，默认 5"},
    },
    {
        "name": "read_doc",
        "description": "按文档 ID 读取原文；可指定 section 只读某一节。引用必须来自这里读到的原文。",
        "args": {"doc_id": "文档 ID，如 D01", "section": "可选，章节名"},
    },
    {
        "name": "check_consistency",
        "description": "对已读取的若干文档做同一事项的取值一致性核查，返回冲突列表、原文引用、是否可用适用范围或优先条款定分。",
        "args": {"question": "用户问题", "doc_ids": "参与比对的文档 ID 列表"},
    },
]


class Tools:
    def __init__(self, store: DocStore, retriever: Retriever, rules: Dict[str, Any],
                 faults: Optional[Dict[str, int]] = None):
        self.store = store
        self.retriever = retriever
        self.rules = rules
        self.faults = dict(faults or {})
        self.calls: List[Dict[str, Any]] = []

    # ---------- 入口 ----------
    def call(self, name: str, args: Dict[str, Any]) -> ToolResult:
        self.calls.append({"tool": name, "args": args})
        if self.faults.get(name, 0) > 0:
            self.faults[name] -= 1
            return ToolResult(ok=False, error=f"tool_error: {name} 调用超时（upstream_timeout）",
                              summary=f"{name} 调用失败：服务超时，未返回数据")
        handler = {
            "search_docs": self._search_docs,
            "read_doc": self._read_doc,
            "check_consistency": self._check_consistency,
        }.get(name)
        if handler is None:
            return ToolResult(ok=False, error=f"unknown_tool: {name}", summary=f"未知工具 {name}")
        try:
            return handler(args)
        except TypeError as exc:
            return ToolResult(ok=False, error=f"bad_arguments: {exc}", summary=f"{name} 参数不合法：{exc}")
        except Exception as exc:  # noqa: BLE001 - 工具异常必须回传给模型观察
            return ToolResult(ok=False, error=f"tool_exception: {exc}", summary=f"{name} 执行异常：{exc}")

    # ---------- 具体工具 ----------
    def _search_docs(self, args: Dict[str, Any]) -> ToolResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolResult(ok=False, error="bad_arguments: query 不能为空", summary="检索词为空，未执行检索")
        top_k = int(args.get("top_k", self.rules["retrieval"]["top_k"]))
        hits = self.retriever.search(query, top_k=top_k)
        data = {"query": query, "hits": [h.__dict__ for h in hits]}
        if not hits:
            return ToolResult(ok=True, data=data, summary=f"检索「{query}」命中 0 篇文档")
        summary = f"检索「{query}」命中 {len(hits)} 篇：" + "、".join(
            f"{h.doc_id}({h.section}, score={h.score})" for h in hits
        )
        return ToolResult(ok=True, data=data, summary=summary)

    def _read_doc(self, args: Dict[str, Any]) -> ToolResult:
        doc_id = str(args.get("doc_id", "")).strip()
        doc = self.store.get(doc_id)
        if doc is None:
            return ToolResult(ok=False, error=f"not_found: 文档 {doc_id} 不存在",
                              summary=f"读取失败：文档 {doc_id} 不存在")
        section = args.get("section")
        if section:
            if section not in doc.sections:
                return ToolResult(ok=False, error=f"not_found: {doc_id} 无章节「{section}」",
                                  summary=f"读取失败：{doc_id} 无章节「{section}」")
            sections = {section: doc.sections[section]}
        else:
            sections = dict(doc.sections)
        text = "\n\n".join(f"## {k}\n{v}" for k, v in sections.items())
        data = {**doc.meta, "sections": list(sections), "section_texts": sections, "text": text}
        return ToolResult(ok=True, data=data,
                          summary=f"读取 {doc_id}《{doc.title}》{doc.version}（{doc.effective_date} 生效，{doc.scope}），"
                                  f"章节：{'、'.join(sections)}")

    def _check_consistency(self, args: Dict[str, Any]) -> ToolResult:
        question = str(args.get("question", "")).strip()
        doc_ids = args.get("doc_ids") or []
        if not isinstance(doc_ids, list) or len(doc_ids) < 2:
            return ToolResult(ok=False, error="bad_arguments: 至少需要两个 doc_id",
                              summary="一致性核查需要至少两篇文档")
        missing = [d for d in doc_ids if self.store.get(d) is None]
        if missing:
            return ToolResult(ok=False, error=f"not_found: {missing}", summary=f"核查失败：文档不存在 {missing}")
        result = detect(question, [str(d) for d in doc_ids], self.store, self.rules)
        n = len(result["conflicts"])
        if n == 0:
            summary = f"比对了 {len(doc_ids)} 篇文档的「{('、'.join(result['patterns']) or '相关事项')}」，未发现取值冲突"
        else:
            unresolved = [c for c in result["conflicts"] if not c["resolution"]]
            summary = f"发现 {n} 处冲突，其中 {len(unresolved)} 处无法用适用范围/优先条款定分"
        return ToolResult(ok=True, data=result, summary=summary)
