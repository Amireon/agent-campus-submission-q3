from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kb_agent.agent import run_question
from kb_agent.cli import main
from kb_agent.demo import CASES, run_case
from kb_agent.domain import DomainError, canonical, load_documents, validate_question
from kb_agent.model import DeepSeekModel, MockModel, ModelError, load_env
from kb_agent.retrieval import search
from kb_agent.tools import TOOL_SCHEMAS, Tools

D01_QUOTE = "员工应在费用发生后 30 天内提交报销申请，逾期需部门负责人书面说明。"
D02_QUOTE = "员工应在费用发生后 15 天内提交报销申请。"
D03_QUOTE = "员工入职满 1 年享有 5 天年假；满 3 年享有 10 天年假；满 5 年享有 15 天年假。"


class ScriptedModel:
    """Adversarial test double; never used as live decision evidence."""

    mode = "mock"
    name = "adversarial-scripted-test-double"

    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, messages, schemas):
        action, args = next(self.actions)
        return {"assistant": {"role": "assistant", "content": None, "tool_calls": [
            {"id": f"test-{len(messages)}", "type": "function", "function": {
                "name": action, "arguments": canonical(args)}}]},
            "metadata": {"mode": "mock"}}


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for source in (ROOT / "data").glob("*.json"):
            (self.data / source.name).write_bytes(source.read_bytes())
        self.docs = load_documents(self.data)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, question="年假有多少天？", history=None, qid="test-001"):
        return {"question_id": qid, "question": question, "history": history or []}

    def run_agent(self, payload=None, actions=None, **kwargs):
        model = ScriptedModel(actions) if actions else MockModel()
        return run_question(payload or self.payload(), model, self.docs, **kwargs)

    def prepared(self, question="报销期限是多久？", query="报销期限"):
        tools = Tools(self.docs, question)
        tools.execute("search_docs", {"query": query, "reason": "检索"})
        return tools

    def test_all_offline_acceptance_cases(self):
        for case in CASES:
            with self.subTest(case=case):
                report = run_case(case, "mock", self.root / case, self.data)
                self.assertTrue(report["passed"], report)

    def test_search_no_false_positive(self):
        self.assertEqual(search(self.docs, "公司今年的营业收入是多少？")["hits"], [])
        self.assertEqual(search(self.docs, "那上限是多少？")["hits"], [])

    def test_search_prefers_active_and_detects_conflict(self):
        refund = search(self.docs, "产品退款期限")
        self.assertEqual(refund["hits"][0]["doc_id"], "D08")
        self.assertEqual(refund["conflict_signals"], [])
        conflict = search(self.docs, "报销期限")
        self.assertTrue(conflict["conflict_signals"])
        self.assertIn("D01", conflict["conflict_signals"][0]["doc_ids"])

    def test_quote_must_be_exact(self):
        tools = Tools(self.docs, "年假有多少天？")
        tools.execute("search_docs", {"query": "年假", "reason": "检索"})
        tools.execute("read_doc", {"doc_id": "D03", "reason": "读取"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "10 天",
            "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": "员工入职满 3 年享有 20 天年假"}],
            "gaps": [], "reason": "测试"})
        self.assertEqual(result["code"], "QUOTE_NOT_FOUND")

    def test_quote_whitespace_is_normalized(self):
        tools = Tools(self.docs, "年假有多少天？")
        tools.execute("search_docs", {"query": "年假", "reason": "检索"})
        tools.execute("read_doc", {"doc_id": "D03", "reason": "读取"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "10 天",
            "citations": [{"doc_id": "D03", "section_id": "D03-S1",
                           "quote": "员工入职满1年享有5天年假；满3年享有10天年假；满5年享有15天年假。"}],
            "gaps": [], "reason": "空格归一化"})
        self.assertTrue(result["ok"], result)

    def test_citation_requires_read(self):
        tools = Tools(self.docs, "年假有多少天？")
        tools.execute("search_docs", {"query": "年假", "reason": "检索"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "10 天",
            "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": D03_QUOTE}],
            "gaps": [], "reason": "未读就引用"})
        self.assertEqual(result["code"], "DOC_NOT_READ")

    def test_superseded_document_cannot_be_cited(self):
        tools = Tools(self.docs, "产品退款期限是多久？")
        tools.execute("search_docs", {"query": "产品退款期限", "reason": "检索"})
        tools.execute("read_doc", {"doc_id": "D07", "reason": "读取旧版"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "7 天",
            "citations": [{"doc_id": "D07", "section_id": "D07-S1",
                           "quote": "客户可在购买后 7 天内申请无理由退款。"}],
            "gaps": [], "reason": "引用旧版"})
        self.assertEqual(result["code"], "DOC_NOT_ACTIVE")

    def test_unsupported_number_rejected(self):
        tools = Tools(self.docs, "年假有多少天？")
        tools.execute("search_docs", {"query": "年假", "reason": "检索"})
        tools.execute("read_doc", {"doc_id": "D03", "reason": "读取"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "入职满 3 年可休 20 天",
            "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": D03_QUOTE}],
            "gaps": [], "reason": "编造数字"})
        self.assertEqual(result["code"], "UNSUPPORTED_NUMBER")

    def test_effective_date_numbers_are_allowed(self):
        tools = Tools(self.docs, "年假有多少天？")
        tools.execute("search_docs", {"query": "年假", "reason": "检索"})
        tools.execute("read_doc", {"doc_id": "D03", "reason": "读取"})
        result = tools.execute("finish", {
            "status": "answered", "answer": "自 2026 年 1 月 1 日起，满 3 年可休 10 天",
            "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": D03_QUOTE}],
            "gaps": [], "reason": "引用元数据日期"})
        self.assertTrue(result["ok"], result)

    def test_conflict_requires_two_documents(self):
        tools = self.prepared()
        tools.execute("read_doc", {"doc_id": "D01", "reason": "读取"})
        result = tools.execute("finish", {
            "status": "needs_clarification", "answer": "请确认制度",
            "citations": [{"doc_id": "D01", "section_id": "D01-S2", "quote": D01_QUOTE}],
            "gaps": [], "clarification_question": "适用哪份制度？", "reason": "冲突"})
        self.assertEqual(result["code"], "CITATIONS_REQUIRED")

    def test_unresolved_conflict_guard_and_correction(self):
        tools = self.prepared()
        tools.execute("read_doc", {"doc_id": "D01", "reason": "读取"})
        tools.execute("read_doc", {"doc_id": "D02", "reason": "读取"})
        bad = tools.execute("finish", {
            "status": "answered", "answer": "报销期限是 30 天",
            "citations": [{"doc_id": "D01", "section_id": "D01-S2", "quote": D01_QUOTE}],
            "gaps": [], "reason": "擅自选择"})
        self.assertEqual(bad["code"], "UNRESOLVED_CONFLICT")
        good = tools.execute("finish", {
            "status": "needs_clarification",
            "answer": "D01 规定 30 天，D02 规定 15 天，请确认适用制度。",
            "citations": [{"doc_id": "D01", "section_id": "D01-S2", "quote": D01_QUOTE},
                          {"doc_id": "D02", "section_id": "D02-S2", "quote": D02_QUOTE}],
            "gaps": [], "clarification_question": "请确认适用制度。", "reason": "冲突待确认"})
        self.assertTrue(good["ok"], good)

    def test_not_found_requires_gaps(self):
        tools = Tools(self.docs, "公司今年营业收入是多少？")
        result = tools.execute("finish", {
            "status": "not_found", "answer": "没有资料", "citations": [], "gaps": [],
            "reason": "测试"})
        self.assertEqual(result["code"], "GAPS_REQUIRED")

    def test_answered_requires_citation(self):
        tools = Tools(self.docs, "年假有多少天？")
        result = tools.execute("finish", {
            "status": "answered", "answer": "10 天", "citations": [], "gaps": [],
            "reason": "无引用"})
        self.assertEqual(result["code"], "CITATIONS_REQUIRED")

    def test_scripted_model_recovers_from_rejected_citation(self):
        actions = [
            ("search_docs", {"query": "年假", "reason": "检索"}),
            ("finish", {"status": "answered", "answer": "10 天",
                        "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": D03_QUOTE}],
                        "gaps": [], "reason": "未读先引用"}),
            ("read_doc", {"doc_id": "D03", "reason": "补读原文"}),
            ("finish", {"status": "answered", "answer": "满 3 年可休 10 天",
                        "citations": [{"doc_id": "D03", "section_id": "D03-S1", "quote": D03_QUOTE}],
                        "gaps": [], "reason": "原文支持"}),
        ]
        output, events = self.run_agent(actions=actions)
        self.assertEqual(output["run_status"], "answered")
        rejected = [event for event in events if event.get("action") == "finish"
                    and event["observation"].get("ok") is False]
        self.assertEqual(rejected[0]["observation"]["code"], "DOC_NOT_READ")

    def test_scripted_model_recovers_from_conflict_guard(self):
        actions = [
            ("search_docs", {"query": "报销期限", "reason": "检索"}),
            ("read_doc", {"doc_id": "D01", "reason": "读取"}),
            ("read_doc", {"doc_id": "D02", "reason": "读取"}),
            ("finish", {"status": "answered", "answer": "30 天",
                        "citations": [{"doc_id": "D01", "section_id": "D01-S2", "quote": D01_QUOTE}],
                        "gaps": [], "reason": "擅自选择"}),
            ("finish", {"status": "needs_clarification",
                        "answer": "D01 规定 30 天，D02 规定 15 天，请确认适用制度。",
                        "citations": [{"doc_id": "D01", "section_id": "D01-S2", "quote": D01_QUOTE},
                                      {"doc_id": "D02", "section_id": "D02-S2", "quote": D02_QUOTE}],
                        "gaps": [], "clarification_question": "请确认适用制度。", "reason": "请求确认"}),
        ]
        output, events = self.run_agent(payload=self.payload("报销期限是多久？"), actions=actions)
        self.assertEqual(output["run_status"], "needs_clarification")
        self.assertEqual(output["citations"][0]["doc_id"], "D01")

    def test_step_limit_returns_incomplete(self):
        output, _ = self.run_agent(max_steps=2)
        self.assertEqual(output["run_status"], "incomplete")
        self.assertEqual(output["stop_reason"], "step_limit")
        self.assertFalse(output["citations"])

    def test_model_failure_does_not_fallback_to_mock(self):
        class UnavailableModel:
            mode, name = "live", "deepseek-flash"

            def decide(self, messages, schemas):
                raise ModelError("MODEL_HTTP_503: unavailable")

        output, events = run_question(self.payload(), UnavailableModel(), self.docs, max_steps=5)
        self.assertEqual(output["run_status"], "incomplete")
        self.assertEqual(output["mode"], "live")
        self.assertEqual(output["stop_reason"], "model_error")
        self.assertEqual(events[-1]["event"], "model_error")

    def test_followup_uses_history_query(self):
        payload = self.payload("那上限是多少？", history=[
            {"role": "user", "content": "加班到晚上十点以后，打车费能报吗？"},
            {"role": "assistant", "content": "可以，依据《加班餐补与交通费》报销。"}])
        output, events = self.run_agent(payload=payload)
        self.assertEqual(output["run_status"], "answered")
        self.assertEqual([citation["doc_id"] for citation in output["citations"]], ["D10"])
        queries = [event["arguments"]["query"] for event in events
                   if event.get("action") == "search_docs"]
        self.assertTrue(any("加班" in query or "打车" in query for query in queries))

    def test_paraphrase_rewrites_query(self):
        output, events = self.run_agent(payload=self.payload("我工作三年了能休几天？"))
        self.assertEqual(output["run_status"], "answered")
        queries = [event["arguments"]["query"] for event in events
                   if event.get("action") == "search_docs"]
        self.assertTrue(any(query != "我工作三年了能休几天？" for query in queries))

    def test_invalid_input_rejected(self):
        for payload in [
            {"question_id": "x", "question": "问题", "extra": 1},
            {"question_id": "x", "question": ""},
            {"question_id": "x", "question": "问题", "history": [{"role": "system", "content": "x"}]},
            {"question_id": "x", "question": "问题", "history": [{"role": "user", "content": ""}]},
        ]:
            with self.subTest(payload=payload):
                with self.assertRaises(DomainError):
                    validate_question(payload)

    def test_demo_requires_empty_output_dir(self):
        target = self.root / "nonempty"
        target.mkdir()
        (target / "keep.txt").write_text("keep")
        from kb_agent.demo import run_demo
        with self.assertRaisesRegex(DomainError, "输出目录非空"):
            run_demo("mock", "answer", target, self.data)

    def test_cli_ask_and_trace(self):
        input_path = self.root / "input.json"
        input_path.write_text(canonical(self.payload()), encoding="utf-8")
        output_path = self.root / "out.json"
        trace_path = self.root / "trace.jsonl"
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = main(["--env-file", str(self.root / "missing.env"), "ask", "--mode", "mock",
                         "--input", str(input_path), "--data-dir", str(self.data),
                         "--out", str(output_path), "--trace", str(trace_path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["run_status"], "answered")
        self.assertTrue(output_path.is_file())
        self.assertTrue(trace_path.is_file())

    def test_cli_failure_exit_code(self):
        input_path = self.root / "failure-input.json"
        input_path.write_text(canonical(self.payload()), encoding="utf-8")
        with patch("sys.stdout", new_callable=io.StringIO):
            code = main(["--env-file", str(self.root / "missing.env"), "ask", "--mode", "mock",
                         "--input", str(input_path), "--data-dir", str(self.data),
                         "--trace", str(self.root / "failure-trace.jsonl"),
                         "--simulate-search-failure", "9"])
        self.assertEqual(code, 1)


class ModelAdapterTests(unittest.TestCase):
    def test_deepseek_native_tool_protocol(self):
        captured = {}

        def opener(request, timeout):
            captured["payload"] = json.loads(request.data)
            captured["url"] = request.full_url
            return io.BytesIO(json.dumps({
                "id": "provider-response", "model": "deepseek-flash",
                "choices": [{"finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": None, "reasoning_content": "must never be collected",
                    "tool_calls": [{"id": "native-call", "type": "function",
                                    "function": {"name": "search_docs",
                                                 "arguments": "{\"query\":\"年假\",\"reason\":\"检索\"}"}}]}}],
                "usage": {"total_tokens": 42}}).encode())

        model = DeepSeekModel(api_key="synthetic-not-real", opener=opener)
        reply = model.decide([{"role": "user", "content": "test"}], TOOL_SCHEMAS)
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["payload"]["model"], "deepseek-flash")
        self.assertEqual(captured["payload"]["thinking"], {"type": "disabled"})
        self.assertEqual(reply["assistant"]["tool_calls"][0]["id"], "native-call")
        self.assertNotIn("reasoning_content", reply["assistant"])

    def test_missing_key_has_no_silent_mock_fallback(self):
        with self.assertRaisesRegex(ModelError, "MISSING_API_KEY"):
            DeepSeekModel(api_key="")

    def test_http_error_does_not_expose_provider_body(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 401, "secret-body", {}, io.BytesIO(b"synthetic-secret"))

        model = DeepSeekModel(api_key="synthetic-key", opener=opener)
        with self.assertRaisesRegex(ModelError, "MODEL_HTTP_401") as caught:
            model.decide([], TOOL_SCHEMAS)
        self.assertNotIn("secret", str(caught.exception))

    def test_multiple_calls_rejected(self):
        call = {"id": "one", "type": "function",
                "function": {"name": "search_docs", "arguments": "{}"}}

        def opener(request, timeout):
            return io.BytesIO(json.dumps({"choices": [{"message": {"tool_calls": [call, call]}}]}).encode())

        with self.assertRaisesRegex(ModelError, "INVALID_MODEL_RESPONSE"):
            DeepSeekModel(api_key="synthetic", opener=opener).decide([], TOOL_SCHEMAS)

    def test_malformed_provider_shapes(self):
        for message in [42, [], {"tool_calls": ["invalid"]}]:
            with self.subTest(message=message):
                def opener(request, timeout):
                    return io.BytesIO(json.dumps({"choices": [{"message": message}]}).encode())
                with self.assertRaisesRegex(ModelError, "INVALID_MODEL_RESPONSE"):
                    DeepSeekModel(api_key="synthetic", opener=opener).decide([], TOOL_SCHEMAS)

    def test_env_loader_accepts_assignments_and_literal_key(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            path = Path(temp) / ".env"
            path.write_text('DEEPSEEK_API_KEY = "synthetic-key"\nDEEPSEEK_MODEL=deepseek-flash\n')
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "synthetic-key")
            os.environ.pop("DEEPSEEK_API_KEY")
            path.write_text("sk-synthetic-literal")
            load_env(path)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-synthetic-literal")


if __name__ == "__main__":
    unittest.main()
