"""Inspect actual packaged artifacts; never turn missing signatures into success."""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "artifacts" / "release"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def packaged_files() -> list[Path]:
    candidates: list[Path] = []
    for folder in (
        ROOT / "target/release/bundle",
        ROOT / "apps/desktop/src-tauri/target/release/bundle",
    ):
        if folder.exists():
            candidates.extend(
                p
                for p in folder.rglob("*")
                if p.is_file() and p.suffix in {".exe", ".msi", ".dmg", ".AppImage", ".gz"}
            )
    return sorted(set(candidates))


def dependencies() -> None:
    analysis = ROOT / "build" / "engine" / "Analysis-00.toc"
    if not analysis.is_file():
        raise RuntimeError(
            "Build the frozen engine before collecting its actual dependency inventory"
        )
    sources: set[Path] = set()

    def collect(value: object) -> None:
        if isinstance(value, str) and os.path.isabs(value):
            sources.add(Path(value).resolve())
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)

    collect(ast.literal_eval(analysis.read_text(encoding="utf-8")))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    packages = []
    notices = [
        (ROOT / "LICENSE").read_text(encoding="utf-8"),
        (ROOT / "NOTICE").read_text(encoding="utf-8"),
    ]
    python_licenses = list(Path(sys.base_prefix).glob("LICENSE*"))
    if not python_licenses:
        raise RuntimeError("The frozen CPython runtime's license text is missing")
    for path in python_licenses:
        if path.is_file():
            notices.append("\n--- CPython runtime ---\n" + path.read_text(encoding="utf-8"))
    for dist in sorted(
        importlib.metadata.distributions(), key=lambda item: item.metadata.get("Name", "")
    ):
        name = dist.metadata.get("Name", "unknown")
        if not any(Path(dist.locate_file(item)).resolve() in sources for item in dist.files or []):
            continue
        license_value = (
            dist.metadata.get("License-Expression") or dist.metadata.get("License") or "NOASSERTION"
        )
        packages.append(
            {
                "type": "library",
                "name": name,
                "version": dist.version,
                "purl": f"pkg:pypi/{name.lower()}@{dist.version}",
                "licenses": [{"license": {"name": license_value}}],
            }
        )
        for item in dist.files or []:
            if any(
                "license" in part.lower() or part.lower().startswith(("copying", "notice"))
                for part in item.parts
            ):
                source = Path(dist.locate_file(item))
                if source.is_file() and source.stat().st_size < 2_000_000:
                    notices.append(
                        f"\n--- {name} {dist.version}: {item} ---\n"
                        + source.read_text(encoding="utf-8", errors="replace")
                    )
    sbom = {"bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1, "components": packages}
    (OUTPUT / "python-sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2) + "\n", encoding="utf-8"
    )
    # npm and Cargo inventories retain their own ecosystem dependency/license metadata.
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("npm required for the actual JavaScript SBOM")
    cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not cli.is_file():
        cli = Path(node).parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    command = [node, str(cli), "sbom", "--sbom-format", "cyclonedx", "--omit", "dev"]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True
    )
    (OUTPUT / "javascript-sbom.cdx.json").write_text(result.stdout, encoding="utf-8")
    cargo = subprocess.run(
        ["cargo", "metadata", "--locked", "--format-version", "1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    (OUTPUT / "cargo-dependencies.json").write_text(cargo.stdout, encoding="utf-8")
    cargo_packages = json.loads(cargo.stdout)["packages"]
    node_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    notice_roots = [
        (f"Cargo {package['name']} {package['version']}", Path(package["manifest_path"]).parent)
        for package in cargo_packages
    ]
    notice_roots.extend(
        (f"npm {name} {package.get('version', '')}", ROOT / name)
        for name, package in node_lock["packages"].items()
        if "node_modules/" in name and not package.get("dev") and not package.get("link")
    )
    for component, folder in notice_roots:
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(folder)
            if "node_modules" in relative.parts:
                continue
            if any(
                "license" in part.lower()
                or "licence" in part.lower()
                or part.lower().startswith(("copying", "notice", "copyright"))
                for part in relative.parts
            ):
                notices.append(
                    f"\n--- {component}: {relative.as_posix()} ---\n"
                    + path.read_text(encoding="utf-8", errors="replace")
                )
    (OUTPUT / "THIRD_PARTY_NOTICES.txt").write_text("".join(notices), encoding="utf-8")
    bundled = ROOT / "dist" / "notices"
    bundled.mkdir(parents=True, exist_ok=True)
    for name in (
        "python-sbom.cdx.json",
        "javascript-sbom.cdx.json",
        "cargo-dependencies.json",
        "THIRD_PARTY_NOTICES.txt",
    ):
        shutil.copyfile(OUTPUT / name, bundled / name)
    print(f"Collected {len(packages)} frozen Python distributions and production npm/Cargo notices")


def inventory() -> None:
    files = packaged_files()
    if not files:
        raise RuntimeError("No actual installer artifacts found")
    dependencies()
    manifest = [
        {"path": str(path.relative_to(ROOT)), "bytes": path.stat().st_size, "sha256": digest(path)}
        for path in files
    ]
    (OUTPUT / "artifacts.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(
        f"Inventoried {len(files)} actual packaged artifacts; dependency inventories and notices written"
    )


def manifest() -> None:
    config = tomllib.loads((ROOT / "release.toml").read_text(encoding="utf-8"))
    repository = config.get("publisher", {}).get("repository", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Set the actual publisher.repository before generating release URLs")
    version = config["version"]
    platforms = {}
    for path in packaged_files():
        if path.suffix == ".exe":
            target, alias = "windows-x86_64", "windows-x86_64-nsis"
        elif path.suffix == ".AppImage":
            target, alias = "linux-x86_64", "linux-x86_64-appimage"
        elif path.name.endswith(".app.tar.gz"):
            architecture = "aarch64" if "aarch64" in path.name or "arm64" in path.name else "x86_64"
            target = f"darwin-{architecture}"
            alias = f"{target}-app"
        else:
            continue
        signature_file = path.with_name(path.name + ".sig")
        if not signature_file.is_file():
            raise RuntimeError(f"Missing actual updater signature: {signature_file}")
        signature = signature_file.read_text(encoding="utf-8").strip()
        decoded = base64.b64decode(signature, validate=True)
        if not decoded.startswith(b"untrusted comment:"):
            raise RuntimeError(f"Invalid Tauri signature encoding: {signature_file}")
        if target in platforms:
            raise RuntimeError(
                f"Multiple updater payloads for {target}; select one release artifact"
            )
        item = {
            "url": f"https://github.com/{repository}/releases/download/v{version}/{path.name}",
            "signature": signature,
        }
        platforms[target] = item
        platforms[alias] = item
    if not platforms:
        raise RuntimeError("No signed updater artifacts available")
    data = {
        "version": version,
        "notes": "Meeting Assistant release",
        "pub_date": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / "latest.json"
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Generated manifest from {len(platforms) // 2} signed artifacts: {destination}")


def check() -> None:
    config = tomllib.loads((ROOT / "release.toml").read_text(encoding="utf-8"))
    failures = []
    publisher = config.get("publisher", {})
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", publisher.get("repository", "")):
        failures.append("publisher.repository must identify the real public GitHub repository")
    if not publisher.get("bundle_identifier"):
        failures.append("publisher.bundle_identifier is required")
    if not publisher.get("support_url", "").startswith("https://"):
        failures.append("publisher.support_url must be HTTPS")
    updates = config.get("updates", {})
    if not updates.get("public_key") or not updates.get("catalog_public_key"):
        failures.append("real updater and separate catalog public keys are required")
    for key in ("google_client_id", "microsoft_client_id", "github_client_id"):
        if not config.get("oauth", {}).get(key):
            failures.append(f"oauth.{key} is required for real OAuth acceptance")
    files = packaged_files()
    if not files:
        failures.append("no actual installers exist")
    evidence_file = ROOT / "artifacts" / "acceptance.json"
    if not evidence_file.exists():
        failures.append("recorded end-to-end acceptance evidence is missing")
    else:
        evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
        required = [*(f"P{n:02}" for n in range(1, 31)), *(f"G{n:02}" for n in range(1, 7))]
        required += [
            "capture",
            "boundaries",
            "crash_durability",
            "live_speakers",
            "providers",
            "local_inference",
            "recording",
            "retranscription",
            "vocabulary",
            "analysis",
            "knowledge",
            "notes",
            "calendar",
            "github",
            "digests",
            "export_backup_import",
            "ui",
            "security",
            "update",
        ]
        for item in required:
            record = evidence.get(item, {})
            if (
                record.get("status") != "passed"
                or not record.get("command")
                or not record.get("output_sha256")
            ):
                failures.append(f"{item}: actual passing acceptance evidence missing")
                continue
            output_file = (ROOT / str(record.get("output_file", ""))).resolve()
            if (
                not output_file.is_relative_to((ROOT / "artifacts").resolve())
                or not output_file.is_file()
            ):
                failures.append(f"{item}: acceptance output must be an actual artifact file")
            elif digest(output_file) != record["output_sha256"]:
                failures.append(f"{item}: acceptance output hash does not match")
    if failures:
        raise RuntimeError("Release prerequisites/gates unmet:\n- " + "\n- ".join(failures))
    minisign = shutil.which("minisign")
    if minisign is None:
        raise RuntimeError("minisign verifier required; no checksum-only signature substitute")
    public_text = base64.b64decode(updates["public_key"], validate=True).decode("utf-8")
    public_key = next(
        line for line in public_text.splitlines() if not line.startswith("untrusted comment:")
    )
    for path in files:
        updater_payload = path.suffix in {".exe", ".AppImage"} or path.name.endswith(".app.tar.gz")
        if updater_payload:
            signature_path = path.with_name(path.name + ".sig")
            if not signature_path.is_file():
                raise RuntimeError(f"Missing signed updater payload: {path.name}")
            signature = base64.b64decode(signature_path.read_text().strip(), validate=True)
            with tempfile.TemporaryDirectory(prefix="meeting-release-verify-") as folder:
                decoded = Path(folder) / "payload.minisig"
                decoded.write_bytes(signature)
                subprocess.run(
                    [minisign, "-Vm", str(path), "-P", public_key, "-x", str(decoded)], check=True
                )
        if os.name == "nt" and path.suffix in {".exe", ".msi"}:
            signtool = shutil.which("signtool")
            if signtool is None:
                raise RuntimeError("Windows SDK signtool is required to verify publisher identity")
            subprocess.run([signtool, "verify", "/pa", "/all", "/v", str(path)], check=True)
        if sys.platform == "darwin" and path.suffix == ".dmg":
            subprocess.run(
                ["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(path)], check=True
            )
            subprocess.run(
                [
                    "spctl",
                    "--assess",
                    "--type",
                    "open",
                    "--context",
                    "context:primary-signature",
                    str(path),
                ],
                check=True,
            )
            subprocess.run(["xcrun", "stapler", "validate", str(path)], check=True)
    manifest_path = OUTPUT / "latest.json"
    if not manifest_path.exists():
        raise RuntimeError("Generated updater manifest missing; published asset URLs not verified")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for target, entry in manifest.get("platforms", {}).items():
        if not entry.get("url", "").startswith(
            "https://github.com/" + publisher["repository"] + "/releases/download/"
        ):
            raise RuntimeError(f"Untrusted/missing version-tagged asset URL for {target}")
        if not entry.get("signature"):
            raise RuntimeError(f"Missing signature contents for {target}")
    if not manifest.get("platforms", {}).get("windows-x86_64"):
        raise RuntimeError("Windows updater manifest entry missing")
    print("Actual installer signatures and required acceptance receipts verified")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["dependencies", "inventory", "manifest", "check"])
    args = parser.parse_args()
    try:
        globals()[args.command]()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
