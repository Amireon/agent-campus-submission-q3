# 企业知识问答 Agent（题目 3）

基座模型：**DeepSeek Flash（`deepseek-flash`）**。通过原生 function tools 实现“模型选择动作 → 执行工具 → 观察结果 → 模型调整下一步或结束”。运行入口为 CLI，资料全部为合成制度/FAQ，**不连接任何真实企业知识库**。

核心约束：回答必须来自本轮实际读取的 `active` 文档；引用必须逐字可核验；答案中的数字必须有引用原文或所引文档元数据支持；同主题冲突不得擅自裁决。

## 评审入口

- [需求、用户与成功标准](docs/01-requirements.md)
- [调用流程、动态决策与技术取舍](docs/02-solution.md)
- [测试、实际运行与失败记录](docs/03-evaluation.md)
- [AI 使用说明](docs/04-ai-usage.md)
- [复盘、用时与限制](docs/05-retrospective.md)
- [真实 DeepSeek 验收报告](examples/live/report.json)
- [正常路径 Trace](examples/live/answer/trace-1.jsonl)、[冲突路径 Trace](examples/live/conflict/trace-1.jsonl)
- [工具失败 Trace](examples/live/tool_failure/trace-1.jsonl)、[资料外 Trace](examples/live/out_of_scope/trace-1.jsonl)
- [离线验收报告](examples/mock/report.json)、[自动测试结果](examples/test-results.txt)
- [首次真实冲突运行失败记录](examples/failures/initial-live/conflict/trace-1.jsonl)

`answered`：有明确答案并带引用；`needs_clarification`：存在无法消解的冲突，已列出双方并请求确认；`not_found`：资料不足，已说明缺口；`failed`：工具持续失败；`incomplete`：达到执行上限或模型错误。**矩阵通过不等于所有问题都能回答**，`not_found`、`failed` 和 `incomplete` 都是预期结果。

## 环境与安装

Python **3.11 或更高版本**。运行和测试仅依赖标准库，无需安装框架、向量库或数据库。

```bash
python3 --version
python3 run.py --help
```

`requirements.txt` 标明无需第三方运行依赖；`run.py` 是零安装入口。以下命令均在仓库根目录执行。

## 离线运行（无需密钥）

```bash
python3 run.py demo --mode mock --out runtime/review-mock
python3 -m unittest discover -s tests -v
```

演示器在隔离目录运行 9 组场景，自动断言结果，并保存输入、实际输出、Trace 与校验结果。输出目录必须为空；重复演示请换目录。

**Mock 是确定性测试替身，只验证流程与代码约束，不证明真实模型的决策效果。真实模型失败时不会自动切换到 Mock。**

## 接入 DeepSeek Flash

复制 `.env.example` 为 `.env` 并填写真实密钥：

```dotenv
DEEPSEEK_API_KEY=你的真实密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-flash
DEEPSEEK_TIMEOUT=45
```

环境变量优先于 `.env`；也可以使用 `--env-file` 指向其他位置。密钥不会写入代码、Trace 或提交材料。

```bash
# 两条最容易核对的真实路径
python3 run.py demo --mode live --cases answer,conflict --out runtime/review-live

# 全部 9 组场景
python3 run.py demo --mode live --out runtime/review-live-all
```

真实模式使用官方 Chat Completions API、原生 function tools、非思考模式；不采集模型内部思维过程。每个动作的 `reason` 是可公开的简短事实依据。调用实现见 [src/kb_agent/model.py](src/kb_agent/model.py)。

接入依据：[DeepSeek 工具调用](https://api-docs.deepseek.com/guides/tool_calls/)、[Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)、[官方模型名](https://api-docs.deepseek.com/quick_start/pricing/)，核对日期 2026-10-28。

## 单问题与人工检查

输入示例：

```json
{
  "question_id": "demo-answer-001",
  "question": "年假有多少天？",
  "history": []
}
```

```bash
python3 run.py ask --mode mock \
  --input examples/live/answer/input-1.json \
  --out runtime/answer.json --trace runtime/answer.jsonl
```

追问示例见 `examples/live/followup/input-1.json`，其中 `history` 是上一轮对话。资料外问题见 `examples/live/out_of_scope/input-1.json`。工具失败可用本地故障注入复现：

```bash
python3 run.py ask --mode mock \
  --input examples/live/tool_failure/input-1.json \
  --trace runtime/failure.jsonl --simulate-search-failure 9
```

该命令返回非零退出码和 `failed` 状态，这是预期行为；不会伪造答案或引用。

## 状态与边界

- 状态：`answered / needs_clarification / not_found / failed / incomplete`。
- 引用：必须来自本轮 `read_doc` 读取过的 `active` 文档，且是章节原文的逐字片段（空白与标点归一化后比较）。
- 数字：答案中的数字必须能在引用片段、所引文档的 `doc_id`/`version`/`effective_date` 或用户问题中找到；`UNSUPPORTED_NUMBER` 会被工具拒绝，模型可据此修正。
- 冲突：工具会报告同一标题下多份 `active` 文档数值不同的 `conflict_signals`；这是观察而非决策。`finish` 会拒绝“只引用其中一份、却未说明适用范围”的 `answered`。
- 失效/草稿：`superseded` 与 `draft` 文档不能作为依据，引用会被 `DOC_NOT_ACTIVE` 拒绝。
- 追问：`history` 最多 8 条；模型需把“那……/这个……”改写成独立检索词。
- 上限：默认每题最多 10 轮模型决策/8 次工具调用、最多 3 次检索；达到上限返回 `incomplete` 和 `stop_reason`。可用 `--max-steps`、`--max-tool-calls` 降低上限。
- 检索是本地关键词/字符 bigram 排序，不是语义向量检索；合成文档只覆盖 12 个制度/FAQ，不构成真实企业知识库。
- 不支持真实文档权限、多租户、实时同步、任意文件导入、多轮会话持久化或线上部署。

## 目录

```text
src/kb_agent/        Agent 循环、工具、检索、模型接入、CLI、演示器
data/docs.json       12 份合成制度/FAQ（含冲突、旧版、草稿、资料外主题）
tests/               29 项可执行测试
examples/live/       实际 DeepSeek 运行（9 组）
examples/mock/       离线流程验证（9 组）
examples/failures/   首次真实冲突运行失败记录
docs/                五份说明文档
```
