"""Small deterministic lexical retriever for synthetic Chinese policy documents.

It is intentionally simple: character bigrams, tag/heading boosts, and a
minimum evidence rule. The model still decides whether to rewrite a query,
read a document, or ask for clarification.
"""

from __future__ import annotations

import re

from .domain import normalize_quote, normalize_text, UNIT_RE

RUN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    text = normalize_text(text)
    tokens: list[str] = []
    for run in RUN_RE.findall(text):
        if run[0].isascii():
            tokens.append(run)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _doc_tokens(doc: dict):
    tags = set(doc["tags"])
    title = set(tokenize(doc["title"]))
    heading, text = set(), set()
    for section in doc["sections"]:
        heading |= set(tokenize(section["heading"]))
        text |= set(tokenize(section["text"]))
    return tags, title, heading, text


def _numbers(text: str) -> set[str]:
    return {match.group(0) for match in UNIT_RE.finditer(normalize_text(text))}


def conflict_signals(hits: list) -> list:
    """Report same-topic numeric differences among active documents.

    This is an observation, not a decision: the model must read the documents
    and either resolve by scope/version or ask the user.
    """
    groups: dict[str, list] = {}
    for hit in hits:
        if hit["status"] != "active":
            continue
        for section in hit["sections"]:
            if section.get("score", 0) <= 0:
                continue
            key = normalize_quote(section["heading"])
            groups.setdefault(key, []).append({
                "doc_id": hit["doc_id"],
                "section_id": section["section_id"],
                "heading": section["heading"],
                "values": sorted(_numbers(section["text"])),
                "score": section["score"],
            })
    signals = []
    for key, entries in groups.items():
        doc_ids = {entry["doc_id"] for entry in entries}
        values = {tuple(entry["values"]) for entry in entries}
        if len(doc_ids) < 2 or len(values) < 2:
            continue
        signals.append({
            "topic": entries[0]["heading"],
            "doc_ids": sorted(doc_ids),
            "documents": entries,
            "note": "多份现行文档对同一事项给出不同数值；请读取原文判断适用范围，无法判断时必须请求确认。",
        })
    signals.sort(key=lambda item: -max(entry["score"] for entry in item["documents"]))
    return signals


def search(docs: dict, query: str, top_k: int = 4) -> dict:
    query = query.strip()
    qtokens = set(tokenize(query))
    if not qtokens:
        return {"query": query, "hits": [], "total_hits": 0, "conflict_signals": [],
                "note": "检索词为空"}
    all_hits = []
    for doc_id, doc in docs.items():
        tags, title, heading, text = _doc_tokens(doc)
        strong = qtokens & (tags | heading)
        text_hits = qtokens & text
        if not strong and len(text_hits) < 2:
            continue
        score = (3 * len(qtokens & tags) + 2 * len(qtokens & heading)
                 + len(text_hits) + 0.5 * len(qtokens & title))
        sections = []
        for section in doc["sections"]:
            overlap = qtokens & set(tokenize(section["heading"] + section["text"]))
            sections.append({"section_id": section["id"], "heading": section["heading"],
                             "text": section["text"], "score": len(overlap)})
        sections.sort(key=lambda item: (-item["score"], item["section_id"]))
        all_hits.append({
            "doc_id": doc_id,
            "title": doc["title"],
            "version": doc["version"],
            "status": doc["status"],
            "effective_date": doc["effective_date"],
            "scope": doc["scope"],
            "department": doc["department"],
            "score": round(score, 3),
            "sections": sections[:3],
        })
    all_hits.sort(key=lambda hit: (hit["status"] != "active", -hit["score"], hit["doc_id"]))
    signals = conflict_signals(all_hits)
    hits = all_hits[:top_k]
    return {"query": query, "hits": hits, "total_hits": len(all_hits),
            "conflict_signals": signals,
            "note": "结果为关键词命中摘要；引用前必须 read_doc 读取原文并逐字摘录。"}
