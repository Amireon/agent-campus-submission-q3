# 方案与技术取舍

## 调用流程

```text
CLI 输入/历史
  → 校验 question_id、question、history
  → 组装 system + user 消息
  → DeepSeek 选择一个工具（search_docs / read_doc / finish）
  → 参数白名单与业务校验
  → 执行工具，得到观察
  → 把原生 assistant tool_call 与 observation 追加回消息
  → 模型决定下一步，直到 finish、达到上限或模型错误
  → 由代码组装最终输出（不使用模型自由文本作为“已保存/已确认”的证据）
```

真实模型实现 `decide(messages, tool_schemas)`：HTTPS POST `/chat/completions`，默认 `deepseek-flash`，`thinking=disabled`，`tool_choice=required`，`parallel_tool_calls=false`，`temperature=0`，每轮只接受一个原生工具调用。调用见 [src/kb_agent/model.py](../src/kb_agent/model.py)。

## 工具

| 工具 | 作用 | 关键返回 |
| --- | --- | --- |
| `search_docs(query)` | 本地关键词/字符 bigram 检索，tag 和章节标题加权 | `hits`（doc_id、状态、版本、命中章节摘要、分数）、`conflict_signals` |
| `read_doc(doc_id)` | 读取完整章节；记录已读章节 | 文档全文；引用前置条件 |
| `finish(status, answer, citations, gaps, clarification_question)` | 结束并提交结果；由代码校验引用、数字、状态与冲突 | 成功或结构化错误码 |

工具参数由白名单校验，模型不能传 `approved`、`force` 等字段；工具集中没有写入文档或“发布”的能力。

## 动态决策路径

1. **正常**：`search_docs("年假天数")` → 读 D03 → `finish(answered)`。
2. **冲突**：`search_docs("报销期限")` 返回 D01/D02 和 `conflict_signals` → 读 D01、D02 → `finish(needs_clarification)`，列出 30 天/15 天并要求确认适用制度。
3. **资料外**：首轮无结果 → 改写搜索词（最多 3 次）→ 仍无结果 → `finish(not_found)`，说明缺口。
4. **相似问题**：把“我工作三年了能休几天”改写成“年假 3 年 天数” → 命中 D03。
5. **追问**：`history` 提供“加班打车”，把“那上限是多少”改写成“加班 打车 上限” → 命中 D10。
6. **工具失败**：`SEARCH_UNAVAILABLE` 可重试一次；持续失败 `finish(failed)`；瞬时失败后继续正常流程。
7. **旧版/草稿**：检索会返回 D07/D09，但 `read_doc` + `finish` 只允许引用 `active` 文档；引用旧版返回 `DOC_NOT_ACTIVE`。
8. **上限**：达到 `max_steps`/`max_tool_calls` 时停止，输出 `incomplete` 和 `stop_reason`，不补写答案。

## 引用与事实校验

`finish` 不是“把模型说的话原样保存”，而是一个校验器：

- `citations` 中每个 `(doc_id, section_id)` 必须已在本轮 `read_doc` 中读取；未读返回 `DOC_NOT_READ`。
- 文档 `status` 必须是 `active`；`superseded/draft` 返回 `DOC_NOT_ACTIVE`。
- `quote` 与章节原文做 NFKC、去空白、去常见标点后的子串比较；不匹配返回 `QUOTE_NOT_FOUND`。
- 答案中的数字（含文档 ID、版本、日期中的数字）必须在引用原文、所引文档元数据或用户问题中出现；否则返回 `UNSUPPORTED_NUMBER`。比较时对 `01`/`1` 做归一化。
- `answered` 至少一条引用；`needs_clarification` 至少两份不同文档并给出具体问题；`not_found` 必须有 `gaps`；`failed` 不得携带引用。
- 对同主题冲突：如果只引用冲突文档中的一份且答案/问题没有提到该文档的适用范围，`finish` 返回 `UNRESOLVED_CONFLICT`；如果引用两份却声称 `answered`，也返回该错误。模型必须改为说明适用制度或请求确认。

## 冲突信号

`search_docs` 会扫描本次命中的 `active` 文档：同一章节标题下出现不同“数字+单位”时，返回 `conflict_signals`，例如：

```json
{"topic": "报销时限", "doc_ids": ["D01", "D02"],
 "documents": [{"doc_id": "D01", "values": ["30天"]},
                {"doc_id": "D02", "values": ["15天"]}]}
```

这是**观察**，不是决策：工具不判断哪份优先，也不自动改答案；模型必须读取原文，再选择说明适用范围或请求用户确认。`draft/superseded` 不参与信号计算。

## 追问与检索

`history` 以 JSON 形式放在 user 消息中。系统提示要求模型先把追问改写成独立问题再检索；真实 Trace 中模型把“那上限是多少”改写成了“加班 晚上十点以后 打车费 报销 上限”。

检索器是确定性的字符 bigram + tag/标题加权，带最小证据规则（必须有 tag/章节强命中，或至少两个正文 token 命中），避免“公司”这类常见词把资料外问题误判为命中。它不依赖外部 embedding 服务，便于离线复现；代价是语义泛化能力有限，需要模型改写查询来补足。

## 上限与失败

- 默认每题最多 10 轮模型决策/8 次工具调用；`search_docs` 最多 3 次（提示层约束）。
- `finish` 也算一次工具调用；非法引用/数字不消耗额外轮次之外的预算，模型可基于错误观察修正。
- 模型 HTTP 错误、连接错误、响应格式错误均返回 `incomplete` + `model_error`，**不会**自动切换 Mock。
- 工具失败返回结构化 `retryable`，模型可选择重试一次或结束为 `failed`。

## 取舍

- **原生实现而非框架**：核心循环、工具协议和校验都可读、可测；无需解释框架版本与职责。代价是没有现成的持久会话、可观测面板和分布式 Runtime，这些不在本题必需范围。
- **本地检索而非向量库**：零依赖、可离线、可解释；对同义改写依赖模型，而不是 embedding。真实生产应改为混合检索并保留引用校验。
- **代码校验而非只靠提示**：提示负责引导，`finish` 负责拒绝无依据引用、编造数字、旧版依据和未消解冲突。这会让模型偶尔“被拒绝后重试”，但这是可接受且可观测的 ReAct 行为。
- **单 Agent + CLI**：足够展示决策循环；不引入多 Agent 协调复杂度。
