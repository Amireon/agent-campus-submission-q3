"""Bounded model -> tool -> observation loop with auditable traces."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .domain import canonical, now, validate_question
from .model import ModelError
from .tools import TOOL_SCHEMAS, Tools

SYSTEM_PROMPT = """你是企业知识问答 Agent，面向员工回答制度或产品问题。
只能依据 search_docs / read_doc 返回的合成文档作答，不得使用常识补全，也不得编造制度、数字或引用。

工作规则：
1. 先检索；检索为空或明显不相关时，改写关键词再次检索，最多检索 3 次。
2. 遇到“那……”“这个……”等追问时，先结合 history 改写成独立问题再检索，不要把指代词直接当检索词。
3. 引用前必须 read_doc；只能引用 status=active 的文档；superseded 或 draft 只能说明其存在，不能作为依据。
4. 若多份现行文档对同一事项给出不同数值，且没有足够信息判断适用范围或优先级，必须列出冲突、逐字引用并请求用户确认，不得自行选择其中一份。
5. 资料不足时明确说明缺口；不得用常识补出一个答案。
6. 工具失败时可重试一次；仍失败则如实结束为 failed，不得假装成功。
7. 每轮只调用一个工具；完成后调用 finish。answer 中的每个数字都必须能在 citations 的原文或所引文档元数据中找到。
8. citations.quote 必须逐字复制 read_doc 返回的章节原文；引用未被读取的文档会被代码拒绝。
9. needs_clarification 必须给出具体问题，且至少引用两份不同的现行文档；not_found 必须说明缺口。"""


def run_question(payload, model, docs: dict, *, max_steps=10, max_tool_calls=8,
                 failures: dict | None = None, trace_path: Path | None = None):
    if not all(isinstance(n, int) and n > 0 for n in (max_steps, max_tool_calls)):
        raise ValueError("执行上限必须为正整数")
    request = validate_question(payload)
    tools = Tools(docs, request["question"], failures)
    started, start_clock = now(), time.monotonic()
    events = []
    steps = tool_calls = 0
    stop_reason = None

    def record(event):
        events.append(event)
        if trace_path:
            trace_path.parent.mkdir(parents=True, exist_ok=True)
            with trace_path.open("a", encoding="utf-8") as handle:
                handle.write(canonical(event) + "\n")

    if trace_path:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("", encoding="utf-8")
    record({"event": "start", "question_id": request["question_id"],
            "question": request["question"], "history": request["history"],
            "mode": model.mode, "at": now()})

    context = {"question_id": request["question_id"], "question": request["question"],
               "history": request["history"]}
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": canonical(context)}]
    while not tools.finished:
        if steps >= max_steps or tool_calls >= max_tool_calls:
            stop_reason = "step_limit" if steps >= max_steps else "tool_call_limit"
            record({"event": "stopped", "reason": stop_reason, "step": steps,
                    "tool_calls": tool_calls, "at": now(), "mode": model.mode})
            break
        steps += 1
        try:
            reply = model.decide(messages, TOOL_SCHEMAS)
            assistant = reply["assistant"]
            calls = assistant.get("tool_calls", [])
            if len(calls) != 1:
                raise ModelError("INVALID_MODEL_RESPONSE: exactly one tool call required")
            call = calls[0]
            function = call["function"]
            action = function["name"]
            raw_arguments = function["arguments"]
            call_id = call["id"]
        except (ModelError, KeyError, TypeError, AttributeError) as exc:
            error = str(exc) if isinstance(exc, ModelError) else "INVALID_MODEL_RESPONSE"
            record({"event": "model_error", "step": steps, "error": error,
                    "mode": model.mode, "at": now()})
            stop_reason = "model_error"
            break
        tool_calls += 1
        try:
            args = json.loads(raw_arguments)
        except (ValueError, TypeError):
            args = None
        observation = tools.execute(action, args)
        record({"event": "action", "step": steps, "action": action, "arguments": args,
                "decision_summary": args.get("reason", "") if isinstance(args, dict) else "参数格式无效",
                "observation": observation, "provider": reply.get("metadata", {}), "at": now()})
        messages.append(assistant)
        messages.append({"role": "tool", "tool_call_id": call_id, "content": canonical(observation)})

    result = tools.result()
    run_status = result["status"] if tools.finished else "incomplete"
    answer = result["answer"]
    if run_status == "incomplete" and not answer:
        answer = "达到执行步数或工具调用上限，任务未完成；没有给出未经核实的答案。"
    output = {
        "question_id": request["question_id"],
        "question": request["question"],
        "history_used": bool(request["history"]),
        "run_status": run_status,
        "answer": answer,
        "citations": result["citations"],
        "gaps": result["gaps"],
        "clarification_question": result["clarification_question"],
        "mode": model.mode,
        "model": model.name,
        "started_at": started,
        "finished_at": now(),
        "elapsed_seconds": round(time.monotonic() - start_clock, 3),
        "steps": steps,
        "tool_calls": tool_calls,
        "limits": {"max_steps": max_steps, "max_tool_calls": max_tool_calls},
        "stop_reason": stop_reason,
        "last_error": result["last_error"],
        "conflict_signals": result["conflict_signals"],
    }
    return output, events
