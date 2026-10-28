"""文档扰动（ablation）实验：替换文档内容，核对答案与动作是否随之改变。

对应审核要点「替换文档内容，核对答案与动作是否改变」——
如果改了原文而结论不变，说明答案不是从原文推出来的。

每次实验把 data/docs 复制到临时目录，改其中一份，再跑同一个问题，
把「改动 / 期望变化 / 实际变化」写成 examples/ablation_report.md。

用法：python -m scripts.run_ablation
"""
from __future__ import annotations

import shutil
import tempfile
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from src.agent import Agent
from src.docs_store import DocStore
from src.models import DOCS_DIR, ROOT, load_rules

OUTPUT = ROOT / "examples" / "ablation_report.md"


@dataclass
class Experiment:
    name: str
    question: str
    target: Optional[str]          # 要改的文档文件名，None 表示基线
    mutate: Optional[Callable[[str], str]]
    change: str                    # 做了什么改动
    expect: str                    # 期望看到的变化
    expect_status: str             # 期望的终止状态（脚本自检用）
    expect_in_answer: str = ""     # 期望出现在答案中的子串（脚本自检用）


def remove_doc(_text: str) -> str:
    return ""


def replace(old: str, new: str) -> Callable[[str], str]:
    def _fn(text: str) -> str:
        return text.replace(old, new)
    return _fn


EXPERIMENTS: List[Experiment] = [
    Experiment("基线（不改文档）", "报销期限是多久？", None, None,
               "—", "needs_clarification：D02 15 天 / D01 30 天冲突未决",
               "needs_clarification", "15 天"),
    Experiment("删除冲突方", "报销期限是多久？", "D02.md", remove_doc,
               "删掉 D02（15 天）", "只剩 D01 → 转为 answered，答 30 天",
               "answered", "30 天"),
    Experiment("改源头数值 A", "报销期限是多久？", "D01.md", replace("30 天", "45 天"),
               "D01 的 30 天 → 45 天", "冲突对变为 15 天 / 45 天，仍追问",
               "needs_clarification", "45 天"),
    Experiment("改源头数值 B", "报销期限是多久？", "D02.md", replace("15 天", "7 天"),
               "D02 的 15 天 → 7 天", "冲突对变为 7 天 / 30 天，引用同步更新",
               "needs_clarification", "7 天"),
    Experiment("改结论性条款", "实习生有年假吗？", "D06.md",
               replace("实习生不享受年假，实习期间按实际出勤计发实习补贴。",
                       "实习生享受 3 天年假，实习期间按实际出勤计发实习补贴。"),
               "D06 由「不享受年假」改为「3 天年假」", "结论从「不享受」变为「3 天」",
               "answered", "3 天"),
    Experiment("移除优先条款", "实习生有年假吗？", "D06.md",
               replace("本规定与公司其他假期制度不一致时，以本规定为准。", ""),
               "删掉 D06 的「以本规定为准」", "仍应凭适用范围定分（resolution.type=scope）",
               "answered", "不享受"),
]


def run_one(exp: Experiment, docs_dir: Path):
    rules = load_rules()
    store = DocStore(docs_dir)
    agent = Agent(store, rules)
    return agent.run(exp.question)


def _numbers(text: str) -> str:
    nums = re.findall(r"\d+(?:\.\d+)?\s*(?:天|个月|个工作日|元)", text)
    return "、".join(dict.fromkeys(n.strip() for n in nums)) or "—"


def _resolution_type(res) -> str:
    """从轨迹里取一致性核查给出的定分依据类型（scope / explicit_precedence）。"""
    for step in res.steps:
        data = (step.get("observation") or {}).get("data") or {}
        for conflict in data.get("conflicts", []) or []:
            resolution = conflict.get("resolution")
            if resolution:
                return resolution.get("type", "?")
    return "—"


def summarise(res) -> str:
    """把一次运行压缩成一行可比对的特征：状态 + 引用文档 + 结论 + 定分依据。"""
    docs = "、".join(sorted({c.doc_id for c in res.citations})) or "无"
    text = res.answer or ""
    winner = re.search(r"本问题应适用\s*(\w+)", text)
    basis = _resolution_type(res)
    if winner:
        tail = text[winner.start():].split("（")[0].strip().rstrip("。")
        conclusion = tail.replace("本问题应适用 ", "适用 ") + f"（定分依据：{basis}）"
    elif res.status == "needs_clarification":
        conclusion = f"追问：{_numbers(text)}"
    else:
        conclusion = _numbers(text)
    return f"`{res.status}` / 引用 {docs} / 结论 {conclusion}"


def main() -> None:
    base = DOCS_DIR
    lines: List[str] = []
    lines.append("# 文档扰动实验（ablation）\n")
    lines.append("对应审核要点：**替换文档内容，核对答案与动作是否改变**。\n")
    lines.append("做法：把 `data/docs` 复制到临时目录，改其中一份，用**同一个问题**重跑 Agent，"
                 "比较状态、引用文档与答案中的数值。\n")
    lines.append("复现命令：`python -m scripts.run_ablation`（本文件由脚本直接生成，非手写）。\n")
    lines.append("\n| # | 实验 | 问题 | 对文档的改动 | 期望变化 | 实际结果 |\n")
    lines.append("| --- | --- | --- | --- | --- | --- |\n")

    failures: List[str] = []
    for idx, exp in enumerate(EXPERIMENTS, start=1):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "docs"
            shutil.copytree(base, work)
            if exp.target and exp.mutate:
                target = work / exp.target
                text = target.read_text(encoding="utf-8")
                mutated = exp.mutate(text)
                if mutated == "":
                    target.unlink()
                else:
                    if mutated == text:
                        failures.append(f"{exp.name}：改动未生效（原文中没有目标字符串）")
                    target.write_text(mutated, encoding="utf-8")
            res = run_one(exp, work)
        actual = summarise(res)
        if res.status != exp.expect_status:
            failures.append(f"{exp.name}：期望状态 {exp.expect_status}，实际 {res.status}")
        if exp.expect_in_answer and exp.expect_in_answer not in (res.answer or ""):
            failures.append(f"{exp.name}：答案中未出现「{exp.expect_in_answer}」")
        lines.append(
            f"| {idx} | {exp.name} | {exp.question} | {exp.change} | {exp.expect} | {actual} |\n"
        )
        print(f"{idx}. {exp.name}: {actual}")

    lines.append("\n## 怎么读这张表\n\n")
    lines.append("- 第 2 行：删掉冲突的另一方，动作从「追问」变成「作答」——说明追问确实是由**文档冲突**触发的，"
                 "而不是写死的流程；\n")
    lines.append("- 第 3、4 行：改原文里的数字，答案与引用里的数字同步变化——说明结论是从原文抽出来的，"
                 "不是模板里的常量；\n")
    lines.append("- 第 5 行：改结论性条款，答案跟着改；\n")
    lines.append("- 第 6 行：移除优先条款后仍凭适用范围定分，验证定分依据有两条而非只有一条。\n")
    lines.append("\n> 同样的断言也固化在 `tests/test_robustness.py` 中，随 `python -m unittest discover -s tests` 执行。\n")

    OUTPUT.write_text("".join(lines), encoding="utf-8")
    print(f"\n已写入 {OUTPUT.relative_to(ROOT)}")
    if failures:
        print("\n[失败] 以下实验未达到预期：")
        for item in failures:
            print("  - " + item)
        raise SystemExit(1)
    print("全部 6 组实验均符合预期")


if __name__ == "__main__":
    main()
