from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from meeting_assistant.application.transcripts import (
    active_run,
    all_items,
    transcript_source_watermark,
)
from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.model import new_id, now_ms
from meeting_assistant.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


def meeting_source(w: Workspace) -> tuple[dict[str, Any], dict[str, Any]]:
    meeting = w.dispatch(
        "meetings.create", {"title": "Synthetic candidate review", "source": "text"}
    )["item"]
    source = w.put(
        "capture_sources",
        new_id(),
        {
            "meeting_id": meeting["id"],
            "source_kind": "file",
            "device_key": "synthetic",
            "state": "stopped",
        },
    )
    return meeting, source


def utterance(
    w: Workspace,
    meeting_id: str,
    source_id: str,
    start: int,
    end: int,
    text: str,
    *,
    run_id: str | None = None,
    words: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return w.dispatch(
        "transcript.ingest",
        {
            "meeting_id": meeting_id,
            "source_id": source_id,
            "provider_stream_id": "synthetic-domain-input",
            "provider_result_id": new_id(),
            "run_id": run_id or active_run(w, meeting_id)["id"],
            "start_ms": start,
            "end_ms": end,
            "text": text,
            "language": "en",
            "words": words or [],
            "is_final": True,
        },
    )["item"]


def candidate(w: Workspace, mid: str, source_id: str, start: int, end: int) -> dict[str, Any]:
    meeting = w.require_item("meetings", mid)
    return w.put(
        "transcript_runs",
        new_id(),
        {
            "meeting_id": mid,
            "source": "retranscribe",
            "state": "candidate",
            "parent_run_id": active_run(w, mid)["id"],
            "input_revision": meeting["transcript_revision"],
            "source_watermark": transcript_source_watermark(w, mid, []),
            "media_ids": [],
            "coverage": [{"source_id": source_id, "start_ms": start, "end_ms": end}],
        },
    )


def complete(w: Workspace, run: dict[str, Any]) -> dict[str, Any]:
    current = w.require_item("transcript_runs", run["id"])
    return w.put(
        "transcript_runs",
        run["id"],
        {"completed_at": now_ms()},
        expected_revision=current["revision"],
    )


def promote(w: Workspace, run: dict[str, Any], **payload: Any) -> dict[str, Any]:
    return w.dispatch(
        "transcript.promote", {"run_id": run["id"], "expected_revision": run["revision"], **payload}
    )


def test_ranged_promotion_preserves_manual_text_speakers_other_sources_and_provenance(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    original_run = active_run(w, mid)
    before = utterance(w, mid, sid, 0, 1000, "The introduction remains unchanged.")
    locked = utterance(w, mid, sid, 1000, 2000, "The wrong date was Monday.")
    locked = w.dispatch(
        "transcript.edit",
        {
            "id": locked["id"],
            "expected_revision": locked["revision"],
            "text": "The corrected date is Friday.",
        },
    )["item"]
    spoken = utterance(w, mid, sid, 2000, 3000, "The old review wording.")
    after = utterance(w, mid, sid, 3000, 4000, "The conclusion remains unchanged.")
    other_source = w.put(
        "capture_sources",
        new_id(),
        {"meeting_id": mid, "source_kind": "system", "device_key": "second-track"},
    )
    utterance(w, mid, other_source["id"], 2000, 3000, "A simultaneous second source stays intact.")
    speaker = w.put(
        "meeting_speakers",
        new_id(),
        {"meeting_id": mid, "display_name": "Reviewed speaker", "manual_name_lock": 1},
    )
    spoken = w.dispatch(
        "transcript.reassign",
        {"id": spoken["id"], "expected_revision": spoken["revision"], "speaker_id": speaker["id"]},
    )["item"]
    moment = w.put(
        "important_moments",
        new_id(),
        {
            "meeting_id": mid,
            "utterance_id": before["id"],
            "start_ms": 0,
            "quote_snapshot": before["text"],
        },
    )
    action = w.put(
        "actions",
        new_id(),
        {
            "meeting_id": mid,
            "stable_origin_key": "synthetic-reviewed-action",
            "description": "Preserve the reviewed action",
            "status": "done",
            "manual_fields": ["status"],
        },
    )
    evidence = w.put(
        "entity_sources",
        new_id(),
        {
            "meeting_id": mid,
            "action_id": action["id"],
            "utterance_id": spoken["id"],
            "source_revision": spoken["text_revision"],
            "start_ms": 2000,
            "end_ms": 3000,
            "quote_snapshot": spoken["text"],
        },
    )
    note = w.put(
        "notes", new_id(), {"meeting_id": mid, "body_markdown": "Independent human context"}
    )
    citation = w.put(
        "citations",
        new_id(),
        {
            "meeting_id": mid,
            "owner_note_id": note["id"],
            "source_utterance_id": spoken["id"],
            "source_revision": spoken["text_revision"],
            "start_ms": 2000,
            "end_ms": 3000,
            "quote_snapshot": spoken["text"],
        },
    )
    run = candidate(w, mid, sid, 1000, 3000)
    raw_locked = utterance(
        w, mid, sid, 1000, 2000, "An ASR guess must not erase Friday.", run_id=run["id"]
    )
    raw_spoken = utterance(
        w, mid, sid, 2000, 3000, "The improved review wording.", run_id=run["id"]
    )
    run = complete(w, run)
    baseline_revision = w.require_item("meetings", mid)["transcript_revision"]
    comparison = w.dispatch("transcript.diff", {"run_id": run["id"]})
    assert not comparison["stale"]
    assert len(comparison["manual_conflicts"]) == 2
    assert comparison["suppressed_candidate_ids"] == [raw_locked["id"]]
    assert w.require_item("meetings", mid)["transcript_revision"] == baseline_revision
    with pytest.raises(AppError) as conflict:
        promote(w, run)
    assert conflict.value.code == "manual_conflicts"
    assert active_run(w, mid)["id"] == original_run["id"]
    result = promote(w, run, acknowledge_manual_conflicts=True)
    rows = all_items(w, "utterances", {"run_id": result["item"]["id"]})
    assert {row["text"] for row in rows} == {
        before["text"],
        locked["text"],
        raw_spoken["text"],
        after["text"],
        "A simultaneous second source stays intact.",
    }
    corrected = next(row for row in rows if row["text"] == locked["text"])
    mapped = next(row for row in rows if row["text"] == raw_spoken["text"])
    assert corrected["manual_text_lock"] == 1
    assert mapped["manual_speaker_lock"] == 1
    assignments = all_items(w, "speaker_assignments", {"utterance_id": mapped["id"]})
    assert [
        (row["speaker_id"], row["start_ms"], row["end_ms"], row["manual_lock"])
        for row in assignments
    ] == [(speaker["id"], 2000, 3000, 1)]
    assert w.require_item("utterances", raw_locked["id"])["text"] == raw_locked["text"]
    assert w.require_item("transcript_runs", run["id"])["state"] == "archived"
    assert w.require_item("transcript_runs", original_run["id"])["state"] == "archived"
    mapped_moment = w.require_item("important_moments", moment["id"])
    assert mapped_moment["utterance_id"] != before["id"] and not mapped_moment["stale"]
    assert mapped_moment["quote_snapshot"] == before["text"]
    mapped_evidence = w.require_item("entity_sources", evidence["id"])
    assert mapped_evidence["utterance_id"] == mapped["id"] and mapped_evidence["stale"]
    assert mapped_evidence["quote_snapshot"] == spoken["text"]
    mapped_citation = w.require_item("citations", citation["id"])
    assert mapped_citation["source_utterance_id"] == mapped["id"] and mapped_citation["stale"]
    assert mapped_citation["quote_snapshot"] == spoken["text"]
    assert w.require_item("actions", action["id"])["status"] == "done"
    assert w.require_item("meeting_speakers", speaker["id"])["manual_name_lock"] == 1
    assert all_items(w, "utterance_revisions", {"utterance_id": corrected["id"]})
    late = w.dispatch(
        "transcript.ingest",
        {
            "meeting_id": mid,
            "run_id": result["item"]["id"],
            "source_id": sid,
            "provider_stream_id": corrected["provider_stream_id"],
            "provider_result_id": corrected["provider_result_id"],
            "start_ms": corrected["start_ms"],
            "end_ms": corrected["end_ms"],
            "text": "A late recognizer still guesses Monday.",
            "language": "en",
        },
    )["item"]
    assert late["text"] == locked["text"] and late["manual_text_lock"]
    wrong_speaker = w.put(
        "meeting_speakers", new_id(), {"meeting_id": mid, "display_name": "Late provider identity"}
    )
    w.dispatch(
        "transcript.ingest",
        {
            "meeting_id": mid,
            "run_id": result["item"]["id"],
            "source_id": sid,
            "provider_stream_id": mapped["provider_stream_id"],
            "provider_result_id": mapped["provider_result_id"],
            "start_ms": mapped["start_ms"],
            "end_ms": mapped["end_ms"],
            "text": mapped["text"],
            "language": "en",
            "speaker_id": wrong_speaker["id"],
        },
    )
    assert {
        row["speaker_id"]
        for row in all_items(w, "speaker_assignments", {"utterance_id": mapped["id"]})
    } == {speaker["id"]}


def test_ranged_promotion_splits_only_real_word_boundaries_and_keeps_unmapped_quotes(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    words = [
        {"text": text, "start_ms": index * 1000, "end_ms": (index + 1) * 1000}
        for index, text in enumerate(["Opening", "old", "wording", "closing."])
    ]
    old = utterance(w, mid, sid, 0, 4000, "Opening old wording closing.", words=words)
    moment = w.put(
        "important_moments",
        new_id(),
        {
            "meeting_id": mid,
            "utterance_id": old["id"],
            "start_ms": 0,
            "quote_snapshot": old["text"],
        },
    )
    run = candidate(w, mid, sid, 1000, 3000)
    utterance(w, mid, sid, 1000, 3000, "Reviewed replacement.", run_id=run["id"])
    run = complete(w, run)
    result = promote(w, run)
    rows = sorted(
        all_items(w, "utterances", {"run_id": result["item"]["id"]}),
        key=lambda row: row["start_ms"],
    )
    assert [(row["start_ms"], row["end_ms"], row["text"]) for row in rows] == [
        (0, 1000, "Opening"),
        (1000, 3000, "Reviewed replacement."),
        (3000, 4000, "closing."),
    ]
    saved = w.require_item("important_moments", moment["id"])
    assert saved["quote_snapshot"] == old["text"] and saved["stale"]
    # A full historical quote cannot be falsely presented as a quote from a fragment.
    assert saved["utterance_id"] == old["id"]


def test_segment_only_candidate_cannot_drop_neighbors_of_manual_text(workspace: Workspace) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    first = utterance(w, mid, sid, 0, 1000, "Keep the first neighbor.")
    middle = utterance(w, mid, sid, 1000, 2000, "Keep the manual wording.")
    w.dispatch(
        "transcript.edit",
        {"id": middle["id"], "expected_revision": middle["revision"], "text": middle["text"]},
    )
    last = utterance(w, mid, sid, 2000, 3000, "Keep the last neighbor.")
    run = candidate(w, mid, sid, 0, 3000)
    generated = utterance(
        w,
        mid,
        sid,
        0,
        3000,
        "A segment spanning every original row without word timing.",
        run_id=run["id"],
    )
    run = complete(w, run)
    comparison = w.dispatch("transcript.diff", {"run_id": run["id"]})
    assert generated["id"] in comparison["suppressed_candidate_ids"]
    result = promote(w, run, acknowledge_manual_conflicts=True)
    rows = all_items(w, "utterances", {"run_id": result["item"]["id"]})
    assert {row["text"] for row in rows} == {first["text"], middle["text"], last["text"]}


@pytest.mark.parametrize("change", ["text", "source", "stop_watermark"])
def test_stale_candidate_cannot_archive_active_transcript(
    workspace: Workspace, change: str
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    old = utterance(w, mid, sid, 0, 2000, "The source watermark must remain current.")
    original_run = active_run(w, mid)
    run = candidate(w, mid, sid, 0, 2000)
    utterance(w, mid, sid, 0, 2000, "A candidate must not overwrite new edits.", run_id=run["id"])
    run = complete(w, run)
    if change == "text":
        w.dispatch(
            "transcript.edit",
            {
                "id": old["id"],
                "expected_revision": old["revision"],
                "text": "A newer human correction survives.",
            },
        )
    elif change == "source":
        w.put(
            "capture_sources",
            sid,
            {"display_name": "Changed source identity"},
            expected_revision=source["revision"],
        )
    else:
        current = w.require_item("meetings", mid)
        w.put("meetings", mid, {"stop_watermark": 42}, expected_revision=current["revision"])
    assert w.dispatch("transcript.diff", {"run_id": run["id"]})["stale"]
    with pytest.raises(AppError) as error:
        promote(w, run, acknowledge_manual_conflicts=True)
    assert error.value.code == "stale_candidate"
    assert active_run(w, mid)["id"] == original_run["id"]
    assert w.require_item("transcript_runs", run["id"])["state"] == "candidate"


def test_completed_candidate_rejects_late_hypothesis(workspace: Workspace) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    run = candidate(w, meeting["id"], source["id"], 0, 1000)
    row = utterance(
        w, meeting["id"], source["id"], 0, 1000, "Immutable completed speech.", run_id=run["id"]
    )
    complete(w, run)
    with pytest.raises(AppError) as error:
        utterance(
            w,
            meeting["id"],
            source["id"],
            0,
            1000,
            "A late change cannot alter the candidate.",
            run_id=run["id"],
        )
    assert error.value.code == "candidate_complete"
    assert w.require_item("utterances", row["id"])["text"] == row["text"]


def media_segment(
    w: Workspace, mid: str, sid: str, start: int, duration: int = 1000
) -> dict[str, Any]:
    return w.put(
        "media_segments",
        new_id(),
        {
            "meeting_id": mid,
            "source_id": sid,
            "relative_path": f"media/{new_id()}.opus",
            "codec": "opus",
            "rate": 48000,
            "channels": 1,
            "start_ms": start,
            "duration_ms": duration,
            "sample_count": duration * 48,
            "bytes": 100,
            "sha256": "ab" * 32,
            "state": "committed",
        },
    )


async def ready_policy(policy: dict[str, Any]) -> dict[str, Any]:
    return policy


async def queue_candidate(
    w: Workspace, monkeypatch: pytest.MonkeyPatch, mid: str, **payload: Any
) -> dict[str, Any]:
    monkeypatch.setattr(w.inference, "preflight_asr", ready_policy)
    return await w.adispatch(
        "media.retranscribe",
        {
            "meeting_id": mid,
            "idempotency_key": new_id(),
            "temporary_decode_consent": True,
            "policy": {"adapter_id": "synthetic-ready-policy"},
            **payload,
        },
    )


async def test_all_retained_segments_are_queued_and_queued_cancel_preserves_active(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    original = utterance(
        w, meeting["id"], source["id"], 0, 1000, "Active transcript survives cancellation."
    )
    original_run = active_run(w, meeting["id"])
    for index in range(205):
        media_segment(w, meeting["id"], source["id"], index * 1000)
    w.native_gateway = lambda operation, payload: {}
    queued = await queue_candidate(w, monkeypatch, meeting["id"])
    assert len(queued["job"]["payload"]["media_ids"]) == 205
    w.dispatch(
        "jobs.cancel", {"id": queued["job"]["id"], "expected_revision": queued["job"]["revision"]}
    )
    assert w.get("transcript_runs", queued["item"]["id"]) is None
    assert active_run(w, meeting["id"])["id"] == original_run["id"]
    assert w.require_item("utterances", original["id"])["text"] == original["text"]
    ranged = await queue_candidate(w, monkeypatch, meeting["id"], start_ms=203500, end_ms=204500)
    assert len(ranged["job"]["payload"]["media_ids"]) == 2
    finished = complete(w, ranged["item"])
    w.dispatch(
        "jobs.cancel", {"id": ranged["job"]["id"], "expected_revision": ranged["job"]["revision"]}
    )
    assert w.require_item("transcript_runs", finished["id"])["completed_at"] is not None
    assert active_run(w, meeting["id"])["id"] == original_run["id"]


async def test_range_across_retained_segments_uses_meeting_timeline_and_preserves_active_until_promote(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    old = utterance(w, mid, sid, 0, 500, "The outside text remains intact.")
    original_run = active_run(w, mid)
    for start in (0, 1000, 2000):
        media_segment(w, mid, sid, start)
    decoded: list[str] = []

    async def decoder(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert operation == "media.decode_existing"
        decoded.append(payload["media_id"])
        path = w.root / payload["output_relative_path"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"\x00" * 64000)
        return {
            "decoded_relative_path": payload["output_relative_path"],
            "sample_rate": 16000,
            "channels": 1,
            "duration_ms": 1000,
            "frames": 16000,
            "bytes": 64000,
        }

    calls: list[tuple[int, int, str | None]] = []

    async def recognizer(pcm: bytes, **request: Any) -> list[dict[str, Any]]:
        origin = request["origin_ms"]
        duration = len(pcm) // 64
        calls.append((origin, duration, request["language"]))
        return [
            {
                "provider_stream_id": request["stream_id"],
                "provider_result_id": str(origin),
                "start_ms": origin,
                "end_ms": origin + duration,
                "text": "Reviewed audio in the requested source range.",
                "language": "en",
                "is_final": True,
            }
        ]

    w.native_gateway = decoder
    monkeypatch.setattr(w.inference, "transcribe", recognizer)
    queued = await queue_candidate(w, monkeypatch, mid, start_ms=1500, end_ms=2500, language="auto")
    await w.adispatch("job.retranscribe", queued["job"])
    assert len(decoded) == 2
    assert calls == [(1500, 500, None), (2000, 500, None)]
    assert active_run(w, mid)["id"] == original_run["id"]
    assert w.require_item("utterances", old["id"])["text"] == old["text"]
    run = w.require_item("transcript_runs", queued["item"]["id"])
    assert run["coverage"] == [
        {"source_id": sid, "start_ms": 1500, "end_ms": 2000},
        {"source_id": sid, "start_ms": 2000, "end_ms": 2500},
    ]
    replay = await w.adispatch("job.retranscribe", queued["job"])
    assert replay["utterances"] == 2 and len(decoded) == 2
    assert w.require_item("transcript_runs", run["id"])["revision"] == run["revision"]
    assert not list((w.root / "staging").glob("*.f32"))
    result = promote(w, run)
    rows = sorted(
        all_items(w, "utterances", {"run_id": result["item"]["id"]}),
        key=lambda row: row["start_ms"],
    )
    assert [(row["start_ms"], row["end_ms"]) for row in rows] == [
        (0, 500),
        (1500, 2000),
        (2000, 2500),
    ]
    assert all(row["source_id"] == sid for row in rows)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_or_cancelled_candidate_keeps_active_and_cleans_temporary_audio(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    original = utterance(
        w, mid, sid, 0, 2000, "The active transcript must survive inference failures."
    )
    original_run = active_run(w, mid)
    media_segment(w, mid, sid, 0, 2000)
    media_segment(w, mid, sid, 2000, 2000)

    async def decoder(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = w.root / payload["output_relative_path"]
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"\x00" * 128000)
        return {
            "decoded_relative_path": payload["output_relative_path"],
            "sample_rate": 16000,
            "channels": 1,
            "duration_ms": 2000,
            "frames": 32000,
            "bytes": 128000,
        }

    calls = 0

    async def failing_recognizer(pcm: bytes, **request: Any) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [
                {
                    "provider_stream_id": request["stream_id"],
                    "provider_result_id": "partial",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "A persisted partial candidate is not active evidence.",
                    "language": "en",
                    "is_final": True,
                }
            ]
        if cancelled:
            raise asyncio.CancelledError()
        raise AppError("synthetic_asr_failure", "Synthetic decoder-boundary failure.")

    w.native_gateway = decoder
    monkeypatch.setattr(w.inference, "transcribe", failing_recognizer)
    queued = await queue_candidate(w, monkeypatch, mid)
    with pytest.raises(asyncio.CancelledError if cancelled else AppError):
        await w.adispatch("job.retranscribe", queued["job"])
    assert active_run(w, mid)["id"] == original_run["id"]
    assert w.require_item("utterances", original["id"])["text"] == original["text"]
    assert not list((w.root / "staging").glob("*.f32"))
    pending = w.get("transcript_runs", queued["item"]["id"])
    if cancelled:
        assert pending is None
    else:
        assert pending is not None and pending["completed_at"] is None
        assert len(all_items(w, "utterances", {"run_id": pending["id"]})) == 1
        with pytest.raises(AppError) as incomplete:
            promote(w, pending)
        assert incomplete.value.code == "candidate_incomplete"


def test_untimed_range_boundary_is_preserved_instead_of_guessing_a_text_cut(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    original = utterance(
        w, mid, sid, 0, 4000, "A segment-only original crosses both range boundaries."
    )
    run = candidate(w, mid, sid, 1000, 3000)
    replacement = utterance(
        w, mid, sid, 1000, 3000, "A middle-only recognizer hypothesis.", run_id=run["id"]
    )
    run = complete(w, run)
    comparison = w.dispatch("transcript.diff", {"run_id": run["id"]})
    assert original["id"] in comparison["preserved_boundary_ids"]
    assert replacement["id"] in comparison["suppressed_candidate_ids"]
    result = promote(w, run)
    rows = all_items(w, "utterances", {"run_id": result["item"]["id"]})
    assert [(row["start_ms"], row["end_ms"], row["text"]) for row in rows] == [
        (0, 4000, original["text"])
    ]


async def test_cancel_during_native_decode_waits_for_writer_before_deleting_stage(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    original = utterance(
        w, mid, sid, 0, 1000, "Active text survives cancellation during native decode."
    )
    original_run = active_run(w, mid)
    media_segment(w, mid, sid, 0)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    writer_closed = False

    async def decoder(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal writer_closed
        if operation == "media.decode_cancel":
            cancelled.set()
            return {"cancelled": True}
        assert operation == "media.decode_existing"
        path = w.root / payload["output_relative_path"]
        path.parent.mkdir(exist_ok=True)
        with path.open("wb") as output:
            output.write(b"\x00" * 64000)
            started.set()
            await cancelled.wait()
        writer_closed = True
        raise AppError("cancelled", "Synthetic native decode cancellation.")

    w.native_gateway = decoder
    queued = await queue_candidate(w, monkeypatch, mid)
    processing = asyncio.create_task(w.adispatch("job.retranscribe", queued["job"]))
    await asyncio.wait_for(started.wait(), timeout=5)
    processing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(processing, timeout=5)
    assert writer_closed and cancelled.is_set()
    assert not list((w.root / "staging").glob("*.f32"))
    assert w.get("transcript_runs", queued["item"]["id"]) is None
    assert active_run(w, mid)["id"] == original_run["id"]
    assert w.require_item("utterances", original["id"])["text"] == original["text"]


def test_missing_new_speech_cannot_erase_manually_assigned_speaker_interval(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting, source = meeting_source(w)
    mid, sid = meeting["id"], source["id"]
    original = utterance(w, mid, sid, 0, 2000, "Manually identified speech must remain reviewable.")
    speaker = w.put(
        "meeting_speakers",
        new_id(),
        {"meeting_id": mid, "display_name": "Human-identified speaker"},
    )
    w.dispatch(
        "transcript.reassign",
        {
            "id": original["id"],
            "expected_revision": original["revision"],
            "speaker_id": speaker["id"],
        },
    )
    run = candidate(w, mid, sid, 0, 4000)
    utterance(w, mid, sid, 2000, 4000, "Other newly recognized speech.", run_id=run["id"])
    run = complete(w, run)
    result = promote(w, run, acknowledge_manual_conflicts=True)
    rows = all_items(w, "utterances", {"run_id": result["item"]["id"]})
    kept = next(row for row in rows if row["text"] == original["text"])
    assert kept["manual_speaker_lock"]
    assert {
        row["speaker_id"]
        for row in all_items(w, "speaker_assignments", {"utterance_id": kept["id"]})
    } == {speaker["id"]}


@pytest.mark.parametrize("operation", ["media.import", "media.retranscribe"])
async def test_explicit_missing_profile_is_not_silently_replaced_by_default(
    workspace: Workspace, operation: str
) -> None:
    w = workspace
    w.native_gateway = lambda native_operation, payload: {}
    payload: dict[str, Any] = {
        "idempotency_key": new_id(),
        "temporary_decode_consent": True,
        "file_grant": "synthetic-unused-grant",
        "profile_id": new_id(),
    }
    if operation == "media.retranscribe":
        meeting, source = meeting_source(w)
        original = utterance(
            w,
            meeting["id"],
            source["id"],
            0,
            1000,
            "A failed explicit profile leaves active text unchanged.",
        )
        payload["meeting_id"] = meeting["id"]
    meetings_before = all_items(w, "meetings")
    runs_before = all_items(w, "transcript_runs")
    jobs_before = all_items(w, "jobs")
    # Exercise the real stored-profile resolver/preflight, not a mock recognizer.
    with pytest.raises(AppError) as error:
        await w.adispatch(operation, payload)
    assert error.value.code == "model_missing"
    assert all_items(w, "meetings") == meetings_before
    assert all_items(w, "transcript_runs") == runs_before
    assert all_items(w, "jobs") == jobs_before
    if operation == "media.retranscribe":
        assert w.require_item("utterances", original["id"])["text"] == original["text"]
