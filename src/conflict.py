"""冲突核查：抽取「同一事项的取值」，比对不同文档是否一致，并尝试用文档内依据定分。

关键约束（对应题目参考实例）：
- 只发现冲突、没有适用范围或优先级依据时，**不做推断**，返回 resolution=None，交给 Agent 追问；
- 定分依据只有两类：文档自身声明的适用范围命中问题，或文档正文写了「以本规定为准」这类优先条款；
- 生效日期更新的文档只作为 hint 提示，不作为自动胜出的依据。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .docs_store import Doc, DocStore, split_sentences


@dataclass
class Claim:
    doc_id: str
    key: str
    label: str
    value: float
    unit: str
    quote: str
    section: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "key": self.key,
            "label": self.label,
            "value": fmt_value(self.value),
            "unit": self.unit,
            "section": self.section,
            "quote": self.quote,
        }


def fmt_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def applicable_patterns(question: str, rules: Dict) -> List[Dict[str, Any]]:
    return [p for p in rules["claim_patterns"] if any(t in question for t in p["question_terms"])]


def extract_claims(doc: Doc, pattern: Dict[str, Any]) -> List[Claim]:
    claims: List[Claim] = []
    value_re = re.compile(pattern["value_regex"])
    negation_re = re.compile(pattern["negation_regex"]) if pattern.get("negation_regex") else None
    exclude_re = re.compile(pattern["exclude_regex"]) if pattern.get("exclude_regex") else None

    for section, body in doc.sections.items():
        for sent in split_sentences(body):
            if negation_re and negation_re.search(sent):
                claims.append(
                    Claim(doc.doc_id, pattern["key"], pattern["label"],
                          float(pattern.get("negation_value", 0)), pattern["unit"], sent, section)
                )
                continue
            if not any(word in sent for word in pattern["sentence_contains"]):
                continue
            if exclude_re and exclude_re.search(sent):
                continue
            for match in value_re.finditer(sent):
                claims.append(
                    Claim(doc.doc_id, pattern["key"], pattern["label"],
                          float(match.group(1)), pattern["unit"], sent, section)
                )
    # 同一句话内重复命中只保留一次
    seen, unique = set(), []
    for c in claims:
        key = (c.value, c.quote)
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


def _doc_brief(doc: Doc, claims: List[Claim]) -> Dict[str, Any]:
    return {
        "doc_id": doc.doc_id,
        "title": doc.title,
        "version": doc.version,
        "effective_date": doc.effective_date,
        "scope": doc.scope,
        "values": [fmt_value(v) for v in sorted({c.value for c in claims})],
        "unit": claims[0].unit if claims else "",
        "quotes": list(dict.fromkeys(c.quote for c in claims)),
        "items": [{"quote": q, "section": next(c.section for c in claims if c.quote == q)}
                  for q in dict.fromkeys(c.quote for c in claims)],
        "sections": list(dict.fromkeys(c.section for c in claims)),
    }


def _has_precedence(doc: Doc, rules: Dict) -> bool:
    return bool(re.search(rules["precedence_regex"], doc.full_text))


def detect(question: str, doc_ids: List[str], store: DocStore, rules: Dict) -> Dict[str, Any]:
    patterns = applicable_patterns(question, rules)
    docs = [store.get(d) for d in doc_ids]
    docs = [d for d in docs if d is not None]

    all_claims: List[Claim] = []
    conflicts: List[Dict[str, Any]] = []
    hints: List[str] = []

    for pattern in patterns:
        by_doc: Dict[str, List[Claim]] = {}
        for doc in docs:
            claims = extract_claims(doc, pattern)
            if claims:
                by_doc[doc.doc_id] = claims
                all_claims.extend(claims)
        if len(by_doc) < 2:
            continue

        # 任意两篇取值集合不相交 → 冲突
        involved = list(by_doc)
        for i in range(len(involved)):
            for j in range(i + 1, len(involved)):
                da, db = store.get(involved[i]), store.get(involved[j])
                va = {c.value for c in by_doc[da.doc_id]}
                vb = {c.value for c in by_doc[db.doc_id]}
                if va & vb or not va or not vb:
                    continue
                conflict = _build_conflict(pattern, da, db, by_doc, question, rules)
                if conflict["hint"]:
                    hints.append(conflict["hint"])
                conflicts.append(conflict)

    return {
        "patterns": [p["label"] for p in patterns],
        "claims": [c.as_dict() for c in all_claims],
        "conflicts": conflicts,
        "hints": list(dict.fromkeys(hints)),
    }


def _build_conflict(pattern: Dict[str, Any], da: Doc, db: Doc, by_doc: Dict[str, List[Claim]],
                    question: str, rules: Dict) -> Dict[str, Any]:
    scope_key, scope_words = None, []
    for key, words in rules.get("scope_map", {}).items():
        if any(w in question for w in words):
            scope_key, scope_words = key, words

    def scope_hit(doc: Doc) -> bool:
        if not scope_key:
            return False
        return scope_key in doc.scope or any(w in doc.title for w in scope_words)

    pa, pb = _has_precedence(da, rules), _has_precedence(db, rules)
    sa, sb = scope_hit(da), scope_hit(db)

    resolution = None
    if (sa and sb) or (pa and pb) or (sa and pb) or (sb and pa):
        # 依据互相矛盾或同时成立，无法单方定分
        resolution = None
    elif (sa and pa) or (sa and not pb and not sb):
        resolution = _resolution(da, sa, pa, scope_key)
    elif (sb and pb) or (sb and not pa and not sa):
        resolution = _resolution(db, sb, pb, scope_key)
    # 仅凭「以本规定为准」但没有适用范围的交叉，不足以对未说明身份的问题定分

    hint = None
    if resolution is None and da.effective_date != db.effective_date:
        newer = da if da.effective_date > db.effective_date else db
        hint = (
            f"{newer.doc_id} 的生效日期更新（{newer.effective_date}），但正文未声明替代或废止另一份制度，"
            "不能据此自动判定优先级。"
        )

    return {
        "key": pattern["key"],
        "label": pattern["label"],
        "unit": pattern["unit"],
        "doc_a": _doc_brief(da, by_doc[da.doc_id]),
        "doc_b": _doc_brief(db, by_doc[db.doc_id]),
        "resolution": resolution,
        "hint": hint,
        "scope_key": scope_key,
    }


def _resolution(doc: Doc, scope_match: bool, precedence: bool, scope_key: Optional[str]) -> Dict[str, Any]:
    reasons = []
    if scope_match:
        reasons.append(f"问题限定的范围是「{scope_key}」，{doc.doc_id} 的适用范围（{doc.scope}）覆盖该问题")
    if precedence:
        reasons.append(f"{doc.doc_id} 正文明确「{doc.title}」与其他制度不一致时以本规定为准")
    return {
        "doc_id": doc.doc_id,
        "type": "+".join(filter(None, ["scope" if scope_match else "", "explicit_precedence" if precedence else ""])),
        "reason": "；".join(reasons),
    }
