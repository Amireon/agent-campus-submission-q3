# 运行材料索引

- `live/`：实际 DeepSeek Flash 决策，文档/检索/工具均为本地合成场景。
- `mock/`：离线确定性流程验证，**不能作为真实决策证据**。
- `failures/initial-live/`：首次真实冲突运行失败记录，不计入最终通过率。

每个场景提供 `input-1.json`、`output-1.json`、`trace-1.jsonl`、`verification.json`。`report.json` 汇总 9 组结果。

## 场景

| 场景 | 真实运行 | 离线运行 | 实际状态 |
| --- | --- | --- | --- |
| 明确答案 | [live/answer](live/answer) | [mock/answer](mock/answer) | answered，引用 D03 |
| 文档冲突 | [live/conflict](live/conflict) | [mock/conflict](mock/conflict) | needs_clarification，引用 D01/D02 |
| 资料外问题 | [live/out_of_scope](live/out_of_scope) | [mock/out_of_scope](mock/out_of_scope) | not_found，3 次检索 |
| 相似问题改写 | [live/paraphrase](live/paraphrase) | [mock/paraphrase](mock/paraphrase) | answered，改写后引用 D03 |
| 上下文追问 | [live/followup](live/followup) | [mock/followup](mock/followup) | answered，结合历史引用 D10 |
| 工具持续失败 | [live/tool_failure](live/tool_failure) | [mock/tool_failure](mock/tool_failure) | failed，无引用 |
| 工具瞬时失败 | [live/tool_recovery](live/tool_recovery) | [mock/tool_recovery](mock/tool_recovery) | 第一次失败、第二次成功后 answered |
| 旧版干扰 | [live/version](live/version) | [mock/version](mock/version) | answered，只引用 D08；首次 finish 被拒绝后修正 |
| 执行上限 | [live/step_limit](live/step_limit) | [mock/step_limit](mock/step_limit) | incomplete，stop_reason=step_limit |

## Trace 字段

每行一个 JSON 事件：

- `start`：问题、历史、模式。
- `action`：`step`、`action`、`arguments`（工具参数）、`observation`（工具返回）、`decision_summary`（模型给出的一句公开依据）、`provider`（真实模式含 `response_id`、返回模型名、token 用量）、`at`。
- `stopped` / `model_error`：上限或模型错误的停止原因。

没有记录模型内部思维过程、认证头或密钥。

## 失败记录

`failures/initial-live/conflict/` 是 14:47 第一次真实冲突运行：第 2 步返回 `INVALID_MODEL_RESPONSE`，输出 `incomplete/model_error`。之后把 `max_tokens` 从 1200 提高到 2000，重跑通过；详情见 [docs/03-evaluation.md](../docs/03-evaluation.md)。

## 其他文件

- `test-results.txt`：`unittest` 的实际输出（29 项）。
- `clean-checkout-verification.json`：在临时目录复制项目、排除 `.env` 后运行测试与 mock 的实际结果。

`not_found`、`failed`、`incomplete` 都是预期状态；矩阵通过表示“行为符合预期”，不表示所有问题都被回答。
