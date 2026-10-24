from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.routing import task_policy
from meeting_assistant.application.transcripts import (
    active_run,
    all_items,
    transcript_source_watermark,
)
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def register_media(w: Workspace, r: CommandRegistry) -> None:
    async def import_media(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("temporary_decode_consent") is True,
            "temporary_decode_consent_required",
            "Import needs explicit permission to create temporary decoded audio.",
            status=403,
        )
        grant = p.get("file_grant", p.get("grant_id"))
        require(
            isinstance(grant, str),
            "file_grant_required",
            "Choose an audio file through the native picker.",
            status=422,
        )
        require(
            w.native_gateway is not None,
            "native_unavailable",
            "Audited file decoding requires the native host.",
            status=503,
        )
        policy = (
            p.get("asr_policy")
            or p.get("policy")
            or ({"model_profile_id": p["profile_id"]} if p.get("profile_id") else None)
            or task_policy(w, "asr", {"project_id": p.get("project_id")})
        )
        require(
            bool(policy),
            "asr_unavailable",
            "Select a ready ASR profile for file import.",
            status=503,
        )
        assert w.inference is not None
        assert policy is not None
        await w.inference.preflight_asr(policy)

        def create() -> dict[str, Any]:
            created = cast(
                dict[str, Any],
                r["meetings.create"](
                    {
                        "title": p.get("title", "Imported recording"),
                        "source": "file",
                        "project_id": p.get("project_id"),
                        "config": {
                            "asr_policy": policy,
                            "recording": bool(p.get("retain_audio", False)),
                            "retention_days": p.get("retention_days", 7),
                            "meeting_language": p.get("language"),
                            "output_language": p.get("output_language", "auto"),
                        },
                    }
                ),
            )
            meeting = created["item"]
            source = w.put(
                "capture_sources",
                new_id(),
                {
                    "meeting_id": meeting["id"],
                    "source_kind": "file",
                    "device_key": "granted-file",
                    "display_name": "Imported audio",
                    "state": "stopped",
                },
            )
            w.put(
                "meetings",
                meeting["id"],
                {"state": "processing"},
                expected_revision=meeting["revision"],
            )
            job = w.queue_job(
                "media_import",
                {
                    **p,
                    "grant_id": grant,
                    "policy": policy,
                    "source_id": source["id"],
                    "run_id": created["transcript_run"]["id"],
                },
                meeting_id=meeting["id"],
                idempotency_key="media-import:" + p["idempotency_key"],
            )
            return {
                "item": w.require_item("meetings", meeting["id"]),
                "job": job,
                "status": "accepted",
            }

        return w.idempotent("media.import", p, create)

    r["media.import"] = import_media

    async def retranscribe(p: dict[str, Any]) -> dict[str, Any]:
        meeting = w.require_item("meetings", p["meeting_id"])
        require(
            meeting["state"] not in ("starting", "recording", "paused", "stopping"),
            "capture_active",
            "Re-transcription is available after live capture has stopped.",
        )
        start = int(p.get("start_ms") or 0)
        end = int(p["end_ms"]) if p.get("end_ms") is not None else None
        require(
            start >= 0 and (end is None or end > start),
            "invalid_range",
            "Choose a nonempty range on the meeting timeline.",
            status=422,
        )
        grant = p.get("file_grant", p.get("grant_id"))
        media = all_items(w, "media_segments", {"meeting_id": meeting["id"], "state": "committed"})
        media = [
            item
            for item in media
            if item["start_ms"] + item["duration_ms"] > start
            and (end is None or item["start_ms"] < end)
            and (not p.get("source_id") or item["source_id"] == p["source_id"])
        ]
        media.sort(key=lambda item: (item["start_ms"], item["source_id"] or "", item["id"]))
        require(
            grant or bool(media),
            "audio_unavailable",
            "Retained audio in the selected range or a newly granted matching source file is required.",
            status=503,
        )
        require(
            p.get("temporary_decode_consent") is True,
            "temporary_decode_consent_required",
            "Re-transcription requires explicit temporary decode permission.",
            status=403,
        )
        require(
            w.native_gateway is not None,
            "native_unavailable",
            "Audited file decoding requires the native host.",
            status=503,
        )
        if p.get("source_id"):
            require(
                w.require_item("capture_sources", p["source_id"])["meeting_id"] == meeting["id"],
                "source_scope",
                "Capture source belongs to another meeting.",
                status=422,
            )
        policy = (
            p.get("asr_policy")
            or p.get("policy")
            or ({"model_profile_id": p["profile_id"]} if p.get("profile_id") else None)
            or meeting["config_snapshot"].get("asr_policy")
        )
        require(bool(policy), "asr_unavailable", "Choose a ready ASR policy.", status=503)
        assert w.inference is not None
        assert policy is not None
        await w.inference.preflight_asr(policy)

        def create() -> dict[str, Any]:
            current = w.require_item("meetings", meeting["id"])
            require(
                current["revision"] == meeting["revision"],
                "revision_conflict",
                "Meeting changed while preparing re-transcription.",
            )
            media_ids = [] if grant else [item["id"] for item in media]
            watermark = transcript_source_watermark(w, meeting["id"], media_ids)
            run = w.put(
                "transcript_runs",
                new_id(),
                {
                    "meeting_id": meeting["id"],
                    "source": "retranscribe",
                    "state": "candidate",
                    "parent_run_id": active_run(w, meeting["id"])["id"],
                    "language_policy": p.get("language") or "auto",
                    "input_revision": current["transcript_revision"],
                    "source_watermark": watermark,
                    "media_ids": media_ids,
                    "requested_range": {"start_ms": start, "end_ms": end},
                },
            )
            job = w.queue_job(
                "retranscribe",
                {
                    **p,
                    "start_ms": start,
                    "end_ms": end,
                    "grant_id": grant,
                    "policy": policy,
                    "run_id": run["id"],
                    "media_ids": media_ids,
                    "input_revision": current["transcript_revision"],
                },
                meeting_id=meeting["id"],
                idempotency_key="retranscribe:" + p["idempotency_key"],
            )
            return {
                "item": run,
                "job": job,
                "status": "accepted",
                "active_transcript_unchanged": True,
            }

        return w.idempotent("media.retranscribe", p, create)

    r["media.retranscribe"] = retranscribe

    async def execute(job: dict[str, Any]) -> dict[str, Any]:
        payload = job["payload"]
        mid = job["meeting_id"]
        meeting = w.require_item("meetings", mid)
        run_id = payload["run_id"]
        staged: list[Path] = []
        candidate = job["kind"] == "retranscribe"
        coverage: list[dict[str, Any]] = []
        elapsed_ms = 0

        def check_cancelled() -> None:
            if w.require_item("jobs", job["id"])["cancellation_requested"]:
                raise AppError("cancelled", "Media processing cancelled.")

        async def native_media(operation: str, request: dict[str, Any]) -> dict[str, Any]:
            task = asyncio.create_task(w.native(operation, request))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if operation in ("media.decode", "media.decode_existing"):
                    with suppress(Exception):
                        await w.native("media.decode_cancel", {"job_id": job["id"]})
                # The native writer owns the file until its response. In particular,
                # Windows cannot unlink an open file or safely race a decoder write.
                with suppress(Exception, asyncio.CancelledError):
                    await task
                raise

        try:
            # A failed/restarted attempt must not make old partial hypotheses promotable.
            if candidate:
                run = w.require_item("transcript_runs", run_id)
                if run.get("completed_at") is not None:
                    return {
                        "run_id": run_id,
                        "candidate": True,
                        "utterances": len(all_items(w, "utterances", {"run_id": run_id})),
                    }
                require(
                    run["state"] == "candidate",
                    "candidate_incomplete",
                    "Candidate is no longer pending.",
                )
                for item in all_items(w, "utterances", {"run_id": run_id}):
                    w.delete("utterances", item["id"], expected_revision=item["revision"])
                w.put(
                    "transcript_runs",
                    run_id,
                    {"completed_at": None, "coverage": []},
                    expected_revision=run["revision"],
                )
            inputs = [None] if payload.get("grant_id") else list(payload.get("media_ids", []))
            require(bool(inputs), "empty_import", "Decoder produced no audio assets.", status=422)
            transcript_count = 0
            for media_id in inputs:
                check_cancelled()
                relative = "staging/" + new_id() + ".f32"
                path = (w.root / relative).resolve()
                staged.append(path)
                source_id = payload.get("source_id")
                timeline_start = 0
                if media_id is None:
                    asset = await native_media(
                        "media.decode",
                        {
                            "grant_id": payload["grant_id"],
                            "job_id": job["id"],
                            "temporary_decode_consent": True,
                            "output_relative_path": relative,
                        },
                    )
                else:
                    media = w.require_item("media_segments", media_id)
                    require(
                        media["meeting_id"] == mid and media["state"] == "committed",
                        "audio_unavailable",
                        "Retained source audio is no longer available.",
                        status=409,
                    )
                    source_id = media["source_id"]
                    timeline_start = int(media["start_ms"])
                    asset = await native_media(
                        "media.decode_existing",
                        {
                            "media_id": media_id,
                            "relative_path": media["relative_path"],
                            "meeting_id": mid,
                            "job_id": job["id"],
                            "temporary_decode_consent": True,
                            "output_relative_path": relative,
                            "timeline_start_ms": timeline_start,
                        },
                    )
                require(
                    asset["decoded_relative_path"] == relative
                    and path.is_relative_to(w.root / "staging")
                    and path.is_file(),
                    "decoder_protocol",
                    "Decoder output is not the job's private temporary file.",
                    status=422,
                )
                require(
                    asset["sample_rate"] == 16000
                    and asset["channels"] == 1
                    and 0 < asset["duration_ms"] <= 8 * 3600000
                    and asset["bytes"] == path.stat().st_size
                    and asset["bytes"] == asset["frames"] * 4
                    and asset["duration_ms"] == asset["frames"] * 1000 // 16000,
                    "invalid_decoded_media",
                    "Decoded audio dimensions or duration are invalid.",
                    status=422,
                )
                if not candidate:
                    w.put(
                        "import_assets",
                        new_id(),
                        {
                            "meeting_id": mid,
                            "source_id": source_id,
                            "original_name": asset["original_name"],
                            "hash": asset["hash"],
                            "duration_ms": asset["duration_ms"],
                            "decoded_profile": {
                                "rate": 16000,
                                "channels": 1,
                                "format": "float32le",
                            },
                            "retained": int(payload.get("retain_audio", False)),
                            "origin_metadata": {"codec": asset["codec"]},
                        },
                    )
                elif payload.get("grant_id"):
                    originals = all_items(w, "import_assets", {"meeting_id": mid})
                    matching = [item for item in originals if item["hash"] == asset["hash"]]
                    require(
                        bool(matching),
                        "source_hash_mismatch",
                        "Selected file does not match a known imported meeting source.",
                        status=409,
                    )
                    known_sources = {
                        item.get("source_id") for item in matching if item.get("source_id")
                    }
                    if not known_sources:
                        known_sources = {
                            item["id"]
                            for item in all_items(w, "capture_sources", {"meeting_id": mid})
                            if item["source_kind"] == "file"
                        }
                    if source_id:
                        require(
                            source_id in known_sources,
                            "source_scope",
                            "Selected file belongs to a different source.",
                            status=422,
                        )
                    else:
                        require(
                            len(known_sources) == 1,
                            "source_scope",
                            "Select the matching imported audio source.",
                            status=422,
                        )
                        source_id = next(iter(known_sources))
                start_ms = max(0, int(payload.get("start_ms") or 0) - timeline_start)
                requested_end = payload.get("end_ms")
                end_ms = min(
                    asset["duration_ms"],
                    int(requested_end) - timeline_start
                    if requested_end is not None
                    else asset["duration_ms"],
                )
                if start_ms >= end_ms:
                    path.unlink(missing_ok=True)
                    continue
                with path.open("rb") as source:
                    source.seek(start_ms * 16 * 4)
                    offset = start_ms
                    while offset < end_ms:
                        check_cancelled()
                        duration = min(12000, end_ms - offset)
                        pcm = source.read(duration * 16 * 4)
                        require(
                            len(pcm) == duration * 16 * 4,
                            "truncated_decode",
                            "Temporary decoded audio ended unexpectedly.",
                            status=422,
                        )
                        assert w.inference is not None
                        language = payload.get("language")
                        results = await w.inference.transcribe(
                            pcm,
                            sample_rate=16000,
                            policy=payload["policy"],
                            origin_ms=offset + timeline_start,
                            stream_id=job["id"] + ":" + str(source_id or "file"),
                            chunk_id=str(offset + timeline_start),
                            vocabulary=payload.get("vocabulary", []),
                            language=None if not language or language == "auto" else language,
                        )
                        check_cancelled()
                        for result in results:
                            require(
                                offset + timeline_start
                                <= result["start_ms"]
                                <= result["end_ms"]
                                <= offset + timeline_start + duration,
                                "invalid_span",
                                "Recognizer result exceeds the decoded source range.",
                                status=422,
                            )
                            ingested = await w.adispatch(
                                "transcript.ingest",
                                {
                                    **result,
                                    "meeting_id": mid,
                                    "run_id": run_id,
                                    "source_id": source_id,
                                    "provider_stream_id": result.get(
                                        "provider_stream_id", job["id"]
                                    ),
                                },
                            )
                            if ingested.get("item"):
                                transcript_count += 1
                        offset += duration
                        current = w.require_item("jobs", job["id"])
                        w.put(
                            "jobs",
                            job["id"],
                            {
                                "progress": {
                                    "processed_ms": offset + timeline_start,
                                    "range_end_ms": end_ms + timeline_start,
                                }
                            },
                            expected_revision=current["revision"],
                        )
                coverage.append(
                    {
                        "source_id": source_id,
                        "start_ms": start_ms + timeline_start,
                        "end_ms": end_ms + timeline_start,
                    }
                )
                elapsed_ms = max(elapsed_ms, timeline_start + asset["duration_ms"])
                if payload.get("retain_audio") and not candidate:
                    retained = await native_media(
                        "media.retain",
                        {
                            "decoded_relative_path": relative,
                            "meeting_id": mid,
                            "source_id": source_id,
                            "retention_days": meeting["config_snapshot"].get("retention_days", 7),
                        },
                    )
                    for commit in retained.get("committed_media", []):
                        await w.adispatch("media.commit", commit)
                path.unlink(missing_ok=True)
            check_cancelled()
            require(
                bool(coverage),
                "invalid_range",
                "Selected range contains no source audio.",
                status=422,
            )
            require(
                transcript_count > 0,
                "empty_transcript",
                "ASR produced no meaningful speech; the previous active transcript is preserved.",
                status=422,
            )
            run = w.require_item("transcript_runs", run_id)
            w.put(
                "transcript_runs",
                run_id,
                {"completed_at": now_ms(), "coverage": coverage},
                expected_revision=run["revision"],
            )
            if candidate:
                return {"run_id": run_id, "candidate": True, "utterances": transcript_count}
            current = w.require_item("meetings", mid)
            w.put(
                "meetings",
                mid,
                {
                    "state": "interrupted",
                    "elapsed_ms": elapsed_ms,
                    "active_ms": elapsed_ms,
                    "interrupted_reason": None,
                },
                expected_revision=current["revision"],
            )
            await w.adispatch(
                "meetings.stop",
                {"meeting_id": mid, "idempotency_key": "import-finalize:" + job["id"]},
            )
            return {"meeting_id": mid, "utterances": transcript_count}
        except (Exception, asyncio.CancelledError) as exc:
            run = w.get("transcript_runs", run_id)
            cancelled = (
                isinstance(exc, asyncio.CancelledError)
                or (isinstance(exc, AppError) and exc.code == "cancelled")
                or bool(w.require_item("jobs", job["id"])["cancellation_requested"])
            )
            if candidate and run and cancelled:
                w.delete("transcript_runs", run_id, expected_revision=run["revision"])
            if not candidate:
                current = w.require_item("meetings", mid)
                w.put(
                    "meetings",
                    mid,
                    {
                        "state": "attention_required",
                        "interrupted_reason": "Audio import needs review; persisted transcript, if any, is retained.",
                    },
                    expected_revision=current["revision"],
                )
            raise
        finally:
            for path in staged:
                path.unlink(missing_ok=True)

    r["job.media_import"] = execute
    r["job.retranscribe"] = execute
