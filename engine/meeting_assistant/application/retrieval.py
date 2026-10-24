from __future__ import annotations

import base64
import json
import math
import re
import struct
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.routing import task_policy
from meeting_assistant.application.transcripts import active_run, all_items
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, fingerprint, new_id
from meeting_assistant.domain.ports import CommandRegistry
from meeting_assistant.infrastructure.providers.contracts import ProviderError

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def paginate(
    items: list[dict[str, Any]],
    payload: dict[str, Any],
    *,
    sort_key: Callable[[dict[str, Any]], Any],
    reverse: bool = False,
) -> dict[str, Any]:
    limit = int(payload.get("limit", 50))
    require(1 <= limit <= 200, "invalid_page", "Page size must be 1..200.", status=422)
    query_fingerprint = fingerprint(
        {k: v for k, v in payload.items() if k not in ("cursor", "limit")}
    )
    items.sort(key=sort_key, reverse=reverse)
    cursor = payload.get("cursor")
    if cursor:
        try:
            decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        except (ValueError, TypeError) as exc:
            raise AppError("invalid_cursor", "Pagination cursor is invalid.", status=422) from exc
        require(
            decoded.get("fingerprint") == query_fingerprint,
            "cursor_filter_conflict",
            "Cursor belongs to different filters.",
            status=409,
        )
        last = decoded["last"]
        items = [
            item
            for item in items
            if (list(sort_key(item)) < last if reverse else list(sort_key(item)) > last)
        ]
    page = items[:limit]
    next_cursor = (
        base64.urlsafe_b64encode(
            canonical({"fingerprint": query_fingerprint, "last": sort_key(page[-1])}).encode()
        )
        .decode()
        .rstrip("=")
        if len(items) > limit and page
        else None
    )
    return {"items": page, "next_cursor": next_cursor}


def rebuild_index(w: Workspace, meeting_id: str) -> None:
    with w.transaction():
        meeting = w.require_item("meetings", meeting_id)
        run = active_run(w, meeting_id)
        # A query may have caught up this revision before its durable job ran.
        # Preserve those chunks (and their embeddings) instead of rebuilding twice.
        if w.list(
            "search_chunks",
            {
                "meeting_id": meeting_id,
                "run_id": run["id"],
                "source_revision": meeting["transcript_revision"],
                "chunker_version": "utf8-budget-256-overlap32-v1",
            },
            limit=1,
        ):
            return
        utterances = all_items(w, "utterances", {"run_id": run["id"], "is_final": 1})
        utterances.sort(key=lambda u: (u["start_ms"], u["id"]))
        w.connection.execute("DELETE FROM search_chunks WHERE meeting_id=?", (meeting_id,))
        tokens: list[tuple[str, dict[str, Any]]] = []
        for utterance in utterances:
            tokens.extend((token, utterance) for token in re.findall(r"\S+", utterance["text"]))
        # Conservative UTF-8 token proxy is versioned; E5 worker performs exact tokenizer validation.
        # Each piece caps bytes to remain safe for multilingual model token limits.
        expanded: list[tuple[str, dict[str, Any]]] = []
        for token, u in tokens:
            if len(token.encode("utf-8")) > 512:
                expanded.extend((token[i : i + 64], u) for i in range(0, len(token), 64))
            else:
                expanded.append((token, u))
        position = 0
        while position < len(expanded):
            end = position
            budget = 0
            while end < len(expanded) and end - position < 256:
                cost = max(1, math.ceil(len(expanded[end][0].encode("utf-8")) / 3))
                if budget + cost > 256 and end > position:
                    break
                budget += cost
                end += 1
            group = expanded[position:end]
            text = " ".join(token for token, _ in group)
            first = group[0][1]
            last = group[-1][1]
            chunk = w.put(
                "search_chunks",
                new_id(),
                {
                    "meeting_id": meeting_id,
                    "run_id": run["id"],
                    "start_utterance_id": first["id"],
                    "end_utterance_id": last["id"],
                    "text": text,
                    "source_revision": meeting["transcript_revision"],
                    "chunker_version": "utf8-budget-256-overlap32-v1",
                    "content_hash": fingerprint(text),
                    "start_ms": first["start_ms"],
                    "end_ms": last["end_ms"],
                },
            )
            w.connection.execute(
                "INSERT INTO search_fts(chunk_id,text) VALUES(?,?)", (chunk["id"], text)
            )
            if end == len(expanded):
                break
            position = max(position + 1, end - 32)


def keyword_search(w: Workspace, p: dict[str, Any]) -> dict[str, Any]:
    query = str(p.get("query", "")).strip()
    require(
        0 < len(query) <= 2000,
        "invalid_query",
        "Search query must contain 1..2000 characters.",
        status=422,
    )
    mode = p.get("mode", "hybrid")
    require(
        mode in ("keyword", "semantic", "hybrid"),
        "invalid_mode",
        "Search mode must be keyword, semantic or hybrid.",
        status=422,
    )
    terms = re.findall(r"\w+", query, flags=re.UNICODE)
    if not terms:
        return {
            "items": [],
            "retrieval_mode": "keyword",
            "fallback_reason": "Query contains no searchable terms.",
        }
    match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms[:64])
    filters = ["search_fts MATCH ?", "c.source_revision=m.transcript_revision", "t.state='active'"]
    params: list[Any] = [match]
    if p.get("project_id"):
        filters.append("m.project_id=?")
        params.append(p["project_id"])
    meeting_ids = p.get("meeting_ids") or ([p["meeting_id"]] if p.get("meeting_id") else [])
    require(
        len(meeting_ids) <= 200,
        "scope_too_large",
        "At most 200 explicit meetings per query.",
        status=422,
    )
    if meeting_ids:
        filters.append("m.id IN(" + ",".join("?" for _ in meeting_ids) + ")")
        params.extend(meeting_ids)
    if p.get("after_ms") is not None:
        filters.append("m.started_at>=?")
        params.append(p["after_ms"])
    if p.get("before_ms") is not None:
        filters.append("m.started_at<=?")
        params.append(p["before_ms"])
    # Edits invalidate stale chunks immediately. Catch up only this query's scope
    # so keyword search does not depend on a background worker having run first.
    freshness_sql = "SELECT DISTINCT m.id FROM meetings m JOIN transcript_runs t ON t.meeting_id=m.id JOIN utterances u ON u.run_id=t.id WHERE t.state='active' AND u.is_final=1 AND NOT EXISTS(SELECT 1 FROM search_chunks current WHERE current.meeting_id=m.id AND current.run_id=t.id AND current.source_revision=m.transcript_revision)"
    if filters[3:]:
        freshness_sql += " AND " + " AND ".join(filters[3:])
    for meeting in w.connection.execute(freshness_sql, params[1:]).fetchall():
        rebuild_index(w, meeting["id"])
    sql = (
        "SELECT c.*,m.title AS meeting_title,bm25(search_fts) AS keyword_rank FROM search_fts JOIN search_chunks c ON c.id=search_fts.chunk_id JOIN meetings m ON m.id=c.meeting_id JOIN transcript_runs t ON t.id=c.run_id WHERE "
        + " AND ".join(filters)
        + " ORDER BY keyword_rank,c.start_ms,c.id LIMIT 100"
    )
    rows = w.connection.execute(sql, params).fetchall()
    limit = int(p.get("limit", 50))
    require(1 <= limit <= 100, "invalid_page", "Search limit must be 1..100.", status=422)
    items: list[dict[str, Any]] = []
    for rank, row in enumerate(rows[:limit], 1):
        item = dict(row)
        item["excerpt"] = item.pop("text")
        item["score"] = 1 / (60 + rank)
        item["citation"] = {
            "meeting_id": item["meeting_id"],
            "utterance_id": item["start_utterance_id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "source_revision": item["source_revision"],
        }
        item.pop("extensions_json", None)
        items.append(item)
    return {
        "items": items,
        "retrieval_mode": "keyword",
        "requested_mode": mode,
        "fallback_reason": None
        if mode == "keyword"
        else "No active compatible embedding generation is configured. Keyword-only results are shown; no semantic score was fabricated.",
    }


def register_retrieval(w: Workspace, r: CommandRegistry) -> None:
    async def search(p: dict[str, Any]) -> dict[str, Any]:
        result = keyword_search(w, p)
        # Existing embeddings are used only with their exact preparation/model fingerprint.
        policy = p.get("embedding_policy") or task_policy(
            w, "embeddings", {"project_id": p.get("project_id")}
        )
        if p.get("mode", "hybrid") == "keyword" or not policy or w.inference is None:
            return result
        try:
            descriptor = w.inference.embedding_descriptor(policy)
        except (AppError, ProviderError) as exc:
            result["fallback_reason"] = "Embedding configuration is unavailable: " + exc.code
            return result
        generations = w.list(
            "embedding_generations", {"state": "active", "fingerprint": descriptor["fingerprint"]}
        )
        if not generations:
            return result
        generation = generations[0]
        try:
            batch = await w.inference.embed(
                {
                    "purpose": "query",
                    "texts": [p["query"]],
                    "model_fingerprint": generation["fingerprint"],
                },
                policy,
            )
            vectors: list[dict[str, Any] | list[float]] = batch.get(
                "vectors", batch.get("vectors_by_input_index", [])
            )
            vector = cast(
                list[float],
                vectors[0].get("vector", vectors[0])
                if isinstance(vectors[0], dict)
                else vectors[0],
            )
            require(
                len(vector) == generation["dimensions"],
                "embedding_mismatch",
                "Embedding dimensions do not match the active index.",
                status=409,
            )
            fp = batch.get("fingerprint")
            require(
                fp == generation["fingerprint"],
                "embedding_mismatch",
                "Embedding model fingerprint differs from the active index.",
                status=409,
            )
            packed = struct.pack("<" + "f" * len(vector), *vector)
            try:
                import sqlite_vec

                w.connection.enable_load_extension(True)
                sqlite_vec.load(w.connection)
            finally:
                w.connection.enable_load_extension(False)
            filters = [
                "e.generation_id=?",
                "c.source_revision=m.transcript_revision",
                "t.state='active'",
            ]
            params = [packed, generation["id"]]
            if p.get("project_id"):
                filters.append("m.project_id=?")
                params.append(p["project_id"])
            meeting_ids = p.get("meeting_ids") or ([p["meeting_id"]] if p.get("meeting_id") else [])
            if meeting_ids:
                filters.append("c.meeting_id IN(" + ",".join("?" for _ in meeting_ids) + ")")
                params.extend(meeting_ids)
            if p.get("after_ms") is not None:
                filters.append("m.started_at>=?")
                params.append(p["after_ms"])
            if p.get("before_ms") is not None:
                filters.append("m.started_at<=?")
                params.append(p["before_ms"])
            rows = w.connection.execute(
                "SELECT c.*,m.title AS meeting_title,vec_distance_cosine(e.vector,?) distance FROM chunk_embeddings e JOIN search_chunks c ON c.id=e.chunk_id JOIN meetings m ON m.id=c.meeting_id JOIN transcript_runs t ON t.id=c.run_id WHERE "
                + " AND ".join(filters)
                + " ORDER BY distance,c.start_ms,c.id LIMIT 100",
                params,
            ).fetchall()
            vector_items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["excerpt"] = item.pop("text")
                item["citation"] = {
                    "meeting_id": item["meeting_id"],
                    "utterance_id": item["start_utterance_id"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "source_revision": item["source_revision"],
                }
                item.pop("extensions_json", None)
                vector_items.append(item)
            scores: dict[str, float] = {}
            merged: dict[str, dict[str, Any]] = {}
            sources: list[list[dict[str, Any]]] = (
                [vector_items] if p.get("mode") == "semantic" else [result["items"], vector_items]
            )
            for source in sources:
                for rank, item in enumerate(source, 1):
                    scores[item["id"]] = scores.get(item["id"], 0) + 1 / (60 + rank)
                    merged[item["id"]] = item
            ranked = sorted(
                merged.values(), key=lambda i: (-scores[i["id"]], i["start_ms"], i["id"])
            )[: int(p.get("limit", 50))]
            for item in ranked:
                item["score"] = scores[item["id"]]
            return {
                "items": ranked,
                "retrieval_mode": p.get("mode", "hybrid"),
                "fallback_reason": None,
            }
        except (ImportError, AttributeError, IndexError, AppError, ProviderError) as exc:
            result["fallback_reason"] = (
                "Embedding runtime or authorization is unavailable: "
                + getattr(exc, "code", type(exc).__name__)
            )
            return result

    r["search.query"] = search

    async def ask(p: dict[str, Any]) -> dict[str, Any]:
        result = await search({**p, "limit": 100})
        selected: list[dict[str, Any]] = []
        budget = int(p.get("context_char_budget", 24000))
        seen: set[tuple[str, int, int]] = set()
        require(
            1000 <= budget <= 100000,
            "invalid_budget",
            "Ask evidence budget must be 1000..100000 characters.",
            status=422,
        )
        for item in result["items"]:
            key = (item["meeting_id"], item["start_ms"], item["end_ms"])
            if any(
                key[0] == other[0] and max(key[1], other[1]) <= min(key[2], other[2])
                for other in seen
            ):
                continue
            if len(selected) >= 12 or len(item["excerpt"]) > budget:
                continue
            budget -= len(item["excerpt"])
            seen.add(key)
            selected.append(item)
        if not selected:
            answer = "Insufficient evidence: no relevant passages were found in the selected scope."
            insufficient = True
            output: dict[str, Any] = {}
            citations: list[dict[str, Any] | str] = []
        else:
            require(
                w.inference is not None,
                "ai_unconfigured",
                "Ask requires a selected AI provider; keyword search remains available.",
                status=503,
            )
            assert w.inference is not None
            from meeting_assistant.application.routing import task_policy

            policy = p.get("policy") or task_policy(w, "ask", {"project_id": p.get("project_id")})
            require(
                bool(policy),
                "policy_required",
                "Choose an Ask model and historical-evidence consent.",
                status=422,
            )
            assert policy is not None
            evidence: list[dict[str, Any]] = [
                {
                    "id": str(index),
                    "citation_id": str(index),
                    "source_id": item["start_utterance_id"],
                    "meeting_id": item["meeting_id"],
                    "text": item["excerpt"],
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "source_revision": item["source_revision"],
                }
                for index, item in enumerate(selected, 1)
            ]
            output = await w.inference.generate(
                "ask",
                evidence,
                policy,
                context={
                    "query": p["query"],
                    "scope": {key: p[key] for key in ("project_id", "meeting_ids") if key in p},
                },
            )
            response = output.get("result", output)
            answer = response.get("answer", "")
            insufficient = bool(response.get("insufficient_evidence", False))
            citations = response.get("citations", [])
            require(
                isinstance(answer, str) and bool(answer),
                "invalid_answer",
                "Provider returned no grounded answer.",
                status=422,
            )
            require(
                insufficient or bool(citations),
                "missing_citations",
                "Grounded Ask answer must cite selected evidence.",
                status=422,
            )
            allowed = {str(i): item for i, item in enumerate(selected, 1)}
            for citation in citations:
                cid = str(
                    citation.get("citation_id", citation.get("id", citation.get("source_id", "")))
                    if isinstance(citation, dict)
                    else citation
                )
                require(
                    cid in allowed,
                    "unknown_citation",
                    "Ask answer cited evidence outside the selected passages.",
                    status=422,
                )
                if isinstance(citation, dict) and citation.get("start_ms") is not None:
                    source = allowed[cid]
                    require(
                        source["start_ms"]
                        <= citation["start_ms"]
                        <= citation.get("end_ms", citation["start_ms"])
                        <= source["end_ms"],
                        "invalid_citation_range",
                        "Citation range is outside its selected evidence.",
                        status=422,
                    )
        with w.transaction():
            item = w.put(
                "ask_answers",
                new_id(),
                {
                    "query": p["query"],
                    "scope": {key: p[key] for key in ("project_id", "meeting_ids") if key in p},
                    "retrieval_fingerprint": fingerprint([i["id"] for i in selected]),
                    "answer": answer,
                    "insufficient_evidence": int(insufficient),
                    "prompt_version": output.get("prompt_version", "none"),
                },
            )
            stored: list[dict[str, Any]] = []
            for citation in citations:
                cid = str(
                    citation.get("citation_id", citation.get("id", citation.get("source_id", "")))
                    if isinstance(citation, dict)
                    else citation
                )
                source = selected[int(cid) - 1]
                stored.append(
                    w.put(
                        "citations",
                        new_id(),
                        {
                            "owner_ask_answer_id": item["id"],
                            "meeting_id": source["meeting_id"],
                            "source_utterance_id": source["start_utterance_id"],
                            "start_ms": source["start_ms"],
                            "end_ms": source["end_ms"],
                            "quote_snapshot": source["excerpt"],
                            "source_revision": source["source_revision"],
                        },
                    )
                )
            return {
                "item": item,
                "citations": stored,
                "retrieval_mode": result["retrieval_mode"],
                "fallback_reason": result.get("fallback_reason"),
            }

    r["ask.query"] = ask
