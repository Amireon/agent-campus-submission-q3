"""Input validation, synthetic document loading, and grounding helpers."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
STATUSES = {"active", "superseded", "draft"}
FINAL_STATUSES = {"answered", "needs_clarification", "not_found", "failed", "incomplete"}
UNIT = r"(?:天|日|小时|分钟|万元|元|%|次|年|个月|周|个工作日)"
UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(" + UNIT + r")")
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
PUNCT_RE = re.compile(r"[，。；：！？、,.!?;:'\"“”‘’（）()《》【】\[\]【】·]")


class DomainError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"\s+", "", value)


def normalize_quote(value: str) -> str:
    return PUNCT_RE.sub("", normalize_text(value))


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_documents(data_dir: Path) -> dict:
    path = data_dir / "docs.json"
    try:
        raw = load_json(path)
    except (OSError, ValueError) as exc:
        raise DomainError("DATA_LOAD_FAILED", "无法读取合成文档库") from exc
    if not isinstance(raw, dict) or not raw:
        raise DomainError("DATA_LOAD_FAILED", "文档库必须是按 doc_id 索引的非空对象")
    docs = {}
    for doc_id, doc in raw.items():
        if not isinstance(doc_id, str) or not isinstance(doc, dict):
            raise DomainError("INVALID_DOCUMENT", "文档结构无效")
        required = {"title", "version", "effective_date", "status", "scope", "department", "tags", "sections"}
        if set(doc) != required:
            raise DomainError("INVALID_DOCUMENT", f"{doc_id} 字段不符合约定")
        if doc["status"] not in STATUSES:
            raise DomainError("INVALID_DOCUMENT", f"{doc_id} 状态无效")
        if not isinstance(doc["sections"], list) or not doc["sections"]:
            raise DomainError("INVALID_DOCUMENT", f"{doc_id} 缺少章节")
        seen = set()
        for section in doc["sections"]:
            if set(section) != {"id", "heading", "text"}:
                raise DomainError("INVALID_DOCUMENT", f"{doc_id} 章节字段无效")
            if section["id"] in seen:
                raise DomainError("INVALID_DOCUMENT", f"{doc_id} 章节 ID 重复")
            seen.add(section["id"])
        docs[doc_id] = doc
    return docs


def validate_question(raw) -> dict:
    if not isinstance(raw, dict) or set(raw) - {"question_id", "question", "history"}:
        raise DomainError("INVALID_INPUT", "输入仅允许 question_id、question、history")
    question_id = raw.get("question_id")
    if not isinstance(question_id, str) or not 1 <= len(question_id) <= 100:
        raise DomainError("INVALID_INPUT", "question_id 必须是 1–100 字符的字符串")
    question = raw.get("question")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 500:
        raise DomainError("INVALID_INPUT", "question 必须是 1–500 字符的字符串")
    history = raw.get("history", [])
    if not isinstance(history, list) or len(history) > 8:
        raise DomainError("INVALID_INPUT", "history 必须是最多 8 条的数组")
    clean = []
    for message in history:
        if not isinstance(message, dict) or set(message) != {"role", "content"}:
            raise DomainError("INVALID_INPUT", "history 每条必须只含 role 和 content")
        if message["role"] not in {"user", "assistant"}:
            raise DomainError("INVALID_INPUT", "history role 必须是 user 或 assistant")
        if not isinstance(message["content"], str) or not 1 <= len(message["content"]) <= 2000:
            raise DomainError("INVALID_INPUT", "history content 必须是 1–2000 字符")
        clean.append({"role": message["role"], "content": message["content"]})
    return {"question_id": question_id, "question": question.strip(), "history": clean}


def doc_section(doc: dict, section_id: str):
    for section in doc["sections"]:
        if section["id"] == section_id:
            return section
    return None


def answer_numbers(text: str) -> set[str]:
    return set(NUMBER_RE.findall(normalize_text(text)))


def allowed_numbers(citations: list, docs: dict, question: str) -> set[str]:
    allowed = set()
    for citation in citations:
        allowed |= answer_numbers(citation["quote"])
        doc = docs[citation["doc_id"]]
        allowed |= answer_numbers(citation["doc_id"])
        allowed |= answer_numbers(doc["version"])
        allowed |= answer_numbers(doc["effective_date"])
    allowed |= answer_numbers(question)
    return allowed
