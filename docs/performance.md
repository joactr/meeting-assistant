# Performance acceptance

These are acceptance targets, not measured claims. Record the reference
machine's CPU, memory, OS build, audio device/sample rate, model/artifact hash,
runtime hash and application build before publishing a result. This source tree
does not invent hardware benchmarks or model resource claims.

Observed Windows development machine (not a provisioned CI performance runner):
Intel Core i9-13900, 24 physical cores / 32 logical processors,
68,443,959,296 bytes of physical memory, Windows build 26200 x86-64. CPU and RAM
were obtained through Win32_Processor/Win32_ComputerSystem, not inferred from
product marketing. No ASR latency or large-library benchmark has been measured.
Native preparation used MSVC 19.39 and Windows SDK 10.0.22621; actual dependency
hashes are written to `dist/native/bin/native-provenance.json`.

| Scenario | Target |
|---|---|
| Warm default local captions | p95 under 2 seconds after VAD utterance end |
| Native callback | Finishes within frame deadline; no allocation/blocking I/O |
| Transcript viewport, 10,000 utterances | Under 200 ms |
| Library first view, 10,000 meetings | Under 2 seconds |
| First search page, 100,000 chunks | Under 500 ms |
| UI update task | No transcript-induced main-thread task over 100 ms |

Speech onset is not utterance end: a 12-second maximum utterance policy cannot
be advertised as two-second captions from speech onset. Measure warm/cold
startup separately and keep unavailable optional inference out of basic
keyword/library timing.

Long-running capture needs clock-drift, discontinuity and backlog evidence for
independent sources. Resource pressure must become visible health/lag or an
explicit capture pause/gap, never silent dropped final captions or hidden audio
spooling while recording is off. Record CPU/RAM/disk alongside latency.

macOS/Linux measurements are absent: those targets remain experimental and
untested. A Windows measurement is not a cross-platform performance claim.
