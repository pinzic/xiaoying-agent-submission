"""答案组装与引用校验。

两条硬约束：
1. 每条引用必须是原文逐字片段（归一化空白后子串匹配），否则视为错误引用，拒绝输出；
2. 答案里出现的量化结论（天数/月份/金额等）必须能在引用原文中找到，防止「引用对但结论加料」。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from .docs_store import DocStore, normalize
from .models import Citation
from .retrieval import content_terms

NUM_UNITS = ("个工作日", "个月", "小时", "天", "元", "次")


def _as_number(value: Any) -> float:
    """把取值字符串转成可比较的数值；非数字退化为 0，保证排序不抛异常。"""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def _value_text(doc: Dict[str, Any]) -> str:
    """把取值渲染成自然语言；0 天这类取值改写成「不享受」，避免『为 0 天』的别扭表达。"""
    if doc.get("values") == ["0"]:
        return "不享受"
    return "、".join(doc.get("values", [])) + f" {doc.get('unit', '')}".rstrip()


class Answerer:
    def __init__(self, store: DocStore, rules: Dict[str, Any]):
        self.store = store
        self.rules = rules
        self.stopwords = rules["stopwords"]
        self.synonyms = rules.get("synonyms", {})

    def _expand(self, query: str) -> str:
        """选句时也用同义词扩展，避免「补休」「报销要几天内交」这类口语说法漏掉制度用语。"""
        extra = []
        for key, variants in self.synonyms.items():
            if key in query or any(v in query for v in variants):
                extra.append(key)
                extra.extend(variants)
        return query + " " + " ".join(dict.fromkeys(extra)) if extra else query

    # ---------- 选句 ----------
    def best_support(self, doc: Dict[str, Any], query: str) -> Tuple[str, str, float]:
        qterms = set(content_terms(self._expand(query), self.stopwords))
        asks_duration = any(k in query for k in
                            ("多久", "多长", "最晚", "期限", "时限", "有效期", "几天", "多少天", "什么时候"))
        best: Tuple[str, str, float] = ("", "", -1.0)
        for section, body in doc.get("section_texts", {}).items():
            head_terms = set(content_terms(section, self.stopwords))
            for sent in re.split(r"(?<=[。！？])", re.sub(r"\s+", " ", body)):
                sent = sent.strip()
                if not sent:
                    continue
                score = len(qterms & set(content_terms(sent, self.stopwords))) + 0.5 * len(qterms & head_terms)
                if asks_duration and re.search(r"\d+(?:\.\d+)?\s*(天|个月|年|小时|个工作日)", sent):
                    score += 1.5  # 问「多久/期限」时，优先带明确时限的句子
                if score > best[2]:
                    best = (section, sent, score)
        return best

    # ---------- 正常作答 ----------
    def compose_clear(self, query: str, docs: List[Dict[str, Any]],
                      claims: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        doc = docs[0]
        section, sentence, score = self.best_support(doc, query)
        if score <= 0:
            section = next(iter(doc.get("section_texts", {})), "")
            sentence = (doc.get("section_texts", {}).get(section, "") or "")[:120]
        citations = [Citation(doc["doc_id"], section, sentence)]
        answer = (f"根据 {doc['doc_id']}《{doc['title']}》（{doc['version']}，{doc['effective_date']} 生效）"
                  f"第「{section}」节：「{sentence.rstrip('。；')}」")

        # 同一份文档对同一事项给出多档取值（如按工龄分档的年假），一并列出，避免只答一半
        siblings = [c for c in (claims or []) if c["doc_id"] == doc["doc_id"] and c["quote"] == sentence]
        if siblings:
            key = siblings[0]["key"]
            all_claims, seen_values = [], set()
            for c in [x for x in (claims or []) if x["doc_id"] == doc["doc_id"] and x["key"] == key]:
                if c["value"] in seen_values:
                    continue
                seen_values.add(c["value"])
                all_claims.append(c)
            # 按数值升序（"10" < "5" 的字典序陷阱会让 5/10/15 排成 10、15、5）
            all_claims.sort(key=lambda x: _as_number(x["value"]))
            extra = [c for c in all_claims if c["quote"] != sentence]
            for c in extra:
                citations.append(Citation(doc["doc_id"], c["section"], c["quote"]))
            if extra:
                values = "、".join(f"{c['value']} {c['unit']}" for c in all_claims)
                answer += f"；同一制度下按适用人群分档为 {values}，需按员工实际情况对应。"
            else:
                answer += f"，即 {siblings[0]['value']} {siblings[0]['unit']}。"
        else:
            answer += "。"

        return {
            "status": "answered",
            "answer": answer,
            "citations": [c.as_dict() for c in citations],
            "gaps": [],
            "reason": "候选文档对同一事项取值一致，直接依据原文作答",
        }

    # ---------- 冲突已定分 ----------
    def compose_resolved(self, conflict: Dict[str, Any], resolution: Dict[str, Any],
                         docs: List[Dict[str, Any]]) -> Dict[str, Any]:
        label = conflict["label"]
        a, b = conflict["doc_a"], conflict["doc_b"]
        winner = a if resolution["doc_id"] == a["doc_id"] else b
        loser = b if winner is a else a
        win_doc = next((d for d in docs if d["doc_id"] == winner["doc_id"]), None)
        answer = (
            f"检索到两份制度对「{label}」的规定不一致："
            f"{a['doc_id']}《{a['title']}》（{a['version']}，{a['effective_date']} 生效，适用范围：{a['scope']}）"
            f"规定 {_value_text(a)}；"
            f"{b['doc_id']}《{b['title']}》（{b['version']}，{b['effective_date']} 生效，适用范围：{b['scope']}）"
            f"规定 {_value_text(b)}。"
            f"按 {winner['doc_id']} 的依据——{resolution['reason']}——本问题应适用 {winner['doc_id']}："
            f"{_value_text(winner)}。"
            f"（{loser['doc_id']} 仍在有效期内，但按上述适用范围/优先条款不适用于本问题。）"
        )
        if win_doc is None:
            answer += " 建议向制度归口部门复核。"
        citations = []
        for item in winner.get("items", []):
            citations.append(Citation(winner["doc_id"], item["section"], item["quote"]))
        for item in loser.get("items", []):
            citations.append(Citation(loser["doc_id"], item["section"], item["quote"]))
        return {
            "status": "answered",
            "answer": answer,
            "citations": [c.as_dict() for c in citations],
            "gaps": [],
            "reason": "存在冲突，但文档内的适用范围或优先条款足以定分，结论同时披露两份依据",
        }

    # ---------- 冲突未决 → 追问 ----------
    def compose_clarification(self, conflict: Dict[str, Any], question: str) -> Dict[str, Any]:
        a, b = conflict["doc_a"], conflict["doc_b"]
        hint = f"（补充线索：{conflict['hint']}）" if conflict.get("hint") else ""
        if a["scope"] != b["scope"]:
            why = (f"两份制度的适用范围不同（{a['doc_id']}：{a['scope']}；{b['doc_id']}：{b['scope']}），"
                   f"但问题里没有说明提问人属于哪一类")
            ask = f"请确认你属于哪一类（{a['scope']} 还是 {b['scope']}），我再按对应制度回答。"
        else:
            why = "两份制度都没有写明适用范围差异或「以谁为准」"
            ask = ("请确认你的情况适用哪一份（例如：本次是日常费用还是差旅费用？费用发生在哪一天？），"
                   "或联系制度管理组按 D09 裁定后再答复。")
        q = (
            f"关于「{conflict['label']}」，我在知识库中检索到两份制度给出了不同要求：\n"
            f"· {a['doc_id']}《{a['title']}》（{a['version']}，{a['effective_date']} 生效，适用范围：{a['scope']}）："
            f"{_value_text(a)}\n"
            f"· {b['doc_id']}《{b['title']}》（{b['version']}，{b['effective_date']} 生效，适用范围：{b['scope']}）："
            f"{_value_text(b)}\n"
            f"{why}，因此我不能替你选一个答案。{hint}{ask}"
        )
        citations = [Citation(item["doc_id"], item["section"], item["quote"]) for item in (
            [{"doc_id": a["doc_id"], **i} for i in a.get("items", [])] +
            [{"doc_id": b["doc_id"], **i} for i in b.get("items", [])]
        )]
        return {
            "status": "needs_clarification",
            "answer": q,
            "citations": [c.as_dict() for c in citations],
            "gaps": [f"缺少说明 {a['doc_id']} 与 {b['doc_id']} 适用范围/优先级的依据"],
            "reason": "存在未决冲突且文档内无适用范围或优先条款，按规则必须向用户确认",
        }

    # ---------- 资料不足 ----------
    @staticmethod
    def compose_insufficient(question: str) -> Dict[str, Any]:
        return {
            "status": "insufficient_info",
            "answer": None,
            "citations": [],
            "gaps": [f"知识库中未检索到与「{question}」相关的制度或 FAQ 条目"],
            "reason": "两轮检索均无命中，无法给出有依据的答案，按规则不猜测",
        }


def verify_answer(answer: Optional[str], citations: List[Dict[str, str]], store: DocStore) -> Dict[str, Any]:
    issues: List[str] = []
    quotes: List[str] = []
    for c in citations:
        doc = store.get(c.get("doc_id", ""))
        if doc is None:
            issues.append(f"引用文档 {c.get('doc_id')} 不存在")
            continue
        quote = c.get("quote", "")
        if len(normalize(quote)) < 8:
            issues.append(f"{c.get('doc_id')} 的引用过短，无法支撑结论：{quote[:20]}")
        if normalize(quote) not in normalize(doc.raw):
            issues.append(f"{c.get('doc_id')} 的引用未在原文中逐字出现：{quote[:40]}")
        quotes.append(normalize(quote))

    if answer:
        joined = "".join(quotes)
        units = "|".join(NUM_UNITS)
        for value, unit in re.findall(rf"(\d+(?:\.\d+)?)\s*({units})", answer):
            if f"{value}{unit}" not in joined:
                issues.append(f"答案中的量化结论「{value} {unit}」在引用原文中找不到依据")

    return {"ok": not issues, "issues": issues, "checked_citations": len(citations)}
