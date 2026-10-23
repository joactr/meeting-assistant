# Changelog

## Unreleased

This is an independently written development project, not a completed or
verified public release. Original company application source is not vendored.

### Added

- Optional NVIDIA CUDA Whisper runtime (`workers/build.py whisper-cuda`, catalog
  descriptor `runtime-whisper-cuda`): separate large download, explicit
  `device: cuda` profile option, read-only GPU eligibility check in the runtime
  catalog, actionable failures and no silent CPU fallback. Verified on an RTX 4060 Ti.
- Separate engine/native/UI ownership and frozen application/worker dependency
  environments, including CPU-only Torch resolution for the diarization worker.
- Connected notebook services for scoped vocabulary, calendar preparation,
  reviewed GitHub handoff, human-note minutes candidates, cited project digests
  and explicit local/Slack/SMTP delivery with uncertain-send reconciliation.
- Real bundled native dependency preparation with checked hashes/licenses,
  small Silero VAD, ONNX Runtime and an original SpeexDSP build wrapper.
- Project-local compiler/audit tooling and guarded protected release assembly;
  no release or external delivery occurs as a side effect of local builds.
- Runtime catalogs are approved and separately Ed25519-signed only after the
  actual archive exists; publication independently rechecks approval, signature,
  archive bytes and release URLs. No model weights or private keys are bundled.
- Explicit CLI installation of first-party developer-built workers into a private,
  immutable registry scoped to one workspace and exact reviewed model; official
  signed-catalog acquisition and release gates remain separate and mandatory.
- Desktop interface redesign: shared light/dark design tokens, Inter and
  Newsreader variable fonts (SIL OFL 1.1) bundled as local files for the strict
  `font-src 'self'` CSP, Lucide icons (ISC), a collapsible icon rail, and
  refreshed lists, transcript, dialogs, tabs and forms. No behavior, route or
  engine contract changed.

### Fixed

- The window close button did nothing when the tray was disabled: the native host
  intercepted the close and emitted `native.shutdown.requested`, but no UI code
  handled it. The UI now flushes drafts, reserves preservation and exits, or shows
  what blocks quitting, with "Keep working" and, only when the engine cannot
  confirm preservation, an explicit "Quit anyway".

- Installed capture sends one canonical epoch and the actual recording, media-cap
  and DSP settings to the native host instead of an ignored nested configuration.
- Audio transport accepts short final PCM packets, preserving every sample at
  pause/Stop boundaries rather than rejecting the packet and blocking drain.
- Native segment filenames resolve under the private media directory and are
  catalogued as workspace-relative paths for replay, backup and restore. Pause
  and Stop await cataloguing the media commits in each acknowledged boundary.
- Startup schedules native integrity reconciliation of retained segment sidecars
  and restores missing catalog entries through the normal media-commit command.
- Successful transcript-candidate promotion clears its review selection instead
  of querying the archived candidate and showing a spurious operation failure.
- Local ASR treats `auto`/empty language as recognizer auto-detection rather than
  forwarding an invalid language code. Model-profile setup preserves the selected
  catalog revision and keeps runtime descriptors out of the model picker.
- Media imports validate decoded PCM bytes separately from original encoded size;
  imports and re-transcription honor explicit profiles, retained segment offsets
  and source identities, with cancellation cleanup and completed-job replay.
- Candidate promotion reconciles a new active run while preserving manual text,
  speaker locks and untouched ranges; prior source runs remain archived. Stale
  parent/revision/watermark promotion is rejected and unmapped quotes stay stale.
- Exports page complete meeting-owned collections and retain approved sections
  alongside reviewed outcomes. Explicit email exports exclude raw notes/artifacts
  and full transcripts, and are available through both desktop and CLI.
- A passphrase secret reference requires an encrypted native backup destination;
  plaintext acknowledgement can no longer silently downgrade that request.
- Corrected transcript text becomes keyword-searchable immediately, before its
  queued semantic-index work; stale provider text cannot replace manual edits.
- Windows runtime notice capture uses UTF-8 explicitly. Native provenance no
  longer includes a stale hash of its own previous manifest.
- Updated the engine cryptography dependency to 50.0.0 after the dependency
  audit rejected the previous pin; advisories were not suppressed.
- Windows engine readiness now verifies that the reported process belongs to
  the supervised Job Object, accepting legitimate Python launcher children
  without weakening process-tree containment or kill-on-close handling.
- Native startup initializes the updater configuration even when publisher
  updates are disabled. The public updater dependency retains its licenses
  and provenance while using ZIP 9 for the audited Windows archive fixes.
- Completed strict Python typing, including the actual ReportLab Unicode
  export API declarations, without ignored diagnostics or runtime wrappers.
- Native heartbeat requests consume their health-response bodies before releasing
  connections, avoiding Windows connection resets that could block safe shutdown.
- Engine HTTP smoke now holds an event subscriber open through the actual
  shutdown reservation/commit and requires a clean process exit.
- Windows freezers resolve system Visual C runtime DLLs before unrelated SDK
  copies on PATH, fixing an actual frozen Whisper model initialization crash.
- Workers import NumPy before starting their control-input reader, avoiding a
  reproduced Windows native-import/stdin deadlock; shutdown stops that reader.
- Failed worker initialization now closes the process and pipe reader; shutdown
  drains owned transports and timeout cancellation consumes its acknowledgement.
- Frozen engine collection includes dynamically registered namespace modules.
- Developer packaging respects explicitly disabled updates without requiring
  updater signing secrets; official signature verification remains mandatory.
- Engine stdio is explicitly UTF-8, fixing a reproduced Windows CLI failure
  after exporting a notebook containing non-Western Unicode. A subprocess
  regression covers creation, reopen and export under a forced CP1252 pipe.
- Native device lists, capture selectors and echo-calibration selectors now use
  the actual stable `key` returned by enumeration. The previous UI rendered
  device names with empty selection values, preventing real device selection.
- Standalone release builds enable Tauri's custom protocol and embed the
  frontend instead of trying to load an absent development server.
- Meeting lifecycle controls now send `meeting_id`, matching the capture API;
  Start reaches actual ASR readiness rather than failing identity validation.
- Library filtering and cursor pagination no longer stop at the first 200 stored
  meetings. Transcript queries enrich only the requested cursor page rather than
  loading every utterance, preserving equal-timestamp ordering across pages.
- Live engine events coalesce query refreshes instead of repeatedly cancelling
  and restarting native requests. A trailing refresh retains updates received
  during an in-flight read without invalidating unrelated meeting queries.

### Verified during development

- Connected consumer regression suite: 31 tests passed on Windows. Provider
  protocol fixtures are not credentialed vendor acceptance.
- Actual SQLite notebook smoke: notes persisted after reopen, stale autosave
  rejected, human-only provenance retained, approved minutes persisted and a
  deterministic digest preview did not enqueue delivery.
- Native dependency preparation built SpeexDSP with MSVC and assembled the
  checksum-verified native resources. This does not establish audio capture or
  inference success.
- Release assembly refused execution without explicit network authorization;
  unconfigured publisher input was rejected before remote operations.
- Core regression suite: 17 tests passed; actual engine-process HTTP smoke
  exercised authorization, persisted edits, stale-revision rejection and
  keyword-only search without model assets.
- The rebuilt Windows Whisper CPU executable transcribed actual synthetic
  speech through the inference service and shared-memory PCM protocol, returning
  both expected sentences with word timestamps and clean shutdown. Evidence is
  in `artifacts/local-whisper-smoke/output.json`. This is one local ASR runtime,
  not real-device, speaker, other-runtime or signed-catalog acceptance.
- Runtime signing/tamper rejection passed a deterministic cryptographic
  contract smoke using isolated test-only keys and artifacts, not real release
  approval or runtime acceptance.
- Current native resource inventory was checked against actual on-disk bytes.
- Integrated `just check` passed Python lint/format/type checks, TypeScript,
  Rust formatting and all-target Clippy with warnings denied.
- Integrated `just smoke` passed actual authenticated engine HTTP, persisted
  edits, stale CAS rejection, keyword search, native file-source/media boundary
  checks and the three existing browser smoke scenarios. Hardware/provider
  and installed-update acceptance are separate and remain unverified.
- The actual Tauri/WebView2 window reached “Capture host ready”, created a
  meeting and preserved exact human-note text across reload. Bounded evidence
  is recorded in `artifacts/native-ui-z0u98z34/verification.json`; it uses the
  developer Python engine, not a signed or installed application.
- Actual Tauri/WebView2 idle shutdown returned `safe_to_exit: true` after the
  native host verified engine exit, with heartbeat and event relay running.
  This does not establish active-capture or installed-update preservation.
- The actual unsigned NSIS installer installed successfully into an isolated
  directory. Its bundled frozen engine created a meeting through the native UI,
  preserved multilingual human notes across reload and exited zero only after
  preservation acknowledgement. Evidence and exact artifact fingerprints are
  in `artifacts/installed-desktop-5wuzvdns/verification.json`.
- Actual WASAPI system-loopback capture retained the generated speech in Opus.
  Decoding that capture and sending it through the frozen Whisper service
  recovered both expected sentences. Evidence is in
  `artifacts/system-loopback-output.json` and
  `artifacts/system-loopback-inference.json`. No microphone was opened; this is
  not both-source, device-loss or complete desktop capture acceptance.
- Actual age passphrase backup and restore activated an isolated workspace and
  preserved exact multilingual human notes and meeting titles. The synthetic
  passphrase was ephemeral. Evidence is in
  `artifacts/encrypted-backup-ray8dwqr/verification.json`; this is not native
  vault/file-dialog or full media-backup acceptance.
- The dependency/secret audit exited zero after native dependency updates;
  eight upstream Cargo maintenance/unsoundness warnings remain reported.
- Windows bootstrap, check, test, smoke, build and unsigned package have passed;
  current tests passed 85 Python, 10 frontend and 2 Rust cases. Release-check
  exited 1 for missing real publisher/OAuth/key inputs and complete acceptance
  evidence. Exact statuses and remaining scenarios are in `docs/acceptance.md`.
- The rebuilt frozen CLI exported the installed notebook's exact multilingual
  note under forced CP1252 pipe encoding, exited zero and emitted UTF-8 JSON.
  Evidence is in `artifacts/frozen-cli-unicode-8tl7bpbw/verification.json`.
- Actual native device tests acknowledged microphone startup and observed
  generated system-loopback speech on the meter without retaining media. This
  does not establish intelligible microphone speech or full capture acceptance;
  see `artifacts/local-capture-acceptance-g5r7iejv/verification.json`.
- The current native desktop paged a synthetic 10,000-meeting library and kept
  an older draft and scroll position stable across an incoming synthetic final
  in a 10,000-utterance transcript. The correction persisted; native page reads
  measured 11–15 ms and preservation-acknowledged exit returned zero. See
  `artifacts/local-large-ui-1siqmlte/verification.json`; this is not ASR or a
  complete reference-machine performance certification.

Full P/G feature acceptance remains incomplete. macOS and Linux are experimental
and untested. Credentialed providers, other model/runtime families, real OAuth,
active-capture preservation and signed installed upgrades remain unverified;
missing inputs and unexercised scenarios are not recorded as successful acceptance.
