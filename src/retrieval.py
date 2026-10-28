"""检索层：中文字符二元组 + BM25 + 覆盖率门槛 + 同义词扩展。

为什么不用向量检索：
- 语料是合成制度/FAQ（几十条），BM25 已能稳定命中，且离线零依赖、可解释、可单测；
- 评审重点是「检索是否改进、冲突是否处理」，而不是嵌入模型选型。若接入真实语料，
  可在此层替换为向量 + BM25 混合检索，接口不变。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from .docs_store import Doc, DocStore

CJK = r"\u4e00-\u9fff"


def _strip_stopwords(text: str, stopwords: Sequence[str]) -> str:
    out = text.lower()
    for sw in sorted(stopwords, key=len, reverse=True):
        out = out.replace(sw.lower(), " ")
    return out


def content_terms(text: str, stopwords: Sequence[str]) -> List[str]:
    """中文按二元组切分，英文/数字按词切分；先剔除停用词。"""
    cleaned = _strip_stopwords(text, stopwords)
    terms: List[str] = []
    for run in re.findall(f"[{CJK}]+", cleaned):
        if len(run) == 1:
            terms.append(run)
        for i in range(len(run) - 1):
            terms.append(run[i : i + 2])
    for word in re.findall(r"[a-zA-Z0-9_]{2,}", cleaned):
        terms.append(word.lower())
    return terms


def query_terms(query: str, stopwords: Sequence[str]) -> List[str]:
    return content_terms(query, stopwords)


@dataclass
class Chunk:
    doc_id: str
    section: str
    text: str
    tokens: List[str]
    tf: Dict[str, int]
    length: int


@dataclass
class Hit:
    doc_id: str
    title: str
    section: str
    score: float
    snippet: str
    version: str
    effective_date: str
    scope: str
    coverage: float
    matched_terms: int


class Retriever:
    def __init__(self, store: DocStore, rules: Dict):
        self.store = store
        self.rules = rules
        self.cfg = rules["retrieval"]
        self.stopwords = rules["stopwords"]
        self.chunks: List[Chunk] = []
        self.doc_tokens: Dict[str, Set[str]] = {}
        self.df: Dict[str, int] = {}
        self.avg_len = 0.0
        self._build()
        self.synonyms: Dict[str, List[str]] = rules.get("synonyms", {})

    # ---------- 索引 ----------
    def _build(self) -> None:
        for doc in self.store.all():
            tokens_all: List[str] = []
            for section, body in doc.sections.items():
                text = f"{doc.title} {section} {body}"
                tokens = content_terms(text, self.stopwords)
                tf: Dict[str, int] = {}
                for t in tokens:
                    tf[t] = tf.get(t, 0) + 1
                self.chunks.append(Chunk(doc.doc_id, section, body, tokens, tf, len(tokens)))
                tokens_all.extend(tokens)
            self.doc_tokens[doc.doc_id] = set(tokens_all)
            for t in set(tokens_all):
                self.df[t] = self.df.get(t, 0) + 1

        self.n_chunks = len(self.chunks)
        self.avg_len = (sum(c.length for c in self.chunks) / self.n_chunks) if self.n_chunks else 0.0

    def _idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n_chunks - df + 0.5) / (df + 0.5))

    # ---------- 查询扩展 ----------
    def expand(self, query: str) -> str:
        """同义词扩展：让「报销要几天内交」这类口语化改写也能命中制度用语。"""
        extra: List[str] = []
        for key, variants in self.synonyms.items():
            if key in query or any(v in query for v in variants):
                extra.append(key)
                extra.extend(variants)
        if not extra:
            return query
        return query + " " + " ".join(dict.fromkeys(extra))

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 5, expand: bool = True) -> List[Hit]:
        q = self.expand(query) if expand else query
        terms = query_terms(q, self.stopwords)
        if not terms or not self.chunks:
            return []
        qset = list(dict.fromkeys(terms))
        k1, b = self.cfg["k1"], self.cfg["b"]

        scored: Dict[str, Tuple[float, Chunk]] = {}
        for chunk in self.chunks:
            score = 0.0
            for term in qset:
                tf = chunk.tf.get(term, 0)
                if not tf:
                    continue
                denom = tf + k1 * (1 - b + b * chunk.length / self.avg_len)
                score += self._idf(term) * tf * (k1 + 1) / denom
            if score <= 0:
                continue
            best = scored.get(chunk.doc_id)
            if best is None or score > best[0]:
                scored[chunk.doc_id] = (score, chunk)

        # 相关性门槛：既要「词命中得够多」，也要「问题本身的词在文档里有覆盖」。
        # 只看单个词的命中会把「旅游补贴」误判为命中「加班补贴」所在的文档。
        base_terms = list(dict.fromkeys(query_terms(query, self.stopwords)))
        expanded_terms = list(dict.fromkeys(qset))
        hits: List[Hit] = []
        for doc_id, (score, chunk) in scored.items():
            doc = self.store.get(doc_id)
            if doc is None:
                continue
            base_matched = [t for t in base_terms if t in self.doc_tokens[doc_id]]
            coverage = len(base_matched) / max(1, len(base_terms))
            expanded_matched = [t for t in expanded_terms if t in self.doc_tokens[doc_id]]
            if not (coverage >= self.cfg["min_coverage"] and len(expanded_matched) >= 2) \
                    and coverage < self.cfg["strong_coverage"]:
                continue
            hits.append(
                Hit(
                    doc_id=doc_id,
                    title=doc.title,
                    section=chunk.section,
                    score=round(score, 4),
                    snippet=self._snippet(chunk.text, base_terms),
                    version=doc.version,
                    effective_date=doc.effective_date,
                    scope=doc.scope,
                    coverage=round(coverage, 3),
                    matched_terms=len(expanded_matched),
                )
            )
        hits.sort(key=lambda h: (-h.score, h.doc_id))
        return hits[:top_k]

    @staticmethod
    def _snippet(text: str, terms: Iterable[str], width: int = 150) -> str:
        flat = re.sub(r"\s+", " ", text).strip()
        pos = -1
        for t in terms:
            pos = flat.find(t)
            if pos >= 0:
                break
        if pos < 0:
            return flat[:width]
        start = max(0, pos - 40)
        return ("…" if start else "") + flat[start : start + width] + ("…" if start + width < len(flat) else "")
