# Acceptance evidence

Completion requires all seven commands to exit zero: `just bootstrap`,
`just check`, `just test`, `just smoke`, `just build`, `just package` and
`just release-check`. In addition every P01–P30 and G01–G06 observable scenario
must pass on Windows through its actual UI/API/native boundary. No requirement
is complete merely because a class, route or panel exists.

Command execution appends truthful status/platform/time receipts to ignored
`artifacts/commands.jsonl`. Required scenario evidence belongs in ignored
`artifacts/acceptance.json`, keyed by requirement ID, recording actual command
or action, status, `output_file` (a path below `artifacts/`), `output_sha256`,
build/runtime/model fingerprints and relevant fixture/platform identity.
`release-check` hashes the actual output file; a claimed hash with no file or
changed bytes is rejected. Do not manufacture receipts for unexercised paths.
In addition to P/G IDs, section 19 scenario keys are `capture`, `boundaries`,
`crash_durability`, `live_speakers`, `providers`, `local_inference`, `recording`,
`retranscription`, `vocabulary`, `analysis`, `knowledge`, `notes`, `calendar`,
`github`, `digests`, `export_backup_import`, `ui`, `security` and `update`.

## Required real scenarios

- Capture mic/system/both; device loss/retry and permissions; numbered samples
  across VAD, pause/resume and Stop; actual safe exit/update preservation.
- Engine/worker/host crash at media commit, drain and candidate promotion;
  reopened committed edits, attention and no duplicate outputs.
- Caption before diarization; same-row revision; unknown short reply; manual
  speaker identity and transcript corrections survive late inference.
- Every required real provider/runtime; timing/schema/refusal/cancel/errors;
  missing assets never silently download or switch destination.
- Recording off leaves no durable audio; enabled replay/expiry/delete/cap;
  re-transcription diff/cancel/promote and manual correction conflicts.
- Grounded analysis, EN/ES question/short-answer boundaries, source citations,
  scoped knowledge, editing/deletion invalidation and keyword-only fallback.
- Note autosave/CAS/provenance-guided minutes; template snapshots and bookmarks.
- Actual OAuth/calendar recurrence/pagination/revocation and reminder behavior;
  actual reviewed GitHub creation and uncertain-send reconciliation.
- Digest DST/catchup/unique periods/no-change and uncertain delivery state.
- Edited Unicode exports, encrypted backup roundtrip, read-only legacy import
  with counts/mapping/report, and native clipboard/email restrictions.
- Keyboard-only UI workflow, focus restoration, error/offline states, 10k-row
  virtualization and documented performance reference machine.
- Host/auth/origin/Markdown/file/archive/redirect/secret boundaries and actual
  install/use/upgrade/tamper rejection with preserved data.

Fixtures and unit tests supplement these checks, not replace them. macOS/Linux
implementations are experimental and untested and have no claimed real-platform
acceptance evidence. Missing accounts, signing material, models or hardware are
reported explicitly. Work stops after at most 100 change/verification cycles.

## Recorded Windows command baseline — 2026-10-22

The earlier complete command sequence is recorded in
`artifacts/commands.jsonl` (entries 32–38):

| Command | Exit status |
| --- | --- |
| `just bootstrap` | 0 |
| `just check` | 0 |
| `just test` | 0 |
| `just smoke` | 0 |
| `just build` | 0 |
| `just package` | 0 |
| `just release-check` | 1 |

The test command passed 83 Python, 9 frontend and 2 Rust tests. Smoke passed
the real engine HTTP/preservation path, native file-source boundaries and three
browser scenarios. Packaging produced an unsigned Windows NSIS installer, not
an official signed release.

Additional bounded evidence includes the actual installed desktop notebook and
safe idle exit (`artifacts/installed-desktop-5wuzvdns/verification.json`), real
WASAPI system-loopback capture followed by frozen Whisper inference
(`artifacts/system-loopback-inference.json`), and actual age-encrypted backup
activation with exact multilingual notes preserved
(`artifacts/encrypted-backup-ray8dwqr/verification.json`). The latest frozen CLI
also exported those notes successfully under forced legacy pipe encoding
(`artifacts/frozen-cli-unicode-8tl7bpbw/verification.json`). The installed-desktop
receipt fingerprints its earlier tested installer; it does not certify a signed
upgrade or the latest repackaged installer.

Release verification rejects the empty real public repository, bundle identity,
HTTPS support URL, updater/catalog public keys, all three OAuth client IDs and
missing complete end-to-end acceptance ledger. Official signing additionally
requires the matching protected private keys and Windows publisher certificate.
Do not invent identities or mark these gates passed.

Still-unexercised acceptance includes complete mic/system/both desktop capture,
device-loss and active-capture preservation, other real runtime/model families,
credentialed providers, real calendar/GitHub/delivery accounts, full crash and
P/G scenario coverage, and signed installed N-to-N+1 update/tamper scenarios.
Community-1 requires upstream gated-term acceptance and authorized model access.
No fixture or partial scenario above substitutes for that missing evidence.

### Additional local verification — 2026-10-23

The standalone Tauri/WebView2 executable exercised actual native device
enumeration and RAM-only audio tests. The microphone test acknowledged startup;
intelligible microphone speech was not verified. System-loopback meters responded
to known synthetic speech. Escape restored dialog-trigger focus, tests retained
no media files, and the process exited cleanly. After correcting lifecycle
identity payloads, Start reached actual missing-ASR readiness and did not claim
active capture. This is not complete meeting-recording acceptance. The receipt
fingerprints its executable and previously packaged resources:
`artifacts/local-capture-acceptance-g5r7iejv/verification.json`.

An actual application/SQLite probe used a synthetic 10,000-meeting and
10,000-utterance fixture. The library reached recent meetings beyond the storage
batch boundary. Bounding transcript reads reduced the observed first-page
backend time from approximately 595 ms to 9 ms. These timings are not native
viewport-render measurements. The earlier native large-fixture run failed its
shutdown request; that observation is retained as a failure, not acceptance:
`artifacts/local-large-ui-1siqmlte/before.json` and `backend-timings.json` in the
same directory.

After coalescing event-driven refreshes, the current standalone native build
rendered the newest library rows and a distinct second page, reloaded without
losing its engine, and kept only 12 transcript rows mounted for its 200-row page
from the 10,000-utterance dataset. An older Greek-text draft and its top-of-list
scroll position survived a synthetic incoming final; the correction then saved
and was read back through the actual native command channel. Five native
200-row transcript reads measured 11.1–15.4 ms. The native process subsequently
completed preservation-acknowledged shutdown and exited with status 0.

`artifacts/local-large-ui-1siqmlte/verification.json` fingerprints the exercised
desktop and frozen engine and links the detailed before/after receipts and
actual editor screenshot. This used previously packaged dependency resources,
not a newly installed upgrade. It is synthetic-data UI/read/save evidence, not
ASR, recording or reference-machine p95 certification.

The current `just check`, `just test`, `just smoke` and `just build` sequence
passed: 85 Python, 10 frontend and 2 Rust tests, real engine HTTP/preservation,
native file-source smoke and three browser scenarios. The new event-burst
regression exercises the real query cache: one read remains in flight, the
trailing revision is fetched, and an unrelated meeting stays unchanged.

The subsequent current `just package` exited 0 and produced
`target/release/bundle/nsis/Meeting Assistant_1.0.0-beta.1_x64-setup.exe`
(34.24 MiB), with dependency notices and inventory. This newly packaged unsigned
installer was not installed in this verification pass; the earlier native
receipts identify their exact exercised binaries and do not certify it.
`just release-check` exited 1 for the real publisher/repository/support,
updater/catalog key, three OAuth client ID and complete acceptance prerequisites
listed above. No gate was bypassed and no credentials or identities were invented.

### Isolated installed local pass — 2026-10-23

`artifacts/unattended-acceptance-bzji1y9h/native-capture-verification.json`
records actual installed native capture through a separately frozen first-party
Whisper worker and reviewed local model. Controlled Windows synthetic speech,
not unrelated microphone recordings, supplied the system-loopback input. The
private developer-worker registry is not an official signed runtime catalog.

Bounded observations include recording-off storage remaining empty, acknowledged
pause/resume/Stop, captions surviving an actual crash, retained Opus playback
advancing, and real retained-audio candidate generation/promotion preserving a
multilingual manual correction. Startup recovered a real native segment that
the earlier build had failed to catalogue. The final installed build also
cleared the promoted candidate review without the previously observed error.

Multilingual structured notes survived save/reload and preservation-aware exit.
The installed frozen CLI exported the edited notebook and backed up/restored its
actual retained Opus. Original/restored Markdown and Opus SHA-256 hashes matched.
That roundtrip used explicitly acknowledged plaintext and controlled synthetic
data; it is not new encrypted-backup evidence.

The final exercised unsigned installer SHA-256 is
`fcb1388d383e838909051cb355ec4be40cbeedde7f9947313320dd14b98dd69e`.
Its actual installed desktop and engine hashes are in the receipt. Final native
preservation-aware shutdown exited 0. Current check/test/smoke/package passed:
125 Python, 10 frontend and 2 Rust tests, authenticated real engine smoke,
native exact-boundary file-source smoke and three browser scenarios.

`artifacts/unattended-acceptance-bzji1y9h/requirement-status.json` explicitly
records all 36 P/G requirements as partial, unverified or externally blocked.
None is marked fully accepted from these bounded observations. Full file-import,
cancel/expiry/cap, hardware/device, diarization, provider, legacy-import and
accessibility matrices remain incomplete. Publisher/signing/OAuth/account/model
prerequisites listed above remain required; no release gate is bypassed.


### Additional unattended verification — 2026-10-23 (cycle 2)

New automated checks added without needing any external account, hardware or
signing material. All are regression evidence from the real engine, Rust crates
or browser surface; none replaces the native/installed scenarios listed above.

- `tests/integration/test_legacy_real_schema.py`: a synthetic database using the
  0.1.x table layout (sessions, transcript_segments, action_items, summaries,
  speakers, topics, questions, bookmarks, speaker_profiles). The importer mapped
  every table, kept provisional segments non-final, reported biometric vectors as
  disabled, left the source file byte-identical and was idempotent on re-import.
  No real user database was read or imported.
- `tests/integration/test_source_loss.py`: loss of the system source leaves the
  meeting recording with degraded health and a persistent timeline gap, the
  microphone keeps ingesting, a later retry does not erase the gap, and Stop
  preserves gap and transcript. This is engine-level behavior; it does not
  exercise a physically removed device.
- `crates/native-host` grant tests and `crates/audio-core` decoder tests: unknown
  purpose, relative path, directory, purpose mismatch, expiry, revocation,
  read-grants-cannot-write, the inclusive 20 GiB bound, exact sample count of a
  valid file, rejection of empty/garbage media and consumer-abort propagation.
- `tests/browser/desktop.spec.ts` keyboard-only journey: link activation, Tab and
  Shift+Tab containment inside a dialog, Escape focus restoration, roving-tab
  arrow/Home/End navigation and an accessible-name audit of visible controls.
  This does not replace a screen-reader pass or the native WebView2 check.

`just check`, `just test` (128 Python, 10 frontend, Rust tests), `just smoke`
(four browser scenarios), `just build` and `just package` exited 0.
`just release-check` still exits 1 for the same missing publisher, repository,
key, OAuth client ID and end-to-end acceptance-ledger prerequisites. No
requirement is promoted to accepted from these additions.

### Installed identity check — 2026-10-23

With the real publisher identity configured, `just package` built an unsigned
developer installer (SHA-256 `678842c8…569c757f`) using bundle identifier
`io.github.joactr.meeting-assistant`. It was silently installed to an isolated
directory, and `tests/acceptance/native/window_smoke.py` drove the actual
WebView2 window through UI Automation: single-instance focus, Settings and Search
shortcuts, and clean close of desktop and engine. The new identifier's data
directory was created under `%LOCALAPPDATA%`, and the installer then uninstalled
cleanly. Receipt: `artifacts/installed-id-check/verification.json`.

This is unsigned and shallow: it does not exercise capture, ASR, upgrade or
signed updates, and supersedes none of the earlier receipts for those scenarios.
