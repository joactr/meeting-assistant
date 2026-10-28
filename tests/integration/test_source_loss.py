from __future__ import annotations

import struct
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from meeting_assistant.application.capture import AudioPipeline, Packet
from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.model import new_id
from meeting_assistant.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


def recording_meeting(w: Workspace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    meeting = w.dispatch(
        "meetings.create", {"title": "Synthetic two-source meeting", "source": "text"}
    )["item"]
    meeting = w.put(
        "meetings",
        meeting["id"],
        {"state": "recording", "capture_epoch": 1, "started_at": 1_000},
        expected_revision=meeting["revision"],
    )
    sources = [
        w.put(
            "capture_sources",
            new_id(),
            {
                "meeting_id": meeting["id"],
                "source_kind": kind,
                "source_index": index,
                "device_key": kind,
                "state": "active",
            },
        )
        for index, kind in enumerate(("microphone", "system"))
    ]
    return meeting, sources[0], sources[1]


def ingest(w: Workspace, meeting_id: str, source_id: str, text: str, start: int) -> None:
    w.dispatch(
        "transcript.ingest",
        {
            "meeting_id": meeting_id,
            "source_id": source_id,
            "provider_stream_id": "synthetic-input",
            "provider_result_id": f"r{start}",
            "start_ms": start,
            "end_ms": start + 500,
            "text": text,
            "language": "en",
            "is_final": True,
        },
    )


async def test_p02_lost_system_source_degrades_but_never_reports_complete_capture(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting, mic, system = recording_meeting(w)
    ingest(w, meeting["id"], system["id"], "System speech before loss.", 0)

    w.dispatch(
        "capture.source_event",
        {"meeting_id": meeting["id"], "source_index": 1, "state": "lost", "start_ms": 500},
    )
    after_loss = w.require_item("meetings", meeting["id"])
    assert after_loss["state"] == "recording"  # still capturing the microphone
    assert after_loss["health"] == "degraded"
    gaps = w.list("timeline_gaps", {"meeting_id": meeting["id"]})
    assert [(g["source_id"], g["start_ms"], g["reason"]) for g in gaps] == [
        (system["id"], 500, "source_lost")
    ]
    assert w.require_item("capture_sources", system["id"])["state"] == "lost"
    assert w.require_item("capture_sources", mic["id"])["state"] == "active"

    # The surviving source keeps producing transcript while the system source is absent.
    ingest(w, meeting["id"], mic["id"], "Microphone speech after loss.", 1000)
    texts = {u["text"] for u in w.list("utterances", {"meeting_id": meeting["id"]})}
    assert {"System speech before loss.", "Microphone speech after loss."} <= texts

    # A retry that succeeds restores the source but the recorded gap and degraded health persist.
    w.dispatch(
        "capture.source_event", {"meeting_id": meeting["id"], "source_index": 1, "state": "active"}
    )
    assert w.require_item("capture_sources", system["id"])["state"] == "active"
    assert w.require_item("meetings", meeting["id"])["health"] == "degraded"
    assert len(w.list("timeline_gaps", {"meeting_id": meeting["id"]})) == 1


async def test_p02_stop_after_loss_preserves_gap_and_transcript(workspace: Workspace) -> None:
    w = workspace
    meeting, mic, system = recording_meeting(w)
    ingest(w, meeting["id"], mic["id"], "Preserved before stop.", 0)
    w.dispatch(
        "capture.source_event", {"meeting_id": meeting["id"], "source_index": 1, "state": "lost"}
    )

    async def native(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        assert operation == "native.capture.stop"
        return {"drained": True, "acknowledged": True, "boundary_sequences": {}, "boundaries": []}

    w.native_gateway = native
    stopped = await w.adispatch(
        "meetings.stop", {"meeting_id": meeting["id"], "idempotency_key": new_id()}
    )
    item = stopped["item"]
    assert item["state"] == "processing"
    assert item["health"] == "degraded"
    assert len(w.list("timeline_gaps", {"meeting_id": meeting["id"]})) == 1
    assert [u["text"] for u in w.list("utterances", {"meeting_id": meeting["id"]})] == [
        "Preserved before stop."
    ]


async def test_source_retry_restarts_capture_with_a_new_epoch(workspace: Workspace) -> None:
    w = workspace
    meeting, _, system = recording_meeting(w)
    w.dispatch(
        "capture.source_event", {"meeting_id": meeting["id"], "source_index": 1, "state": "lost"}
    )
    calls: list[tuple[str, Any]] = []

    async def native(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((operation, payload.get("capture_epoch")))
        state = "paused" if operation == "native.capture.pause" else "active"
        return {"state": state, "boundary_sequences": {}, "boundaries": []}

    w.native_gateway = native
    retried = await w.adispatch("meetings.source_retry", {"source_id": system["id"]})
    assert [operation for operation, _ in calls] == [
        "native.capture.pause",
        "native.capture.resume",
    ]
    assert calls[1][1] > calls[0][1]
    assert retried["item"]["state"] == "active"
    assert retried["item"]["epoch"] == calls[1][1]
    assert w.require_item("meetings", meeting["id"])["state"] == "recording"


async def test_dropped_utterances_become_gaps_and_flag_a_stall_once(workspace: Workspace) -> None:
    w = workspace
    meeting, _, system = recording_meeting(w)
    w.put("capture_sources", system["id"], {"epoch": 1}, expected_revision=system["revision"])
    pipeline = AudioPipeline(w, "ab" * 32)
    pcm = struct.pack("<960f", *([0.1] * 960))
    pipeline.buffers[(1, 1)] = deque(
        Packet(sequence, 1, 1, 48000, 960, sequence * 20_000_000, pcm) for sequence in range(50)
    )
    for attempt in range(4):
        start = 5_000 + attempt * 1_000  # beyond the one second of buffered audio
        with pytest.raises(AppError) as error:
            await pipeline.utterance(
                {"capture_epoch": 1, "source_index": 1, "start_ms": start, "end_ms": start + 500}
            )
        assert error.value.code == "audio_range_missing"
    gaps = w.list("timeline_gaps", {"meeting_id": meeting["id"]})
    assert sorted(g["start_ms"] for g in gaps if g["reason"] == "audio_range_missing") == [
        5_000,
        6_000,
        7_000,
        8_000,
    ]
    assert w.require_item("meetings", meeting["id"])["health"] == "degraded"
    stalls = [
        row
        for row in w.connection.execute(
            "SELECT payload_json FROM outbox_events WHERE event_type='capture.attention'"
        )
        if "asr_stalled" in row[0]
    ]
    assert len(stalls) == 1

    # Audio that is in the window is accepted again and resets the stall count.
    accepted = await pipeline.utterance(
        {"capture_epoch": 1, "source_index": 1, "start_ms": 100, "end_ms": 600}
    )
    assert accepted["accepted"] is True
    assert (1, 1) not in pipeline.misses
    await pipeline.close()
