"""CLI entry points for single questions and the offline/live acceptance matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import run_question
from .domain import DomainError, load_documents, load_json
from .model import DeepSeekModel, MockModel, ModelError, load_env


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parser():
    root = argparse.ArgumentParser(description="企业知识问答 Agent（仅依据合成文档作答）")
    root.add_argument("--env-file", type=Path, default=Path(".env"))
    commands = root.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask", help="回答一个问题")
    ask.add_argument("--input", type=Path, required=True)
    ask.add_argument("--mode", choices=["live", "mock"], default="live")
    ask.add_argument("--data-dir", type=Path, default=Path("data"))
    ask.add_argument("--out", type=Path)
    ask.add_argument("--trace", type=Path, default=Path("runtime/trace.jsonl"))
    ask.add_argument("--max-steps", type=int, default=10)
    ask.add_argument("--max-tool-calls", type=int, default=8)
    ask.add_argument("--simulate-search-failure", type=int, default=0)
    ask.add_argument("--simulate-read-failure", type=int, default=0)
    demo = commands.add_parser("demo", help="运行全部验收用例并校验")
    demo.add_argument("--mode", choices=["live", "mock"], default="mock")
    demo.add_argument("--cases", default="all")
    demo.add_argument("--out", type=Path)
    demo.add_argument("--data-dir", type=Path, default=Path("data"))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    load_env(args.env_file)
    try:
        if args.command == "demo":
            from .demo import run_demo
            report = run_demo(args.mode, args.cases, args.out, args.data_dir)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["passed"] else 1
        payload = load_json(args.input)
        failures = {}
        if args.simulate_search_failure > 0:
            failures["search"] = args.simulate_search_failure
        if args.simulate_read_failure > 0:
            failures["read"] = args.simulate_read_failure
        model = MockModel() if args.mode == "mock" else DeepSeekModel()
        output, _ = run_question(payload, model, load_documents(args.data_dir),
                                 max_steps=args.max_steps,
                                 max_tool_calls=args.max_tool_calls,
                                 failures=failures, trace_path=args.trace)
        if args.out:
            write_json(args.out, output)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 1 if output["run_status"] in {"failed", "incomplete"} else 0
    except (DomainError, ModelError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, DomainError) else type(exc).__name__
        message = str(exc) if isinstance(exc, (DomainError, ModelError)) else "配置、输入或本地文件错误"
        print(json.dumps({"error": code, "message": message}, ensure_ascii=False))
        return 2
