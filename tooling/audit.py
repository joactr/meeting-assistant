"""Audit shipped source and locked dependencies; never suppress failed audits."""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import subprocess
import tarfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path

from run import ARTIFACTS, ROOT, environment, executable, npm, run, uv

GITLEAKS_VERSION = "8.30.1"
CARGO_AUDIT_VERSION = "0.22.2"
GITLEAKS_ARTIFACTS = {
    ("Windows", "AMD64"): (
        "windows_x64.zip",
        "d29144deff3a68aa93ced33dddf84b7fdc26070add4aa0f4513094c8332afc4e",
    ),
    ("Darwin", "arm64"): (
        "darwin_arm64.tar.gz",
        "b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5",
    ),
    ("Darwin", "x86_64"): (
        "darwin_x64.tar.gz",
        "dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709",
    ),
    ("Linux", "x86_64"): (
        "linux_x64.tar.gz",
        "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb",
    ),
}


def prepare_gitleaks() -> Path:
    """Install a checksum-pinned public build tool, not application model data."""
    destination = ROOT / ".tools" / "gitleaks"
    binary_name = "gitleaks.exe" if os.name == "nt" else "gitleaks"
    binary = destination / binary_name
    if binary.exists():
        version = subprocess.check_output([str(binary), "version"], text=True).strip()
        if version != GITLEAKS_VERSION:
            raise RuntimeError(f"Expected Gitleaks {GITLEAKS_VERSION}, found {version}")
        return binary
    target = (platform.system(), platform.machine())
    if target not in GITLEAKS_ARTIFACTS:
        raise RuntimeError(f"No reviewed Gitleaks artifact for {target}")
    suffix, expected = GITLEAKS_ARTIFACTS[target]
    url = f"https://github.com/gitleaks/gitleaks/releases/download/v{GITLEAKS_VERSION}/gitleaks_{GITLEAKS_VERSION}_{suffix}"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024 or hashlib.sha256(payload).hexdigest() != expected:
        raise RuntimeError("Gitleaks archive size/hash verification failed")
    destination.mkdir(parents=True, exist_ok=True)
    # Read only named regular members; never extract attacker-controlled paths.
    if suffix.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            files = {name: archive.read(name) for name in (binary_name, "LICENSE")}
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            files = {}
            for name in (binary_name, "LICENSE"):
                member = archive.getmember(name)
                if not member.isfile() or member.size > 32 * 1024 * 1024:
                    raise RuntimeError("Unexpected Gitleaks archive member")
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError("Missing Gitleaks archive member")
                files[name] = stream.read()
    for name, content in files.items():
        with (destination / name).open("xb") as stream:
            stream.write(content)
    binary.chmod(0o755)
    return binary


def audit() -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    scanner = prepare_gitleaks()
    run(
        str(scanner),
        "dir",
        str(ROOT),
        "--config",
        str(ROOT / ".gitleaks.toml"),
        "--redact",
        "--no-banner",
        "--report-format",
        "json",
        "--report-path",
        str(ARTIFACTS / "secrets.json"),
    )
    # Export hashes from each independent lock without installing heavy workers.
    projects = [
        ("engine", ROOT),
        *[(p.name, p) for p in sorted((ROOT / "workers").iterdir()) if (p / "uv.lock").is_file()],
    ]
    for name, project in projects:
        locked = tomllib.loads((project / "uv.lock").read_text(encoding="utf-8"))
        # PyPI verifies registry hashes; OSV also covers official +cpu builds
        # hosted outside PyPI. Frozen worker installation checks their hashes.
        advisory_service = (
            "osv" if any("+" in package["version"] for package in locked["package"]) else "pypi"
        )
        requirements = ARTIFACTS / f"{name}-audit-requirements.txt"
        uv(
            "export",
            "--project",
            str(project),
            "--frozen",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(requirements),
        )
        uv(
            "run",
            "--frozen",
            "pip-audit",
            "--vulnerability-service",
            advisory_service,
            "--strict",
            "--require-hashes",
            "--disable-pip",
            "-r",
            str(requirements),
            "--format",
            "json",
            "--output",
            str(ARTIFACTS / f"{name}-vulnerabilities.json"),
        )
    npm("audit", "--audit-level=low")
    auditor = (
        ROOT
        / ".tools"
        / "cargo-audit"
        / "bin"
        / ("cargo-audit.exe" if os.name == "nt" else "cargo-audit")
    )
    if not auditor.exists():
        run(
            executable("cargo"),
            "install",
            "cargo-audit",
            "--version",
            CARGO_AUDIT_VERSION,
            "--locked",
            "--root",
            str(auditor.parent.parent),
        )
    version = subprocess.check_output([str(auditor), "--version"], env=environment(), text=True)
    if version.strip() != f"cargo-audit {CARGO_AUDIT_VERSION}":
        raise RuntimeError("Rust security auditor version mismatch")
    run(str(auditor), "audit", "--file", str(ROOT / "Cargo.lock"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--prepare", action="store_true", help="Acquire only pinned source-audit tools"
    )
    args = parser.parse_args()
    try:
        if args.prepare:
            print(prepare_gitleaks())
        else:
            audit()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Audit failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
