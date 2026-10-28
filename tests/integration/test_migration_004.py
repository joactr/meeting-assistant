from __future__ import annotations

import sqlite3
from pathlib import Path

from meeting_assistant.workspace import Workspace

MIGRATIONS = Path(__file__).resolve().parents[2] / "engine" / "migrations"


def test_drop_templates_keeps_meetings_and_projects(tmp_path: Path) -> None:
    path = tmp_path / "workspace.sqlite"
    db = sqlite3.connect(path)
    for name in ("001_initial.sql", "002_history.sql", "003_speaker_scope.sql"):
        db.executescript((MIGRATIONS / name).read_text(encoding="utf-8"))
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("INSERT INTO templates(id,created_at,updated_at) VALUES('general',1,1)")
    db.execute(
        "INSERT INTO template_versions(id,template_id,version,content_hash,created_at,updated_at)"
        " VALUES('general:v1','general',1,'h',1,1)"
    )
    db.execute(
        "INSERT INTO projects(id,name,default_template_id,created_at,updated_at)"
        " VALUES('p1','Project','general',1,1)"
    )
    db.execute(
        "INSERT INTO meetings(id,title,state,project_id,template_version_id,extensions_json,"
        "created_at,updated_at) VALUES('m1','Kept','draft','p1','general:v1',"
        """'{"template_snapshot":{"id":"general"},"attendees":[]}',1,1)"""
    )
    db.commit()
    db.close()

    workspace = Workspace(path)
    try:
        connection = workspace.connection
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert not {"templates", "template_versions"} & tables
        assert "template_version_id" not in workspace.columns["meetings"]
        assert "default_template_id" not in workspace.columns["projects"]
        meeting = workspace.require_item("meetings", "m1")
        assert meeting["title"] == "Kept" and meeting["project_id"] == "p1"
        assert "template_snapshot" not in meeting and meeting["attendees"] == []
    finally:
        workspace.close()
