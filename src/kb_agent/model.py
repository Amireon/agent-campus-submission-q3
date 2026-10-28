"""DeepSeek native tool calling plus an explicit offline observation-based Mock."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .domain import canonical

KEYWORDS = ["加班", "餐补", "打车", "交通", "报销", "差旅", "年假", "休假", "退款",
            "采购", "远程", "住宿", "会议室", "信息安全", "网盘"]


class ModelError(RuntimeError):
    pass


def load_env(path: Path):
    """Small literal KEY=VALUE loader. Never executes shell or prints secrets."""
    if not path.is_file():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    meaningful = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    if len(meaningful) == 1 and meaningful[0].startswith("sk-") and "=" not in meaningful[0]:
        os.environ.setdefault("DEEPSEEK_API_KEY", meaningful[0])
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip().startswith("DEEPSEEK_"):
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


class DeepSeekModel:
    mode = "live"

    def __init__(self, api_key=None, base_url=None, model=None, timeout=None, opener=None):
        self.api_key = api_key if api_key is not None else os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.name = model or os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.timeout = float(timeout if timeout is not None else os.getenv("DEEPSEEK_TIMEOUT", "45"))
        if not self.api_key or self.api_key.startswith("your-"):
            raise ModelError("MISSING_API_KEY: 请在 .env 或环境变量配置 DEEPSEEK_API_KEY；离线运行显式使用 --mode mock")
        parsed = urlparse(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ModelError("INVALID_BASE_URL: 模型服务必须使用无内嵌凭据的 HTTPS URL")
        if not 1 <= self.timeout <= 120:
            raise ModelError("INVALID_TIMEOUT: DEEPSEEK_TIMEOUT 必须在 1–120 秒之间")
        self.opener = opener or urllib.request.urlopen
        self.calls = 0

    def decide(self, messages: list, schemas: list) -> dict:
        payload = {"model": self.name, "messages": messages, "tools": schemas,
                   "tool_choice": "required", "parallel_tool_calls": False,
                   "thinking": {"type": "disabled"}, "temperature": 0, "max_tokens": 2000,
                   "stream": False}
        request = urllib.request.Request(self.base_url + "/chat/completions",
                    data=canonical(payload).encode(),
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Content-Type": "application/json"})
        self.calls += 1
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise ModelError("MODEL_RESPONSE_TOO_LARGE")
            raw = json.loads(body)
            choice = raw["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls", [])
            if choice.get("finish_reason") == "length" or len(calls) != 1:
                raise ModelError(f"INVALID_MODEL_RESPONSE: finish_reason={choice.get('finish_reason')}, tool_calls={len(calls)}")
            call = calls[0]
            if call.get("type") != "function" or not isinstance(call.get("id"), str):
                raise ModelError("INVALID_MODEL_RESPONSE: 工具调用格式无效")
            assistant = {"role": "assistant", "content": message.get("content"), "tool_calls": calls}
            return {"assistant": assistant,
                    "metadata": {"mode": "live", "requested_model": self.name,
                                 "returned_model": raw.get("model"), "response_id": raw.get("id"),
                                 "usage": raw.get("usage", {})}}
        except urllib.error.HTTPError as exc:
            raise ModelError(f"MODEL_HTTP_{exc.code}: 模型请求失败，未切换 Mock") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise ModelError(f"MODEL_CONNECTION_ERROR: {type(exc).__name__}；未切换 Mock") from None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ModelError(f"INVALID_MODEL_RESPONSE: {type(exc).__name__}") from None


class MockModel:
    """Deterministic test double driven by observations, not a canned trace."""

    mode = "mock"
    name = "offline-observation-mock"

    def __init__(self):
        self.calls = 0

    def decide(self, messages: list, schemas: list) -> dict:
        self.calls += 1
        context = json.loads(messages[1]["content"])
        call_args, names = {}, {}
        for message in messages:
            for call in message.get("tool_calls", []):
                call_args[call["id"]] = json.loads(call["function"]["arguments"])
                names[call["id"]] = call["function"]["name"]
        actions = []
        for message in messages:
            if message["role"] == "tool":
                call_id = message["tool_call_id"]
                actions.append((names[call_id], call_args[call_id], json.loads(message["content"])))
        action, args = self._next(context, actions)
        return {"assistant": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"mock-{self.calls}", "type": "function", "function": {
                    "name": action, "arguments": canonical(args)}}]},
                "metadata": {"mode": "mock", "requested_model": self.name,
                             "returned_model": self.name, "usage": {}}}

    @staticmethod
    def _keywords(text: str) -> list:
        return [word for word in KEYWORDS if word in text]

    @staticmethod
    def _query(question: str, history: list) -> str:
        if history and (question.startswith(("那", "这")) or len(question) <= 8 or "上述" in question):
            context = " ".join(message["content"] for message in history[-4:] if message["role"] == "user") + " " + question
            words = MockModel._keywords(context)
            if words:
                return " ".join(words)
        return question

    @staticmethod
    def _rewrite(question: str, history: list, previous: str) -> str:
        if "年假" not in previous and ("休" in question or "假" in question):
            if "三年" in question:
                return "年假 3 年 天数"
            if "两年" in question:
                return "年假 2 年 天数"
            return "年假 天数"
        if "营业收入" in question or "营收" in question:
            return "营业收入 财报 收入"
        words = MockModel._keywords(" ".join(message["content"] for message in history[-4:] if message["role"] == "user") + " " + question)
        if words:
            return " ".join(words) + " 上限"
        return question + " 制度"

    @staticmethod
    def _failed(actions, kind):
        failures = [item for item in actions if item[0] == kind and not item[2].get("ok")]
        if failures and failures[-1][2].get("retryable") and len(failures) < 2:
            return "retry"
        return "stop"

    def _next(self, context, actions):
        question = context["question"]
        history = context.get("history", [])
        last = actions[-1] if actions else None
        search_actions = [item for item in actions if item[0] == "search_docs"]
        read_actions = [item for item in actions if item[0] == "read_doc"]

        if last and last[0] == "search_docs" and not last[2].get("ok"):
            if self._failed(actions, "search_docs") == "retry":
                return "search_docs", {"query": last[1]["query"], "reason": "检索失败，按可重试标记再试一次"}
            return "finish", {"status": "failed", "citations": [], "gaps": ["检索服务不可用"],
                              "answer": "检索工具持续不可用，无法给出有依据的答案。",
                              "reason": "工具失败且不可恢复"}
        if last and last[0] == "read_doc" and not last[2].get("ok"):
            if self._failed(actions, "read_doc") == "retry":
                return "read_doc", {"doc_id": last[1]["doc_id"], "reason": "读取失败，按可重试标记再试一次"}
            return "finish", {"status": "failed", "citations": [], "gaps": ["文档读取服务不可用"],
                              "answer": "文档读取工具持续不可用，无法核对原文。",
                              "reason": "工具失败且不可恢复"}

        if not search_actions:
            return "search_docs", {"query": self._query(question, history),
                                   "reason": "先检索与问题相关的制度或 FAQ"}

        last_search = search_actions[-1]
        observation = last_search[2]
        if observation.get("ok") and not observation["hits"]:
            if len(search_actions) < 3:
                return "search_docs", {"query": self._rewrite(question, history, last_search[1]["query"]),
                                       "reason": "首轮无结果，改写关键词补充检索"}
            return "finish", {"status": "not_found", "citations": [],
                              "gaps": ["未检索到与问题相关的现行制度或 FAQ"],
                              "answer": "现有合成资料中没有找到与该问题相关的制度或 FAQ，无法给出有依据的答复。",
                              "reason": "补充检索后仍无资料"}

        hits = observation["hits"]
        signals = observation.get("conflict_signals", [])
        if signals:
            signal = signals[0]
            read_ids = {item[1]["doc_id"] for item in read_actions if item[2].get("ok")}
            for doc_id in signal["doc_ids"]:
                if doc_id not in read_ids:
                    return "read_doc", {"doc_id": doc_id, "reason": "读取冲突文档原文以核对引用"}
            documents = {item[1]["doc_id"]: item[2]["document"] for item in read_actions
                         if item[2].get("ok") and "document" in item[2]}
            citations, values, scopes = [], [], []
            for entry in signal["documents"]:
                doc = documents.get(entry["doc_id"])
                if not doc:
                    continue
                section = next(section for section in doc["sections"] if section["id"] == entry["section_id"])
                citations.append({"doc_id": entry["doc_id"], "section_id": entry["section_id"],
                                  "quote": section["text"]})
                values.append(f"{entry['doc_id']} 规定 {'、'.join(entry['values'])}")
                scopes.append(doc["scope"])
            if len(citations) < 2:
                return "finish", {"status": "failed", "citations": [], "gaps": ["冲突文档读取不完整"],
                                  "answer": "无法完整读取冲突文档，不能给出结论。",
                                  "reason": "证据不足"}
            return "finish", {
                "status": "needs_clarification",
                "answer": "检索到多份现行制度对同一事项规定不一致：" + "；".join(values) + "。请确认适用哪一份制度。",
                "citations": citations,
                "gaps": [],
                "clarification_question": "请确认适用制度：" + "，还是".join(scopes) + "？",
                "reason": "发现同主题冲突，需用户确认适用范围",
            }

        active = [hit for hit in hits if hit["status"] == "active"]
        if not active:
            return "finish", {"status": "not_found", "citations": [],
                              "gaps": ["只检索到已废止或草稿状态的文档，没有可引用的现行依据"],
                              "answer": "只找到已废止或草稿状态的资料，不能作为现行答案。",
                              "reason": "无 active 文档可作为依据"}
        target = active[0]
        read_ids = {item[1]["doc_id"] for item in read_actions if item[2].get("ok")}
        if target["doc_id"] not in read_ids:
            return "read_doc", {"doc_id": target["doc_id"], "reason": "读取相关文档原文以核对引用"}
        documents = {item[1]["doc_id"]: item[2]["document"] for item in read_actions if item[2].get("ok")}
        doc = documents[target["doc_id"]]
        section_id = target["sections"][0]["section_id"]
        section = next(section for section in doc["sections"] if section["id"] == section_id)
        answer = f"依据 {doc['doc_id']}《{doc['title']}》{doc['version']}：{section['text']}"
        return "finish", {
            "status": "answered",
            "answer": answer,
            "citations": [{"doc_id": doc["doc_id"], "section_id": section["id"], "quote": section["text"]}],
            "gaps": [],
            "reason": "原文逐字支持该回答",
        }
