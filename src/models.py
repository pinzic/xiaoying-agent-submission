"""通用数据结构与路径配置。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DIR = DATA_DIR / "docs"
RULES_PATH = DATA_DIR / "rules.json"


def load_rules(path: Path = RULES_PATH) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


@dataclass
class Action:
    """规划器输出的一个动作。"""

    name: str
    args: Dict[str, Any] = field(default_factory=dict)
    decision_basis: str = ""


@dataclass
class ToolResult:
    ok: bool
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    summary: str = ""


@dataclass
class Citation:
    doc_id: str
    section: str
    quote: str

    def as_dict(self) -> Dict[str, str]:
        return {"doc_id": self.doc_id, "section": self.section, "quote": self.quote}


@dataclass
class Context:
    """规划器在一次运行中能看到的全部信息（不含模型内部思维过程）。"""

    question: str
    history: List[Dict[str, str]]
    steps: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    last_observation: Optional[Dict[str, Any]] = None
    tool_failures: Dict[str, int] = field(default_factory=dict)
    read_docs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    failed_docs: Dict[str, str] = field(default_factory=dict)
    citation_retries: int = 0


@dataclass
class RunResult:
    status: str
    answer: Optional[str]
    citations: List[Citation]
    gaps: List[str]
    question: str
    history: List[Dict[str, str]]
    steps: List[Dict[str, Any]]
    planner: str
    tool_calls: int
    reason: Optional[str] = None
    validation: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "answer": self.answer,
            "citations": [c.as_dict() for c in self.citations],
            "gaps": self.gaps,
            "question": self.question,
            "history": self.history,
            "planner": self.planner,
            "tool_calls": self.tool_calls,
            "reason": self.reason,
            "validation": self.validation,
            "steps": self.steps,
        }
