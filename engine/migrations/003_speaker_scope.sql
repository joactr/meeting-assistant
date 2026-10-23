ALTER TABLE speaker_clusters ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'system';
ALTER TABLE speaker_clusters ADD COLUMN finalized INTEGER NOT NULL DEFAULT 0 CHECK(finalized IN(0,1));
CREATE INDEX speaker_evidence_space ON speaker_clusters(meeting_id,source_kind,model_fingerprint);
PRAGMA user_version=3;
