# Privacy, retention and external effects

There is no application account, hosted synchronization, analytics or automatic
meeting attendance. Explicit Start authorizes capture only for that meeting.
A calendar reminder opens preparation and must never start recording.

## Data on the machine

SQLite and retained media are readable by processes with access to the user's
OS account. The application does not claim workspace encryption; use OS disk
encryption and appropriate account controls. Provider/OAuth credentials belong
in the native OS credential vault, never the WebView, SQL or exported settings.
An unavailable vault must not fall back to a plaintext secrets file.

Live recording retention is off by default. When enabled, the default is seven
days with separate source tracks and a 10 GiB workspace media cap. Recordings
may be partial or expired while the transcript remains available. At the cap,
retaining new audio stops visibly rather than silently deleting pinned media.
Participant permission and legal compliance remain the user's responsibility.

Imported files may require explicit private temporary decoding even when the
original is not retained. Temporary import audio is not a live recording
journal and must not enter backups. Cancellation/startup sweep removes it.

## External destinations

Audio transcription, current text analysis, historical Ask excerpts, notes,
calendar access, model/runtime acquisition, GitHub handoff, Slack/SMTP delivery
and update checks have distinct scopes. An endpoint/model/data-class change
can require renewed consent. A loopback proxy is not proof of local inference.

Actions extracted by AI are not authorization to create issues or deliver mail.
Review the destination and payload. A timed-out remote write may have succeeded;
unknown-send state must not trigger a blind duplicate retry.

## Deletion and backups

Deleting a meeting invalidates search, memory and derived evidence and removes
owned retained media, speaker embeddings and project digests built from it.
Deleting a person removes their voice profiles. Local deletion cannot remove previously sent issues,
email or Slack messages. SSD unlink/vacuum is not secure physical erasure.
Portable backups exclude secrets, temporary audio, caches and runtimes/models;
selected retained media is a separate choice. Preserve encrypted backup keys
outside the backup itself. Restore validates into a new private directory,
not over a live database.

Diagnostics should contain identifiers/timings/safe codes, not prompts,
transcripts, attendees or credentials. Review a support bundle before export.
