# 02 · 方案：调用流程、动态决策与技术取舍

## 1. 整体调用流程

```
CLI (src/cli.py)
  └─ Agent.run(question, history)                      src/agent.py
       ├─ Context：问题 / 历史 / 已读文档 / 工具失败计数 / 步数
       ├─ Planner.decide(ctx) -> Action(name, args, decision_basis)
       │     ├─ MockPlanner（离线，启发式，默认）
       │     └─ LLMPlanner（OpenAI 兼容 /chat/completions，JSON 动作协议）
       ├─ Tools.call(name, args) -> ToolResult(ok, data, summary, error)
       │     ├─ search_docs(query, top_k)         BM25 + 二元组 + 同义词扩展
       │     ├─ read_doc(doc_id, section?)        原文（引用只能来自这里）
       │     └─ check_consistency(question, ids)  同一事项取值比对 + 定分
       ├─ 观察写回 Context，再进入下一轮
       └─ 终止动作 finish / ask_user → verify_answer → RunResult
```

**为什么循环是显式的**：决策依据、工具参数、观察摘要、下一步动作都写在 trace 里，评审可以直接判断
"模型是否因为工具结果改变了下一步"，而不是从框架日志里猜。

## 2. 动态决策（ReAct）

每一轮的动作完全由**上一步观察**决定，而不是预置脚本：

| 观察 | 下一步动作 | 依据 |
| --- | --- | --- |
| 检索命中 1–2 篇 | `read_doc` 读原文（摘要不足以判定冲突） | 引用必须来自原文 |
| 检索零命中 | 换更宽/同义词扩展的关键词再检索一次 | 排除表述差异 |
| 二次仍零命中 | `finish(insufficient_info)` | 不猜 |
| 两篇取值不一致且无定分依据 | `ask_user`（列出双方原值 + 引用） | 不替用户选 |
| 两篇取值不一致但有适用范围/优先条款 | `finish(answered)`，同时披露双方 | 可解释的定分 |
| 工具报错 | 第一次：换策略重试；第二次：`finish(failed)` | 不伪造结果 |
| 引用校验失败 | 回灌一次让规划器重新取证；再失败 → `failed` | 错误引用是关键缺陷 |

Trace 里每一步都带 `decision_basis`（面向输出的简要理由，不含模型内部思维过程）。

## 3. 检索：为什么是 BM25 + 二元组

- 语料只有 13 份文档、约 5 千字，**BM25 的排序已经够用**，且完全离线、可单测、可解释；
- 中文没有分词器依赖：按**字符二元组**切分（"报销期限" → 报销/销期/期限），避免引入 jieba 之类的依赖；
- **同义词扩展**（`data/rules.json` 的 `synonyms`）解决口语改写："补休/换休" → "调休"，"报销要几天内交" → "报销时限"；
- **相关性双门槛**（`min_coverage` + 命中词数）是迭代出来的：初版只看覆盖率，
  结果"我想问下旅游补贴"因为命中"补贴"一词而召回《加班与调休管理规定》，会让 Agent 答非所问；
  加上"扩展后命中词数 ≥ 2"后该问题被正确判为资料外（见 `docs/03-evaluation.md` 的回归记录）。

## 4. 冲突处理：只认两类定分依据

`src/conflict.py` 的做法：

1. **取值抽取**：按 `data/rules.json` 的 `claim_patterns`（事项 key + 上下文词 + 数值正则 + 排除词）从原文句子中抽取
   `(doc_id, key, value, unit, quote, section)`。例如报销期限 = 句子含"报销/提交"且匹配 `(\d+)\s*天`。
2. **冲突判定**：同一 key 下，两篇文档的取值集合**不相交** → 冲突（30 vs 15；5/10/15 vs 0）。
3. **定分**：只有两种情况允许下结论
   - 文档 `scope` 或标题命中问题里的适用主体（"实习生""正式员工""差旅"）；
   - 文档正文存在优先条款（"以本规定为准"）。
4. **版本日期只做提示**：D02（2025-06-01）比 D01（2024-03-01）新，但正文没有写替代/废止，
   因此只输出一句 `hint`，**不作为自动胜出依据**——这正对应题目参考实例。

> 一个实现中踩到的边界（已修）：最初把"仅凭优先条款"也当作可定分依据，导致
> "年假有几天？"（没说明身份）被 D06《实习生管理规定》的"以本规定为准"抢答成"不享受"。
> 修正为：优先条款必须与**适用范围命中**同时成立，或至少存在明确的适用范围匹配，才能定分；
> 否则回到追问。这个用例现在固化在 `tests/test_conflict.py::test_generic_audience_does_not_resolve`。

## 5. 引用校验：两道闸

`src/answer.py::verify_answer`：

1. **逐字校验**：`quote` 归一化空白后必须是原文子串；文档 ID 必须存在；引用长度 ≥ 8 字。
2. **数字溯源**：从 `answer` 中抽 `(\d+)\s*(天|个月|个工作日|元|次)`，每个都必须出现在引用原文里。

校验失败不会"降级输出"：先回灌给规划器一次重新取证的机会，仍失败则 `status=failed`、`answer=null`。
这条闸门拦下过一个真实 bug：追问话术里列了 D05 的"5、10、15 天"，但只引用了 5 天那一句，
数字校验直接报"15 天在引用中找不到依据"，促使把该文档的所有相关句子都加入引用。

## 6. 技术选型与取舍

| 选择 | 理由 | 代价 / 边界 |
| --- | --- | --- |
| 原生实现，不引入 LangGraph/ADK | 5 个动作、一条主循环，图编排带来的抽象成本高于收益；显式循环更利于解释"为什么这一步这么做" | 没有框架的可视化/持久化能力 |
| 只用标准库 | 评审 `pip install` 都不需要，离线可跑；测试用 unittest | 没有 SDK 的重试/流式等便利 |
| BM25 + 二元组 | 语料小、可解释、可单测 | 无语义召回，纯同义改写仍可能漏 |
| 规则化 claim 抽取 | 冲突判定需要"同一事项"的锚点，规则比通用 IE 更可控、可审计 | 新主题需要在 `rules.json` 增补 pattern |
| Mock + LLM 两个 Planner 共用一套循环 | 无密钥也能复现完整链路；接入真实模型只换 Planner | Mock 只能证明流程，不能证明模型决策质量 |
| 引用校验放在 Agent 层而非提示词里 | 提示词约束不可靠，代码闸门才是硬约束 | 严格校验会拒掉"合理概括"式引用（本项目的取舍：宁可失败，不可错引） |

## 7. 上限与终止状态

- 默认 `max_steps=8`、`max_tool_calls=6`，均可通过 CLI 覆盖；
- 触达上限 → `status=incomplete`，`reason` 说明是哪一种上限（`step_limit_reached` /
  `tool_call_limit_reached`），**不输出未验证的答案**，轨迹最后一步标记为 `final`；
  此时 `validation` 为 `null`——没有任何引用产出，也就没有需要校验的东西；
- 两种上限各有一组材料：`examples/case8_step_limit`、`examples/case9_tool_call_limit`；
- 终止状态共 5 种：`answered` / `needs_clarification` / `insufficient_info` / `incomplete` / `failed`。

失败原因也做了分类：`bad_arguments`（参数错误，不重试、如实说明）与 `upstream_timeout`
（上游故障，允许换策略重试一次）走不同的分支。把"检索词为空"报成"服务超时"会误导排查方向，
所以这是一条独立的判断而不是统一的重试逻辑（见 `docs/03-evaluation.md` §5 第 6 条）。

## 8. 可扩展点

- 换检索：实现同签名的 `Retriever.search`，可替换为向量 + BM25 混合；
- 换模型：`LLMPlanner` 只用 `/chat/completions`，可平移到任意兼容网关；
- 加主题：在 `data/rules.json` 增加 `claim_patterns` 与 `synonyms`，不需要改代码。
