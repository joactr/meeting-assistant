# Contributing

Use the exact toolchains and frozen dependency locks. Run `just bootstrap`,
`just check`, `just test` and `just smoke` before proposing a change. A feature
must be connected through its actual consumer surface, not merely registered
as a capability or represented by a button.

Domain code contains value types and ports, not FastAPI/provider/SQL imports.
Application services orchestrate workflows. Infrastructure implements storage,
providers and workers. The engine is the only SQL owner. Native Rust owns OS
capture/media, credentials, process supervision and tightly scoped desktop
integration, not transcript business logic. The UI owns presentation only.

New provider protocols require a real adapter, capability descriptor, errors,
documentation and conformance evidence. A new display name is not support.
No test/provider mocks may enter a production path. No network/model download
in deterministic tests; real service acceptance is explicit and credentialed.

Use independently authored code. Do not introduce employer-owned source,
private meeting data, internal endpoints or copied prompts/tests/assets.
Public dependencies must retain their licenses and exact provenance.
Fixtures must be synthetic or explicitly licensed.

Schema/contract changes need migration and consumer updates. User corrections
are durable: late inference and regeneration cannot silently overwrite manual
fields. Remote side effects require reviewed payloads and uncertain-send
handling. Never weaken a release check to make a partial build pass.

Do not expose secrets in issues, logs, screenshots or CI artifacts. Review
`SECURITY.md` for reporting vulnerabilities. MacOS/Linux changes remain
experimental until actual target acceptance is recorded.
