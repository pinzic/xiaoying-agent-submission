"""重新生成 examples/ 下的运行材料（输入、实际输出、Trace）。

用法：python -m scripts.gen_examples
"""
from __future__ import annotations

import json

from src.agent import Agent
from src.docs_store import DocStore
from src.models import DOCS_DIR, ROOT, load_rules

EXAMPLES = ROOT / "examples"

CASES = [
    {
        "dir": "case1_clear_answer",
        "question": "员工加班后调休的有效期是多久？",
        "history": [],
        "faults": {},
        "max_steps": 8,
        "note": "正常路径：命中单一制度，读取原文后直接作答并给出逐字引用。",
    },
    {
        "dir": "case2_doc_conflict",
        "question": "报销期限是多久？",
        "history": [],
        "faults": {},
        "max_steps": 8,
        "note": "文档冲突：D01（30 天）与 D02（15 天）都适用且无优先条款，列出冲突与引用并请求确认。",
    },
    {
        "dir": "case3_out_of_scope",
        "question": "公司可以帮我办理日本签证吗？",
        "history": [],
        "faults": {},
        "max_steps": 8,
        "note": "资料外问题：两轮检索零命中，返回 insufficient_info 与资料缺口，不给答案。",
    },
    {
        "dir": "case4_paraphrase",
        "question": "加班换来的补休，最晚多久要休掉？",
        "history": [],
        "faults": {},
        "max_steps": 8,
        "note": "改写问句：口语「补休/最晚多久」经同义词扩展仍命中 D04，答案与标准问法一致。",
    },
    {
        "dir": "case5_followup",
        "question": "那要提前几天申请？",
        "history": [
            {"role": "user", "content": "正式员工的年假有多少天？"},
            {"role": "assistant", "content": "根据 D05，年假按工龄分为 5/10/15 天。"},
        ],
        "faults": {},
        "max_steps": 8,
        "note": "上下文追问：问题本身没有「年假」，Agent 用上一轮问题补全检索词后命中 D05 的申请时限。",
    },
    {
        "dir": "case6_tool_failure_recover",
        "question": "报销期限是多久？",
        "history": [],
        "faults": {"search_docs": 1},
        "max_steps": 8,
        "note": "工具失败（可恢复）：首次检索超时，改用更短关键词重试成功，最终仍正确处理冲突。",
    },
    {
        "dir": "case7_tool_failure_terminal",
        "question": "报销期限是多久？",
        "history": [],
        "faults": {"search_docs": 2},
        "max_steps": 8,
        "note": "工具失败（不可恢复）：两次检索均超时，返回 failed 并说明工具不可用，不猜测答案。",
    },
    {
        "dir": "case8_step_limit",
        "question": "报销期限是多久？",
        "history": [],
        "faults": {},
        "max_steps": 2,
        "note": "执行上限：把步数预算压到 2 步，任务在读原文之前就被中止，返回 incomplete 且不输出未验证答案。",
    },
    {
        "dir": "case9_tool_call_limit",
        "question": "报销期限是多久？",
        "history": [],
        "faults": {},
        "max_steps": 8,
        "max_tool_calls": 1,
        "note": "工具调用上限：只允许 1 次工具调用，检索完成后无法读原文，返回 incomplete 并说明是哪种上限。",
    },
]


def main() -> None:
    rules = load_rules()
    store = DocStore(DOCS_DIR)
    for case in CASES:
        out_dir = EXAMPLES / case["dir"]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "input.json").write_text(
            json.dumps({
                "question": case["question"],
                "history": case["history"],
                "faults": case["faults"],
                "max_steps": case.get("max_steps", 8),
                "max_tool_calls": case.get("max_tool_calls", 6),
                "note": case["note"],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        agent = Agent(store, rules, max_steps=case.get("max_steps", 8),
                      max_tool_calls=case.get("max_tool_calls", 6), faults=case["faults"])
        result = agent.run(case["question"], case["history"]).as_dict()
        (out_dir / "output.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        with (out_dir / "trace.jsonl").open("w", encoding="utf-8") as fh:
            for step in result["steps"]:
                fh.write(json.dumps(step, ensure_ascii=False) + "\n")
        print(f"{case['dir']}: {result['status']} ({len(result['steps'])} steps / {result['tool_calls']} tool calls)")


if __name__ == "__main__":
    main()
