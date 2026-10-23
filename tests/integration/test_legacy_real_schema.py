from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from meeting_assistant.application.import_export import import_legacy, legacy_preview
from meeting_assistant.workspace import Workspace

# Table definitions as written by the 0.1.x application (schema_version 21); data is synthetic.
LEGACY_DDL = """
CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, started_at_ms INTEGER, ended_at_ms INTEGER,
  config_snapshot TEXT, category_id TEXT, project_id TEXT, subcategory_id TEXT, template TEXT);
CREATE TABLE transcript_segments(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, text TEXT,
  speaker TEXT, speaker_label TEXT, start_ms INTEGER, end_ms INTEGER, confidence REAL, is_final INTEGER,
  word_timestamps TEXT, created_at_ms INTEGER, speaker_confidence REAL);
CREATE TABLE action_items(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, description TEXT,
  assignee TEXT, deadline TEXT, status TEXT, source_segment_id INTEGER, created_at_ms INTEGER,
  priority TEXT, dedupe_key TEXT, owner_kind TEXT, input_watermark INTEGER, analysis_kind TEXT);
CREATE TABLE summaries(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, summary_type TEXT,
  content TEXT, timestamp_ms INTEGER, dedupe_key TEXT, owner_kind TEXT, input_watermark INTEGER, analysis_kind TEXT);
CREATE TABLE speakers(id TEXT PRIMARY KEY, session_id TEXT, name TEXT, color TEXT);
CREATE TABLE topics(id TEXT PRIMARY KEY, session_id TEXT, title TEXT, start_ms INTEGER, end_ms INTEGER,
  subtopics_json TEXT, sentiment TEXT, created_at TEXT, dedupe_key TEXT, owner_kind TEXT,
  input_watermark INTEGER, analysis_kind TEXT);
CREATE TABLE questions(id TEXT PRIMARY KEY, session_id TEXT, question_text TEXT, speaker_id TEXT,
  start_ms INTEGER, is_answered INTEGER, created_at TEXT);
CREATE TABLE bookmarks(id TEXT PRIMARY KEY, session_id TEXT, start_ms INTEGER, label TEXT, created_at TEXT,
  segment_id INTEGER, quote TEXT, note TEXT, include_in_minutes INTEGER, status TEXT, assignee TEXT,
  due_date TEXT, priority TEXT);
CREATE TABLE speaker_profiles(id TEXT PRIMARY KEY, name TEXT, embedding_blob BLOB, cosine_threshold REAL,
  created_at TEXT, last_seen_at TEXT);
"""


def build_source(path: Path) -> None:
    db = sqlite3.connect(path)
    db.executescript(LEGACY_DDL)
    for sid, title in (("s-1", "Synthetic Ünïcode ✓ planning"), ("s-2", "Synthetic retro 日本語")):
        db.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?)",
            (sid, title, 1_700_000_000_000, 1_700_000_900_000, "{}", None, None, None, "general"),
        )
    words = json.dumps([{"word": "hello", "start": 0.0, "end": 0.4, "probability": 0.9}])
    db.execute(
        "INSERT INTO transcript_segments VALUES(1,'s-1','Hello from the synthetic fixture','ME','ME',0,1500,0.9,1,?,1,NULL)",
        (words,),
    )
    db.execute(
        "INSERT INTO transcript_segments VALUES(2,'s-1','Provisional tail','System','System',1500,2500,0.5,0,NULL,2,NULL)"
    )
    db.execute(
        "INSERT INTO transcript_segments VALUES(3,'s-2','Segunda reunión sintética','System','System',0,900,0.8,1,NULL,3,0.7)"
    )
    db.execute(
        "INSERT INTO action_items VALUES(1,'s-1','Review the synthetic fixture','alice','Friday','open',1,1,'high','k1','human',1,'final')"
    )
    db.execute(
        "INSERT INTO summaries VALUES(1,'s-1','final','Synthetic final summary',1,'k2','model',1,'final')"
    )
    db.execute("INSERT INTO speakers VALUES('sp-1','s-1','Alice','#ff0000')")
    db.execute(
        "INSERT INTO topics VALUES('t-1','s-1','Release plan',0,1500,'[\"dates\"]','neutral','2023-11-14','k3','model',1,'final')"
    )
    db.execute(
        "INSERT INTO questions VALUES('q-1','s-1','Who owns the fixture?','sp-1',100,0,'2023-11-14')"
    )
    db.execute(
        "INSERT INTO bookmarks VALUES('b-1','s-1',200,'Decision',NULL,1,'Hello from','Keep this',1,'open','alice','2030-01-01','normal')"
    )
    db.execute(
        "INSERT INTO speaker_profiles VALUES('p-1','Alice',?,0.6,'2023-11-14','2023-11-14')",
        (b"\x00\x01\x02\x03",),
    )
    db.commit()
    db.close()


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


def test_p29_legacy_schema_import_maps_every_table_and_never_writes_source(
    workspace: Workspace, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.db"
    build_source(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    preview = legacy_preview(source)
    assert preview["counts"]["sessions"] == 2
    assert preview["counts"]["transcript_segments"] == 3
    assert preview["secrets_imported"] is False

    result = import_legacy(workspace, source, {"confirm": True, "source_stopped": True})
    report = result["report"]
    assert report["rejected"] == []
    counts = report["imported_counts"]
    assert counts["meetings"] == 2
    assert counts["utterances"] == 3
    assert counts["actions"] == 1
    assert counts["important_moments"] == 1
    assert counts["questions"] == 1
    assert counts["voice_profiles"] == 1
    provisional = [u for u in workspace.list("utterances") if u["text"] == "Provisional tail"]
    assert provisional and provisional[0]["is_final"] == 0
    assert any("biometric" in w for w in report["warnings"])
    assert workspace.list("actions")[0]["description"] == "Review the synthetic fixture"
    texts = {u["text"] for u in workspace.list("utterances")}
    assert "Segunda reunión sintética" in texts
    titles = {m["title"] for m in workspace.list("meetings")}
    assert "Synthetic Ünïcode ✓ planning" in titles
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    assert not Path(str(source) + "-wal").exists()
    assert import_legacy(workspace, source, {"confirm": True, "source_stopped": True})[
        "already_imported"
    ]
