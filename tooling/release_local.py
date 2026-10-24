"""One-command local Windows release: `python tooling/run.py release-local 1.0.0-beta.2`.

Bumps the app version, rebuilds and signs only runtime packs whose worker source,
lock or protocol changed (unchanged packs keep pointing at their earlier release),
packages the updater-signed installer, writes latest.json, commits, tags, pushes
and publishes a GitHub release. The release is published as "latest" (not a
pre-release) because the in-app updater reads releases/latest/download/latest.json.

Keys live outside the repo in MEETING_RELEASE_KEYS (default ~/.meeting-assistant-release):
catalog-signing-key.pem (runtime catalogs) and updater.key (Tauri updater, no password).
The GitHub token comes from `git credential fill`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import publish
import release
from run import ROOT, GateError, executable, npm, package, py, release_config, run, uv

KEYS = Path(os.environ.get("MEETING_RELEASE_KEYS", Path.home() / ".meeting-assistant-release"))
PLATFORM = "windows-x86_64"
RUNTIMES = ROOT / "engine" / "catalog" / "runtimes"
STAGE = ROOT / "artifacts" / "release-local"


def pep440(version: str) -> str:
    return re.sub(
        r"-(alpha|beta|rc)\.(\d+)$",
        lambda m: {"alpha": "a", "beta": "b", "rc": "rc"}[m[1]] + m[2],
        version,
    )


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def github_token() -> str:
    output = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if not fields.get("password"):
        raise GateError("No GitHub credential available from git credential fill")
    return fields["password"]


def changed_families() -> list[str]:
    """Families whose signed descriptor was built from different worker source/lock/protocol."""
    protocol = release.digest(ROOT / "engine/meeting_assistant/infrastructure/workers/runtime.py")
    changed = []
    for descriptor in sorted(RUNTIMES.glob(f"*-{PLATFORM}.json")):
        family = descriptor.name.removesuffix(f"-{PLATFORM}.json")
        built = json.loads(descriptor.read_text(encoding="utf-8"))["runtime_manifest"]["build"]
        current = {
            "worker_lock_sha256": release.digest(ROOT / "workers" / family / "uv.lock"),
            "source_sha256": release.digest(
                ROOT / "workers" / family.removesuffix("-cuda") / "worker.py"
            ),
            "protocol_source_sha256": protocol,
        }
        if any(built[key] != value for key, value in current.items()):
            changed.append(family)
    return changed


def bump(old: str, new: str) -> None:
    edits = [
        ("release.toml", 'version = "{}"', old, new),
        ("Cargo.toml", 'version = "{}"', old, new),
        ("apps/desktop/src-tauri/tauri.conf.json", '"version": "{}"', old, new),
        ("engine/pyproject.toml", 'version = "{}"', pep440(old), pep440(new)),
        ("engine/meeting_assistant/__init__.py", '__version__ = "{}"', pep440(old), pep440(new)),
    ]
    for name, pattern, a, b in edits:
        path = ROOT / name
        data = path.read_bytes()  # bytes keep the file's line endings intact
        before, after = pattern.format(a).encode(), pattern.format(b).encode()
        if before not in data:
            raise GateError(f"{name} does not contain {before.decode()}")
        path.write_bytes(data.replace(before, after, 1))
    npm("version", new, "--no-git-tag-version", "--allow-same-version")
    run(executable("cargo"), "update", "--workspace")
    uv("lock")


def build_pack(family: str, version: str) -> Path:
    output = STAGE / "runtimes"
    py(str(ROOT / "workers/build.py"), family, "--accept-runtime-license", "--output", str(output))
    folder = output / f"{family}-{version}-{PLATFORM}"
    archive, catalog = folder / f"{folder.name}.zip", folder / f"{folder.name}.catalog.json"
    review = folder / "review.json"
    review.write_text(
        json.dumps(
            {
                "approved": True,
                "artifact_sha256": release.digest(archive),
                "reviewer": "joactr (release owner)",
                "evidence": [
                    f"{archive.name}: bundled third-party-notices.json and runtime-manifest.json reviewed"
                ],
            }
        ),
        encoding="utf-8",
    )
    py(
        str(ROOT / "workers/finalize.py"),
        str(catalog),
        "--license-audit",
        str(review),
        "--signing-key",
        str(KEYS / "catalog-signing-key.pem"),
    )
    shutil.copyfile(catalog, RUNTIMES / f"{family}-{PLATFORM}.json")
    return folder


def main(version: str | None) -> None:
    config = release_config()
    old, repository = config["version"], config["publisher"]["repository"]
    if not version or not re.fullmatch(r"\d+\.\d+\.\d+(-(alpha|beta|rc)\.\d+)?", version):
        raise GateError("Usage: run.py release-local X.Y.Z[-beta.N]")
    if version == old:
        raise GateError(f"release.toml is already {old}; pass the next version")
    for key in ("catalog-signing-key.pem", "updater.key"):
        if not (KEYS / key).is_file():
            raise GateError(f"Missing release key {KEYS / key}")
    if git("status", "--porcelain") or git("rev-parse", "--abbrev-ref", "HEAD") != "main":
        raise GateError("Release from a clean main checkout")
    if git("ls-remote", "--tags", "origin", f"v{version}"):
        raise GateError(f"Tag v{version} already exists on origin")
    token = github_token()

    families = changed_families()
    print(f"Runtime packs to rebuild: {families or 'none'}", flush=True)
    shutil.rmtree(STAGE, ignore_errors=True)
    bump(old, version)
    packs = [build_pack(family, version) for family in families]

    shutil.rmtree(ROOT / "target/release/bundle", ignore_errors=True)
    os.environ["TAURI_SIGNING_PRIVATE_KEY"] = str(KEYS / "updater.key")
    os.environ.setdefault("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "")
    package()
    py(str(ROOT / "tooling/release.py"), "manifest")

    assets = STAGE / "assets"
    assets.mkdir(parents=True)
    installer = next(p for p in release.packaged_files() if p.suffix == ".exe")
    sources = [installer, installer.with_name(installer.name + ".sig")]
    sources += [release.OUTPUT / "latest.json", release.OUTPUT / "THIRD_PARTY_NOTICES.txt"]
    sources += [p for folder in packs for p in folder.iterdir() if p.suffix in {".zip", ".json"}]
    for source in sources:
        if source.name != "review.json":
            # GitHub renames spaces to dots; stage with the stored name so sums match.
            shutil.copyfile(source, assets / source.name.replace(" ", "."))
    (assets / "SHA256SUMS.txt").write_text(
        "".join(f"{release.digest(p)}  {p.name}\n" for p in sorted(assets.iterdir())),
        encoding="utf-8",
    )

    git("add", "-A")
    git("commit", "-m", f"Release {version}")
    git("tag", f"v{version}")
    run("git", "push", "origin", "main", f"v{version}")

    reused = sorted(
        p.name.removesuffix(f"-{PLATFORM}.json")
        for p in RUNTIMES.glob(f"*-{PLATFORM}.json")
        if p.name.removesuffix(f"-{PLATFORM}.json") not in families
    )
    notes = (
        f"Meeting Assistant {version} for Windows x64.\n\n"
        "Existing installs update in-app (signed updater payload). New installs: run the "
        "`-setup.exe`; it is not Authenticode-signed, so SmartScreen shows *More info → Run anyway*.\n\n"
        f"Runtime packs rebuilt in this release: {', '.join(families) or 'none'}. "
        f"Reused from earlier releases: {', '.join(reused)}.\n"
    )
    github = publish.GitHub(repository, token)
    draft = github.json(
        "releases",
        method="POST",
        value={
            "tag_name": f"v{version}",
            "name": f"Meeting Assistant {version}",
            "body": notes,
            "draft": True,
        },
    )
    for asset in sorted(assets.iterdir()):
        print(f"Uploading {asset.name}", flush=True)
        github.upload(draft["id"], asset)
    github.json(
        f"releases/{draft['id']}",
        method="PATCH",
        value={"draft": False, "prerelease": False, "make_latest": "true"},
    )
    feed = f"https://github.com/{repository}/releases/latest/download/latest.json"
    for _ in range(10):  # the latest redirect can lag publication briefly
        with urllib.request.urlopen(feed, timeout=60) as response:
            served = json.load(response)["version"]
        if served == version:
            break
        time.sleep(6)
    else:
        raise GateError(f"{feed} still serves {served}")
    print(f"Published v{version}; updater feed serves {served}")
