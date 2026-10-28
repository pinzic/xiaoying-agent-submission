# 企业知识问答 Agent（小影校招 AI Agent 开发岗 · 考核提交）

## 提交信息

| 项 | 值 |
| --- | --- |
| 题号 | **企业知识问答 Agent** |
| GitHub URL | https://github.com/pinzic/xiaoying-agent-submission |
| 最终 commit SHA | `1e57475899f206a8b675973c649b2c23612a7ed1` |
| 内容锚点 tag | `v1.0-submission`（与该 SHA 指向同一 commit） |

> 仓库为 **Public**，评审无需额外授权即可访问与克隆。
>
> **关于 SHA 的一点说明**：包含全部代码、测试、9 组验证材料与五份文档的提交是 `1e57475`，
> 并已打 tag `v1.0-submission` 固定下来（`commit` 的 SHA 无法自指，所以把地址与 SHA 回填进本文件本身
> 会产生一个新的 README 提交）。因此若 `git rev-parse HEAD` 比上表的 SHA 多出一两个 `docs:` 提交，属正常。
> 核对内容请用：`git rev-parse v1.0-submission^{commit}`，或 `git checkout v1.0-submission`。

> 目标：帮员工在合成制度库/产品 FAQ 中找到**可核验**的答案——每条结论都能落到文档 ID、章节与逐字原文；
> 遇到文档冲突且没有适用范围/优先依据时，**追问而不是替用户选**。

离线即可完整运行（无第三方依赖、无需 API Key）。真实模型链路通过 OpenAI 兼容接口接入，见 [模型接入](#5-模型接入真实-llm)。

---

## 1. 快速开始

```bash
cd xiaoying-agent-submission

# 无需安装依赖；Python 3.9+ 即可（开发环境 3.13.14）
python -m src.cli --question "员工加班后调休的有效期是多久？"
```

输出是一个 JSON：`status` / `answer` / `citations`（文档 ID + 章节 + 逐字原文）/ `gaps` / `steps`（完整决策轨迹）。

### 其余验收场景（可直接复制）

```bash
# 文档冲突：D01 说 30 天，D02 说 15 天，且都没有适用范围/优先级 → 必须追问
python -m src.cli --question "报销期限是多久？"

# 资料外问题 → insufficient_info，不编答案
python -m src.cli --question "公司可以帮我办理日本签证吗？"

# 改写问句：口语说法「补休 / 最晚多久」→ 仍应命中 D04
python -m src.cli --question "加班换来的补休，最晚多久要休掉？"

# 上下文追问：问题本身不含「年假」，需要结合历史
python -m src.cli --question "那要提前几天申请？" --history examples/case5_followup/input.json

# 工具失败（可恢复）：首次检索超时 → 换短关键词重试
python -m src.cli --question "报销期限是多久？" --fault search_docs:1

# 工具失败（不可恢复）→ failed，明确报告工具不可用
python -m src.cli --question "报销期限是多久？" --fault search_docs:2

# 执行上限 → incomplete（步数上限 / 工具调用上限两种）
python -m src.cli --question "报销期限是多久？" --max-steps 2
python -m src.cli --question "报销期限是多久？" --max-tool-calls 1
```

把结果落盘（`output.json` + `trace.jsonl`）：

```bash
python -m src.cli --question "报销期限是多久？" --out .tmp/run1
```

### 测试

```bash
python -m unittest discover -s tests -v
# Ran 39 tests ... OK
```

### 重新生成验证材料

```bash
python -m scripts.gen_examples    # examples/ 下 9 组输入 / 输出 / trace
python -m scripts.run_ablation    # 文档扰动对照实验 → examples/ablation_report.md
```

`run_ablation` 自带断言：改动文档后若状态或答案没有按预期变化，脚本以非零码退出。

---

## 2. 评审入口

| 内容 | 位置 |
| --- | --- |
| 需求、成功标准 | [`docs/01-requirements.md`](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [`docs/02-solution.md`](docs/02-solution.md) |
| 测试预期 / 实际结果 / 失败与限制 | [`docs/03-evaluation.md`](docs/03-evaluation.md) |
| AI 使用范围与个人验证 | [`docs/04-ai-usage.md`](docs/04-ai-usage.md) |
| 复盘、用时、未完成项 | [`docs/05-retrospective.md`](docs/05-retrospective.md) |
| 9 组输入 / 实际输出 / Trace | [`examples/`](examples/README.md) |
| 替换文档内容的对照实验 | [`examples/ablation_report.md`](examples/ablation_report.md) |
| 合成制度与冲突规则 | [`data/docs/`](data/docs)、[`data/rules.json`](data/rules.json) |
| 核心代码 | [`src/agent.py`](src/agent.py)（ReAct 循环）、[`src/retrieval.py`](src/retrieval.py)、[`src/conflict.py`](src/conflict.py)、[`src/answer.py`](src/answer.py) |

**建议的 5 分钟评审路径**：先看 `examples/case2_doc_conflict/trace.jsonl`（冲突如何被发现与处理），
再看 `src/agent.py` 的 `run()`（循环与上限）和 `src/answer.py` 的 `verify_answer()`（引用校验），
然后看 `examples/ablation_report.md`（改文档后答案是否跟着变），
最后跑 `python -m unittest discover -s tests`。

---

## 3. 目录结构

```
xiaoying-agent-submission/
├── README.md
├── requirements.txt          # 无第三方运行时依赖（仅标准库）
├── .env.example              # 可选：接入真实 LLM 时的变量
├── src/
│   ├── agent.py              # ReAct 主循环、步数/工具调用上限、终止状态
│   ├── models.py             # Action / ToolResult / Context / RunResult
│   ├── docs_store.py         # 文档解析（front matter：ID/版本/生效日期/适用范围）+ 章节切分
│   ├── retrieval.py          # 中文二元组 + BM25 + 同义词扩展 + 相关性双门槛
│   ├── tools.py              # search_docs / read_doc / check_consistency（含故障注入）
│   ├── conflict.py           # 同一事项取值抽取、冲突判定、适用范围/优先条款定分
│   ├── answer.py             # 答案组装、追问话术、引用与数字校验
│   ├── planner_mock.py       # 离线启发式规划器（验证流程，不代表真实模型能力）
│   ├── planner_llm.py        # 真实模型规划器（OpenAI 兼容 /chat/completions）
│   └── cli.py                # 运行入口
├── scripts/
│   ├── gen_examples.py       # 生成 9 组验证材料
│   └── run_ablation.py       # 文档扰动对照实验（带自检断言）
├── tests/                    # 39 个 unittest 用例
├── examples/                 # 9 组输入 + 实际输出 + trace.jsonl + 扰动报告
├── data/
│   ├── docs/                 # 12 份合成制度 / FAQ（D01–D09、P01–P03）
│   └── rules.json            # 同义词、停用词、冲突取值规则、适用范围映射
└── docs/                     # 五份交付文档
```

---

## 4. 它怎么工作（一屏版）

```
问题 + 历史
   │
   ▼
[决策] 模型/规划器输出一个动作 ──► search_docs ──► 观察：命中文档 ID/版本/片段
   ▲                                     │
   │                                     ├─ 无命中 → 换更宽的关键词再检索一次 → 仍无 → insufficient_info
   │                                     ▼
   └──────────────────────────── read_doc（读原文，最多 2 篇候选）
                                         │
                                         ▼
                                 check_consistency（比对同一事项取值）
                                         │
                    ┌────────────────────┼─────────────────────┐
                    ▼                    ▼                     ▼
              无冲突→作答          有冲突且可定分→作答       有冲突且不可定分→追问
              （逐字引用）        （同时披露双方）         （列出双方原值与引用）
                                         │
                                         ▼
                              verify_answer：引用逐字 + 数字可溯源
                              不通过 → 回灌一次 → 仍不通过 → failed
```

- **状态**：`answered` / `needs_clarification` / `insufficient_info` / `incomplete`（触达上限）/ `failed`（工具或引用失败）。
- **上限**：默认 8 步、6 次工具调用（`--max-steps` / `--max-tool-calls`），触达即返回 `incomplete`，不输出未验证答案。
- **冲突定分**只认两类依据：文档声明适用范围命中问题主体，或文档正文写了「以本规定为准」。**版本更新只作为提示**，不作为自动胜出理由——这正是题目参考实例要求的行为。

---

## 5. 模型接入（真实 LLM）

默认使用离线 Mock 规划器（`--planner auto` 且无环境变量时自动降级，并在 stderr 提示）。
接真实模型只需三项环境变量，接口按 OpenAI 兼容协议：

```bash
cp .env.example .env      # 填写后 export，或直接在 shell 里 export
export LLM_API_KEY=...
export LLM_BASE_URL=https://your-gateway/v1
export LLM_MODEL=your-model-name

python -m src.cli --question "报销期限是多久？" --planner llm
```

`src/planner_llm.py` 用标准库 `urllib` 直接请求 `/chat/completions`，不依赖 SDK；
模型返回 JSON 动作，Agent 执行工具后把观察摘要回灌给模型，进入下一轮。

> 边界说明：**Mock 只验证流程与工程约束，不证明真实模型的决策效果**。仓库中的 9 组 trace 均由 Mock 规划器生成，
> 目的是让评审在没有密钥的环境里也能复现完整链路。真实模型链路的代码路径已实现，但未在本次提交中附上真实调用记录（原因见 `docs/03-evaluation.md`）。

---

## 6. 已知限制

1. **冲突识别依赖规则配置**：取值抽取按 `data/rules.json` 的 `claim_patterns`（正则 + 上下文词）执行，是"半开放"的，
   不是通用信息抽取。新增业务主题（如"年假折现比例"）需要在规则里补一条 pattern。
2. **检索是词法检索**：BM25 + 二元组，没有语义向量。对"完全不含原词"的同义改写仍可能漏召回（当前靠同义词表兜底）。
3. **冲突只在同一事项内比对**：跨主题的语义矛盾（例如一份文件说"必须审批"、另一份说"无需审批"但措辞完全不同）识别不到。
4. **Mock 规划器是启发式的**：它按观察结果分支（无命中→扩检索、冲突未决→追问、工具报错→重试/失败），
   但不具备真实模型的语义理解能力。
5. **无会话存储**：`history` 由调用方传入，服务端不保存多轮状态。
6. **候选上限 2 篇**：`MockPlanner` 只取得分最高的 2 篇候选做一致性核查，
   若冲突存在于第 3 篇及以后（当前语料不存在这种情况）会被漏掉。真实模型规划器无此限制。

---

## 7. 仓库地址

**https://github.com/pinzic/xiaoying-agent-submission**（Public）

```bash
git clone https://github.com/pinzic/xiaoying-agent-submission.git
cd xiaoying-agent-submission
git log --oneline -3
```

- 仓库为 **Public**，评审可直接访问，无需额外授权。
- **提交 SHA**：`1e57475899f206a8b675973c649b2c23612a7ed1`（tag `v1.0-submission`）。
  在该 SHA 之后只会有回填本表地址/SHA 的 `docs:` 提交，不含任何代码变更，故以此作为内容基准。
  以下命令可取得完全一致的内容：

  ```bash
  git checkout v1.0-submission          # 或 git show v1.0-submission
  git rev-parse v1.0-submission^{commit}   # → 1e57475899f206a8b675973c649b2c23612a7ed1
  ```
- 仓库中**不含任何真实密钥**：`.env.example` 只有变量名，`.env` 已被 `.gitignore` 忽略。
- 克隆后建议再跑一遍验收，确认远端代码与本地一致：
  ```bash
  python -m unittest discover -s tests        # 39 passed
  python -m scripts.run_ablation              # 6 组扰动实验全部符合预期
  ```
