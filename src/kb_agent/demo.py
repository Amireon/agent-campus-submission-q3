"""Executable acceptance fixtures. Every artifact comes from a real run."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

from .agent import run_question
from .domain import DomainError, TZ, load_documents, normalize_text
from .model import DeepSeekModel, MockModel

CASES = ["answer", "conflict", "out_of_scope", "paraphrase", "followup",
         "tool_failure", "tool_recovery", "version", "step_limit"]

SPECS = {
    "answer": {
        "question": "年假有多少天？",
        "expect": {"status": "answered", "doc_ids": ["D03"], "answer_contains": ["10 天"]},
    },
    "conflict": {
        "question": "报销期限是多久？",
        "expect": {"status": "needs_clarification", "doc_ids": ["D01", "D02"],
                   "answer_contains": ["30 天", "15 天"], "clarification": True},
    },
    "out_of_scope": {
        "question": "公司今年的营业收入是多少？",
        "expect": {"status": "not_found", "no_citations": True, "gaps": True, "min_searches": 2},
    },
    "paraphrase": {
        "question": "我工作三年了能休几天？",
        "expect": {"status": "answered", "doc_ids": ["D03"], "answer_contains": ["10 天"],
                   "rewritten_query": True},
    },
    "followup": {
        "question": "那上限是多少？",
        "history": [
            {"role": "user", "content": "加班到晚上十点以后，打车费能报吗？"},
            {"role": "assistant", "content": "可以，依据《加班餐补与交通费》报销。"},
        ],
        "expect": {"status": "answered", "doc_ids": ["D10"], "answer_contains": ["80 元"],
                   "history_query": True},
    },
    "tool_failure": {
        "question": "年假有多少天？",
        "failures": {"search": 99},
        "expect": {"status": "failed", "no_citations": True, "search_failures_at_least": 2},
    },
    "tool_recovery": {
        "question": "年假有多少天？",
        "failures": {"search": 1},
        "expect": {"status": "answered", "doc_ids": ["D03"], "first_search_failed": True,
                   "answer_contains": ["10 天"]},
    },
    "version": {
        "question": "产品退款期限是多久？",
        "expect": {"status": "answered", "doc_ids": ["D08"], "answer_contains": ["15 天"]},
    },
    "step_limit": {
        "question": "年假有多少天？",
        "max_steps": 2,
        "expect": {"status": "incomplete", "stop_reason": "step_limit", "no_citations": True},
    },
}


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def verify(case, output, events, expect, question):
    check(output["run_status"] == expect["status"],
          f"期望 {expect['status']}，实际 {output['run_status']}")
    if "doc_ids" in expect:
        actual = {citation["doc_id"] for citation in output["citations"]}
        check(actual == set(expect["doc_ids"]), f"引用文档应为 {expect['doc_ids']}，实际 {sorted(actual)}")
    for text in expect.get("answer_contains", []):
        actual = normalize_text(output["answer"] or "")
        check(normalize_text(text) in actual, f"答案缺少 {text}")
    if expect.get("no_citations"):
        check(not output["citations"], "不应携带引用")
    if expect.get("gaps"):
        check(bool(output["gaps"]), "应说明资料缺口")
    if expect.get("clarification"):
        check(bool(output["clarification_question"]), "应给出待确认问题")
    if expect.get("stop_reason"):
        check(output["stop_reason"] == expect["stop_reason"], "停止原因不符")
    searches = [event for event in events
                if event.get("event") == "action" and event.get("action") == "search_docs"]
    if "min_searches" in expect:
        check(len(searches) >= expect["min_searches"], "检索次数不足")
    if expect.get("rewritten_query"):
        queries = [event["arguments"].get("query") for event in searches
                   if isinstance(event.get("arguments"), dict) and event["arguments"].get("query")]
        check(any(query != question for query in queries) or len(set(queries)) >= 2,
              "应把原问题改写成更可检索的关键词")
    if expect.get("history_query"):
        check(any("加班" in (event["arguments"].get("query") or "") or "打车" in (event["arguments"].get("query") or "")
                  for event in searches), "追问检索应结合历史改写")
    if expect.get("first_search_failed"):
        check(searches and searches[0]["observation"].get("ok") is False, "首轮检索应失败")
    if "search_failures_at_least" in expect:
        failures = [event for event in searches if not event["observation"].get("ok")]
        check(len(failures) >= expect["search_failures_at_least"], "应观察到工具失败")


def run_case(case, mode, out, data_dir):
    spec = SPECS[case]
    payload = {"question_id": f"demo-{case}-001", "question": spec["question"],
               "history": spec.get("history", [])}
    out = out / case
    write(out / "input-1.json", payload)
    model = MockModel() if mode == "mock" else DeepSeekModel()
    output, events = run_question(
        payload, model, load_documents(data_dir),
        max_steps=spec.get("max_steps", 10),
        max_tool_calls=8,
        failures=spec.get("failures"),
        trace_path=out / "trace-1.jsonl",
    )
    write(out / "output-1.json", output)
    try:
        verify(case, output, events, spec["expect"], spec["question"])
        verification = {"passed": True}
    except AssertionError as exc:
        verification = {"passed": False, "error": str(exc)}
    write(out / "verification.json", verification)
    return {"case": case, **verification, "status": output["run_status"],
            "steps": output["steps"], "mode": mode}


def run_demo(mode, selected, out, data_dir):
    cases = CASES if selected == "all" else selected.split(",")
    if not cases or set(cases) - set(CASES):
        raise DomainError("UNKNOWN_CASE", f"用例名称不支持；可选：{','.join(CASES)}")
    out = out or Path("runtime") / ("demo-" + mode + "-" + datetime.now(TZ).strftime("%Y%m%d-%H%M%S"))
    if out.exists() and any(out.iterdir()):
        raise DomainError("OUTPUT_EXISTS", "输出目录非空，请换一个目录以保留原始证据")
    out.mkdir(parents=True, exist_ok=True)
    if mode == "live":
        DeepSeekModel()  # Fail before emitting misleading live artifacts.
    results = []
    for case in cases:
        result = run_case(case, mode, out, data_dir)
        results.append(result)
        print(json.dumps({"case_completed": case, "passed": result["passed"],
                          "status": result["status"], "mode": mode}, ensure_ascii=False), flush=True)
    report = {"passed": all(result["passed"] for result in results), "mode": mode,
              "case_count": len(results), "cases": results, "output_dir": str(out),
              "boundary": "Mock 仅验证流程，不证明真实模型决策效果" if mode == "mock"
              else "实际 DeepSeek 工具调用；文档、工具和检索均为本地合成场景"}
    write(out / "report.json", report)
    return report
