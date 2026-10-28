"""CLI 入口：python -m src.cli --question "..." """
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .docs_store import DocStore
from .models import DOCS_DIR, load_rules
from .planner_llm import LLMPlanner


def parse_faults(items):
    faults = {}
    for item in items or []:
        name, _, count = item.partition(":")
        faults[name] = int(count or 1)
    return faults


def load_history(path):
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("history", [])
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(prog="src.cli", description="企业知识问答 Agent（CLI）")
    parser.add_argument("--question", required=True, help="用户问题")
    parser.add_argument("--history", help="对话历史 JSON 文件（[{role,content}, ...]）")
    parser.add_argument("--planner", choices=["auto", "mock", "llm"], default="auto")
    parser.add_argument("--docs-dir", default=str(DOCS_DIR))
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=6)
    parser.add_argument("--fault", action="append", default=[], help="故障注入，如 search_docs:2")
    parser.add_argument("--out", help="输出目录：写 output.json 与 trace.jsonl")
    parser.add_argument("--quiet", action="store_true", help="只写文件不打印")
    args = parser.parse_args(argv)
    if not args.question.strip():
        parser.error("--question 不能为空：没有问题时无法检索，也就无法给出有依据的答案")

    rules = load_rules()
    store = DocStore(Path(args.docs_dir))

    if args.planner == "llm" or (args.planner == "auto" and LLMPlanner.available()):
        planner = LLMPlanner(rules)
        if args.planner == "llm" and not LLMPlanner.available():
            print("未配置 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL，无法使用 llm 规划器", file=sys.stderr)
            return 2
    else:
        planner = None  # Agent 内部默认 mock
        if args.planner == "auto":
            print("[提示] 未检测到模型配置，使用离线 Mock 规划器（仅验证流程与工程约束）", file=sys.stderr)

    agent = Agent(store, rules, planner=planner,
                  max_steps=args.max_steps, max_tool_calls=args.max_tool_calls,
                  faults=parse_faults(args.fault))
    result = agent.run(args.question, load_history(args.history))

    payload = result.as_dict()
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "output.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with (out_dir / "trace.jsonl").open("w", encoding="utf-8") as fh:
            for step in payload["steps"]:
                fh.write(json.dumps(step, ensure_ascii=False) + "\n")
    if not args.quiet:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
