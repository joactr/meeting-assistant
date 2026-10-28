from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from meeting_assistant.application.import_export import backup
from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.model import canonical, new_id
from meeting_assistant.workspace import Workspace


@pytest.fixture
def workspace(tmp_path: Path):
    workspace = Workspace(tmp_path / "workspace.sqlite3")
    try:
        yield workspace
    finally:
        workspace.close()


def meeting(workspace: Workspace, title: str) -> dict:
    return workspace.dispatch("meetings.create", {"title": title, "source": "text"})["item"]


def analysis_run(workspace: Workspace, meeting_id: str) -> dict:
    return workspace.put(
        "analysis_runs",
        new_id(),
        {
            "meeting_id": meeting_id,
            "task": "summary",
            "input_revision": 0,
            "watermark": 0,
            "policy_hash": "synthetic-export-policy",
            "status": "succeeded",
        },
    )


def result(workspace: Workspace, run_id: str, phrase: str, state: str = "promoted") -> dict:
    payload = {"text": phrase}
    return workspace.put(
        "analysis_results",
        new_id(),
        {
            "run_id": run_id,
            "kind": "summary",
            "payload": payload,
            "content_hash": hashlib.sha256(canonical(payload).encode()).hexdigest(),
            "state": state,
        },
    )


@pytest.mark.asyncio
async def test_export_includes_complete_meeting_library_without_foreign_results(workspace):
    foreign = meeting(workspace, "Other private meeting")
    selected = meeting(workspace, "Selected synthetic meeting")
    expected: dict[str, set[str]] = {
        key: set() for key in ("speakers", "important_moments", "notes", "artifacts", "results")
    }
    with workspace.transaction():
        foreign_run = analysis_run(workspace, foreign["id"])
        for index in range(205):
            result(workspace, foreign_run["id"], f"Foreign confidential summary {index}")
        for index in range(205):
            rows = (
                ("speakers", "meeting_speakers", {"display_name": f"Attendee {index}"}),
                (
                    "important_moments",
                    "important_moments",
                    {"label": f"Moment {index}", "quote_snapshot": f"Reviewed quote {index}"},
                ),
                (
                    "notes",
                    "notes",
                    {"heading": f"Note {index}", "body_markdown": f"Human note {index}"},
                ),
                ("artifacts", "artifacts", {"kind": "decision", "title": f"Decision {index}"}),
            )
            for key, collection, fields in rows:
                row = workspace.put(collection, new_id(), {"meeting_id": selected["id"], **fields})
                expected[key].add(row["id"])
            run = analysis_run(workspace, selected["id"])
            row = result(workspace, run["id"], f"Selected summary {index}")
            expected["results"].add(row["id"])
        for index in range(205):
            row = result(workspace, run["id"], f"Selected final run summary {index}")
            expected["results"].add(row["id"])
        result(workspace, run["id"], "Unreviewed candidate", state="candidate")
    exported = await workspace.adispatch(
        "export.create", {"meeting_id": selected["id"], "format": "json"}
    )
    data = json.loads(base64.b64decode(exported["content"]))
    for key, ids in expected.items():
        assert {row["id"] for row in data[key]} == ids
        assert len(data[key]) == len(ids)
    assert "transcript" not in data
    minutes = await workspace.adispatch(
        "export.create", {"meeting_id": selected["id"], "format": "markdown"}
    )
    for phrase in (
        "Attendee 204",
        "Moment 204",
        "Human note 204",
        "Decision 204",
        "Selected summary 204",
        "Selected final run summary 204",
    ):
        assert phrase in minutes["text"]
    assert "Foreign confidential" not in minutes["text"]
    assert "Unreviewed candidate" not in minutes["text"]


@pytest.mark.asyncio
async def test_approved_minutes_keep_current_reviewed_outcomes_and_email_privacy(workspace):
    selected = meeting(workspace, "Reviewed synthetic meeting")
    saved = workspace.dispatch(
        "notes.save",
        {
            "meeting_id": selected["id"],
            "expected_revision": 0,
            "blocks": [{"id": "summary", "heading": "Summary", "body": "Approved human summary"}],
        },
    )["item"]
    await workspace.adispatch(
        "minutes.prepare", {"meeting_id": selected["id"], "note_revision": saved["revision"]}
    )
    workspace.put(
        "actions",
        new_id(),
        {
            "meeting_id": selected["id"],
            "stable_origin_key": "reviewed-action",
            "description": "Current reviewed action",
            "status": "done",
        },
    )
    workspace.put(
        "important_moments",
        new_id(),
        {
            "meeting_id": selected["id"],
            "label": "Current reviewed moment",
            "quote_snapshot": "Exact reviewed quote",
            "note": "Moment provenance",
        },
    )
    workspace.put(
        "important_moments",
        new_id(),
        {
            "meeting_id": selected["id"],
            "label": "Excluded private moment",
            "include_in_minutes": 0,
        },
    )
    workspace.put(
        "artifacts",
        new_id(),
        {
            "meeting_id": selected["id"],
            "kind": "decision",
            "title": "Current decision",
        },
    )
    workspace.put(
        "notes",
        new_id(),
        {
            "meeting_id": selected["id"],
            "heading": "Private scratch note",
            "body_markdown": "Unapproved private notebook text",
        },
    )
    workspace.dispatch(
        "transcript.ingest",
        {
            "meeting_id": selected["id"],
            "provider_stream_id": "synthetic-export",
            "provider_result_id": "one",
            "start_ms": 0,
            "end_ms": 1000,
            "text": "Private full transcript",
            "is_final": True,
        },
    )
    for fmt in ("markdown", "email"):
        exported = await workspace.adispatch(
            "export.create",
            {
                "meeting_id": selected["id"],
                "format": fmt,
                "include_transcript": fmt == "email",
            },
        )
        body = exported["text"]
        assert body == base64.b64decode(exported["content"]).decode()
        for phrase in (
            "Approved human summary",
            "Source: Human note",
            "Current reviewed action",
            "Current reviewed moment",
            "Exact reviewed quote",
            "Moment provenance",
        ):
            assert phrase in body
        assert "Excluded private moment" not in body
        assert "Private full transcript" not in body
        if fmt == "email":
            assert "Unapproved private notebook text" not in body
            assert "Current decision" not in body
        else:
            assert "Unapproved private notebook text" in body
            assert "Current decision" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("plain_warning_accepted", [False, True])
async def test_secret_ref_backup_without_destination_never_downgrades_to_plaintext(
    workspace, direct, plain_warning_accepted
):
    meeting(workspace, "Private synthetic notebook")
    payload = {
        "passphrase_secret_ref": "vault:synthetic-backup-passphrase",
        "plain_warning_accepted": plain_warning_accepted,
    }
    with pytest.raises(AppError) as error:
        if direct:
            backup(workspace, payload)
        else:
            await workspace.adispatch("backup.create", payload)
    assert error.value.code == "file_grant_required"


def test_native_media_root_commit_is_portable_and_integrity_checked(workspace):
    selected = workspace.dispatch(
        "meetings.create",
        {"title": "Retained-byte fixture", "source": "system", "config": {"recording": True}},
    )["item"]
    media_id = new_id()
    filename = f"{media_id}.opus"
    body = b"synthetic retained-source bytes for catalog and backup integrity"
    media = workspace.root / "media" / filename
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(body)
    payload = {
        "media_id": media_id,
        "meeting_id": selected["id"],
        "relative_path": filename,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "frame_count": 48000,
        "sample_rate": 48000,
    }
    workspace.dispatch("media.commit", payload)
    archive = backup(workspace, {"plain_warning_accepted": True, "include_media": True})
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(archive["content"]))) as saved:
        assert saved.read(f"media/{filename}") == body
    media.write_bytes(b"corrupted source")
    with pytest.raises(AppError) as damaged:
        workspace.dispatch("media.commit", payload)
    assert damaged.value.code == "media_integrity"
    outside = workspace.root / "outside.opus"
    outside.write_bytes(body)
    with pytest.raises(AppError) as escaped:
        workspace.dispatch("media.commit", {**payload, "relative_path": "../outside.opus"})
    assert escaped.value.code == "unsafe_media_path"
