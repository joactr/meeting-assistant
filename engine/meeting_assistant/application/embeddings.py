from __future__ import annotations

import math
import struct
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.retrieval import rebuild_index
from meeting_assistant.application.routing import task_policy
from meeting_assistant.application.transcripts import all_items
from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import fingerprint, new_id
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def register_embeddings(workspace: Workspace, registry: CommandRegistry) -> None:
    async def reindex(payload: dict[str, Any]) -> dict[str, Any]:
        policy = payload.get("policy") or task_policy(
            workspace, "embeddings", {"project_id": payload.get("project_id")}
        )
        require(
            workspace.inference is not None and bool(policy),
            "embedding_unconfigured",
            "Choose an embedding model and authorize historical text before building the semantic index.",
            status=503,
        )
        assert workspace.inference is not None
        assert policy is not None
        descriptor = workspace.inference.embedding_descriptor(policy)
        raw_meeting_ids: object = payload.get("meeting_ids")
        require(
            raw_meeting_ids is None
            or isinstance(raw_meeting_ids, list)
            and len(cast(list[object], raw_meeting_ids)) <= 200,
            "scope_too_large",
            "Choose up to 200 explicit meetings or rebuild the workspace index.",
            status=422,
        )
        meeting_ids = cast(list[str] | None, raw_meeting_ids)
        job = workspace.queue_job(
            "embedding_index",
            {
                "policy": policy,
                "descriptor": descriptor,
                "meeting_ids": meeting_ids,
                "project_id": payload.get("project_id"),
            },
            idempotency_key=payload.get("idempotency_key")
            or "embedding-index:"
            + fingerprint(
                {
                    "descriptor": descriptor,
                    "meeting_ids": meeting_ids,
                    "revision": workspace.events()["watermark"],
                }
            ),
        )
        return {"job": job, "status": "accepted", "previous_generation_remains_active": True}

    async def execute(job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        descriptor = payload["descriptor"]
        policy = payload["policy"]
        meetings = all_items(workspace, "meetings")
        if payload.get("meeting_ids") is not None:
            meetings = [meeting for meeting in meetings if meeting["id"] in payload["meeting_ids"]]
        if payload.get("project_id"):
            meetings = [
                meeting
                for meeting in meetings
                if meeting.get("project_id") == payload["project_id"]
            ]
        for meeting in meetings:
            if not workspace.list(
                "search_chunks",
                {"meeting_id": meeting["id"], "source_revision": meeting["transcript_revision"]},
            ):
                rebuild_index(workspace, meeting["id"])
        meeting_ids = {meeting["id"] for meeting in meetings}
        chunks = [
            chunk
            for chunk in all_items(workspace, "search_chunks")
            if chunk["meeting_id"] in meeting_ids
            and workspace.require_item("meetings", chunk["meeting_id"])["transcript_revision"]
            == chunk["source_revision"]
        ]
        require(
            bool(chunks),
            "empty_index",
            "There are no finalized current transcript chunks to embed.",
            status=422,
        )
        generations = workspace.list(
            "embedding_generations", {"fingerprint": descriptor["fingerprint"]}
        )
        if generations:
            generation = generations[0]
        else:
            generation = workspace.put(
                "embedding_generations",
                new_id(),
                {
                    "fingerprint": descriptor["fingerprint"],
                    "dimensions": descriptor.get("dimensions") or 1,
                    "preparation_version": descriptor["preparation_version"],
                    "normalization": descriptor["normalization"],
                    "state": "building",
                },
            )
        try:
            assert workspace.inference is not None
            for start in range(0, len(chunks), 16):
                require(
                    not workspace.require_item("jobs", job["id"])["cancellation_requested"],
                    "cancelled",
                    "Embedding indexing cancelled.",
                )
                batch = chunks[start : start + 16]
                result = await workspace.inference.embed(
                    {
                        "purpose": "document",
                        "texts": [chunk["text"] for chunk in batch],
                        "model_fingerprint": descriptor["fingerprint"],
                    },
                    policy,
                )
                vectors: list[list[float]] = result["vectors_by_input_index"]
                dimensions = result["dimensions"]
                require(
                    result["fingerprint"] == descriptor["fingerprint"]
                    and len(vectors) == len(batch)
                    and all(
                        len(vector) == dimensions and all(math.isfinite(value) for value in vector)
                        for vector in vectors
                    ),
                    "embedding_mismatch",
                    "Embedding batch dimensions, count or preparation fingerprint changed.",
                    status=422,
                )
                with workspace.transaction():
                    current = workspace.require_item("embedding_generations", generation["id"])
                    if current["dimensions"] != dimensions:
                        workspace.put(
                            "embedding_generations",
                            current["id"],
                            {"dimensions": dimensions},
                            expected_revision=current["revision"],
                        )
                    for chunk, vector in zip(batch, vectors, strict=True):
                        live = workspace.get("search_chunks", chunk["id"])
                        require(
                            live is not None
                            and live["content_hash"] == chunk["content_hash"]
                            and workspace.require_item("meetings", chunk["meeting_id"])[
                                "transcript_revision"
                            ]
                            == chunk["source_revision"],
                            "stale_index",
                            "Source changed while its replacement index was being built; the prior active generation is retained.",
                        )
                        norm = math.sqrt(sum(value * value for value in vector))
                        require(
                            norm > 0, "invalid_embedding", "Embedding is a zero vector.", status=422
                        )
                        normalized = [value / norm for value in vector]
                        workspace.connection.execute(
                            "INSERT INTO chunk_embeddings(generation_id,chunk_id,vector,content_hash) VALUES(?,?,?,?) ON CONFLICT(generation_id,chunk_id) DO UPDATE SET vector=excluded.vector,content_hash=excluded.content_hash",
                            (
                                generation["id"],
                                chunk["id"],
                                struct.pack("<" + "f" * dimensions, *normalized),
                                chunk["content_hash"],
                            ),
                        )
                    current_job = workspace.require_item("jobs", job["id"])
                    workspace.put(
                        "jobs",
                        job["id"],
                        {
                            "progress": {
                                "completed_chunks": min(start + 16, len(chunks)),
                                "total_chunks": len(chunks),
                            }
                        },
                        expected_revision=current_job["revision"],
                    )
            with workspace.transaction():
                for active in workspace.list("embedding_generations", {"state": "active"}):
                    if active["id"] != generation["id"]:
                        workspace.put(
                            "embedding_generations",
                            active["id"],
                            {"state": "retired"},
                            expected_revision=active["revision"],
                        )
                current = workspace.require_item("embedding_generations", generation["id"])
                workspace.put(
                    "embedding_generations",
                    current["id"],
                    {"state": "active"},
                    expected_revision=current["revision"],
                )
                workspace.emit(
                    "search.index_activated",
                    {
                        "generation_id": generation["id"],
                        "fingerprint": descriptor["fingerprint"],
                        "chunk_count": len(chunks),
                    },
                )
            return {"generation_id": generation["id"], "chunks": len(chunks)}
        except BaseException:
            current = workspace.require_item("embedding_generations", generation["id"])
            if current["state"] != "active":
                workspace.put(
                    "embedding_generations",
                    current["id"],
                    {"state": "failed"},
                    expected_revision=current["revision"],
                )
            raise

    registry["search.reindex"] = reindex
    registry["job.embedding_index"] = execute
