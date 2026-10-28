# examples · 输入、实际输出与 Trace

所有材料由 `python -m scripts.gen_examples` 生成（离线 Mock 规划器，无外部依赖）。

每个用例目录包含：

| 文件 | 内容 |
| --- | --- |
| `input.json` | 问题、对话历史、故障注入配置、步数上限、用例说明 |
| `output.json` | 实际输出：`status` / `answer` / `citations` / `gaps` / `reason` / `validation` / `steps` |
| `trace.jsonl` | 逐步骤轨迹：动作、参数、观察（ok / summary / data）、决策依据 |

## 用例索引

| 用例 | 覆盖验收点 | 预期状态 | 实际状态 |
| --- | --- | --- | --- |
| [case1_clear_answer](case1_clear_answer/) | 明确答案；引用可核验 | `answered` | `answered` |
| [case2_doc_conflict](case2_doc_conflict/) | 文档冲突（D01 30 天 vs D02 15 天） | `needs_clarification` | `needs_clarification` |
| [case3_out_of_scope](case3_out_of_scope/) | 资料外问题 | `insufficient_info` | `insufficient_info` |
| [case4_paraphrase](case4_paraphrase/) | 改写后的相似问题 | `answered`（同 D04 / 6 个月） | `answered` |
| [case5_followup](case5_followup/) | 上下文追问 | `answered`（D05 / 3 个工作日） | `answered` |
| [case6_tool_failure_recover](case6_tool_failure_recover/) | 工具失败（可恢复） | 重试成功 | `needs_clarification` |
| [case7_tool_failure_terminal](case7_tool_failure_terminal/) | 工具失败（不可恢复） | `failed` | `failed` |
| [case8_step_limit](case8_step_limit/) | 执行步数上限 | `incomplete` | `incomplete` |
| [case9_tool_call_limit](case9_tool_call_limit/) | 工具调用次数上限 | `incomplete` | `incomplete` |

另有 [ablation_report.md](ablation_report.md)：**替换文档内容**的对照实验
（`python -m scripts.run_ablation` 生成，改动原文后答案/动作是否随之变化）。

## 怎么读 trace

以 `case2_doc_conflict/trace.jsonl` 第 4 步为例：

```json
{
  "step": 4,
  "action": "check_consistency",
  "args": {"question": "报销期限是多久？", "doc_ids": ["D02", "D01"]},
  "decision_basis": "候选原文已读完，执行取值一致性核查，确认是否存在冲突",
  "observation": {
    "ok": true,
    "summary": "发现 1 处冲突，其中 1 处无法用适用范围/优先条款定分",
    "data": {
      "conflicts": [{
        "key": "reimbursement.deadline",
        "doc_a": {"doc_id": "D02", "values": ["15"], "unit": "天", "...": "..."},
        "doc_b": {"doc_id": "D01", "values": ["30"], "unit": "天", "...": "..."},
        "resolution": null,
        "hint": "D02 的生效日期更新（2025-06-01）…不能据此自动判定优先级。"
      }]
    }
  }
}
```

`decision_basis` 是面向输出的简要理由，不含模型内部思维过程；下一步动作由下一条记录体现。
