"""Prepare only bundled, pinned native DSP dependencies; no optional model downloads.

Run explicitly: python tooling/native_prepare.py --download
Offline runs omit --download and require the verified cache. This builds dependency
libraries/helpers, not tests. macOS/Linux implementations remain experimental until
hardware acceptance records exist. Python, CMake and the platform C toolchain are
build-machine prerequisites, never installed-app runtime prerequisites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "crates/dsp/native-dependencies.json"
CACHE = ROOT / "crates/dsp/.cache"
OUT = ROOT / "dist/native"


def acquire(name: str, item: dict, download: bool) -> Path:
    path = CACHE / name
    if not path.exists():
        if not download:
            raise RuntimeError(f"Bundled dependency {name} missing: rerun with explicit --download")
        request = urllib.request.Request(
            item["url"], headers={"User-Agent": "MeetingAssistant-native-build/1"}
        )
        part = path.with_suffix(path.suffix + ".part")
        with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as file:
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > item["bytes"]:
                    raise RuntimeError(f"Oversize dependency: {name}")
                file.write(chunk)
        part.replace(path)
    if (
        path.stat().st_size != item["bytes"]
        or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
    ):
        raise RuntimeError(
            f"Dependency hash/size mismatch: {name}; existing cache retained for inspection"
        )
    return path


def safe_extract(path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)

    def target(name: str) -> Path:
        if "\\" in name or ":" in name or name.startswith("/"):
            raise RuntimeError("Unsafe native dependency archive path")
        result = (destination / name).resolve()
        if not result.is_relative_to(destination.resolve()):
            raise RuntimeError("Native dependency archive traversal")
        return result

    total = 0
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                dest = target(entry.filename)
                total += entry.file_size
                if total > 512 * 1024 * 1024 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise RuntimeError("Unsafe native dependency archive")
                if entry.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry) as source, dest.open("wb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(path) as archive:
            for entry in archive:
                dest = target(entry.name)
                total += entry.size
                if total > 512 * 1024 * 1024 or not (
                    entry.isfile() or entry.isdir() or entry.issym()
                ):
                    raise RuntimeError("Unsafe native dependency archive")
                if entry.isdir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif entry.isfile():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(entry)
                    if source is None:
                        raise RuntimeError("Missing dependency archive member")
                    with source, dest.open("wb") as output:
                        shutil.copyfileobj(source, output)
                else:
                    # Runtime aliases are not needed: only real library bytes are copied.
                    target(str(Path(entry.name).parent / entry.linkname))


def run(arguments: list[str], cwd: Path = ROOT) -> None:
    subprocess.run(arguments, cwd=cwd, check=True)


def prepare(download: bool) -> dict:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    dsp = OUT / "dsp"
    bin_dir = OUT / "bin"
    dsp.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    artifacts = manifest["artifacts"]
    for name in ("silero_vad.onnx", "SILERO-LICENSE.txt"):
        shutil.copy2(acquire(name, artifacts[name], download), dsp / name)
    system, machine = platform.system(), platform.machine().lower()
    target = {
        ("Windows", "amd64"): "win-x64",
        ("Windows", "x86_64"): "win-x64",
        ("Darwin", "arm64"): "osx-arm64",
        ("Darwin", "x86_64"): "osx-x86_64",
        ("Linux", "x86_64"): "linux-x64",
    }.get((system, machine))
    if target is None:
        raise RuntimeError(f"No audited native dependency archive for {system}/{machine}")
    name = f"onnxruntime-{target}-1.22.0.{'zip' if system == 'Windows' else 'tgz'}"
    extraction = CACHE / f"onnxruntime-{target}"
    safe_extract(acquire(name, artifacts[name], download), extraction)
    libraries = (
        list(extraction.glob("**/lib/onnxruntime.dll"))
        if system == "Windows"
        else list(
            extraction.glob(
                "**/lib/libonnxruntime*.dylib"
                if system == "Darwin"
                else "**/lib/libonnxruntime.so.*"
            )
        )
    )
    libraries = [path for path in libraries if path.is_file() and not path.is_symlink()]
    if not libraries:
        raise RuntimeError("Audited ONNX Runtime library absent from archive")
    library_name = {
        "Windows": "onnxruntime.dll",
        "Darwin": "libonnxruntime.dylib",
        "Linux": "libonnxruntime.so",
    }[system]
    shutil.copy2(max(libraries, key=lambda path: path.stat().st_size), dsp / library_name)
    for notice in ("LICENSE", "ThirdPartyNotices.txt"):
        matches = list(extraction.glob(f"**/{notice}"))
        if not matches:
            raise RuntimeError(f"ONNX Runtime mandatory notice missing: {notice}")
        shutil.copy2(matches[0], dsp / f"ONNXRUNTIME-{notice}")
    speex_archive = acquire("speexdsp-1.2.1.tar.gz", artifacts["speexdsp-1.2.1.tar.gz"], download)
    speex_extract = CACHE / "speex-source"
    safe_extract(speex_archive, speex_extract)
    source = next(speex_extract.glob("speexdsp-*"))
    build = CACHE / f"speex-build-{target}"
    run(
        [
            "cmake",
            "-S",
            (ROOT / "crates/dsp/native").as_posix(),
            "-B",
            build.as_posix(),
            f"-DSPEEX_SOURCE={source.as_posix()}",
            f"-DCMAKE_INSTALL_PREFIX={dsp.as_posix()}",
            "-DCMAKE_BUILD_TYPE=Release",
        ]
    )
    run(["cmake", "--build", str(build), "--config", "Release"])
    run(["cmake", "--install", str(build), "--config", "Release"])
    shutil.copy2(source / "COPYING", dsp / "SPEEXDSP-COPYING")
    if system == "Darwin":
        native = ROOT / "crates/audio-macos/native"
        ring = CACHE / "macos-ring.o"
        run(["xcrun", "clang", "-std=c11", "-O2", "-c", str(native / "ring.c"), "-o", str(ring)])
        run(
            [
                "xcrun",
                "swiftc",
                "-parse-as-library",
                "-O",
                "-import-objc-header",
                str(native / "ring.h"),
                str(native / "main.swift"),
                str(ring),
                "-framework",
                "AVFoundation",
                "-framework",
                "CoreAudio",
                "-framework",
                "ScreenCaptureKit",
                "-o",
                str(bin_dir / "meeting-audio-macos"),
            ]
        )
    elif system == "Linux":
        flags = subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libpipewire-0.3"], text=True
        ).split()
        run(
            [
                "cc",
                "-std=c11",
                "-D_DEFAULT_SOURCE",
                "-O2",
                str(ROOT / "crates/audio-pipewire/native/main.c"),
                "-pthread",
                *flags,
                "-o",
                str(bin_dir / "meeting-audio-pipewire"),
            ]
        )
    # Ensure resources contains a genuine audited manifest on Windows as well;
    # there is no capture helper because WASAPI is linked into the Rust executable.
    provenance = bin_dir / "native-provenance.json"
    result = {
        "schema_version": 1,
        "platform": target,
        "experimental": system != "Windows",
        "dependencies": manifest,
        "outputs": {
            str(path.relative_to(OUT)): {
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in OUT.rglob("*")
            if path.is_file() and path != provenance
        },
    }
    provenance.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--download",
        action="store_true",
        help="Consent to public acquisition of only pinned bundled native dependencies",
    )
    args = parser.parse_args()
    print(json.dumps(prepare(args.download), indent=2))


if __name__ == "__main__":
    main()
