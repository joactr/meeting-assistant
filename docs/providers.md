# Providers, runtimes and capability reporting

Connections identify an adapter, endpoint and opaque native-vault secret
reference. Model profiles identify actual models/deployments/artifact revisions.
Task policies select profiles and explicit fallback order per task. No fallback
is enabled merely because another provider is configured.

## Required protocols

LLM adapters distinguish OpenAI Responses, generic OpenAI Chat Completions,
Azure OpenAI v1, Anthropic Messages, Gemini generateContent and native Ollama.
Schema dialects, streaming events, refusals, output limits and model-specific
options are not interchangeable across these protocols.

ASR paths distinguish faster-whisper chunk scheduling, sherpa Parakeet TDT,
Deepgram live/files, AssemblyAI Streaming v3/files and Azure Speech SDK/files.
Community-1 diarization is separate unless ASR supplies speaker labels.
Embeddings distinguish reviewed local multilingual E5 and actual compatible,
Azure, Ollama and Gemini embedding protocols. Equal dimensions do not imply
compatible vector spaces.

## Windows developer worker setup

Official runtime acquisition still requires its real signed catalog. For source
and identity-less developer builds only, a developer can explicitly trust a
first-party frozen worker as native code. This is not a sandbox, signed artifact
attestation, official license audit or production release approval. Publisher-
configured builds reject this registry. Do not edit manifest hashes to reuse a
worker built from different first-party source, protocol or lock files.

From the repository root, with the pinned developer tools on `PATH`, acquire the
model explicitly and build a fresh worker (existing build output is not overwritten):

```powershell
uv run --frozen python -m meeting_assistant.application.inference.catalog_cli fetch whisper-small-multilingual --install-root artifacts/local-workspace/inference --accept-license --accept-destination
uv run --frozen python workers/build.py whisper --accept-runtime-license --output artifacts/local-builds
$workspace = (Resolve-Path artifacts/local-workspace).Path
$registry = Join-Path $PWD artifacts/local-workers
$package = Join-Path $PWD artifacts/local-builds/whisper-1.0.0-beta.1-windows-x86_64/package/meeting-whisper
```

Review the emitted runtime manifest and `third-party-notices.json` before the
explicit native-code/license/destination acceptance below. Use the package path
printed by the build if its version or target differs from this Windows example.

```powershell
uv run --frozen python -m meeting_assistant.application.inference.developer_worker_cli install --package-directory $package --registry $registry --workspace $workspace --model-id whisper-small-multilingual --model-revision 536b0662742c02347bc0e980a01041f333bce120 --accept-native-code --accept-license --accept-destination
$env:MEETING_ASSISTANT_WORKSPACE = $workspace
$env:MEETING_ASSISTANT_DEVELOPER_WORKER_REGISTRY = $registry
```

Launch the rebuilt developer desktop from that shell. Configure a local
`faster-whisper` connection (`local://whisper`, no authentication), an ASR model
profile with model ID `whisper-small-multilingual` and the installer-emitted
`profile_options` unchanged, then select it in the ASR task policy. Do not combine
`developer_worker_id` and `runtime_id`. Run the actual readiness test before Start.
The model must already be installed in that workspace's `inference` directory.
Copied registry receipts, a different workspace/model revision, modified files
or broadened registry permissions are not new execution authorizations. A catalog
recommendation or an installer receipt alone does not prove working inference.

## Optional NVIDIA CUDA Whisper runtime

The installer and the default `whisper` runtime are CPU-only (INT8). GPU
inference is a separate, optional runtime, `runtime-whisper-cuda`
(`engine/catalog/runtimes/whisper-cuda.json`, built by `workers/build.py whisper-cuda`).
It carries the NVIDIA cuBLAS, cuDNN and NVRTC libraries, so it is a very large
download (about 2.2 GiB unpacked, about 1.4 GiB zipped on Windows) and is never
bundled with the installer. It supports Windows and Linux x86-64 only.

- Settings → Providers shows the runtime card with a read-only host check
  (`nvidia-smi`: driver 525 or newer, 2 GiB of GPU memory). Detection never
  selects a device for you.
- GPU use is explicit: set `"device": "cuda"` in the model profile options
  (`compute_type` defaults to `float16`, `device_index` to 0). `cpu` is the
  default. `auto` is rejected.
- There is no silent CPU fallback. If the driver, libraries or GPU memory are
  unusable, readiness fails with an actionable error and you choose the CPU
  runtime yourself. The check really loads the CUDA libraries and runs a tiny
  inference, so preflight is honest.
- The official download is blocked, like every runtime, until the release owner
  supplies a public archive URL, NVIDIA redistribution review and the detached
  catalog signature. For local development, build it and install it with the
  developer worker CLI exactly as the CPU worker above.

Measured locally (RTX 4060 Ti, Whisper small, 7 s clip, same text): CPU 2.25 s,
CUDA 0.25 s warm. Receipt: `artifacts/cuda-acceptance/runtime-acceptance.json`.

## Extending an adapter

Implement the typed port and normalize times into integer meeting milliseconds.
Preserve timing provenance (word/token/derived/chunk), unavailable confidence,
late speaker revisions, overlap, typed errors and cooperative cancellation.
An offline chunk recognizer is not advertised as native interim streaming.

Declare resolved language/streaming/timing/vocabulary/schema/cancellation and
resource capabilities. Test unsupported options explicitly. Validate complete
structured output and all source references before promotion; partial JSON and
unknown citations are not usable results. Preserve prior promoted output on
failure. Never make up token context limits, monetary costs or model hashes.

Vocabulary is compiled to actual supported prompt hints/keyterms/phrase lists
with effective limits and omissions visible. Greedy Parakeet vocabulary is
unsupported unless a distinct tested biasing profile is installed; never
perform global text replacement as an ASR substitute.

## Real versus deterministic verification

Synthetic contract fixtures exercise parsers and invariants without credentials.
Real adapter acceptance requires an explicit credentialed bounded request.
Neither successful fixture parsing nor a nonempty output proves live service
compatibility. Keep tokens and historical meeting text out of test logs.

Local inference acceptance uses actual reviewed runtime/model artifacts and
licensed audio. Absent assets return actionable unavailable errors, not a mock
transcript or hidden first-use download. Community-1 acquisition additionally
needs user acceptance of upstream gated terms and appropriate HF credentials.
