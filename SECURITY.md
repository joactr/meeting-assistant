# Security

Do not put meeting content, access tokens, audio or voice vectors in public issues.
For a security report, use the repository host's private vulnerability-reporting
feature when enabled; otherwise obtain a private maintainer contact before
sharing sensitive details. This source tree does not invent a support address.

## Boundaries

The native host owns credentials, native file grants, capture and process
supervision. The engine owns domain state and is the sole SQLite writer. The
WebView must not receive bearer tokens, provider secrets, arbitrary file access
or a generic network/shell command.

Local APIs require explicit authorization. Loopback is not authentication.
Transcript, notes, Markdown, calendar items, provider responses and downloaded
metadata are untrusted content, not authority to perform operations.

The workspace is not encrypted by the application. Protect the OS account and
use OS disk encryption where required. Process-isolated inference is not an OS
sandbox against hostile plugins. Local deletion cannot recall remote delivery
or guarantee physical erasure from an SSD or backup.

## Release trust

Updater payload signatures, Windows Authenticode and macOS notarization serve
different purposes. Official builds require actual publisher credentials.
Missing release identities or keys must block official release verification,
not be replaced by placeholder keys or checksum-only update verification.

## Source dependency audits

`just audit` runs the checksum-pinned Gitleaks 8.30.1 source scanner with
redacted reports, pip-audit 2.10.1 against hash-exported engine and independent
worker locks, npm audit, and cargo-audit 0.22.2. Auditor installation is separate
from application inference; it never acquires model weights or uses secrets.
Gitleaks excludes downloaded dependency/build/evidence trees, not source/tests.
Reports belong under ignored `artifacts/`; do not publish them before review.
Every auditor's nonzero status fails the command; there is no ignore-all gate.
These checks use current public advisory databases and do not establish that
dependencies, native binaries, model weights or application code are risk-free.
Actual packaged-component notices/provenance remain a separate release gate.
