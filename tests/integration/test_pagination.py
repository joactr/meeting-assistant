from pathlib import Path

import pytest
from meeting_assistant.domain.errors import AppError
from meeting_assistant.workspace import Workspace


def test_library_pages_and_filters_reach_meetings_beyond_storage_batch(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace.sqlite3")
    try:
        with workspace.transaction():
            created = [
                workspace.dispatch(
                    "meetings.create", {"title": f"Synthetic meeting {index}", "source": "text"}
                )["item"]
                for index in range(201)
            ]
            workspace.put(
                "meetings",
                created[-1]["id"],
                {"state": "interrupted"},
                expected_revision=created[-1]["revision"],
            )
        collected = []
        cursor = None
        while True:
            page = workspace.dispatch(
                "meetings.list", {"limit": 73, **({"cursor": cursor} if cursor else {})}
            )
            collected.extend(row["id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert set(collected) == {row["id"] for row in created}
        assert len(collected) == len(set(collected))
        filtered = workspace.dispatch("meetings.list", {"state": "interrupted"})
        assert [row["id"] for row in filtered["items"]] == [created[-1]["id"]]
    finally:
        workspace.close()


def test_transcript_pages_preserve_equal_timestamp_rows_and_cursor_scope(tmp_path: Path) -> None:
    workspace = Workspace(tmp_path / "workspace.sqlite3")
    try:
        meeting = workspace.dispatch(
            "meetings.create", {"title": "Synthetic paged transcript", "source": "text"}
        )["item"]
        with workspace.transaction():
            expected = [
                workspace.dispatch(
                    "transcript.ingest",
                    {
                        "meeting_id": meeting["id"],
                        "provider_stream_id": "synthetic-pagination",
                        "provider_result_id": str(index),
                        "start_ms": (index // 3) * 1000,
                        "end_ms": (index // 3) * 1000 + 900,
                        "text": f"Synthetic boundary phrase {index}",
                        "is_final": True,
                    },
                )["item"]
                for index in range(205)
            ]
        payload = {"meeting_id": meeting["id"], "limit": 73}
        first = workspace.dispatch("transcript.list", payload)
        collected = first["items"]
        cursor = first["next_cursor"]
        while cursor:
            page = workspace.dispatch("transcript.list", {**payload, "cursor": cursor})
            collected.extend(page["items"])
            cursor = page["next_cursor"]
        assert [row["id"] for row in collected] == [
            row["id"] for row in sorted(expected, key=lambda row: (row["start_ms"], row["id"]))
        ]
        with pytest.raises(AppError) as error:
            workspace.dispatch(
                "transcript.list",
                {**payload, "cursor": first["next_cursor"], "include_deleted": True},
            )
        assert error.value.code == "cursor_filter_conflict"
    finally:
        workspace.close()
