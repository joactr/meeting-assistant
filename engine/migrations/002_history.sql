CREATE TABLE document_history(seq INTEGER PRIMARY KEY AUTOINCREMENT,collection TEXT NOT NULL,entity_id TEXT NOT NULL,revision INTEGER NOT NULL,changed_at INTEGER NOT NULL,tombstone INTEGER NOT NULL DEFAULT 0,meeting_id TEXT,project_id TEXT,payload_json TEXT NOT NULL);
CREATE INDEX history_snapshot ON document_history(collection,changed_at,entity_id,revision);
CREATE INDEX history_meeting ON document_history(meeting_id);
PRAGMA user_version=2;
