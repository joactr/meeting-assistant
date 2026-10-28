-- Per-meeting and per-utterance lookups the transcript, speakers and capture paths run on every
-- refresh; without these each one scans the whole table.
CREATE INDEX speaker_assignments_utterance ON speaker_assignments(utterance_id);
CREATE INDEX utterance_revisions_utterance ON utterance_revisions(utterance_id);
CREATE INDEX capture_sources_meeting ON capture_sources(meeting_id);
CREATE INDEX timeline_gaps_meeting ON timeline_gaps(meeting_id);
CREATE INDEX meeting_speakers_meeting ON meeting_speakers(meeting_id);
CREATE INDEX media_segments_meeting ON media_segments(meeting_id);
CREATE INDEX citations_meeting ON citations(meeting_id);
CREATE INDEX entity_sources_meeting ON entity_sources(meeting_id);
CREATE INDEX analysis_runs_meeting ON analysis_runs(meeting_id);
CREATE INDEX jobs_meeting ON jobs(meeting_id);
CREATE INDEX outbox_events_meeting ON outbox_events(meeting_id, seq);
PRAGMA user_version=5;
