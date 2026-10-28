"""Business tools. The model can search, read, and finish; it cannot invent evidence."""

from __future__ import annotations

from .domain import (DomainError, allowed_numbers, answer_numbers, doc_section,
                     normalize_quote)
from .retrieval import search

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_docs",
            "description": "检索合成制度/FAQ。返回文档摘要、命中章节和可能的冲突信号；无结果时可改写关键词再次检索。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词；追问时应先结合历史改写成独立问题"},
                    "reason": {"type": "string", "description": "一句基于已观察事实的简短动作依据"},
                },
                "required": ["query", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_doc",
            "description": "读取指定文档的完整章节原文。引用前必须读取；只能引用 active 文档。",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "检索结果中的文档 ID"},
                    "reason": {"type": "string", "description": "一句简短动作依据"},
                },
                "required": ["doc_id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "结束问答并提交答案、逐字引用、资料缺口或待确认问题。代码会校验引用原文、文档状态和答案数字。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["answered", "needs_clarification", "not_found", "failed", "incomplete"]},
                    "answer": {"type": "string", "description": "面向用户的回答；不得包含引用原文不支持的数字"},
                    "citations": {
                        "type": "array",
                        "description": "逐字引用；每项必须来自已读取的 active 文档",
                        "items": {
                            "type": "object",
                            "properties": {
                                "doc_id": {"type": "string"},
                                "section_id": {"type": "string"},
                                "quote": {"type": "string", "description": "原文逐字片段"},
                            },
                            "required": ["doc_id", "section_id", "quote"],
                            "additionalProperties": False,
                        },
                    },
                    "gaps": {"type": "array", "items": {"type": "string"}, "description": "资料缺口；not_found 时必填"},
                    "clarification_question": {"type": "string", "description": "需要用户确认的问题；needs_clarification 时必填"},
                    "reason": {"type": "string", "description": "一句简短动作依据"},
                },
                "required": ["status", "answer", "citations", "gaps", "reason"],
                "additionalProperties": False,
            },
        },
    },
]


class Tools:
    def __init__(self, docs: dict, question: str, failures: dict | None = None):
        self.docs = docs
        self.question = question
        self.failures = dict(failures or {})
        self.read_sections: set[tuple[str, str]] = set()
        self.read_docs: set[str] = set()
        self.conflict_signals: list = []
        self.last_error = None
        self.finished = False
        self.status = None
        self.final = None
        self.search_calls = 0
        self.read_calls = 0

    @staticmethod
    def _text(args, key, limit=5000):
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise DomainError("INVALID_ARGUMENTS", f"{key} 必须是 1–{limit} 字符的非空字符串")
        return value.strip()

    @staticmethod
    def validate(name, args):
        if not isinstance(args, dict):
            raise DomainError("INVALID_ARGUMENTS", "参数必须是对象")
        if name == "search_docs":
            if set(args) != {"query", "reason"}:
                raise DomainError("INVALID_ARGUMENTS", "search_docs 仅接受 query、reason")
            Tools._text(args, "query", 200)
            Tools._text(args, "reason")
        elif name == "read_doc":
            if set(args) != {"doc_id", "reason"}:
                raise DomainError("INVALID_ARGUMENTS", "read_doc 仅接受 doc_id、reason")
            Tools._text(args, "doc_id", 20)
            Tools._text(args, "reason")
        elif name == "finish":
            allowed = {"status", "answer", "citations", "gaps", "reason", "clarification_question"}
            if set(args) - allowed or not {"status", "answer", "citations", "gaps", "reason"} <= set(args):
                raise DomainError("INVALID_ARGUMENTS", "finish 参数缺失或包含未授权字段")
            if args["status"] not in {"answered", "needs_clarification", "not_found", "failed", "incomplete"}:
                raise DomainError("INVALID_ARGUMENTS", "status 无效")
            Tools._text(args, "answer", 4000)
            Tools._text(args, "reason")
            if "clarification_question" in args and not isinstance(args["clarification_question"], str):
                raise DomainError("INVALID_ARGUMENTS", "clarification_question 必须是字符串")
            for key in ("citations", "gaps"):
                if not isinstance(args[key], list):
                    raise DomainError("INVALID_ARGUMENTS", f"{key} 必须是数组")
            if len(args["citations"]) > 6 or len(args["gaps"]) > 6:
                raise DomainError("INVALID_ARGUMENTS", "引用或缺口最多 6 条")
            for citation in args["citations"]:
                if not isinstance(citation, dict) or set(citation) != {"doc_id", "section_id", "quote"}:
                    raise DomainError("INVALID_ARGUMENTS", "引用必须只含 doc_id、section_id、quote")
                for key in ("doc_id", "section_id", "quote"):
                    if not isinstance(citation[key], str) or not citation[key].strip():
                        raise DomainError("INVALID_ARGUMENTS", "引用字段必须是非空字符串")
            for gap in args["gaps"]:
                if not isinstance(gap, str) or not gap.strip() or len(gap) > 500:
                    raise DomainError("INVALID_ARGUMENTS", "缺口必须是非空字符串")
        else:
            raise DomainError("INVALID_TOOL", "动作不在工具白名单")

    def execute(self, name, args):
        try:
            self.validate(name, args)
            if name == "search_docs":
                return {"ok": True, **self._search(args["query"])}
            if name == "read_doc":
                return {"ok": True, **self._read(args["doc_id"])}
            return self._finish(args)
        except DomainError as exc:
            code = exc.code
            self.last_error = {"code": code, "message": str(exc)}
            return {"ok": False, "code": code, "message": str(exc),
                    "retryable": code in {"SEARCH_UNAVAILABLE", "READ_UNAVAILABLE"}}

    def _search(self, query: str):
        self.search_calls += 1
        if self.failures.get("search", 0) > 0:
            self.failures["search"] -= 1
            raise DomainError("SEARCH_UNAVAILABLE", "检索服务暂时不可用；可重试一次或说明失败")
        observation = search(self.docs, query)
        self.conflict_signals = observation["conflict_signals"]
        return observation

    def _read(self, doc_id: str):
        self.read_calls += 1
        if self.failures.get("read", 0) > 0:
            self.failures["read"] -= 1
            raise DomainError("READ_UNAVAILABLE", "文档读取服务暂时不可用；可重试一次或说明失败")
        doc = self.docs.get(doc_id)
        if not doc:
            raise DomainError("DOC_NOT_FOUND", f"文档 {doc_id} 不存在")
        self.read_docs.add(doc_id)
        self.read_sections |= {(doc_id, section["id"]) for section in doc["sections"]}
        return {"doc_id": doc_id, "document": {**doc, "doc_id": doc_id}}

    def _check_citation(self, citation: dict):
        doc_id, section_id, quote = citation["doc_id"], citation["section_id"], citation["quote"]
        doc = self.docs.get(doc_id)
        if not doc:
            raise DomainError("CITATION_UNKNOWN_DOC", f"引用文档 {doc_id} 不存在")
        section = doc_section(doc, section_id)
        if not section:
            raise DomainError("CITATION_UNKNOWN_SECTION", f"引用章节 {section_id} 不存在")
        if (doc_id, section_id) not in self.read_sections:
            raise DomainError("DOC_NOT_READ", "引用前必须先 read_doc 读取该文档")
        if doc["status"] != "active":
            raise DomainError("DOC_NOT_ACTIVE", f"{doc_id} 状态为 {doc['status']}，不能作为现行依据")
        normalized_quote = normalize_quote(quote)
        if len(normalized_quote) < 4 or normalized_quote not in normalize_quote(section["text"]):
            raise DomainError("QUOTE_NOT_FOUND", "引用片段不是该章节的逐字原文")
        return doc

    @staticmethod
    def _number_key(value: str) -> str:
        if value.isdigit():
            return str(int(value))
        try:
            return str(float(value))
        except ValueError:
            return value

    def _scope_hit(self, answer: str, doc: dict) -> bool:
        scope = doc["scope"]
        aliases = {scope, scope.split("及")[0]}
        for token in ("总部", "子公司", "华东", "集团"):
            if token in scope:
                aliases.add(token)
        return any(alias and (alias in answer or alias in self.question) for alias in aliases)

    def _finish(self, args):
        status = args["status"]
        answer = args["answer"].strip()
        citations = args["citations"]
        gaps = [gap.strip() for gap in args["gaps"]]
        clarification = args.get("clarification_question", "").strip()
        if status == "answered":
            if not citations:
                raise DomainError("CITATIONS_REQUIRED", "answered 至少需要一条逐字引用")
            if clarification:
                raise DomainError("INVALID_ARGUMENTS", "answered 不能同时携带待确认问题")
        elif status == "needs_clarification":
            if len({citation["doc_id"] for citation in citations}) < 2:
                raise DomainError("CITATIONS_REQUIRED", "请求确认时至少引用两份不同文档")
            if not clarification:
                raise DomainError("CLARIFICATION_REQUIRED", "needs_clarification 必须给出具体待确认问题")
        elif status == "not_found":
            if citations:
                raise DomainError("INVALID_ARGUMENTS", "not_found 不应携带引用")
            if not gaps:
                raise DomainError("GAPS_REQUIRED", "not_found 必须说明资料缺口")
        elif status == "failed":
            if citations:
                raise DomainError("INVALID_ARGUMENTS", "failed 不应携带引用")
        elif status == "incomplete":
            if citations:
                raise DomainError("INVALID_ARGUMENTS", "incomplete 不应携带引用")

        for citation in citations:
            self._check_citation(citation)

        allowed = allowed_numbers(citations, self.docs, self.question)
        allowed_keys = {self._number_key(value) for value in allowed}
        missing = {value for value in answer_numbers(answer)
                   if self._number_key(value) not in allowed_keys}
        if missing:
            raise DomainError("UNSUPPORTED_NUMBER", f"答案中的数字 {sorted(missing)} 未被引用原文或文档元数据支持")

        cited_ids = {citation["doc_id"] for citation in citations}
        for signal in self.conflict_signals:
            docs = set(signal["doc_ids"])
            overlap = cited_ids & docs
            if not overlap:
                continue
            if status == "answered":
                if len(overlap) >= 2:
                    raise DomainError("UNRESOLVED_CONFLICT", "引用了多份冲突文档，不能直接标记 answered；请说明适用制度或请求确认")
                if not docs <= cited_ids:
                    for citation in citations:
                        if not self._scope_hit(answer, self.docs[citation["doc_id"]]):
                            raise DomainError("UNRESOLVED_CONFLICT", "存在同主题冲突文档；请说明适用范围或请求用户确认")
            if status == "needs_clarification":
                if not any(len(cited_ids & set(item["doc_ids"])) >= 2 for item in self.conflict_signals):
                    raise DomainError("CONFLICT_EVIDENCE_REQUIRED", "请求确认时必须引用同一冲突主题的至少两份现行文档")

        self.finished = True
        self.status = status
        self.final = {"status": status, "answer": answer, "citations": citations,
                      "gaps": gaps, "clarification_question": clarification,
                      "reason": args["reason"].strip()}
        return {"ok": True, "status": status, "citation_count": len(citations),
                "note": "已结束；引用与数字已通过代码复核"}

    def citations_detail(self) -> list:
        if not self.final:
            return []
        details = []
        for citation in self.final["citations"]:
            doc = self.docs[citation["doc_id"]]
            details.append({
                "doc_id": citation["doc_id"],
                "section_id": citation["section_id"],
                "quote": citation["quote"],
                "title": doc["title"],
                "version": doc["version"],
                "effective_date": doc["effective_date"],
                "scope": doc["scope"],
            })
        return details

    def result(self) -> dict:
        return {
            "status": self.status or "incomplete",
            "answer": self.final["answer"] if self.final else None,
            "citations": self.citations_detail(),
            "gaps": self.final["gaps"] if self.final else [],
            "clarification_question": self.final["clarification_question"] if self.final else "",
            "last_error": self.last_error,
            "conflict_signals": [
                {"topic": signal["topic"], "doc_ids": signal["doc_ids"]}
                for signal in self.conflict_signals
            ],
        }
