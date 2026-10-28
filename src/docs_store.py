"""文档加载：解析 front matter（文档 ID / 版本 / 生效日期 / 适用范围）与章节。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class Doc:
    doc_id: str
    title: str
    version: str
    effective_date: str
    scope: str
    owner: str
    sections: Dict[str, str]
    raw: str

    @property
    def full_text(self) -> str:
        parts = [f"## {name}\n{body}" for name, body in self.sections.items()]
        return "\n\n".join(parts)

    @property
    def meta(self) -> Dict[str, str]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "version": self.version,
            "effective_date": self.effective_date,
            "scope": self.scope,
            "owner": self.owner,
        }


def _parse_front_matter(text: str) -> (Dict[str, str], str):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: Dict[str, str] = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, parts[2].lstrip("\n")


def parse_doc(text: str, doc_id_fallback: str = "") -> Doc:
    meta, body = _parse_front_matter(text)
    sections: Dict[str, str] = {}
    current = "正文"
    buf: List[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if buf:
                sections[current] = "\n".join(buf).strip()
            current = line[3:].strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections[current] = "\n".join(buf).strip()
    return Doc(
        doc_id=meta.get("id", doc_id_fallback),
        title=meta.get("title", ""),
        version=meta.get("version", ""),
        effective_date=meta.get("effective_date", ""),
        scope=meta.get("scope", ""),
        owner=meta.get("owner", ""),
        sections={k: v for k, v in sections.items() if v},
        raw=text,
    )


class DocStore:
    """按文件名排序加载 data/docs/*.md，无数据库依赖。"""

    def __init__(self, docs_dir: Path):
        self.docs_dir = Path(docs_dir)
        self.docs: Dict[str, Doc] = {}
        self._load()

    def _load(self) -> None:
        for path in sorted(self.docs_dir.glob("*.md")):
            doc = parse_doc(path.read_text(encoding="utf-8"), path.stem)
            self.docs[doc.doc_id] = doc

    def get(self, doc_id: str) -> Optional[Doc]:
        return self.docs.get(doc_id)

    def all(self) -> Iterable[Doc]:
        return self.docs.values()

    def sections(self, doc_id: str) -> Dict[str, str]:
        doc = self.get(doc_id)
        return dict(doc.sections) if doc else {}

    def sentence_index(self) -> List[Dict[str, str]]:
        """把全部文档切成句子，供引用校验与断句使用。"""
        out: List[Dict[str, str]] = []
        for doc in self.docs.values():
            for name, body in doc.sections.items():
                for sent in split_sentences(body):
                    out.append({"doc_id": doc.doc_id, "section": name, "sentence": sent})
        return out


def split_sentences(text: str) -> List[str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[。！？])", text)
    out = []
    for part in parts:
        part = part.strip()
        if part:
            out.append(part)
    return out


def normalize(text: str) -> str:
    """去掉空白与全角空格，用于逐字引用比对。"""
    return re.sub(r"[\s\u3000]+", "", text)
