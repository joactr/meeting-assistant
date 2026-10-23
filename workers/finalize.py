"""Review and sign an existing runtime artifact without rebuilding its bytes."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def finalize(catalog: Path, audit_path: Path, signing_key: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
    repository = config["publisher"]["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Configure the actual public publisher repository")
    descriptor = json.loads(catalog.read_text(encoding="utf-8"))
    if descriptor.get("role") != "runtime" or len(descriptor.get("files", [])) != 1:
        raise ValueError("Expected one target-native runtime archive")
    file = descriptor["files"][0]
    if Path(file["path"]).name != file["path"]:
        raise ValueError("Runtime archive must be adjacent to its catalog")
    archive = catalog.parent / file["path"]
    with archive.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != file["sha256"] or archive.stat().st_size != file["bytes"]:
        raise ValueError("Runtime archive no longer matches its build descriptor")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if not (
        audit.get("approved") is True
        and audit.get("artifact_sha256") == actual
        and audit.get("reviewer")
        and audit.get("evidence")
    ):
        raise ValueError("Actual artifact-bound license/codec review is required")
    key = serialization.load_pem_private_key(signing_key.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Runtime catalogs require the separate Ed25519 catalog key")
    public = key.public_key().public_bytes_raw()
    if public != base64.b64decode(config["updates"]["catalog_public_key"], validate=True):
        raise ValueError("Signing key does not match the configured catalog trust root")
    file["url"] = (
        f"https://github.com/{repository}/releases/download/v{descriptor['revision']}/{archive.name}"
    )
    descriptor["license_audit"] = audit
    descriptor["blocked_reason"] = None
    canonical = json.dumps(
        {key: value for key, value in descriptor.items() if key != "signature"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    descriptor["signature"] = base64.b64encode(key.sign(canonical)).decode()
    temporary = catalog.with_name(catalog.name + ".part")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(descriptor, indent=2) + "\n")
    temporary.replace(catalog)
    print(f"Reviewed and signed existing runtime artifact: {archive.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--license-audit", type=Path, required=True)
    parser.add_argument("--signing-key", type=Path, required=True)
    args = parser.parse_args()
    try:
        finalize(args.catalog, args.license_audit, args.signing_key)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Runtime finalization failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
