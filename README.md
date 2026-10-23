# Meeting Assistant

An independently implemented local-first developer meeting notebook. This
repository is a development project, not a claim of a verified public release.
The implementation targets explicit capture, editable evidence, reviewed AI
outputs and user-controlled destinations rather than automatic meeting bots.

## Platform status

| Platform | Status | Intended system-audio backend |
|---|---|---|
| Windows 11 x86-64 | Primary verification target; see recorded acceptance evidence | WASAPI shared-mode loopback |
| macOS 15+ arm64/x86-64 | **Experimental and untested** | ScreenCaptureKit audio and CoreAudio microphone |
| Ubuntu 24.04 x86-64 | **Experimental and untested** | PipeWire monitor nodes |

macOS and Linux cannot currently be tested by the maintainer in this environment.
Source presence or successful compilation is not a guarantee of capture,
permissions, playback, packaging or update behavior on those systems.

## Development

Required toolchains are pinned in `.python-version`, `.node-version` and
`rust-toolchain.toml`. Install Python 3.12.10, Node 24.22.1, Rust 1.94.0, uv 0.13.5
and just 1.59.0. Windows native builds additionally require Visual Studio C++
build tools and WebView2. Native codec SDK prerequisites are documented in
[platform setup](docs/platforms.md).

```text
just bootstrap
just dev
```

The engine is a Python package under `engine/meeting_assistant`; the React
application is in `apps/desktop`; the Tauri host and native audio/media crates
are separate from engine business logic. Workspaces use application-data
locations, never an implicit database in the current working directory.

## Verification

```text
just check
just test
just smoke
just build
just package
just release-check
```

These commands propagate failures. `smoke` exercises a real local engine,
native deterministic file-source processing, and the actual browser surface.
It does not replace real microphone/system capture, credentialed provider,
installed-desktop, OAuth or signed-upgrade acceptance. Command receipts are
written under ignored `artifacts/`; missing acceptance evidence is not a pass.

## Privacy and inference

Capture always needs an explicit Start. Retained recording is separately
opt-in and off by default; recording off must not create an audio crash journal.
Optional AI, embeddings and diarization must not prevent basic notebook use.
The selected ASR must be ready before capture transcription starts; no silent
provider switch is permitted.

Large models and inference runtimes require explicit acquisition. An empty
installation is not offline-inference-ready. Connections use user-provided
credentials and declared destinations; no project-operated inference account
or company gateway is required. Cloud processing and delivery are distinct
consents. Read [privacy](docs/privacy.md) before using real meeting data.

Source and identity-less developer builds can explicitly install a first-party
worker into a private, workspace/model-scoped native-code registry. This is a
separate developer action, not an unsigned official catalog or an executable
model import. See [Windows developer setup](docs/providers.md#windows-developer-worker-setup).
It does not make an empty installation ready or satisfy signed-release gates.

## Distribution

`release.toml` is the version and publisher-identity authority. Its empty
publisher, OAuth and signing inputs must be supplied by the actual publisher;
no fake owner/repository, production key or registration is bundled. Developer
builds and official signed releases are different. Release checks fail until
required identities, credentials, actual installers and acceptance evidence
are present. No publishing or paid service is performed by developer recipes.

Original application source uses MIT. Dependencies, codecs and model weights
retain their own licenses; see `NOTICE` and generated artifact inventories.
