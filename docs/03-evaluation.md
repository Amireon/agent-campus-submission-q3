# 测试、实际运行与失败记录

## 执行环境与命令

- 日期：2026-10-28，Asia/Shanghai。
- 环境：macOS，Python 3.14.6；项目最低要求 Python 3.11，尚未实际运行其他 Python 版本。
- 自动测试：`python3 -m unittest discover -s tests -v`。
- 离线矩阵：`python3 run.py demo --mode mock --out examples/mock`。
- 真实矩阵：`python3 run.py --env-file ../.env demo --mode live --out examples/live`（项目根目录执行时通常用默认 `.env`）。
- 用本地 `.env` 中的密钥连接 DeepSeek 官方 API，返回模型字段为 `deepseek-flash`；未使用 Mock 替代真实请求。

## 实际结果

- 29 项自动测试全部通过，原始输出见 [test-results.txt](../examples/test-results.txt)。
- 离线矩阵 9/9 通过，30 步，见 [Mock 报告](../examples/mock/report.json)。
- 真实矩阵 9/9 通过，31 步，执行区间 14:49:43–14:50:19，逐次输出累计耗时 36.22 秒，Trace 中 `usage.total_tokens` 累计 53,879（包含每轮历史上下文，不等于独立文本长度；未据此推算账单），见 [live 报告](../examples/live/report.json)。

| 用例 | 预期 | 实际与证据 |
| --- | --- | --- |
| `answer` 明确答案 | 引用 D03 回答年假天数 | `answered`，3 步，引用 D03-S1/D03-S2；[输出](../examples/live/answer/output-1.json)、[Trace](../examples/live/answer/trace-1.jsonl) |
| `conflict` 文档冲突 | 列 D01/D02、请求确认，不擅自选 | `needs_clarification`，5 步，引用 D01/D02 各两段，给出待确认问题；[输出](../examples/live/conflict/output-1.json)、[Trace](../examples/live/conflict/trace-1.jsonl) |
| `out_of_scope` 资料外 | 改检索后仍无资料，说明缺口 | `not_found`，3 次检索、无引用、`gaps` 非空；[输出](../examples/live/out_of_scope/output-1.json)、[Trace](../examples/live/out_of_scope/trace-1.jsonl) |
| `paraphrase` 相似问题 | 改写成可检索关键词后命中 D03 | 首次检索即用“工作三年 年休假 天数”，`answered`，引用 D03；[Trace](../examples/live/paraphrase/trace-1.jsonl) |
| `followup` 上下文追问 | 结合历史把“那上限是多少”改写 | 检索词“加班 晚上十点以后 打车费 报销 上限”，命中 D10；[输出](../examples/live/followup/output-1.json)、[Trace](../examples/live/followup/trace-1.jsonl) |
| `tool_failure` 工具失败 | 重试一次后失败，不编造 | 连续两次 `SEARCH_UNAVAILABLE`，`failed`，无引用；[输出](../examples/live/tool_failure/output-1.json)、[Trace](../examples/live/tool_failure/trace-1.jsonl) |
| `tool_recovery` 瞬时失败 | 首轮失败、重试成功 | 第一次检索失败、第二次成功，`answered` D03；[Trace](../examples/live/tool_recovery/trace-1.jsonl) |
| `version` 旧版干扰 | 只引用 active 的 D08，说明旧版已失效 | 首轮 `finish` 因答案提到旧版日期/天数被 `UNSUPPORTED_NUMBER` 拒绝，模型删去旧数值后通过；[输出](../examples/live/version/output-1.json)、[Trace](../examples/live/version/trace-1.jsonl) |
| `step_limit` 执行上限 | 明确未完成 | 2 步后 `incomplete`，`stop_reason=step_limit`，无引用；[输出](../examples/live/step_limit/output-1.json) |

`not_found`、`failed`、`incomplete` 是预期结果，不能把“矩阵通过”理解为这些请求都得到了答案。所有输出都没有真实发布或写入文档的动作。

## 两条核心真实决策路径

### 1. 正常回答（answer）

```text
search_docs(query="年假天数")
  → D03《员工年假制度》v2，active
read_doc(doc_id="D03")
  → 读到 D03-S1/D03-S2 原文
finish(status="answered", citations=[D03-S1, D03-S2])
  → 代码校验逐字引用与数字，通过
```

决策依据：检索结果只有一个高相关的 active 文档，读取原文后数字 1/3/5/10/15 均能在引用中找到，可以给出确定答案。Trace 第 1–3 步见 [answer/trace-1.jsonl](../examples/live/answer/trace-1.jsonl)。

### 2. 冲突处理（conflict）

```text
search_docs(query="报销期限")
  → 命中 D01（30 天，总部）、D02（15 天，华东子公司）、D09（草稿，60 天）
  → conflict_signals 提示 D01/D02 的“报销时限”数值不同
read_doc(D01) → read_doc(D02)
finish(needs_clarification, citations=[D01-S1/D01-S2/D02-S1/D02-S2])
  → 第一次被 UNSUPPORTED_NUMBER 拒绝：答案提到草稿编号 D09，数字 09 未被引用支持
  → 模型删去 D09 编号、保留“草稿未生效”的说明，再次 finish → 通过
```

决策依据：两份文档都是 active，且没有给全局优先级；用户问题没有说明所属主体，所以不能自行选 30 或 15。最终答案分别引用两份原文，并问“集团总部/直属部门，还是华东子公司”。

第一次 `finish` 被拒绝是**代码校验生效**的证据，不是测试失败：模型观察到 `UNSUPPORTED_NUMBER` 后调整了答案。完整 Trace 见 [conflict/trace-1.jsonl](../examples/live/conflict/trace-1.jsonl)。

### 3. 资料外与工具失败

- `out_of_scope`：模型连续三次改写检索词（“公司营业收入 年度财务数据”“财务报告 营收 经营业绩 披露”“年度经营目标 收入指标 公司业绩数据查询”），均无命中，最后 `finish(not_found)` 并列出缺口。没有引用、没有编造数字。
- `tool_failure`：两次 `SEARCH_UNAVAILABLE` 后 `finish(failed)`，答案明确说明检索不可用；Trace 中没有引用。
- `tool_recovery`：第一次 `SEARCH_UNAVAILABLE`、第二次成功，随后正常读取 D03 并回答，证明“失败观察 → 重试 → 成功”不是预设日志。

## 首次真实运行的失败与改进

第一次真实冲突运行（14:47:11–14:47:13）在第 2 步返回 `INVALID_MODEL_RESPONSE`，运行以 `incomplete/model_error` 结束，没有生成答案。当时的错误信息只说明“每轮必须返回恰好一个完整工具调用”，未区分是输出长度还是调用数量；把请求的 `max_tokens` 从 1200 提高到 2000 后重跑，冲突路径通过。该改动只放宽输出预算，不改变工具权限、引用校验或冲突校验。失败记录保留在 [examples/failures/initial-live/conflict](../examples/failures/initial-live/conflict/trace-1.jsonl)，不计入最终通过率。

另外两次“模型先被拒绝、再修正”的真实过程：

- `conflict`：答案提到草稿编号 D09，`UNSUPPORTED_NUMBER` 拒绝数字 `09`；模型删去编号后通过。
- `version`：答案提到旧版 D07 的 `2025` 和 `7 天`，但只引用了 active 的 D08；`UNSUPPORTED_NUMBER` 拒绝后，模型改为“旧版已失效、不引用其数值”并通过。

这说明 `finish` 的校验不是形式检查：它会改变模型下一步的输出，且 Trace 保留了拒绝原因。

## 自动测试覆盖

29 项测试分为两组：

- `AgentTests`：9 组离线验收矩阵；检索不误命中；active 优先与冲突信号；引用逐字校验；空白归一化；未读引用拒绝；旧版引用拒绝；编造数字拒绝；元数据日期数字放行；冲突至少两份文档；未消解冲突守卫与修正；`not_found` 缺口语义；`answered` 必须有引用；脚本模型被拒绝后重试；执行上限；模型错误不降级 Mock；追问历史改写；相似问题改写；输入校验；输出目录保护；CLI 成功/失败退出码。
- `ModelAdapterTests`：DeepSeek 原生工具协议；缺少密钥不静默降级；HTTP 错误不泄露上游 body；多工具调用拒绝；畸形响应拒绝；`.env` 解析。

## 限制

- 检索是关键词/字符 bigram，不是语义向量检索；同义改写依赖模型，复杂问法可能不如 embedding 稳定。
- 合成文档只有 12 份，冲突/旧版/草稿是刻意样本；真实企业文档的格式、权限和更新频率远更复杂。
- 真实矩阵是有限样本的一轮运行；`temperature=0` 也不保证跨模型版本完全一致。
- 引用校验只保证“原文存在且被读取”，不证明模型对原文的解释一定正确；数字校验只覆盖阿拉伯数字，中文数字（如“三十天”）不会触发同一检查。
- 不支持文档权限、多租户、文档上传/解析、实时同步或多轮会话持久化。
- `conflict_signals` 只比较同一章节标题下的数字差异；标题不同但语义冲突的情况不会被自动提示。
