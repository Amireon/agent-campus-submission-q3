# 提交信息

- 题目：**3 — 企业知识问答 Agent**。
- 基座模型：DeepSeek Flash，API 模型名 `deepseek-flash`。
- 项目根目录：本目录（`q3/` 的内容作为独立仓库根）。
- GitHub URL：https://github.com/Amireon/agent-campus-submission-q3
- 最终 commit SHA：见 `git rev-parse HEAD` 或 tag `submission-v1`；提交表单中填写该 SHA。
- 仓库可见性：公开；如改为私有，需让指定评审账号可读。
- 默认分支：`master`（仓库创建时为空，首次推送使用 `master`）。

评审先读 [README.md](README.md) 和 [docs/03-evaluation.md](docs/03-evaluation.md)，再运行：

```bash
python3 -m unittest discover -s tests -v
python3 run.py demo --mode mock --out runtime/reviewer-offline
```

真实模型需要 `.env` 中的 `DEEPSEEK_API_KEY`：

```bash
python3 run.py demo --mode live --cases answer,conflict --out runtime/reviewer-live
```

`examples/live/` 已包含 9 组真实运行；重新运行会覆盖同名输出目录前先换目录，以保留原始证据。
