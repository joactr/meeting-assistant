# Architecture and ownership

The desktop is a React application inside Tauri. Typed commands cross the native
bridge; the WebView never receives engine tokens or provider credentials.
The native host launches the bundled lightweight Python engine, supplies a
random capability over an inherited private pipe and checks the ready frame's
protocol/build identity before exposing application operations.

Rust owns capture permissions/devices, audio clocks, real-time callback rings,
DSP/VAD, media bytes/playback, the OS vault, OAuth browser callbacks, native
preferences, safe process shutdown and signed updates. Audio PCM takes a bounded
authenticated binary path to the engine, not JSON or WebView events.

The Python engine owns business workflows, workspace SQL and filesystem object
catalogs, jobs, providers, evidence and retrieval. SQLite WAL has one writer;
domain mutations and durable outbox events commit atomically. Editable entities
use compare-and-swap revisions. AI work produces candidates outside SQL
transactions; promotion validates the source watermark and manual-field locks.

Inference is isolated by runtime family. Workers receive typed envelopes and
bounded PCM/shared-memory grants, not workspace SQL or arbitrary user paths.
Process isolation supports fault containment, not a hostile-plugin sandbox.
Large runtimes/models require explicit, verified acquisition. The base engine
must not import Torch or every model library to start.

## Durable versus ephemeral state

Final utterances, user edits, lifecycle changes, result promotion and attention
are durable events with ordered database sequence numbers. Meters, interim
hypotheses and progress are ephemeral and coalescible. Reconnect reconciles a
snapshot plus watermark rather than replaying stale UI state into another
meeting. A slow consumer must resync instead of growing an unbounded queue.

Recording retention is independent from transcription. With recording off,
unacknowledged in-memory audio cannot be recovered after a crash. With recording
on, native media commits fsynced segments before the engine indexes them.
Candidate re-transcription never mutates the active transcript mid-stream.

## External effects

Provider protocols normalize into domain contracts. Capabilities reflect the
actual adapter/model/runtime combination, not a provider-name enum. Unknown
confidence, speaker identity or timing stays unknown. No hidden model/cloud
fallback, generic agent framework or inference gateway is required.

Calendar is read-only preparation. GitHub issue creation and Slack/SMTP delivery
require reviewed destinations and explicit scope. Persist an operation before a
remote write. A timeout is uncertain, not proof of failure or authorization to
retry. Transcript/notes/provider content cannot authorize tools or destinations.

## Source independence

This project is newly authored. Requirements describe observable behavior;
existing company implementation source is not vendored, translated or copied.
Public libraries retain their upstream licenses. Technical similarity is not
represented as a legal ownership determination.
