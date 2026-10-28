# 合成文档库说明

`docs.json` 是按 `doc_id` 索引的 12 份合成制度/FAQ。所有内容均为工程考核用虚构资料，不来自真实公司。

## 结构

每份文档包含：

- `title`、`version`、`effective_date`；
- `status`：`active`（现行）、`superseded`（已被替代）、`draft`（草稿，未生效）；
- `scope`、`department`、`tags`；
- `sections`：`id`、`heading`、`text`。

## 刻意设计的场景

| 文档 | 作用 |
| --- | --- |
| D01 / D02 | 报销期限 30 天 vs 15 天，两个 active，无全局优先级，用于冲突验收 |
| D03 | 年假天数，正常回答与相似问法 |
| D07 / D08 | 退款政策 v1（superseded，7 天）vs v2（active，15 天），用于旧版干扰 |
| D09 | 报销补充说明草稿（draft，60 天），不能作为依据 |
| D10 | 加班餐补/打车，用于上下文追问 |
| D04 / D05 / D06 / D11 / D12 | 信息安全、采购、远程办公、住宿、会议室，作为检索噪声与正常主题 |

## 修改建议

可以直接改 `text` 或 `tags` 来观察检索与回答变化，但改完后应：

1. 重跑 `python3 -m unittest discover -s tests -v`；
2. 重跑 `python3 run.py demo --mode mock --out runtime/...`；
3. 若改动了被 live 证据引用的内容，不要覆盖 `examples/live/`，输出到 `runtime/` 并在文档中说明差异；
4. 注意测试和演示断言可能依赖具体数值（例如“10 天”）。
