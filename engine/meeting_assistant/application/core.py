from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from meeting_assistant.application.routing import output_language, task_policy
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, fingerprint, new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry
from meeting_assistant.infrastructure.providers.contracts import ProviderError, normalize_endpoint

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

EDITABLE = frozenset(
    {
        "projects",
        "actions",
        "important_moments",
        "questions",
        "people",
        "provider_connections",
        "model_profiles",
        "task_policies",
        "consents",
        "vocabulary_terms",
        "integration_connections",
        "notes",
    }
)
READABLE = EDITABLE | {
    "meetings",
    "utterances",
    "transcript_runs",
    "meeting_speakers",
    "analysis_runs",
    "analysis_results",
    "jobs",
    "media_segments",
    "memory_segments",
    "briefings",
    "topics",
    "artifacts",
    "capture_sources",
    "timeline_gaps",
    "voice_profiles",
    "installed_artifacts",
}


def requeue_job(w: Workspace, job: dict[str, Any]) -> dict[str, Any]:
    return w.put(
        "jobs",
        job["id"],
        {
            "status": "queued",
            "available_at": now_ms(),
            "error": None,
            "cancellation_requested": 0,
        },
        expected_revision=job["revision"],
    )


def register_core(w: Workspace, r: CommandRegistry) -> None:
    def status(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": {
                "protocol_major": 1,
                "build_id": w.build_id,
                "workspace_id": "workspace",
                "schema_version": w.connection.execute("PRAGMA user_version").fetchone()[0],
                "native_available": w.native_gateway is not None,
                "ai_configured": bool(task_policy(w, "summary")),
                "capabilities": {
                    "keyword_search": True,
                    "deterministic_export": True,
                    "capture": w.native_gateway is not None,
                },
                "active_meetings": [
                    m
                    for m in w.list("meetings")
                    if m["state"] in ("starting", "recording", "paused", "stopping")
                ],
                "shutdown_reservation": w.shutdown_reservation,
            }
        }

    r["workspace.status"] = status
    r["settings.get"] = lambda p: {"item": w.require_item("workspace_settings", "workspace")}

    def settings_save(p: dict[str, Any]) -> dict[str, Any]:
        settings = p.get("settings", p.get("nonsecret_settings", p.get("data", {})))
        require(
            isinstance(settings, dict),
            "invalid_settings",
            "Settings must be an object.",
            status=422,
        )
        if "retention_days" in settings:
            require(
                settings["retention_days"] in (1, 7, 30, None),
                "invalid_retention",
                "Retention must be 1, 7, 30 days or null.",
                status=422,
            )
        return {
            "item": w.put(
                "workspace_settings",
                "workspace",
                {"nonsecret_settings": {**w.settings(), **settings}},
                expected_revision=p.get("expected_revision"),
            )
        }

    r["settings.save"] = settings_save

    def create_meeting(p: dict[str, Any]) -> dict[str, Any]:
        title = str(p.get("title", "")).strip()
        require(
            0 < len(title) <= 300,
            "invalid_title",
            "A meeting needs a title of at most 300 characters.",
            status=422,
        )
        project = p.get("project_id")
        settings = w.settings()
        analysis = cast(dict[str, Any], settings.get("analysis") or {})
        if analysis.get("output_language"):
            settings = {**settings, "output_language": analysis["output_language"]}
        config = {**settings, **p.get("config", {})}
        if isinstance(config.get("capture"), dict):
            config = {**config, **config["capture"]}
        if isinstance(config.get("recording"), dict):
            recording = config["recording"]
            config = {**config, **recording, "recording": bool(recording.get("enabled", False))}
        for task in ("asr", "diarization"):
            selected = task_policy(w, task, {"project_id": project, "config_snapshot": config})
            if selected:
                config[task + "_policy"] = selected
        language = p.get("language")
        if language and language != "auto":
            config["meeting_language"] = language
        config["source"] = p.get("source", config.get("source", "both"))
        require(
            config["source"] in ("mic", "system", "both", "file", "text"),
            "invalid_source",
            "Select mic, system, both, file or text.",
            status=422,
        )
        with w.transaction():
            item = w.put(
                "meetings",
                new_id(),
                {
                    "title": title,
                    "state": "draft",
                    "project_id": project,
                    "calendar_event_id": p.get("calendar_event_id"),
                    "calendar_series_id": p.get("calendar_series_id"),
                    "attendees": p.get("attendees", []),
                    "config_snapshot": config,
                    "policy_hash": fingerprint(config),
                },
            )
            run = w.put(
                "transcript_runs",
                new_id(),
                {
                    "meeting_id": item["id"],
                    "source": "import" if config["source"] in ("file", "text") else "live",
                    "state": "active",
                    "language_policy": config.get("output_language", "auto"),
                },
            )
            return {"item": item, "transcript_run": run}

    r["meetings.create"] = create_meeting

    def list_meetings(p: dict[str, Any]) -> dict[str, Any]:
        from meeting_assistant.application.retrieval import paginate
        from meeting_assistant.application.transcripts import all_items

        filters = dict(p.get("filters", {}))
        filters.update({key: p[key] for key in ("project_id", "state") if key in p})
        items = all_items(w, "meetings", filters)
        return paginate(
            items,
            p,
            sort_key=lambda i: (i.get("started_at") or i["created_at"], i["id"]),
            reverse=True,
        )

    r["meetings.list"] = list_meetings
    r["meetings.get"] = lambda p: {
        "item": w.require_item("meetings", cast(str, p.get("id", p.get("meeting_id"))))
    }

    def update_meeting(p: dict[str, Any]) -> dict[str, Any]:
        entity_id = cast(str, p.get("id", p.get("meeting_id")))
        data = p.get(
            "changes",
            p.get(
                "data",
                {k: v for k, v in p.items() if k in ("title", "project_id")},
            ),
        )
        require(
            set(data) <= {"title", "project_id"},
            "invalid_edit",
            "Lifecycle and revision fields cannot be patched.",
            status=422,
        )
        return {
            "item": w.put("meetings", entity_id, data, expected_revision=p.get("expected_revision"))
        }

    r["meetings.update"] = update_meeting

    def delete_meeting(p: dict[str, Any]) -> dict[str, Any]:
        entity_id = cast(str, p.get("id", p.get("meeting_id")))
        item = w.require_item("meetings", entity_id)
        require(
            p.get("confirm") is True,
            "confirmation_required",
            "Explicit deletion confirmation is required.",
            status=422,
        )
        require(
            item["state"] not in ("starting", "recording", "paused", "stopping"),
            "capture_active",
            "Stop and preserve capture before deletion.",
        )
        with w.transaction():
            media = w.list("media_segments", {"meeting_id": entity_id})
            remote = w.connection.execute(
                "SELECT remote_url FROM issue_links WHERE action_id IN(SELECT id FROM actions WHERE meeting_id=?)",
                (entity_id,),
            ).fetchall()
            w.connection.execute(
                "DELETE FROM ask_answers WHERE id IN(SELECT owner_ask_answer_id FROM citations WHERE meeting_id=?)",
                (entity_id,),
            )
            if getattr(w, "connected", None):
                w.connected.digest.forget(entity_id)
            embeddings = [
                row[0]
                for row in w.connection.execute(
                    "SELECT embedding_object_ref FROM speaker_clusters WHERE meeting_id=?",
                    (entity_id,),
                )
            ]
            w.connection.execute(
                "UPDATE jobs SET status='cancelled',cancellation_requested=1 WHERE meeting_id=?",
                (entity_id,),
            )
            w.connection.execute("DELETE FROM memory_segments WHERE meeting_id=?", (entity_id,))
            w.connection.execute(
                "DELETE FROM briefings WHERE meeting_id=? OR project_id IS ?",
                (entity_id, item.get("project_id")),
            )
            result = w.delete(
                "meetings", entity_id, expected_revision=cast(int, p.get("expected_revision"))
            )
            if media:
                w.queue_job("purge_media", {"paths": [m["relative_path"] for m in media]})
            w.purge_objects(embeddings)
            result["remote_not_erased"] = [row["remote_url"] for row in remote]
            result["secure_erasure_claim"] = False
            return result

    r["meetings.delete"] = delete_meeting

    async def compile_vocabulary(
        item: dict[str, Any], acknowledgement: str | None = None
    ) -> dict[str, Any] | None:
        policy = item["config_snapshot"].get("asr_policy")
        if not policy or not getattr(w, "connected", None):
            return None
        stored_policy: dict[str, Any] | None = (
            w.get("task_policies", policy["policy_id"]) if policy.get("policy_id") else {}
        )
        profile_id = (
            policy.get(
                "model_profile_id",
                policy.get("profile_id", policy.get("profile", {}).get("id")),
            )
            or (stored_policy or {}).get("model_profile_id")
            or (stored_policy or {}).get("profile_id")
        )
        if not profile_id:
            return None
        return await w.connected.vocabulary.compile(
            {
                "project_id": item.get("project_id"),
                "profile_id": profile_id,
                "backend": policy.get("connection", {}).get("adapter"),
                "capabilities": policy.get("capabilities", {}),
                "acknowledgement": acknowledgement,
            }
        )

    async def vocabulary_check(p: dict[str, Any]) -> dict[str, Any]:
        vocabulary = await compile_vocabulary(w.require_item("meetings", p["meeting_id"]))
        if not vocabulary or not vocabulary["omitted"]:
            return {}
        return {
            "acknowledgement_key": vocabulary["acknowledgement_key"],
            "omitted": vocabulary["omitted"],
        }

    r["meetings.vocabulary_check"] = vocabulary_check

    async def lifecycle(p: dict[str, Any], operation: str) -> dict[str, Any]:
        meeting_id = cast(str, p.get("id", p.get("meeting_id")))
        item = w.require_item("meetings", meeting_id)
        prior = w.connection.execute(
            "SELECT request_hash,response_json FROM idempotency_records WHERE key=? AND operation=?",
            (p.get("idempotency_key"), operation),
        ).fetchone()
        if prior:
            require(
                prior["request_hash"] == fingerprint(p),
                "idempotency_conflict",
                "Lifecycle key reused with different payload.",
            )
            return json.loads(prior["response_json"])
        if operation == "start":
            require(
                w.shutdown_reservation is None,
                "shutdown_reserved",
                "Shutdown reservation blocks new capture.",
            )
            require(item["state"] == "draft", "invalid_state", "Only a draft can start.")
            recording = p.get("recording_enabled", False)
            require(
                isinstance(recording, bool),
                "invalid_recording_choice",
                "Choose recording explicitly for this capture.",
                status=422,
            )
            require(
                not recording or p.get("recording_notice_acknowledged") is True,
                "recording_notice_required",
                "Recording needs separate opt-in and acknowledgement that participant permission is your responsibility.",
                status=403,
            )
            config = {
                **item["config_snapshot"],
                "recording": recording,
                "recording_notice_acknowledged": bool(recording),
                "recording_consented_at": now_ms() if recording else None,
            }
            # Drafts may predate model setup or a model switch: use the current choice.
            for task in ("asr", "diarization"):
                selected = task_policy(
                    w, task, {"project_id": item.get("project_id"), "config_snapshot": config}
                )
                if selected:
                    config[task + "_policy"] = selected
            item = w.put(
                "meetings",
                meeting_id,
                {"config_snapshot": config, "policy_hash": fingerprint(config)},
                expected_revision=item["revision"],
            )
            require(
                w.native_gateway is not None,
                "native_unavailable",
                "Live capture requires the native host.",
                status=503,
            )
            require(
                w.inference is not None and bool(item["config_snapshot"].get("asr_policy")),
                "asr_unavailable",
                "Select a ready ASR model/profile before starting. No audio is sent to an alternative provider.",
                status=503,
            )
            assert w.inference is not None
            await w.inference.preflight_asr(item["config_snapshot"]["asr_policy"])
            vocabulary = await compile_vocabulary(item, p.get("vocabulary_acknowledgement"))
            if vocabulary is not None:
                require(
                    not vocabulary.get("acknowledgement_required"),
                    "vocabulary_acknowledgement_required",
                    "Review omitted vocabulary terms before capture.",
                    status=422,
                    details=vocabulary,
                )
                config = {**item["config_snapshot"], "vocabulary": vocabulary}
                item = w.put(
                    "meetings",
                    meeting_id,
                    {"config_snapshot": config, "policy_hash": fingerprint(config)},
                    expected_revision=item["revision"],
                )
            raw_sources: object = p.get("sources", item["config_snapshot"].get("sources", []))
            require(
                isinstance(raw_sources, list) and bool(cast(list[object], raw_sources)),
                "devices_required",
                "Explicit stable source device selections are required.",
                status=422,
            )
            sources = cast(list[dict[str, Any]], raw_sources)
            require(
                all(s.get("device_key") and s.get("kind") in ("mic", "system") for s in sources),
                "devices_required",
                "Each source needs its stable device key and kind.",
                status=422,
            )
            requested = item["config_snapshot"].get("source", "both")
            require(
                set(s["kind"] for s in sources)
                == ({"mic", "system"} if requested == "both" else {requested}),
                "source_mismatch",
                "Source selections must match meeting capture policy.",
                status=422,
            )

            def reserve() -> dict[str, Any]:
                current = w.require_item("meetings", meeting_id)
                epoch = w.connection.execute(
                    "SELECT COALESCE(MAX(epoch),0)+1 FROM capture_sources"
                ).fetchone()[0]
                updated = w.put(
                    "meetings",
                    meeting_id,
                    {"state": "starting", "capture_epoch": epoch, "started_at": now_ms()},
                    expected_revision=current["revision"],
                )
                mapped: list[dict[str, Any]] = []
                for index, source in enumerate(sources):
                    s = w.put(
                        "capture_sources",
                        new_id(),
                        {
                            "meeting_id": meeting_id,
                            "source_kind": source["kind"],
                            "device_key": source["device_key"],
                            "display_name": source.get("display_name", source["kind"]),
                            "epoch": updated["capture_epoch"],
                            "state": "starting",
                            "source_index": index,
                        },
                    )
                    speaker = w.put(
                        "meeting_speakers",
                        new_id(),
                        {
                            "meeting_id": meeting_id,
                            "source_kind": source["kind"],
                            "display_name": "Me" if source["kind"] == "mic" else "System",
                        },
                    )
                    w.put(
                        "capture_sources",
                        s["id"],
                        {"speaker_id": speaker["id"]},
                        expected_revision=s["revision"],
                    )
                    mapped.append({**source, "source_index": index, "source_id": s["id"]})
                return {
                    "item": updated,
                    "sources": mapped,
                    "capture_epoch": updated["capture_epoch"],
                }

            response = w.idempotent(operation, p, reserve)
            try:
                ack = await w.native(
                    "native.capture.start",
                    {
                        "meeting_id": meeting_id,
                        "capture_epoch": response["capture_epoch"],
                        "sources": [
                            {
                                key: source[key]
                                for key in ("source_index", "source_id", "device_key", "kind")
                            }
                            for source in response["sources"]
                        ],
                        "recording": recording,
                        "media_cap_bytes": config.get("byte_cap", 10 * 1024**3),
                        "vad": {
                            "threshold": config.get("vad_threshold", 0.5),
                            "pre_roll_ms": config.get("pre_roll_ms", 300),
                            "post_roll_ms": config.get("post_silence_ms", 400),
                            "minimum_speech_ms": config.get("minimum_true_speech_ms", 250),
                            "maximum_utterance_ms": config.get("maximum_utterance_ms", 12000),
                        },
                        "denoise": config.get("noise_suppression", False),
                        "aec": config.get("echo_cancellation", False),
                    },
                )
            except Exception as exc:
                with w.transaction():
                    current = w.require_item("meetings", meeting_id)
                    w.put(
                        "meetings",
                        meeting_id,
                        {
                            "state": "interrupted",
                            "health": "degraded",
                            "interrupted_reason": exc.message
                            if isinstance(exc, AppError)
                            else "Native capture failed.",
                        },
                        expected_revision=current["revision"],
                    )
                raise
            final_response = acknowledge(
                {
                    **p,
                    "capture_epoch": response["capture_epoch"],
                    "state": "recording",
                    "native_ack": ack,
                }
            )
            with w.transaction():
                w.connection.execute(
                    "UPDATE idempotency_records SET response_json=? WHERE key=? AND operation=?",
                    (canonical(final_response), p["idempotency_key"], operation),
                )
            return final_response
        if operation in ("pause", "resume"):
            require(
                item["state"] == ("recording" if operation == "pause" else "paused"),
                "invalid_state",
                "Capture state does not permit this transition.",
            )
            require(
                w.native_gateway is not None,
                "native_unavailable",
                "Native capture acknowledgement is required.",
                status=503,
            )
            epoch = (
                w.connection.execute(
                    "SELECT COALESCE(MAX(epoch),0)+1 FROM capture_sources"
                ).fetchone()[0]
                if operation == "resume"
                else item["capture_epoch"]
            )
            if operation == "resume":
                with w.transaction():
                    for source in w.list("capture_sources", {"meeting_id": meeting_id}):
                        w.put(
                            "capture_sources",
                            source["id"],
                            {"epoch": epoch, "state": "starting"},
                            expected_revision=source["revision"],
                        )
            ack = await w.native(
                "native.capture." + operation,
                {
                    "meeting_id": meeting_id,
                    "capture_epoch": epoch,
                    "boundary_sequence": p.get("boundary_sequence"),
                },
            )
            require(
                ack.get("acknowledged") is True
                or ack.get("state") in ("paused", "active", "recording"),
                "capture_unacknowledged",
                "Native source boundary is not acknowledged.",
                status=503,
            )
            if w.audio_pipeline is not None:
                await w.audio_pipeline.drain(meeting_id, ack.get("boundary_sequences"))
            for boundary in ack.get("boundaries", []):
                for media in boundary.get("committed_media", []):
                    await w.adispatch("media.commit", media)

            def transition() -> dict[str, Any]:
                current = w.require_item("meetings", meeting_id)
                now = now_ms()
                data = {
                    "state": "paused" if operation == "pause" else "recording",
                    "capture_epoch": epoch,
                    "active_since": None if operation == "pause" else now,
                    "elapsed_ms": max(current["elapsed_ms"], now - (current["started_at"] or now)),
                }
                if operation == "pause":
                    data["active_ms"] = current["active_ms"] + max(
                        0, now - (current.get("active_since") or now)
                    )
                    w.put(
                        "timeline_gaps",
                        new_id(),
                        {
                            "meeting_id": meeting_id,
                            "start_ms": data["elapsed_ms"],
                            "reason": "paused",
                            "acknowledged": 1,
                        },
                    )
                else:
                    for gap in w.list("timeline_gaps", {"meeting_id": meeting_id, "end_ms": None}):
                        w.put(
                            "timeline_gaps",
                            gap["id"],
                            {"end_ms": data["elapsed_ms"]},
                            expected_revision=gap["revision"],
                        )
                updated = w.put("meetings", meeting_id, data, expected_revision=current["revision"])
                for source in w.list("capture_sources", {"meeting_id": meeting_id}):
                    w.put(
                        "capture_sources",
                        source["id"],
                        {"state": "paused" if operation == "pause" else "active", "epoch": epoch},
                        expected_revision=source["revision"],
                    )
                return {"item": updated, "acknowledgement": ack}

            return w.idempotent(operation, p, transition)
        require(
            item["state"]
            in (
                "recording",
                "paused",
                "interrupted",
                "attention_required",
                "complete",
                "processing",
                "stopping",
            ),
            "invalid_state",
            "Meeting cannot stop/finalize from this state.",
        )
        live = any(
            j["status"] in ("queued", "running")
            for j in w.list("jobs", {"meeting_id": meeting_id, "kind": "finalize"})
        )
        if operation == "stop" and (
            item["state"] == "complete" or (item["state"] == "processing" and live)
        ):
            return w.idempotent(
                operation,
                p,
                lambda: {"item": item, "jobs": w.list("jobs", {"meeting_id": meeting_id})},
            )
        ack: dict[str, Any] = {}
        if item["state"] in ("recording", "paused", "stopping"):
            with w.transaction():
                current = w.require_item("meetings", meeting_id)
                w.put(
                    "meetings",
                    meeting_id,
                    {"state": "stopping"},
                    expected_revision=current["revision"],
                )
            ack = await w.native(
                "native.capture.stop",
                {
                    "meeting_id": meeting_id,
                    "capture_epoch": item["capture_epoch"],
                },
            )
            require(
                ack.get("drained") is True
                or ack.get("acknowledged") is True
                or (ack.get("state") == "stopped" and "boundary_sequences" in ack),
                "capture_not_preserved",
                "Stop requires explicit native drained acknowledgement.",
                status=503,
                retryable=True,
            )
            if w.audio_pipeline is not None:
                await w.audio_pipeline.drain(meeting_id, ack.get("boundary_sequences"))
            for boundary in ack.get("boundaries", []):
                for media in boundary.get("committed_media", []):
                    await w.adispatch("media.commit", media)
        await w.analysis_scheduler.stop_meeting(meeting_id)

        def finalize() -> dict[str, Any]:
            current = w.require_item("meetings", meeting_id)
            now = now_ms()
            watermark = current["transcript_revision"]
            updated = w.put(
                "meetings",
                meeting_id,
                {
                    "state": "processing",
                    "ended_at": now,
                    "stop_watermark": watermark,
                    "elapsed_ms": max(current["elapsed_ms"], now - (current["started_at"] or now)),
                    "active_ms": current["active_ms"]
                    + max(0, now - (current.get("active_since") or now)),
                    "active_since": None,
                },
                expected_revision=current["revision"],
            )
            for source in w.list("capture_sources", {"meeting_id": meeting_id}):
                w.put(
                    "capture_sources",
                    source["id"],
                    {"state": "stopped"},
                    expected_revision=source["revision"],
                )
            jobs: list[dict[str, Any]] = []
            for stage in (
                "drain",
                "final_diarization",
                "final_analysis",
                "memory",
                "index",
                "minutes",
            ):
                job = w.queue_job(
                    "finalize",
                    {
                        "stage": stage,
                        "input_revision": watermark,
                        "note_revision": current["note_revision"],
                        "policy_hash": current["policy_hash"],
                        "depends_on": [jobs[-1]["id"]] if jobs else [],
                    },
                    meeting_id=meeting_id,
                    idempotency_key=f"finalize:{meeting_id}:{stage}:{watermark}:{current['policy_hash']}",
                )
                if job["status"] in ("failed", "cancelled"):
                    # Same inputs return the earlier attempt; finishing again must run it again.
                    job = requeue_job(w, job)
                jobs.append(job)
            return {"item": updated, "jobs": jobs, "preservation_watermark": watermark}

        return w.idempotent(operation, p, finalize)

    def acknowledge(p: dict[str, Any]) -> dict[str, Any]:
        require(
            "native_ack" in p,
            "native_ack_required",
            "Capture acknowledgement must originate from the native gateway.",
            status=403,
        )
        meeting_id = cast(str, p.get("id", p.get("meeting_id")))
        current = w.require_item("meetings", meeting_id)
        require(
            current["state"] == "starting" and p["capture_epoch"] == current["capture_epoch"],
            "capture_epoch_conflict",
            "Native acknowledgement does not match current capture.",
        )
        with w.transaction():
            item = w.put(
                "meetings",
                meeting_id,
                {"state": "recording", "active_since": now_ms()},
                expected_revision=current["revision"],
            )
            for source in w.list("capture_sources", {"meeting_id": meeting_id}):
                w.put(
                    "capture_sources",
                    source["id"],
                    {"state": "active"},
                    expected_revision=source["revision"],
                )
            return {"item": item}

    # Acknowledge is private to the host credential scope in transport.
    r["meetings.acknowledge"] = acknowledge
    for operation in ("start", "pause", "resume", "stop"):

        async def handler(p: dict[str, Any], op: str = operation) -> dict[str, Any]:
            return await lifecycle(p, op)

        r["meetings." + operation] = handler

    def retry(p: dict[str, Any]) -> dict[str, Any]:
        mid = p.get("meeting_id", p.get("id"))
        jobs = w.list("jobs", {"meeting_id": mid}) if mid else [w.require_item("jobs", p["job_id"])]
        selected: list[dict[str, Any]] = []
        with w.transaction():
            for job in jobs:
                if job["status"] in ("failed", "cancelled") and (
                    not p.get("stage") or p["stage"] == job["stage"]
                ):
                    selected.append(requeue_job(w, job))
            if mid:
                item = w.require_item("meetings", mid)
                if selected and item["state"] == "attention_required":
                    w.put(
                        "meetings", mid, {"state": "processing"}, expected_revision=item["revision"]
                    )
        return {"items": selected}

    r["meetings.retry"] = retry
    r["jobs.retry"] = retry
    r["meetings.finalization"] = lambda p: {
        "items": w.list(
            "jobs", {"meeting_id": p.get("meeting_id", p.get("id")), "kind": "finalize"}
        )
    }

    async def source_retry(p: dict[str, Any]) -> dict[str, Any]:
        # Restarting capture gives every source a new epoch and stream, and re-anchors speech
        # detection; the pause gap and any recorded loss gap stay on the timeline.
        source = w.require_item("capture_sources", p["source_id"])
        meeting = w.require_item("meetings", source["meeting_id"])
        require(
            meeting["state"] in ("recording", "paused"),
            "invalid_state",
            "Source retry requires active capture.",
        )
        if meeting["state"] == "recording":
            await w.adispatch(
                "meetings.pause",
                {"meeting_id": meeting["id"], "idempotency_key": "source_retry:" + new_id()},
            )
        await w.adispatch(
            "meetings.resume",
            {"meeting_id": meeting["id"], "idempotency_key": "source_retry:" + new_id()},
        )
        return {"item": w.require_item("capture_sources", source["id"])}

    r["meetings.source_retry"] = source_retry

    def shutdown_prepare(p: dict[str, Any]) -> dict[str, Any]:
        active = [
            m
            for m in w.list("meetings")
            if m["state"] in ("starting", "recording", "paused", "stopping")
        ]
        reservation = {
            "reservation_id": new_id(),
            "blockers": [
                {"meeting_id": m["id"], "code": "capture_requires_preservation"} for m in active
            ],
            "preservation_watermarks": {
                m["id"]: m["stop_watermark"]
                for m in w.list("meetings")
                if m.get("stop_watermark") is not None
            },
            "safe_to_exit": not active,
        }
        w.shutdown_reservation = reservation
        return reservation

    r["shutdown.prepare"] = shutdown_prepare

    def shutdown_cancel(p: dict[str, Any]) -> dict[str, Any]:
        require(
            w.shutdown_reservation is not None
            and w.shutdown_reservation["reservation_id"] == p["reservation_id"],
            "reservation_conflict",
            "Shutdown reservation is not current.",
        )
        w.shutdown_reservation = None
        return {"cancelled": True}

    r["shutdown.cancel"] = shutdown_cancel

    def shutdown_commit(p: dict[str, Any]) -> dict[str, Any]:
        require(
            w.shutdown_reservation is not None
            and w.shutdown_reservation["reservation_id"] == p["reservation_id"],
            "reservation_conflict",
            "Shutdown reservation is not current.",
        )
        require(
            not any(
                m["state"] in ("starting", "recording", "paused", "stopping")
                for m in w.list("meetings")
            ),
            "shutdown_blocked",
            "Capture must be stopped and preserved before shutdown.",
        )
        w.connection.execute("PRAGMA wal_checkpoint(FULL)")
        w.shutdown_requested.set()
        return {
            "safe_to_exit": True,
            "reservation_id": p["reservation_id"],
            "preservation_watermarks": {
                m["id"]: m["stop_watermark"]
                for m in w.list("meetings")
                if m.get("stop_watermark") is not None
            },
        }

    r["shutdown.commit"] = shutdown_commit

    def entity_list(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p["collection"] in READABLE,
            "collection_forbidden",
            "This collection is not publicly readable.",
            status=403,
        )
        return {
            "items": w.list(
                p["collection"],
                p.get("filters", {}),
                limit=min(int(p.get("limit", 50)), 200),
                offset=int(p.get("offset", 0)),
            )
        }

    r["entity.list"] = entity_list

    def entity_get(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p["collection"] in READABLE,
            "collection_forbidden",
            "This collection is not publicly readable.",
            status=403,
        )
        return {"item": w.require_item(p["collection"], p["id"])}

    r["entity.get"] = entity_get

    def mutate(p: dict[str, Any], op: str) -> dict[str, Any]:
        collection = p["collection"]
        require(
            collection in EDITABLE,
            "collection_forbidden",
            "This resource requires a dedicated workflow.",
            status=403,
        )
        entity_id = p.get("id") or new_id()
        data = p.get("changes", p.get("data", {}))
        if collection == "provider_connections" and isinstance(data.get("endpoint"), str):
            data = {**data, "endpoint": normalize_endpoint(data["endpoint"])}
        if collection == "actions":
            if op == "create":
                data = {"stable_origin_key": "manual:" + entity_id, **data}
            else:
                old = w.require_item(collection, entity_id)
                data = {
                    **data,
                    "manual_fields": sorted(set(old.get("manual_fields", [])) | set(data)),
                }
        if collection == "voice_profiles":
            require(
                p.get("consent") is True,
                "consent_required",
                "Voice profile storage requires explicit biometric consent.",
                status=403,
            )
        if collection == "vocabulary_terms":
            data = {**data, "normalized_term": str(data.get("term", "")).casefold().strip()}
        if op == "delete":
            with w.transaction():
                profiles = (
                    w.connection.execute(
                        "SELECT id,vector_object_ref FROM voice_profiles WHERE person_id=?",
                        (entity_id,),
                    ).fetchall()
                    if collection == "people"
                    else []
                )
                result = w.delete(
                    collection, entity_id, expected_revision=cast(int, p.get("expected_revision"))
                )
                w.purge_objects([v[1] for v in profiles], *(v[0] for v in profiles))
                return result
        with w.transaction():
            result = w.put(
                collection, entity_id, data, expected_revision=p.get("expected_revision")
            )
            if collection in ("notes", "important_moments") and result.get("meeting_id"):
                w.invalidate(result["meeting_id"], notes=collection == "notes")
            return {"item": result}

    for op in ("create", "update", "delete"):
        r["entity." + op] = lambda p, op=op: mutate(p, op)
    for prefix, collection in (
        ("projects", "projects"),
        ("actions", "actions"),
        ("moments", "important_moments"),
        ("questions", "questions"),
        ("people", "people"),
        ("voice_profiles", "voice_profiles"),
        ("consents", "consents"),
        ("media", "media_segments"),
    ):
        r[prefix + ".list"] = lambda p, c=collection: {
            "items": w.list(
                c,
                {
                    **p.get("filters", {}),
                    **({"meeting_id": p["meeting_id"]} if p.get("meeting_id") else {}),
                },
                limit=min(int(p.get("limit", 50)), 200),
            )
        }
        for op in ("create", "update", "delete"):
            if collection in EDITABLE:
                r[prefix + "." + op] = lambda p, c=collection, op=op: mutate(
                    {
                        **p,
                        "collection": c,
                        "data": p.get(
                            "data",
                            {
                                k: v
                                for k, v in p.items()
                                if k
                                not in ("expected_revision", "id", "changes", "idempotency_key")
                            },
                        ),
                    },
                    op,
                )
    r["projects.archive"] = lambda p: {
        "item": w.put(
            "projects",
            p["id"],
            {"archived_at": now_ms()},
            expected_revision=p.get("expected_revision"),
        )
    }

    def clear_actions(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("confirm") is True,
            "confirmation_required",
            "Confirm clearing actions.",
            status=422,
        )
        with w.transaction():
            commands: list[dict[str, Any]] = []
            for action in w.list("actions", {"meeting_id": p["meeting_id"]}):
                updated = w.put(
                    "actions",
                    action["id"],
                    {
                        "status": "dismissed",
                        "manual_fields": sorted(set(action["manual_fields"]) | {"status"}),
                    },
                    expected_revision=action["revision"],
                )
                commands.append(
                    {
                        "collection": "actions",
                        "id": action["id"],
                        "expected_revision": updated["revision"],
                        "changes": {
                            "status": action["status"],
                            "manual_fields": action["manual_fields"],
                        },
                    }
                )
            undo = w.put(
                "undo_commands",
                new_id(),
                {
                    "entity_scope": p["meeting_id"],
                    "expected_revision": 0,
                    "inverse_patch": commands,
                    "expires_at": now_ms() + 600000,
                },
            )
            w.connection.execute(
                "DELETE FROM undo_commands WHERE id NOT IN(SELECT id FROM undo_commands ORDER BY created_at DESC LIMIT 20)"
            )
            return {"count": len(commands), "undo_id": undo["id"]}

    r["actions.clear"] = clear_actions

    def undo(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            command = w.require_item("undo_commands", cast(str, p.get("id", p.get("undo_id"))))
            require(
                command["expires_at"] >= now_ms() and command.get("applied_at") is None,
                "undo_expired",
                "Undo is expired or already applied.",
            )
            items = [
                w.put(
                    c["collection"], c["id"], c["changes"], expected_revision=c["expected_revision"]
                )
                for c in command["inverse_patch"]
            ]
            w.put(
                "undo_commands",
                command["id"],
                {"applied_at": now_ms()},
                expected_revision=command["revision"],
            )
            return {"items": items}

    r["undo.apply"] = undo

    def followups(p: dict[str, Any]) -> dict[str, Any]:
        from meeting_assistant.application.retrieval import paginate

        items = [
            {**i, "kind": kind}
            for collection, kind in (("actions", "action"), ("important_moments", "moment"))
            for i in w.list(collection, limit=200)
        ]
        for key in ("meeting_id", "status", "priority"):
            if p.get(key):
                items = [i for i in items if i.get(key) == p[key]]
        if not p.get("status"):
            items = [i for i in items if i.get("status") != "dismissed"]
        if p.get("assignee"):
            items = [i for i in items if i.get("assignee_text", i.get("assignee")) == p["assignee"]]
        today = __import__("datetime").date.today().isoformat()
        order = {"open": 1, "in_progress": 2, "done": 3, "dismissed": 4}
        return paginate(
            items,
            p,
            sort_key=lambda i: (
                0
                if i.get("status") == "open" and i.get("due_date") and i["due_date"] < today
                else order.get(i.get("status", "open"), 1),
                i.get("due_date") or "9999",
                i["id"],
            ),
        )

    r["followups.list"] = followups
    r["jobs.list"] = lambda p: {"items": w.list("jobs", p.get("filters", {}), limit=200)}
    r["jobs.attention"] = lambda p: {
        "items": w.list("jobs", {"status": "failed"}, limit=200),
        "meetings": [
            m for m in w.list("meetings") if m["state"] in ("attention_required", "interrupted")
        ],
    }

    def cancel_job(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            job = w.require_item("jobs", cast(str, p.get("id", p.get("job_id"))))
            item = w.put(
                "jobs",
                job["id"],
                {
                    "cancellation_requested": 1,
                    "status": "cancelled" if job["status"] == "queued" else job["status"],
                },
                expected_revision=p.get("expected_revision", job["revision"]),
            )
            if job["kind"] == "retranscribe" and job["status"] == "queued":
                run = w.get("transcript_runs", job["payload"].get("run_id", ""))
                if (
                    run
                    and run["meeting_id"] == job["meeting_id"]
                    and run["state"] == "candidate"
                    and not run.get("completed_at")
                ):
                    w.delete("transcript_runs", run["id"], expected_revision=run["revision"])
            w.cancel_active_job(job["id"])
        return {"item": item}

    r["jobs.cancel"] = cancel_job
    r["events.snapshot"] = lambda p: {
        "items": w.list("meetings"),
        "watermark": w.events()["watermark"],
    }

    def language(p: dict[str, Any]) -> dict[str, Any]:
        value = p.get("language", "auto")
        require(
            value == "auto"
            or re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", value) is not None,
            "invalid_language",
            "Language must be auto or a BCP-47 tag.",
            status=422,
        )
        item = w.require_item("meetings", p["meeting_id"])
        config = {
            **item["config_snapshot"],
            "output_language": value,
            "language_policy_revision": item["config_snapshot"].get("language_policy_revision", 0)
            + 1,
        }
        w.analysis_scheduler.policy_changed(item["id"])
        return {
            "item": w.put(
                "meetings",
                item["id"],
                {"config_snapshot": config, "policy_hash": fingerprint(config)},
                expected_revision=p.get("expected_revision"),
            )
        }

    r["language.set"] = language

    async def generate(p: dict[str, Any]) -> dict[str, Any]:
        meeting = w.require_item("meetings", p["meeting_id"])
        task = p.get("task", p.get("kind", "summary"))
        require(
            task
            in (
                "summary",
                "actions",
                "topics",
                "artifacts",
                "briefings",
                "minutes",
                "memory",
                "cues",
            ),
            "invalid_task",
            "Unknown analysis task.",
            status=422,
        )
        if task == "briefings":
            return await w.adispatch("briefings.prepare", p)
        if task == "minutes":
            return await w.connected.notes.prepare(
                {
                    "meeting_id": meeting["id"],
                    "note_revision": meeting["note_revision"],
                    "transcript_revision": meeting["transcript_revision"],
                    **p,
                }
            )
        policy: dict[str, Any] | None = p.get("policy") or task_policy(w, task, meeting)
        require(
            w.inference is not None and isinstance(policy, dict) and bool(policy),
            "ai_unconfigured",
            "Choose and consent to an AI provider before generation. Deterministic exports remain available.",
            status=503,
        )
        job = w.queue_job(
            "analysis",
            {
                "task": task,
                "input_revision": meeting["transcript_revision"],
                "note_revision": meeting["note_revision"],
                "policy_hash": meeting["policy_hash"],
                "policy": policy,
            },
            meeting_id=meeting["id"],
            idempotency_key=p.get("idempotency_key")
            or f"analysis:{meeting['id']}:{task}:{meeting['transcript_revision']}:{meeting['note_revision']}:{meeting['policy_hash']}",
        )
        if job["status"] in ("failed", "cancelled"):
            # The deterministic key returns the earlier attempt; a retry must run it again.
            job = requeue_job(w, job)
        return {"job": job, "status": "accepted"}

    r["analysis.generate"] = generate

    def analysis_list(p: dict[str, Any]) -> dict[str, Any]:
        ids = {run["id"] for run in w.list("analysis_runs", {"meeting_id": p["meeting_id"]})}
        return {"items": [i for i in w.list("analysis_results") if i["run_id"] in ids]}

    r["analysis.list"] = analysis_list

    def analysis_status(p: dict[str, Any]) -> dict[str, Any]:
        # Latest activity per task; the finalize stages run analysis inline and record the tasks
        # that failed in stage_output, so they report too. `error` is the human message.
        latest: dict[str, dict[str, Any]] = {}
        for job in sorted(
            w.list("jobs", {"meeting_id": p["meeting_id"]}),
            key=lambda j: (j["updated_at"], j["created_at"]),
        ):
            if job["kind"] == "analysis":
                tasks = [job["payload"]["task"]]
            elif job["kind"] == "finalize" and job["stage"] in (
                "final_analysis",
                "minutes",
                "memory",
            ):
                tasks = (
                    ["summary", "actions", "topics", "artifacts"]
                    if job["stage"] == "final_analysis"
                    else [job["stage"]]
                )
            else:
                continue
            output = cast(dict[str, Any], job.get("stage_output") or {})
            soft = cast(dict[str, Any], output.get("failed") or {})
            for task in tasks:
                error: Any = soft.get(task) or job.get("error")
                failed = task in soft or job["status"] == "failed"
                detail = cast(dict[str, Any], error) if isinstance(error, dict) else None
                latest[task] = {
                    "task": task,
                    "status": "failed" if failed else job["status"],
                    "error": (detail.get("message") or detail.get("code"))
                    if detail is not None
                    else error,
                    "error_code": detail.get("code") if detail is not None else None,
                    "job_id": job["id"],
                }
        return {"items": list(latest.values())}

    r["analysis.status"] = analysis_status
    r["analysis.read"] = lambda p: {"item": w.require_item("analysis_results", p["id"])}
    r["analysis.promote"] = lambda p: promote_result(w, p["id"], p.get("expected_revision"))

    def grant(p: dict[str, Any]) -> dict[str, Any]:
        data = p.get("data", p)
        return {
            "item": w.put(
                "consents",
                new_id(),
                {
                    "purpose": data["purpose"],
                    "destination_fingerprint": data["destination_fingerprint"],
                    "data_classes": data["data_classes"],
                    "scope": data["scope"],
                    "accepted_at": now_ms(),
                    "policy_version": data.get("policy_version", "1"),
                },
            )
        }

    r["consents.grant"] = grant

    def revoke(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            item = w.require_item("consents", p["id"])
            result = w.put(
                "consents",
                item["id"],
                {"revoked_at": now_ms()},
                expected_revision=p.get("expected_revision"),
            )
            w.connection.execute(
                "UPDATE jobs SET cancellation_requested=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END WHERE status IN('queued','running') AND kind IN('analysis','delivery','calendar_sync')"
            )
            return {"item": result}

    r["consents.revoke"] = revoke


async def execute_analysis(w: Workspace, job: dict[str, Any]) -> None:
    meeting = w.require_item("meetings", job["meeting_id"])
    payload = job["payload"]
    task = payload.get("task", "summary")
    from meeting_assistant.application.transcripts import all_items

    run_id = new_id()
    run = w.put(
        "analysis_runs",
        run_id,
        {
            "meeting_id": meeting["id"],
            "task": task,
            "input_revision": payload["input_revision"],
            "note_revision": payload.get("note_revision", meeting["note_revision"]),
            "watermark": payload["input_revision"],
            "policy_hash": payload.get("policy_hash", meeting["policy_hash"]),
            "status": "running",
            "prompt_version": "pending",
        },
    )
    active = w.list("transcript_runs", {"meeting_id": meeting["id"], "state": "active"})
    utterances = all_items(w, "utterances", {"run_id": active[0]["id"]}) if active else []
    utterances = [u for u in utterances if u["end_ms"] >= payload.get("window_start_ms", 0)]
    speakers = {
        speaker["id"]: speaker
        for speaker in w.list("meeting_speakers", {"meeting_id": meeting["id"]})
    }
    evidence: list[dict[str, Any]] = []
    for utterance in utterances:
        assignments = w.list("speaker_assignments", {"utterance_id": utterance["id"]})
        effective = speakers.get(assignments[0]["speaker_id"]) if len(assignments) == 1 else None
        evidence.append(
            {
                "id": utterance["id"],
                "source_id": utterance["id"],
                "meeting_id": utterance["meeting_id"],
                "text": utterance["text"],
                "start_ms": utterance["start_ms"],
                "end_ms": utterance["end_ms"],
                "revision": utterance["text_revision"],
                "kind": "transcript",
                "detected_language": utterance.get("language"),
                "speaker_name": effective["display_name"]
                if effective and effective.get("person_id")
                else None,
            }
        )
    notes = all_items(w, "notes", {"meeting_id": meeting["id"]})
    evidence += [
        {
            "id": n["id"],
            "source_id": n["id"],
            "text": n["body_markdown"],
            "heading": n["heading"],
            "revision": n["revision"],
            "kind": "human_note",
        }
        for n in notes
    ]
    try:
        timezone = meeting["config_snapshot"].get(
            "timezone", w.require_item("workspace", "workspace")["default_timezone"]
        )
        local_date = (
            datetime.fromtimestamp(
                (meeting["started_at"] or meeting["created_at"]) / 1000, ZoneInfo(timezone)
            )
            .date()
            .isoformat()
        )
        assert w.inference is not None
        output = await w.inference.generate(
            task,
            evidence,
            payload.get("policy", meeting["config_snapshot"].get("ai_policy", {})),
            language=output_language(meeting),
            context={
                "meeting_id": meeting["id"],
                "meeting_local_date": local_date,
                "meeting_timezone": timezone,
                "resolved_people": [
                    speaker["display_name"]
                    for speaker in speakers.values()
                    if speaker.get("person_id")
                ],
                "note_revision": run["note_revision"],
                "important_moments": w.list("important_moments", {"meeting_id": meeting["id"]}),
            },
        )
        result = output.get("result", output)
        allowed = {item["id"] for item in evidence}

        def validate_refs(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in cast(dict[str, Any], value).items():
                    if key in ("source_id", "utterance_id", "note_id") and child is not None:
                        require(
                            child in allowed,
                            "unknown_citation",
                            "Generated source references are outside selected evidence.",
                            status=422,
                        )
                    validate_refs(child)
            elif isinstance(value, list):
                for child in cast(list[Any], value):
                    validate_refs(child)

        validate_refs(result)
        with w.transaction():
            latest = w.require_item("analysis_runs", run_id)
            w.put(
                "analysis_runs",
                run_id,
                {
                    "status": "succeeded",
                    "prompt_version": output.get("prompt_version", "unknown"),
                    "provider_fingerprint": output.get("provider_fingerprint", ""),
                    "model_fingerprint": output.get("model_fingerprint", ""),
                },
                expected_revision=latest["revision"],
            )
            candidate = w.put(
                "analysis_results",
                new_id(),
                {
                    "run_id": run_id,
                    "kind": task,
                    "payload": result,
                    "state": "candidate",
                    "language": output_language(meeting),
                    "content_hash": fingerprint(result),
                },
            )
            latest_meeting = w.require_item("meetings", meeting["id"])
            if (
                latest_meeting["transcript_revision"] != run["input_revision"]
                or latest_meeting["note_revision"] != run["note_revision"]
                or latest_meeting["policy_hash"] != run["policy_hash"]
            ):
                w.put(
                    "analysis_results",
                    candidate["id"],
                    {"state": "superseded"},
                    expected_revision=candidate["revision"],
                )
                w.emit(
                    "analysis.superseded",
                    {"result_id": candidate["id"], "reason": "inputs_changed"},
                    meeting_id=meeting["id"],
                )
                return
            if task != "minutes":
                promote_result(w, candidate["id"], candidate["revision"])
    except Exception as exc:
        latest = w.require_item("analysis_runs", run_id)
        w.put(
            "analysis_runs",
            run_id,
            {
                "status": "failed",
                "error": exc.as_dict()
                if isinstance(exc, (AppError, ProviderError))
                else {"code": "provider_failed", "message": "Analysis failed."},
            },
            expected_revision=latest["revision"],
        )
        raise


def promote_result(w: Workspace, result_id: str, expected_revision: int | None) -> dict[str, Any]:
    with w.transaction():
        result = w.require_item("analysis_results", result_id)
        run = w.require_item("analysis_runs", result["run_id"])
        meeting = w.require_item("meetings", run["meeting_id"])
        require(
            meeting["transcript_revision"] == run["input_revision"]
            and meeting["note_revision"] == run.get("note_revision", meeting["note_revision"])
            and meeting["policy_hash"] == run["policy_hash"],
            "stale_analysis",
            "Candidate inputs no longer match the current meeting.",
        )
        w.connection.execute(
            "UPDATE analysis_results SET state='superseded' WHERE kind=? AND state='promoted' AND run_id IN(SELECT id FROM analysis_runs WHERE meeting_id=?)",
            (result["kind"], meeting["id"]),
        )
        promoted = w.put(
            "analysis_results",
            result_id,
            {"state": "promoted", "promoted_at": now_ms()},
            expected_revision=expected_revision,
        )
        raw_payload: object = result["payload"]
        payload = cast(dict[str, Any], raw_payload)
        if result["kind"] == "actions":
            candidates: list[dict[str, Any]] = (
                payload.get("candidates", payload.get("actions", []))
                if isinstance(raw_payload, dict)
                else []
            )
            for action in candidates:
                references = action.get("source_refs", [])
                source = action.get(
                    "source_id",
                    action.get("utterance_id", references[0]["source_id"] if references else None),
                )
                description = str(action.get("description", "")).strip()
                require(
                    bool(description) and bool(source),
                    "unsupported_action",
                    "Actions require explicit description and source evidence.",
                    status=422,
                )
                assert source is not None
                evidence = w.get("utterances", source) or w.require_item("notes", source)
                require(
                    evidence["meeting_id"] == meeting["id"],
                    "source_scope",
                    "Action source belongs to another meeting.",
                    status=422,
                )
                origin = fingerprint(
                    {"description": " ".join(description.casefold().split()), "source": source}
                )
                existing = w.list(
                    "actions", {"meeting_id": meeting["id"], "stable_origin_key": origin}
                )
                data = {
                    "meeting_id": meeting["id"],
                    "stable_origin_key": origin,
                    "description": description,
                    "assignee_text": action.get("assignee"),
                    "due_date": action.get("due_date"),
                    "deadline_text": action.get("deadline_text"),
                    "priority": action.get("priority", "normal"),
                    "source_result_id": result_id,
                }
                if data["assignee_text"] and str(data["assignee_text"]).casefold() in (
                    "i",
                    "we",
                    "you",
                    "they",
                    "someone",
                    "tbd",
                    "yo",
                    "nosotros",
                ):
                    data["assignee_text"] = None
                if existing:
                    current = existing[0]
                    data = {
                        k: v for k, v in data.items() if k not in current.get("manual_fields", [])
                    }
                    item = w.put(
                        "actions", current["id"], data, expected_revision=current["revision"]
                    )
                else:
                    item = w.put("actions", new_id(), data)
                for reference in references or [{"source_id": source}]:
                    cited = w.get("utterances", reference["source_id"])
                    note = w.get("notes", reference["source_id"]) if cited is None else None
                    require(
                        (cited or note or {}).get("meeting_id") == meeting["id"],
                        "source_scope",
                        "Action citation belongs to another meeting.",
                        status=422,
                    )
                    assert cited is not None or note is not None
                    source_data = (
                        {
                            "utterance_id": cited["id"],
                            "source_revision": cited["text_revision"],
                            "start_ms": cited["start_ms"],
                            "end_ms": cited["end_ms"],
                            "quote_snapshot": cited["text"],
                        }
                        if cited
                        else {
                            "note_id": cast(dict[str, Any], note)["id"],
                            "source_revision": cast(dict[str, Any], note)["revision"],
                            "quote_snapshot": cast(dict[str, Any], note)["body_markdown"],
                        }
                    )
                    if not any(
                        (row.get("utterance_id") or row.get("note_id")) == reference["source_id"]
                        for row in w.list("entity_sources", {"action_id": item["id"]})
                    ):
                        w.put(
                            "entity_sources",
                            new_id(),
                            {"action_id": item["id"], "meeting_id": meeting["id"], **source_data},
                        )
        elif result["kind"] == "topics":
            w.connection.execute("DELETE FROM topics WHERE meeting_id=?", (meeting["id"],))
            topics: list[dict[str, Any]] = payload.get("topics", [])
            identities: dict[str, str] = {}
            require(
                len({topic["title"] for topic in topics}) == len(topics),
                "duplicate_topic_key",
                "Topic hierarchy needs unambiguous title keys.",
                status=422,
            )
            pending = list(topics)
            while pending:
                ready = [
                    topic
                    for topic in pending
                    if not topic.get("parent_key") or topic["parent_key"] in identities
                ]
                require(
                    bool(ready),
                    "invalid_topic_hierarchy",
                    "Topic parents are missing or cyclic.",
                    status=422,
                )
                for topic in ready:
                    item = w.put(
                        "topics",
                        new_id(),
                        {
                            "meeting_id": meeting["id"],
                            "parent_id": identities.get(topic["parent_key"])
                            if topic.get("parent_key") is not None
                            else None,
                            "title": topic["title"],
                            "start_ms": topic["start_ms"],
                            "end_ms": topic["end_ms"],
                            "sentiment": topic.get("sentiment"),
                            "result_id": result_id,
                        },
                    )
                    identities[topic["title"]] = item["id"]
                    pending.remove(topic)
        elif result["kind"] == "artifacts":
            for section, kind in (
                ("decisions", "decision"),
                ("risks", "risk"),
                ("milestones", "milestone"),
                ("deliverables", "deliverable"),
                ("follow_ups", "follow_up"),
            ):
                for value in payload.get(section, []):
                    references = value.get("source_refs", [])
                    origin = fingerprint(
                        {
                            "kind": kind,
                            "title": value["title"].casefold(),
                            "sources": sorted(ref["source_id"] for ref in references),
                        }
                    )
                    previous = w.list(
                        "artifacts", {"meeting_id": meeting["id"], "stable_origin_key": origin}
                    )
                    fields = {
                        "meeting_id": meeting["id"],
                        "project_id": meeting.get("project_id"),
                        "kind": kind,
                        "title": value["title"],
                        "status": value.get("status", "open"),
                        "owner_text": value.get("owner"),
                        "date": value.get("date"),
                        "payload": value,
                        "result_id": result_id,
                        "stable_origin_key": origin,
                    }
                    item = w.put(
                        "artifacts",
                        previous[0]["id"] if previous else new_id(),
                        fields,
                        expected_revision=previous[0]["revision"] if previous else None,
                    )
                    for ref in references:
                        source = w.require_item("utterances", ref["source_id"])
                        w.put(
                            "entity_sources",
                            new_id(),
                            {
                                "artifact_id": item["id"],
                                "meeting_id": meeting["id"],
                                "utterance_id": source["id"],
                                "source_revision": source["text_revision"],
                                "start_ms": ref.get("start_ms", source["start_ms"]),
                                "end_ms": ref.get("end_ms", source["end_ms"]),
                                "quote_snapshot": ref.get("quote", source["text"]),
                            },
                        )
        elif result["kind"] == "cues":
            for cue in payload.get("cues", []):
                source = cue["source_refs"][0]["source_id"]
                w.put(
                    "cues",
                    new_id(),
                    {
                        "meeting_id": meeting["id"],
                        "kind": cue["kind"],
                        "text": cue["text"],
                        "source_utterance_id": source,
                        "expires_at": now_ms() + 30000,
                    },
                )
        elif result["kind"] == "memory":
            text = "\n\n".join(
                section["heading"] + "\n" + section["body"]
                for section in payload.get("sections", [])
            )
            w.put(
                "memory_segments",
                new_id(),
                {
                    "project_id": meeting.get("project_id"),
                    "meeting_id": meeting["id"],
                    "result_id": result_id,
                    "input_revision": run["input_revision"],
                    "summary": text,
                },
            )
        elif result["kind"] == "briefings":
            w.put(
                "briefings",
                new_id(),
                {
                    "meeting_id": meeting["id"],
                    "project_id": meeting.get("project_id"),
                    "kind": "post" if meeting["state"] in ("processing", "complete") else "pre",
                    "scope_hash": fingerprint(
                        {
                            "project_id": meeting.get("project_id"),
                        }
                    ),
                    "input_fingerprint": fingerprint(
                        {
                            "transcript": run["input_revision"],
                            "notes": run["note_revision"],
                            "policy": run["policy_hash"],
                        }
                    ),
                    "result_id": result_id,
                },
            )

        def citation_refs(value: Any) -> Iterator[dict[str, Any]]:
            if isinstance(value, dict):
                if "source_id" in value:
                    yield cast(dict[str, Any], value)
                for child in cast(dict[str, Any], value).values():
                    yield from citation_refs(child)
            elif isinstance(value, list):
                for child in cast(list[Any], value):
                    yield from citation_refs(child)

        seen: set[str] = set()
        for ref in citation_refs(payload):
            source_id = ref["source_id"]
            if source_id in seen:
                continue
            seen.add(source_id)
            utterance = w.get("utterances", source_id)
            note = w.get("notes", source_id)
            if utterance or note:
                source = utterance or note
                assert source is not None
                w.put(
                    "citations",
                    new_id(),
                    {
                        "owner_result_id": result_id,
                        "meeting_id": source["meeting_id"],
                        "source_utterance_id": source_id if utterance else None,
                        "source_note_id": source_id if note else None,
                        "source_revision": source.get("text_revision", source["revision"]),
                        "start_ms": ref.get("start_ms"),
                        "end_ms": ref.get("end_ms"),
                        "quote_snapshot": ref.get(
                            "quote", source.get("text", source.get("body_markdown", ""))
                        ),
                    },
                )
        return {"item": promoted}
