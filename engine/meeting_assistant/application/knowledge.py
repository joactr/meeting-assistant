from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.routing import task_policy
from meeting_assistant.application.transcripts import active_run, all_items
from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import fingerprint, new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def scoped_meetings(
    workspace: Workspace, meeting: dict[str, Any], payload: dict[str, Any], *, limit: int | None = 5
) -> list[dict[str, Any]]:
    raw_explicit: object = payload.get("meeting_ids", [])
    require(
        isinstance(raw_explicit, list) and len(cast(list[object], raw_explicit)) <= 200,
        "scope_too_large",
        "Select no more than 200 historical meetings.",
        status=422,
    )
    explicit = cast(list[str], raw_explicit)
    meetings = all_items(workspace, "meetings")
    if explicit:
        selected = [item for item in meetings if item["id"] in explicit]
    elif meeting.get("project_id"):
        selected = [
            item
            for item in meetings
            if item.get("project_id") == meeting["project_id"]
            and item.get("work_package_id") in (None, meeting.get("work_package_id"))
        ]
    else:
        selected = []
    selected = [
        item
        for item in selected
        if item["id"] != meeting["id"]
        and item["state"] in ("complete", "attention_required", "interrupted")
    ]
    ordered = sorted(
        selected,
        key=lambda item: (item["started_at"] or item["created_at"], item["id"]),
        reverse=True,
    )
    return ordered[:limit] if limit is not None else ordered


def evidence_for(workspace: Workspace, meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for meeting in meetings:
        run = active_run(workspace, meeting["id"])
        for utterance in all_items(workspace, "utterances", {"run_id": run["id"], "is_final": 1}):
            evidence.append(
                {
                    "id": utterance["id"],
                    "source_id": utterance["id"],
                    "meeting_id": meeting["id"],
                    "text": utterance["text"],
                    "start_ms": utterance["start_ms"],
                    "end_ms": utterance["end_ms"],
                    "revision": utterance["text_revision"],
                    "detected_language": utterance.get("language"),
                    "kind": "transcript",
                }
            )
        for note in all_items(workspace, "notes", {"meeting_id": meeting["id"]}):
            evidence.append(
                {
                    "id": note["id"],
                    "source_id": note["id"],
                    "meeting_id": meeting["id"],
                    "text": note["body_markdown"],
                    "revision": note["revision"],
                    "kind": "human_note",
                }
            )
    return evidence


def references(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        document = cast(dict[str, Any], value)
        if document.get("source_id"):
            yield document
        for child in document.values():
            yield from references(child)
    elif isinstance(value, list):
        for child in cast(list[Any], value):
            yield from references(child)


def save_grounded_result(
    workspace: Workspace,
    meeting: dict[str, Any],
    task: str,
    output: dict[str, Any],
    evidence: list[dict[str, Any]],
    input_hash: str,
) -> dict[str, Any]:
    sources = {item["source_id"]: item for item in evidence}
    result = output.get("result", output)
    for reference in references(result):
        source = sources.get(reference["source_id"])
        require(
            source is not None,
            "unknown_citation",
            "Generated knowledge cites a source outside its explicit scope.",
            status=422,
        )
        assert source is not None
        if reference.get("start_ms") is not None:
            require(
                source.get("start_ms", 0)
                <= reference["start_ms"]
                <= reference.get("end_ms", reference["start_ms"])
                <= source.get("end_ms", 0),
                "invalid_citation_range",
                "Generated knowledge has an out-of-source time range.",
                status=422,
            )
    with workspace.transaction():
        run = workspace.put(
            "analysis_runs",
            new_id(),
            {
                "meeting_id": meeting["id"],
                "task": task,
                "input_revision": meeting["transcript_revision"],
                "note_revision": meeting["note_revision"],
                "watermark": meeting["transcript_revision"],
                "policy_hash": meeting["policy_hash"],
                "status": "succeeded",
                "prompt_version": output.get("prompt_version", "deterministic-empty-v1"),
                "model_fingerprint": output.get("model_fingerprint", ""),
                "input_fingerprint": input_hash,
            },
        )
        item = workspace.put(
            "analysis_results",
            new_id(),
            {
                "run_id": run["id"],
                "kind": task,
                "payload": result,
                "state": "promoted",
                "promoted_at": now_ms(),
                "language": meeting["config_snapshot"].get("output_language", "auto"),
                "content_hash": fingerprint(result),
            },
        )
        seen: set[str] = set()
        for reference in references(result):
            if reference["source_id"] in seen:
                continue
            seen.add(reference["source_id"])
            source = sources[reference["source_id"]]
            utterance = workspace.get("utterances", source["source_id"])
            workspace.put(
                "citations",
                new_id(),
                {
                    "owner_result_id": item["id"],
                    "meeting_id": source["meeting_id"],
                    "source_utterance_id": source["source_id"] if utterance else None,
                    "source_note_id": source["source_id"] if not utterance else None,
                    "source_revision": source["revision"],
                    "start_ms": reference.get("start_ms"),
                    "end_ms": reference.get("end_ms"),
                    "quote_snapshot": reference.get("quote", source["text"]),
                },
            )
        return item


def register_knowledge(workspace: Workspace, registry: CommandRegistry) -> None:
    async def prepare(payload: dict[str, Any]) -> dict[str, Any]:
        meeting = workspace.require_item("meetings", payload["meeting_id"])
        kind = payload.get("kind", "pre")
        require(
            kind in ("pre", "post"),
            "invalid_briefing_kind",
            "Select pre or post meeting briefing.",
            status=422,
        )
        historical = scoped_meetings(workspace, meeting, payload)
        selected = historical + ([meeting] if kind == "post" else [])
        evidence = evidence_for(workspace, selected)
        if kind == "pre":
            evidence += [
                {
                    "id": note["id"],
                    "source_id": note["id"],
                    "meeting_id": meeting["id"],
                    "text": note["body_markdown"],
                    "revision": note["revision"],
                    "kind": "human_note",
                }
                for note in all_items(workspace, "notes", {"meeting_id": meeting["id"]})
            ]
        policy = payload.get("policy") or task_policy(workspace, "briefings", meeting)
        scope = {
            "project_id": meeting.get("project_id"),
            "work_package_id": meeting.get("work_package_id"),
            "meeting_ids": [item["id"] for item in selected],
            "kind": kind,
        }
        actions = [
            action
            for item in historical
            for action in all_items(workspace, "actions", {"meeting_id": item["id"]})
            if action["status"] not in ("done", "dismissed")
        ]
        decisions = [
            artifact
            for item in historical
            for artifact in all_items(workspace, "artifacts", {"meeting_id": item["id"]})
            if artifact["kind"] in ("decision", "risk")
        ]
        event = (
            workspace.get("calendar_events", meeting["calendar_event_id"])
            if meeting.get("calendar_event_id")
            else None
        )
        input_hash = fingerprint(
            {
                "scope": scope,
                "inputs": [(item["source_id"], item["revision"]) for item in evidence],
                "actions": [(item["id"], item["revision"]) for item in actions],
                "decisions": [(item["id"], item["revision"]) for item in decisions],
                "calendar_event_revision": event.get("revision") if event else None,
                "language": meeting["config_snapshot"].get("output_language", "auto"),
                "policy": policy,
                "prompt": "briefings-v1",
                "meeting_note_revision": meeting["note_revision"],
            }
        )
        cached = workspace.list(
            "briefings",
            {
                "meeting_id": meeting["id"],
                "kind": kind,
                "input_fingerprint": input_hash,
                "stale": 0,
            },
        )
        if cached:
            return {
                "item": cached[-1],
                "result": workspace.require_item("analysis_results", cached[-1]["result_id"]),
                "cached": True,
            }
        if not evidence:
            output: dict[str, Any] = {
                "result": {
                    "sections": [],
                    "warnings": [
                        "No relevant selected meeting evidence or human agenda is available."
                    ],
                },
                "prompt_version": "deterministic-empty-v1",
            }
        else:
            require(
                workspace.inference is not None and bool(policy),
                "briefing_model_required",
                "Choose a briefing policy. No unrelated meeting is substituted for missing scope.",
                status=503,
            )
            assert workspace.inference is not None
            assert policy is not None
            output = await workspace.inference.generate(
                "briefings",
                evidence,
                policy,
                language=meeting["config_snapshot"].get("output_language", "auto"),
                context={
                    "kind": kind,
                    "scope": scope,
                    "unresolved_actions": actions,
                    "recent_decisions_and_risks": decisions,
                    "calendar_event": {
                        key: event.get(key) for key in ("title", "start_ms", "end_ms", "timezone")
                    }
                    if event
                    else None,
                    "human_agenda": [item for item in evidence if item["kind"] == "human_note"],
                    "post_brief_policy": "Compare current meeting delta with previous scoped facts; distinguish changed/superseded decisions, new/open actions and unresolved risks.",
                },
            )
        for source in evidence:
            live = workspace.get("utterances", source["source_id"]) or workspace.get(
                "notes", source["source_id"]
            )
            require(
                live is not None
                and live.get("text_revision", live["revision"]) == source["revision"],
                "stale_briefing",
                "A selected source changed while the briefing was being generated.",
            )
        require(
            workspace.require_item("meetings", meeting["id"])["policy_hash"]
            == meeting["policy_hash"],
            "stale_briefing",
            "Meeting language or provider policy changed while the briefing was generated.",
        )
        with workspace.transaction():
            result = save_grounded_result(
                workspace, meeting, "briefings", output, evidence, input_hash
            )
            item = workspace.put(
                "briefings",
                new_id(),
                {
                    "meeting_id": meeting["id"],
                    "project_id": meeting.get("project_id"),
                    "work_package_id": meeting.get("work_package_id"),
                    "kind": kind,
                    "scope_hash": fingerprint(scope),
                    "input_fingerprint": input_hash,
                    "result_id": result["id"],
                },
            )
        return {"item": item, "result": result, "cached": False, "sparse": not bool(evidence)}

    def compress(payload: dict[str, Any]) -> dict[str, Any]:
        meeting = workspace.require_item("meetings", payload["meeting_id"])
        policy = payload.get("policy") or task_policy(workspace, "memory", meeting)
        require(
            workspace.inference is not None and bool(policy),
            "memory_model_required",
            "Choose a memory task policy before compression.",
            status=503,
        )
        job = workspace.queue_job(
            "memory_compress",
            {"policy": policy, "meeting_ids": payload.get("meeting_ids", [])},
            meeting_id=meeting["id"],
            idempotency_key=payload.get("idempotency_key")
            or "memory-compress:" + meeting["id"] + ":" + str(now_ms() // 86400000),
        )
        return {"job": job, "status": "accepted"}

    async def execute_compress(job: dict[str, Any]) -> dict[str, Any]:
        meeting = workspace.require_item("meetings", job["meeting_id"])
        selected = scoped_meetings(workspace, meeting, job["payload"], limit=None)
        ids = {item["id"] for item in selected}
        segments = [
            item
            for item in all_items(workspace, "memory_segments", {"stale": 0})
            if item["meeting_id"] in ids
            and item["created_at"] < now_ms() - 30 * 86400000
            and not item.get("compressed_into")
        ]
        if not segments:
            return {
                "state": "skipped",
                "reason": "No uncompressed scoped memory is older than 30 days.",
            }
        sources = evidence_for(
            workspace,
            [
                item
                for item in selected
                if item["id"] in {segment["meeting_id"] for segment in segments}
            ],
        )
        input_hash = fingerprint([(item["id"], item["revision"]) for item in segments])
        assert workspace.inference is not None
        output = await workspace.inference.generate(
            "memory",
            sources,
            job["payload"]["policy"],
            language=meeting["config_snapshot"].get("output_language", "auto"),
            context={
                "compress_summaries": [
                    {"summary": segment["summary"], "meeting_id": segment["meeting_id"]}
                    for segment in segments
                ],
                "preserve": "Retain cited decisions, open obligations and original evidence; compression never deletes source transcript.",
            },
        )
        with workspace.transaction():
            for segment in segments:
                require(
                    workspace.require_item("memory_segments", segment["id"])["revision"]
                    == segment["revision"],
                    "stale_memory",
                    "Memory changed during compression.",
                )
            result = save_grounded_result(workspace, meeting, "memory", output, sources, input_hash)
            summary = "\n\n".join(
                section["heading"] + "\n" + section["body"]
                for section in result["payload"].get("sections", [])
            )
            compressed = workspace.put(
                "memory_segments",
                new_id(),
                {
                    "project_id": meeting.get("project_id"),
                    "work_package_id": meeting.get("work_package_id"),
                    "meeting_id": meeting["id"],
                    "result_id": result["id"],
                    "input_revision": meeting["transcript_revision"],
                    "summary": summary,
                    "compressed_from": [item["id"] for item in segments],
                },
            )
            for segment in segments:
                workspace.put(
                    "memory_segments",
                    segment["id"],
                    {"compressed_into": compressed["id"]},
                    expected_revision=segment["revision"],
                )
        return {"item": compressed, "source_evidence_preserved": True}

    registry["briefings.prepare"] = prepare
    registry["memory.compress"] = compress
    registry["job.memory_compress"] = execute_compress
