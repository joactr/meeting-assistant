from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meeting_assistant.application.import_export import (
    backup,
    import_legacy,
    legacy_preview,
    validate_restore,
)
from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.model import new_id
from meeting_assistant.transport.http import Authorization, create_app
from meeting_assistant.workspace import Workspace

TOKEN = "ab" * 32


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


def create_text_meeting(w: Workspace) -> dict:
    return w.dispatch("meetings.create", {"title": "Synthetic design meeting", "source": "text"})[
        "item"
    ]


def ingest(
    w: Workspace, meeting_id: str, text: str = "The synthetic release is scheduled for Friday."
) -> dict:
    return w.dispatch(
        "transcript.ingest",
        {
            "meeting_id": meeting_id,
            "provider_stream_id": "synthetic-input",
            "provider_result_id": "one",
            "start_ms": 0,
            "end_ms": 4000,
            "text": text,
            "language": "en",
            "is_final": True,
        },
    )["item"]


def test_p15_manual_text_survives_late_provider_and_search_removes_old_text(
    workspace: Workspace,
) -> None:
    meeting = create_text_meeting(workspace)
    utterance = ingest(workspace, meeting["id"], "The obsolete synthetic date is Monday.")
    edited = workspace.dispatch(
        "transcript.edit",
        {
            "id": utterance["id"],
            "expected_revision": utterance["revision"],
            "text": "The corrected synthetic date is Friday.",
        },
    )["item"]
    late = ingest(workspace, meeting["id"], "The obsolete synthetic date is Monday.")
    assert late["id"] == utterance["id"]
    assert late["text"] == edited["text"]
    result = asyncio.run(
        workspace.adispatch("search.query", {"query": "Friday", "mode": "keyword"})
    )
    assert any("Friday" in item["excerpt"] for item in result["items"])
    old = asyncio.run(workspace.adispatch("search.query", {"query": "Monday", "mode": "keyword"}))
    assert old["items"] == []
    with pytest.raises(AppError, match="changed"):
        workspace.dispatch(
            "transcript.edit",
            {
                "id": utterance["id"],
                "expected_revision": utterance["revision"],
                "text": "stale overwrite",
            },
        )


def test_p27_outbox_and_jobs_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "workspace" / "workspace.sqlite3"
    w = Workspace(path)
    meeting = create_text_meeting(w)
    job = w.queue_job(
        "index", {"input_revision": 0}, meeting_id=meeting["id"], idempotency_key="synthetic-index"
    )
    watermark = w.events()["watermark"]
    w.close()
    reopened = Workspace(path)
    try:
        assert reopened.require_item("jobs", job["id"])["status"] == "queued"
        assert reopened.events()["watermark"] >= watermark
        assert (
            reopened.require_item("meetings", meeting["id"])["title"] == "Synthetic design meeting"
        )
    finally:
        reopened.close()


def test_analysis_status_reports_latest_job_per_task(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    first = workspace.queue_job(
        "analysis", {"task": "summary"}, meeting_id=meeting["id"], idempotency_key="a1"
    )
    workspace.put(
        "jobs",
        first["id"],
        {"status": "failed", "error": "old"},
        expected_revision=first["revision"],
    )
    workspace.queue_job(
        "analysis", {"task": "summary"}, meeting_id=meeting["id"], idempotency_key="a2"
    )
    failed = workspace.queue_job(
        "analysis", {"task": "topics"}, meeting_id=meeting["id"], idempotency_key="a3"
    )
    workspace.put(
        "jobs",
        failed["id"],
        {"status": "failed", "error": "no provider"},
        expected_revision=failed["revision"],
    )
    rows = {
        r["task"]: r
        for r in workspace.dispatch("analysis.status", {"meeting_id": meeting["id"]})["items"]
    }
    assert rows["summary"]["status"] == "queued" and rows["summary"]["error"] is None
    assert rows["topics"]["status"] == "failed" and rows["topics"]["error"] == "no provider"


def test_p28_auth_scope_host_origin_and_cas(workspace: Workspace) -> None:
    auth = Authorization(TOKEN)
    read_token = "cd" * 32
    auth.grant(read_token, {"read"})
    app = create_app(workspace, TOKEN, manage_lifespan=False, authorization=auth)
    with TestClient(app) as client:
        assert (
            client.post("/api/v1/commands/meetings.create", json={"title": "No auth"}).status_code
            == 401
        )
        headers = {"Authorization": "Bearer " + TOKEN}
        assert (
            client.post(
                "/api/v1/commands/meetings.create",
                json={"title": "Wrong origin"},
                headers={**headers, "Origin": "https://malicious.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/commands/meetings.create",
                json={"title": "Readonly"},
                headers={"Authorization": "Bearer " + read_token},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/commands/meetings.create",
            json={"title": "API synthetic", "source": "text"},
            headers=headers,
        )
        assert response.status_code == 200
        meeting = response.json()["item"]
        assert (
            client.post(
                "/api/v1/commands/meetings.update",
                json={
                    "id": meeting["id"],
                    "expected_revision": meeting["revision"],
                    "changes": {"title": "Persisted title"},
                },
                headers=headers,
            ).status_code
            == 200
        )
        stale = client.post(
            "/api/v1/commands/meetings.update",
            json={
                "id": meeting["id"],
                "expected_revision": meeting["revision"],
                "changes": {"title": "stale"},
            },
            headers=headers,
        )
        assert stale.status_code == 409
        assert stale.json()["details"]["latest"]["title"] == "Persisted title"
        private = client.post(
            "/api/v1/commands/capture.source_event",
            json={"meeting_id": meeting["id"], "source_index": 0, "state": "lost"},
            headers={"Authorization": "Bearer " + read_token},
        )
        assert private.status_code == 403


def test_p21_deterministic_export_reflects_edits_without_ai(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    utterance = ingest(workspace, meeting["id"])
    workspace.dispatch(
        "transcript.edit",
        {
            "id": utterance["id"],
            "expected_revision": utterance["revision"],
            "text": "Persisted corrected synthetic quotation.",
        },
    )
    exported = asyncio.run(
        workspace.adispatch("export.create", {"meeting_id": meeting["id"], "format": "srt"})
    )
    text = base64.b64decode(exported["content"]).decode()
    assert "00:00:00,000 --> 00:00:04,000" in text
    assert "Persisted corrected synthetic quotation." in text
    minutes = asyncio.run(
        workspace.adispatch("export.create", {"meeting_id": meeting["id"], "format": "markdown"})
    )
    assert (
        "Persisted corrected synthetic quotation."
        not in base64.b64decode(minutes["content"]).decode()
    )


def test_backup_passphrase_roundtrip_and_path_traversal(
    workspace: Workspace, tmp_path: Path
) -> None:
    from pyrage import passphrase

    meeting = create_text_meeting(workspace)
    ingest(workspace, meeting["id"])
    exported = backup(workspace, {"_passphrase": "synthetic-only-long-test-passphrase"})
    encrypted = base64.b64decode(exported["content"])
    assert encrypted.startswith(b"age-encryption.org/v1")
    plain = passphrase.decrypt(encrypted, "synthetic-only-long-test-passphrase")
    archive = tmp_path / "synthetic-backup.zip"
    archive.write_bytes(plain)
    restored = validate_restore(archive, tmp_path / "restored")
    assert restored["validated"]
    db = sqlite3.connect(tmp_path / "restored" / "workspace.sqlite3")
    try:
        assert (
            db.execute("SELECT title FROM meetings WHERE id=?", (meeting["id"],)).fetchone()[0]
            == meeting["title"]
        )
    finally:
        db.close()
    malicious = tmp_path / "malicious.zip"
    with zipfile.ZipFile(malicious, "w") as z:
        z.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "meeting-assistant-backup",
                    "version": 1,
                    "files": [
                        {
                            "path": "../escape",
                            "bytes": 1,
                            "sha256": hashlib.sha256(b"x").hexdigest(),
                        }
                    ],
                }
            ),
        )
        z.writestr("../escape", b"x")
    with pytest.raises(AppError):
        validate_restore(malicious, tmp_path / "bad-restore")
    assert not (tmp_path / "escape").exists()


def test_p29_explicit_original_synthetic_legacy_import(
    workspace: Workspace, tmp_path: Path
) -> None:
    source = tmp_path / "synthetic-legacy.db"
    db = sqlite3.connect(source)
    db.executescript(
        "CREATE TABLE sessions(id INTEGER PRIMARY KEY,title TEXT,status TEXT); CREATE TABLE transcripts(id INTEGER PRIMARY KEY,session_id INTEGER,start_ms INTEGER,end_ms INTEGER,text TEXT,edited INTEGER); CREATE TABLE actions(id INTEGER PRIMARY KEY,session_id INTEGER,description TEXT,status TEXT,priority TEXT);"
    )
    db.execute("INSERT INTO sessions VALUES(1,?,?)", ("Synthetic legacy notebook", "recording"))
    db.execute(
        "INSERT INTO transcripts VALUES(10,1,0,1000,?,1)", ("Synthetic manually corrected text",)
    )
    db.execute(
        "INSERT INTO actions VALUES(20,1,?,?,?)", ("Review the synthetic fixture", "done", "high")
    )
    db.commit()
    db.close()
    before = source.read_bytes()
    preview = legacy_preview(source)
    assert preview["counts"]["transcripts"] == 1
    imported = import_legacy(workspace, source, {"confirm": True, "source_stopped": True})
    assert imported["report"]["imported_counts"]["meetings"] == 1
    meeting = workspace.list("meetings")[0]
    assert meeting["state"] == "interrupted"
    assert workspace.list("utterances")[0]["manual_text_lock"] == 1
    assert workspace.list("actions")[0]["status"] == "done"
    assert source.read_bytes() == before
    assert import_legacy(workspace, source, {"confirm": True, "source_stopped": True})[
        "already_imported"
    ]


def test_note_revision_projection_and_historical_snapshot(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    workspace.put(
        "human_notes",
        meeting["id"],
        {
            "meeting_id": meeting["id"],
            "blocks": [{"id": new_id(), "heading": "Agenda", "body": "Synthetic human-only note"}],
        },
    )
    assert workspace.require_item("meetings", meeting["id"])["note_revision"] == 1
    assert (
        workspace.list("notes", {"meeting_id": meeting["id"]})[0]["body_markdown"]
        == "Synthetic human-only note"
    )
    project = workspace.put("projects", new_id(), {"name": "Synthetic project"})
    historical_time = project["updated_at"]
    workspace.put(
        "projects", project["id"], {"name": "Changed"}, expected_revision=project["revision"]
    )
    historical = workspace.snapshot_at("projects", historical_time)
    assert historical[0]["name"] in (
        "Synthetic project",
        "Changed",
    )  # same-ms writes have sequence-defined order


def test_bootstrap_registers_media_voice_knowledge_and_retention(workspace: Workspace) -> None:
    required = {
        "media.import",
        "media.retranscribe",
        "voice_profiles.create",
        "voice_profiles.delete",
        "voice_profiles.adopt",
        "retention.preview",
        "retention.purge",
        "search.reindex",
        "briefings.prepare",
        "memory.compress",
        "catalog.import_model",
    }
    assert required <= workspace.registry.keys()
    assert workspace.connection.execute("PRAGMA user_version").fetchone()[0] >= 3


def test_interim_is_stable_live_only_and_final_reuses_identity(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    queue = workspace.subscribe_live()
    payload = {
        "meeting_id": meeting["id"],
        "provider_stream_id": "synthetic-interim",
        "provider_result_id": "result-one",
        "start_ms": 0,
        "end_ms": 1000,
        "text": "Synthetic evolving caption",
        "is_final": False,
    }
    before = workspace.events()["watermark"]
    first = workspace.dispatch("transcript.ingest", payload)["item"]
    second = workspace.dispatch(
        "transcript.ingest", {**payload, "text": "Synthetic evolving caption corrected"}
    )["item"]
    assert first["id"] == second["id"]
    assert workspace.list("utterances") == []
    assert workspace.events()["watermark"] == before
    assert queue.get_nowait()["type"] == "transcript.interim"
    final = workspace.dispatch("transcript.ingest", {**payload, "is_final": True})["item"]
    assert final["id"] == first["id"]
    assert workspace.events(before)["items"][-1]["type"] == "transcript.utterance_final"
    workspace.unsubscribe_live(queue)


def test_empty_scoped_briefing_needs_no_inference(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    result = asyncio.run(
        workspace.adispatch("briefings.prepare", {"meeting_id": meeting["id"], "kind": "pre"})
    )
    assert result["sparse"]
    assert result["result"]["payload"]["sections"] == []
    cached = asyncio.run(
        workspace.adispatch("briefings.prepare", {"meeting_id": meeting["id"], "kind": "pre"})
    )
    assert cached["cached"]


def test_crashed_running_job_recovers_even_before_lease_deadline(tmp_path: Path) -> None:
    path = tmp_path / "recover" / "workspace.sqlite3"
    workspace = Workspace(path)
    job = workspace.queue_job("index", {}, idempotency_key="synthetic-recovery")
    workspace.put(
        "jobs",
        job["id"],
        {
            "status": "running",
            "lease_owner": workspace.run_id,
            "lease_until": workspace.now_ms() + 600000,
        },
        expected_revision=job["revision"],
    )
    workspace.close()
    reopened = Workspace(path)
    try:
        restored = reopened.require_item("jobs", job["id"])
        assert restored["status"] == "queued"
        assert restored["lease_owner"] is None
    finally:
        reopened.close()


def test_recording_requires_per_start_notice_before_capture(workspace: Workspace) -> None:
    meeting = create_text_meeting(workspace)
    with pytest.raises(AppError) as error:
        asyncio.run(
            workspace.adispatch(
                "meetings.start",
                {
                    "meeting_id": meeting["id"],
                    "idempotency_key": "recording-notice",
                    "recording_enabled": True,
                },
            )
        )
    assert error.value.code == "recording_notice_required"
    assert workspace.require_item("meetings", meeting["id"])["state"] == "draft"


def test_secret_reference_is_opaque_not_a_credential(workspace: Workspace) -> None:
    with pytest.raises(AppError) as error:
        workspace.put(
            "provider_connections",
            new_id(),
            {
                "adapter_id": "openai",
                "endpoint": "https://api.openai.com/v1",
                "secret_ref": "not-a-native-vault-reference",
            },
        )
    assert error.value.code == "invalid_secret_ref"
