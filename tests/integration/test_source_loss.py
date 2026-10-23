from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
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
