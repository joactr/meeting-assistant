# Release verification

`release.toml` owns the SemVer release version and actual publisher identities.
Developer builds must not pretend to be signed official releases. Populate the
real repository, reverse-DNS bundle identity, HTTPS support URL, OAuth public
client registrations, updater public key and separate runtime-catalog key before
attempting official release verification. Never commit private keys or tokens.

Run frozen checks and real runtime smoke before `just build` and `just package`.
The lightweight engine is frozen separately from optional inference workers.
Each worker family has its own dependency environment and target artifact.
Packaging inventories actual binaries and dependency/license metadata, not a
hardcoded list of presumed packages.

`release.toml` enables updates with the public Tauri updater key. `just package`
creates updater artifacts (`.sig`) only when `TAURI_SIGNING_PRIVATE_KEY` is set;
without it, it builds a plain developer installer. Without
`WINDOWS_CERTIFICATE_THUMBPRINT` the installer is Authenticode-unsigned (SmartScreen
warns) but update payloads are still minisign-verified by the app. `just release-check`
still rejects an unsigned installer; this mode does not satisfy official release acceptance.

**One-command local release (Windows):** `just release-local 1.0.0-beta.2` (or `python tooling/run.py release-local VERSION`)
bumps the app version, rebuilds and catalog-signs only runtime packs whose
`worker.py`, `uv.lock` or worker protocol differ from the hashes in their signed
descriptor (unchanged packs keep their earlier release URLs, so never delete old
release assets), packages the updater-signed installer, writes `latest.json`,
commits, tags, pushes and publishes the GitHub release as "latest" after all
assets are uploaded and byte-verified. It needs a clean `main`, a GitHub
credential in git's credential helper, and `catalog-signing-key.pem` plus
`updater.key` in `MEETING_RELEASE_KEYS` (default `~/.meeting-assistant-release`).
Run `just check` and `just test` first; the script does not.

Windows builds prioritize System32 Visual C runtimes over unrelated SDK DLLs
on PATH. Worker NumPy imports must precede the control-input reader: importing
its native extension while that reader blocks on stdin reproduced a deadlock.

`python tooling/release.py manifest` derives updater entries from actual signed
payloads. Windows uses NSIS payload plus `.sig`; macOS updater payloads are app
archives rather than DMG installer bytes; Linux uses AppImage. Signature fields
contain complete Tauri signature file contents. URLs are version-tagged assets
under the configured real repository. GitHub stores asset names with spaces as
dots, so manifest URLs use the dotted name. The app checks
`releases/latest/download/latest.json` shortly after start, daily and at most hourly
on focus, then asks the user before installing. GitHub's "latest" excludes
pre-releases, so updater-carrying releases must be published as normal releases.
Turning off automatic checks or enabling privacy mode in Settings stops all update requests.

`just release-check` rejects missing publisher input, installer signatures,
required real acceptance receipts and manifest entries. It verifies updater
signatures with minisign and Windows publisher signatures with SDK signtool.
This is separate from installed N-to-N+1 update acceptance: create/edit data in
N, safely stop capture, install signed N+1, reopen and verify preserved state.
Tampered payload rejection and shutdown-blocker refusal must be exercised.

Official Windows publisher signing requires an actual certificate/service.
macOS public distribution requires Developer ID signing/notarization credentials;
these are not supplied by a Tauri updater key. macOS/Linux remain experimental
and untested in this project until real target acceptance is recorded.

No local build/package command publishes assets or spends money. Publication must be an
explicitly approved action after all required artifacts, signatures, identities,
licenses and actual acceptance evidence have been reviewed. An empty release
input is a blocker, never permission to fabricate a production value.

## Protected release execution

`.github/workflows/release.yml` is manual-only and requires a real protected
`release-approval` environment with required reviewers. It uses trusted native
self-hosted signing runners labeled `meeting-assistant-release`, never fork PR
runners. Provision the documented compiler/SDK/native dependencies, minisign,
Windows certificate-store private key or Apple signing/notarization identity,
and a private separate catalog key file via `MEETING_CATALOG_SIGNING_KEY_FILE`.
`WINDOWS_CERTIFICATE_THUMBPRINT` is passed as a Tauri signing merge config;
updater signing uses its different protected Tauri signing secrets.

Without the thumbprint, `just package` builds an **unsigned developer installer** and prints
a warning. Release verification runs
`signtool verify` on the installer, so an unsigned developer build can never pass
`just release-check` or be published.

Supply an existing protected tag matching `release.toml` and the ID of a
successful same-commit acceptance run containing `acceptance-evidence` (the
actual `acceptance.json` plus referenced logs). `tooling/release_inputs.py`
rejects unrelated commits, PR/fork runs, missing environment protection and
unconfigured publisher identities before evidence is downloaded.

The workflow builds on each real target, verifies actual local signatures and
receipt hashes, builds the five isolated CPU runtime packages without model
weights, then stages globally unique artifacts and inventories. This workflow
has not been run in the current environment: no signing runners, protected
repository/environment or maintainer credentials are provisioned here.

`tooling/publish.py` refuses network effects without `--allow-network`; creating
a remote draft is itself an explicit external action. It rejects an existing
release rather than replacing assets, uploads with bounded memory, downloads
every stored asset and verifies its bytes against the locally signature-checked
artifact. All assets, including the updater manifest, are present before draft
publication. `--publish` (or the workflow's separately selected publish input)
is additionally required to make the fully staged release public. No release
assembly or publication has been performed during development.

The release assembly authorization guard was exercised locally: invoking the
script without `--allow-network` exits 2 before any remote operation. This is
not evidence of a successful draft, public asset download or installed update.

## Runtime artifact approval

`uv run --project engine python workers/build.py FAMILY --accept-runtime-license --output artifacts/runtimes`
builds an unsigned target-native CPU archive and inventories its actual license
files and linked codec libraries. It does not acquire weights or approve
redistribution. Destination directories are never overwritten.

Review the resulting archive, then supply a separate JSON review containing
`approved: true`, its exact `artifact_sha256`, `reviewer`, and an `evidence`
reference to the actual license/codec assessment. Approval applies to those
bytes, not to a family name or an earlier build. Finalize without rebuilding:

```powershell
uv run --project engine python workers/finalize.py PATH_TO_CATALOG --license-audit PATH_TO_REVIEW --signing-key PATH_TO_PRIVATE_CATALOG_KEY
```

Finalization checks the archive size/hash and requires the separate Ed25519
key to match `release.toml`'s catalog trust root. It never changes the archive.
For protected release assembly, provision `MEETING_RUNTIME_LICENSE_AUDIT_DIRECTORY`
with one `<catalog filename>.review.json` for each built artifact. Reviewers
inspect the native-job artifacts and provision these reviews before approving
the assembly job. `MEETING_CATALOG_SIGNING_KEY_FILE` stays on the trusted runner.
The publisher independently verifies signatures, artifact-bound approval,
actual archive bytes and public URLs before any remote operation; all five
Windows CPU worker families are required. Inventory alone is not approval.

The rebuilt Windows Whisper CPU executable transcribed synthetic English speech
through the actual inference service and shared-memory PCM protocol using the
checksum-verified small multilingual model. Both expected sentences and word
timestamps were returned, and the worker shut down without stderr errors.
Bounded output and runtime/model identity are recorded in
`artifacts/local-whisper-smoke/output.json`. This does not verify other workers,
real audio capture, or runtime-catalog approval; the artifact remains unsigned
and blocked from official publication.
