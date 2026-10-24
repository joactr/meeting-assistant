"""Build a target-native isolated CPU (or optional CUDA) runtime from its frozen worker lock.

Usage: python workers/build.py whisper --accept-runtime-license --output dist/workers
       python workers/build.py whisper-cuda --accept-runtime-license --output dist/workers
Use workers/finalize.py afterward for artifact-bound review and signing. Builds
are unsigned developer artifacts until that separate approval succeeds.
No weights are acquired and nothing is published by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import zipfile
from pathlib import Path

FAMILIES = {
    "whisper": ["faster_whisper", "ctranslate2", "av"],
    # Optional GPU variant of the same worker protocol/source; CUDA libraries are never in the installer.
    "whisper-cuda": [
        "faster_whisper",
        "ctranslate2",
        "av",
        "nvidia.cublas",
        "nvidia.cudnn",
        "nvidia.cuda_nvrtc",
    ],
    "sherpa": ["sherpa_onnx"],
    "phonon": ["fermion", "torch"],
    "pyannote": [
        "pyannote.audio",
        "pyannote.core",
        "pyannote.pipeline",
        "torch",
        "torchaudio",
        "torchcodec",
    ],
    "embeddings": ["onnxruntime", "tokenizers"],
    "azure-speech": ["azure.cognitiveservices.speech"],
}


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family", choices=FAMILIES)
    parser.add_argument("--accept-runtime-license", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument(
        "--repository", help="Actual public GitHub owner/repository; defaults to release.toml"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    import tomllib

    release = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
    primary = json.loads(
        (root / "engine/catalog/runtimes" / (args.family + ".json")).read_text(encoding="utf-8")
    )
    args.version = args.version or release["version"]
    repository = args.repository or release.get("publisher", {}).get("repository")
    if repository and (
        repository.count("/") != 1
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-"
            for c in repository
        )
    ):
        parser.error("Repository must be an actual owner/repository identifier")
    family = root / "workers" / args.family
    runtime_family = args.family.removesuffix("-cuda")
    gpu_variant = args.family.endswith("-cuda")
    target = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(platform.system())
    architecture = {
        "AMD64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(platform.machine())
    if target is None or architecture is None:
        parser.error("Unsupported native build platform")
    platform_id = f"{target}-{architecture}"
    if platform_id not in primary["platforms"]:
        parser.error(
            primary.get("unsupported_platforms", {}).get(
                platform_id, "Runtime is not supported on this platform"
            )
        )
    output = args.output.resolve() / f"{args.family}-{args.version}-{platform_id}"
    if output.exists():
        parser.error("Build destination already exists; refusing overwrite")
    output.mkdir(parents=True)
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv must be provisioned before an explicit developer build")
    environment = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    if os.name == "nt":
        # PyInstaller otherwise picks stale VC DLLs from unrelated PATH entries.
        environment["PATH"] = (
            str(Path(os.environ["SYSTEMROOT"]) / "System32")
            + os.pathsep
            + environment.get("PATH", "")
        )
    subprocess.run(
        [uv, "sync", "--project", str(family), "--frozen", "--group", "build"],
        check=True,
        cwd=root,
        env=environment,
    )
    worker_source = root / "workers" / runtime_family / "worker.py"
    command = [
        uv,
        "run",
        "--project",
        str(family),
        "--frozen",
        "--group",
        "build",
        "python",
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onedir",
        "--name",
        f"meeting-{runtime_family}",
        "--paths",
        str(root / "engine"),
        "--distpath",
        str(output / "package"),
        "--workpath",
        str(output / "build"),
        "--specpath",
        str(output / "spec"),
    ]
    for module in FAMILIES[args.family]:
        command.extend(["--collect-all", module])
    command.extend(["--collect-all", "numpy", str(worker_source)])
    subprocess.run(command, check=True, cwd=root, env=environment)
    package = output / "package" / f"meeting-{runtime_family}"
    notices = subprocess.run(
        [
            uv,
            "run",
            "--project",
            str(family),
            "--frozen",
            "--group",
            "build",
            "python",
            str(root / "workers/notices.py"),
        ],
        check=True,
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    (package / "third-party-notices.json").write_text(notices.stdout, encoding="utf-8")
    # Plain-text copy so redistributed terms (e.g. NVIDIA's EULA) are readable without tooling.
    (package / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n\n".join(
            f"=== {entry['name']} {entry['version']}: {entry['path']} ===\n\n{entry['text']}"
            for entry in (
                {**package_notice, **file}
                for package_notice in json.loads(notices.stdout)["packages"]
                for file in package_notice["license_files"]
            )
        ),
        encoding="utf-8",
    )
    executable = package / (
        f"meeting-{runtime_family}.exe" if target == "windows" else f"meeting-{runtime_family}"
    )
    files = [
        {
            "path": path.relative_to(package).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for path in sorted(package.rglob("*"))
        if path.is_file()
    ]
    manifest = {
        "schema_version": 1,
        "family": runtime_family,
        "version": args.version,
        "platform": platform_id,
        "protocol_major": 1,
        "executable": executable.name,
        "executable_sha256": digest(executable),
        "files": files,
        "build": {
            "python": platform.python_version(),
            "worker_lock_sha256": digest(family / "uv.lock"),
            "source_sha256": digest(worker_source),
            "protocol_source_sha256": digest(
                root / "engine/meeting_assistant/infrastructure/workers/runtime.py"
            ),
        },
        "weights_included": False,
        "platform_validation": "requires real target smoke"
        if target == "windows"
        else "experimental-untested",
    }
    (package / "runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    archive = output / (output.name + ".zip")
    with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(package).as_posix())
    asset_url = (
        f"https://github.com/{repository}/releases/download/v{args.version}/{archive.name}"
        if repository
        else None
    )
    blockers = []
    if not asset_url:
        blockers.append("Actual public GitHub repository is not configured")
    blockers.append(
        "Artifact-level license/codec redistribution review and separate catalog signing are required"
    )
    descriptor = {
        "schema_version": 1,
        "id": f"runtime-{args.family}-{platform_id}",
        "role": "runtime",
        "revision": args.version,
        "source": "target-native frozen-lock source build",
        "files": [
            {
                "path": archive.name,
                "url": asset_url,
                "bytes": archive.stat().st_size,
                "sha256": digest(archive),
            }
        ],
        "runtime_manifest": manifest,
        "license": primary["license"],
        "license_audit": None,
        "transformations": [
            "PyInstaller isolated CUDA 12 executable with redistributed NVIDIA cuBLAS/cuDNN/NVRTC; no weights"
            if gpu_variant
            else "PyInstaller isolated CPU executable"
        ],
        "gating": {"required": False},
        "platforms": [platform_id],
        "runtime": {"family": runtime_family, "version_range": ">=1.0.0,<2.0.0"},
        "minimum_resources": {
            "free_disk_bytes": sum(item["bytes"] for item in files) * 3,
            "ram_bytes": None,
        },
        "capability_profile": {"protocol_major": 1, "gpu_variant": gpu_variant},
        "signature": None,
        "blocked_reason": "; ".join(blockers),
    }
    catalog_path = output / (output.name + ".catalog.json")
    catalog_path.write_text(json.dumps(descriptor, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output),
                "archive_sha256": digest(archive),
                "catalog": str(catalog_path),
                "signed": False,
                "release_ready": False,
            }
        )
    )


if __name__ == "__main__":
    main()
