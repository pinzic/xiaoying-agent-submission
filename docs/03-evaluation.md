# 03 · 评估：预期、实际结果、失败与限制

环境：Windows 11 / Python 3.13.14 / 无第三方依赖 / 规划器为离线 Mock。
复现：

```bash
python -m unittest discover -s tests -v     # 28 tests, OK
python -m scripts.gen_examples              # 重新生成 examples/
```

> 说明：Mock 规划器只验证**流程与工程约束**（工具调用、引用、上限、状态机），
> 不证明真实 LLM 的决策质量。真实模型路径见 `src/planner_llm.py`，本次未附真实调用记录（见 §6）。

---

## 1. 用例结果总览

| 用例 | 场景 | status | 步数 | 工具调用 | 实际结论 |
| --- | --- | --- | --- | --- | --- |
| [case1_clear_answer](../examples/case1_clear_answer/output.json) | 正常 | `answered` | 3 | 2 | 调休有效期 6 个月，引用 D04「调休有效期」 |
| [case2_doc_conflict](../examples/case2_doc_conflict/output.json) | 文档冲突 | `needs_clarification` | 5 | 4 | 列出 D02 15 天 / D01 30 天，请用户确认适用制度 |
| [case3_out_of_scope](../examples/case3_out_of_scope/output.json) | 资料外 | `insufficient_info` | 3 | 2 | 两轮零命中，`answer=null` + 资料缺口 |
| [case4_paraphrase](../examples/case4_paraphrase/output.json) | 改写问句 | `answered` | 3 | 2 | 与 case1 同文档同结论（D04 / 6 个月） |
| [case5_followup](../examples/case5_followup/output.json) | 上下文追问 | `answered` | 5 | 4 | 借上一轮补全「年假」，答 3 个工作日（D05） |
| [case6_tool_failure_recover](../examples/case6_tool_failure_recover/output.json) | 工具失败（可恢复） | `needs_clarification` | 6 | 5 | 首次超时 → 换「报销期限」重试 → 仍正确识别冲突 |
| [case7_tool_failure_terminal](../examples/case7_tool_failure_terminal/output.json) | 工具失败（不可恢复） | `failed` | 3 | 2 | 两次超时 → 明确失败，不编答案 |

每个用例目录含：`input.json`（问题/历史/故障注入）、`output.json`（完整结果）、`trace.jsonl`（逐步骤轨迹）。

---

## 2. 验收点覆盖

| 验收点 | 覆盖用例 | 实际证据 |
| --- | --- | --- |
| 明确答案 | case1 | `citations[0].quote` 与 `data/docs/D04.md` 逐字一致；`validation.ok=true` |
| 文档冲突 | case2 | 两条引用分别含「15 天」「30 天」；`status=needs_clarification`；答案为向用户提问 |
| 资料外问题 | case3 | `answer=null`、`citations=[]`、`gaps[0]` 说明知识库无相关条目 |
| 改写后的相似问题 | case4 + `test_retrieval.py::test_paraphrase_still_hits` | 口语「补休/最晚多久」仍命中 D04（同义词扩展） |
| 上下文追问 | case5 | 检索 query 变为「那要提前几天申请？ 正式员工 年假 天」，命中 D05「年假申请」 |
| 工具失败 | case6 / case7 | case6 第 1 步 `ok=false` 且第 2 步换词重试成功；case7 连续失败后 `status=failed` |
| 执行上限 | `test_agent_paths.py::test_limits_return_incomplete` | `--max-steps 2` / `--max-tool-calls 1` → `incomplete` |
| 引用校验 | `test_citation_guard.py`（4 例） | 伪造引用、数字加料、未知文档 ID 全部被拒 |

---

## 3. 动态决策证据（两条执行路径）

**路径 A：正常环境（case2）**

```
1 search_docs  命中 D02(25.51) D01(18.22) D03(7.36) → 摘要有两个不同数值，需读原文
2 read_doc     D02 → 15 天
3 read_doc     D01 → 30 天（不满足只看第一篇就作答）
4 check_consistency → 1 处冲突，0 处可定分
5 ask_user     列出双方原值 + 逐字引用，请用户确认
```

**路径 B：工具故障（case6，同一问题）**

```
1 search_docs  ✗ upstream_timeout
2 search_docs  query 由整句改为「报销期限」（观察到失败后换策略，而非直接作答）
3-4 read_doc   D02 / D01
5 check_consistency → 冲突未决
6 ask_user     与路径 A 同样的冲突结论
```

两条路径的差异**由工具返回驱动**，不是脚本分支：第 2 步动作从 `read_doc` 变成 `search_docs`。

**路径 C：文档环境变化（`tests/test_robustness.py`）**

- 删除 D02 → 同一问题从 `needs_clarification` 变为 `answered`（只引 D01，30 天）；
- 把 D01 的 30 天改为 45 天 → 答案变为 45 天，旧数字不再出现；
- 把 D02 的 15 天改为 7 天 → 冲突载荷与引用同步变为 7 天；
- 把 D06「不享受年假」改为「3 天年假」→ 结论变为 3 天；
- 去掉 D06 的优先条款 → 仍可凭适用范围定分（`resolution.type = "scope"`），已在测试中固化。

这组测试直接对应审核要点"替换文档内容，核对答案与动作是否改变"。

---

## 4. 检索质量

10 个问题（含口语改写）上的期望文档召回：

```
调休有效期 → D04 ✓    报销期限 → D02 ✓(D01 次席)   年假有几天 → D05 ✓   实习生年假 → D06 ✓
退款政策 → P03 ✓      导出有水印吗 → P01 ✓          换绑手机号 → P02 ✓   出差住宿标准 → D03 ✓
采购几家比价 → D08 ✓  客户数据给合作方 → D07 ✓
```

- 期望文档 **top-1 命中 9/10**，**top-2 命中 10/10**（"报销期限"的 top-2 正是冲突对 D02/D01，属于正确行为）；
- 资料外问题 0 命中：「公司可以帮我办理日本签证吗？」「我想问下旅游补贴」。

---

## 5. 开发过程中被测试拦下的 3 个缺陷（真实记录）

| # | 现象 | 根因 | 修复 | 固化用例 |
| --- | --- | --- | --- | --- |
| 1 | 「我想问下旅游补贴」被答成"加班补贴" | 相关性只按覆盖率算，命中"补贴"一个词就放行 | 增加"扩展后命中词数 ≥ 2"门槛 | `test_retrieval.py::test_out_of_scope_returns_nothing` |
| 2 | 「年假有几天？」被 D06 抢答"不享受" | 只凭"以本规定为准"就定分，忽略 D06 仅适用实习生 | 优先条款必须与适用范围命中同时成立 | `test_conflict.py::test_generic_audience_does_not_resolve` |
| 3 | 追问话术列了 5/10/15 天，校验报"15 天无依据" | 只引用了 5 天那一句 | 追问/作答引用该文档全部相关句子 | `test_citation_guard.py::test_number_must_be_supported_by_quote` |

第 3 个缺陷尤其说明"引用校验不能只靠提示词"：模型（这里是模板）自己不会发现引用不完整，代码闸门能。

---

## 6. 未验证项与限制

1. **真实 LLM 链路未跑通实测**：本次环境没有可用的模型密钥，`--planner llm` 只做了代码实现与协议约束，
   没有真实模型 trace。因此本提交能证明的是"流程、工具、校验、状态机可用"，
   **不能证明**"某个模型能在该提示词下稳定做出正确决策"。这是本次最大的未验证项。
2. **冲突识别是规则驱动的**：`claim_patterns` 覆盖了 4 类事项（报销期限、年假天数、调休有效期、退款期限）。
   语料里其它潜在冲突（如审批层级 5000 元 vs 其它金额）没有 pattern，不会被识别。
3. **词法检索的天花板**：不含任何原词的同义改写（例如用"费用申报"问报销）仍可能漏召回；
   同义词表是人工维护的。
4. **Mock 的决策依据是启发式**：它能根据观察改变动作（重试/追问/终止），但没有语义推理，
   不能代表真实模型的"理解"能力。
5. **单轮工具预算**：默认 8 步 / 6 次调用，复杂问题（3 篇以上候选）可能触达上限返回 `incomplete`。
