"""Cross-platform developer commands; every gate propagates real failures."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENGINE = ROOT / "engine"
DESKTOP = ROOT / "apps" / "desktop"
PYCONFIG = str(ENGINE / "pyproject.toml")


class GateError(RuntimeError):
    pass


def environment() -> dict[str, str]:
    env = os.environ.copy()
    paths = [
        ENGINE / ".venv" / ("Scripts" if os.name == "nt" else "bin"),
        ROOT / ".tools" / "node" / "node-v24.22.1-win-x64",
        ROOT / ".tools" / "python" / "bin",
        ROOT / ".tools" / "just",
    ]
    if os.name == "nt":
        # Freezers must resolve the system VC runtime before unrelated SDK copies.
        paths.append(Path(os.environ["SYSTEMROOT"]) / "System32")
    env["PATH"] = (
        os.pathsep.join(str(p) for p in paths if p.exists()) + os.pathsep + env.get("PATH", "")
    )
    env["PYTHONPATH"] = str(ENGINE)
    env["UV_PROJECT"] = str(ENGINE)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYANNOTE_METRICS_ENABLED"] = "0"
    browser_cache = ROOT / ".tools" / "playwright"
    if browser_cache.exists():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
    return env


def executable(name: str) -> str:
    found = shutil.which(name, path=environment()["PATH"])
    if found is None:
        raise GateError(f"Required executable unavailable: {name}")
    return found


def run(*args: str, cwd: Path = ROOT, timeout: int = 1800) -> None:
    print("+ " + subprocess.list2cmdline(args), flush=True)
    subprocess.run(args, cwd=cwd, env=environment(), timeout=timeout, check=True)


def uv(*args: str) -> None:
    run(executable("uv"), *args)


def py(*args: str) -> None:
    uv("run", "--frozen", "python", *args)


def npm(*args: str) -> None:
    # Invoke npm-cli.js through node: npm.cmd via cmd.exe breaks on paths containing spaces.
    node = Path(executable("node"))
    cli = node.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not cli.is_file():  # POSIX layout: <prefix>/bin/node, <prefix>/lib/node_modules
        cli = node.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"
    run(str(node), str(cli), *args, cwd=DESKTOP)


def release_config() -> dict:
    return tomllib.loads((ROOT / "release.toml").read_text(encoding="utf-8"))


def bootstrap() -> None:
    if sys.version_info[:2] != (3, 12):
        raise GateError("Python 3.12 is required")
    node_version = subprocess.check_output(
        [executable("node"), "--version"], env=environment(), text=True
    ).strip()
    required_node = "v" + (DESKTOP / ".node-version").read_text().strip()
    if node_version != required_node:
        raise GateError(f"Node mismatch: expected {required_node}, found {node_version}")
    for lock in (ENGINE / "uv.lock", DESKTOP / "package-lock.json", ROOT / "Cargo.lock"):
        if not lock.is_file():
            raise GateError(
                f"Missing committed lockfile: {lock.relative_to(ROOT)}; resolve dependencies explicitly before bootstrap"
            )
    uv("sync", "--frozen")
    npm("ci")
    run(executable("cargo"), "fetch", "--locked")
    generate()
    py(str(ROOT / "tooling" / "native_prepare.py"), "--download")


def generate() -> None:
    py("-m", "meeting_assistant.contracts", "generate")


def check() -> None:
    py("-m", "meeting_assistant.contracts", "check")
    ruff = ("run", "--frozen", "ruff")
    uv(*ruff, "check", "--config", PYCONFIG, "engine", "tooling", "tests", "workers")
    uv(*ruff, "format", "--config", PYCONFIG, "--check", "engine", "tooling", "tests", "workers")
    uv("run", "--frozen", "pyright", "--project", PYCONFIG)
    npm("run", "check")
    run(executable("cargo"), "fmt", "--all", "--", "--check")
    run(
        executable("cargo"),
        "clippy",
        "--workspace",
        "--all-targets",
        "--locked",
        "--",
        "-D",
        "warnings",
    )


def test() -> None:
    uv("run", "--frozen", "pytest", "-c", PYCONFIG, "--rootdir", str(ROOT), "tests", "-q")
    npm("run", "test")
    run(executable("cargo"), "test", "--workspace", "--locked")


def smoke() -> None:
    py(str(ROOT / "tooling" / "smoke.py"))


def audit() -> None:
    py(str(ROOT / "tooling" / "audit.py"))


def build() -> None:
    py(str(ROOT / "tooling" / "native_prepare.py"))
    generate()
    npm("run", "build")
    # Freeze only the lightweight engine; optional workers have independent locks.
    uv(
        "run",
        "--frozen",
        "pyinstaller",
        "--noconfirm",
        "--clean",
        str(ROOT / "tooling" / "engine.spec"),
    )
    run(
        executable("cargo"),
        "build",
        "--workspace",
        "--release",
        "--locked",
        "--features",
        "tauri/custom-protocol",
    )


def package() -> None:
    build()
    py(str(ROOT / "tooling" / "release.py"), "dependencies")
    arguments = [
        "exec",
        "--",
        "tauri",
        "build",
        "--bundles",
        "nsis" if os.name == "nt" else "appimage" if sys.platform.startswith("linux") else "dmg",
    ]
    config = release_config()
    publisher = config.get("publisher", {})
    updates = config.get("updates", {})
    thumbprint = os.environ.get("WINDOWS_CERTIFICATE_THUMBPRINT")
    if os.name == "nt" and not thumbprint:
        if updates.get("enabled", False):
            raise GateError(
                "Update-enabled Windows packaging requires WINDOWS_CERTIFICATE_THUMBPRINT and its provisioned private key"
            )
        print(
            "WARNING: WINDOWS_CERTIFICATE_THUMBPRINT is unset; building an UNSIGNED developer installer. "
            "release-check rejects unsigned artifacts."
        )
    overrides: dict = {
        "bundle": {"createUpdaterArtifacts": bool(updates.get("enabled", False))},
        "plugins": {"updater": {"pubkey": updates.get("public_key", "")}},
    }
    if publisher.get("bundle_identifier"):
        overrides["identifier"] = publisher["bundle_identifier"]
    if os.name == "nt" and thumbprint:
        overrides["bundle"]["windows"] = {
            "certificateThumbprint": thumbprint,
            "digestAlgorithm": "sha256",
            "timestampUrl": "http://timestamp.digicert.com",
        }
    with tempfile.TemporaryDirectory(prefix="meeting-assistant-packaging-") as directory:
        bundle_config = Path(directory) / "bundle.json"
        bundle_config.write_text(json.dumps(overrides), encoding="utf-8")
        npm(*arguments, "--config", str(bundle_config))
    py(str(ROOT / "tooling" / "release.py"), "inventory")


def release_check() -> None:
    py(str(ROOT / "tooling" / "release.py"), "check")


def dev_engine() -> None:
    with tempfile.TemporaryDirectory(prefix="meeting-assistant-dev-") as directory:
        py("-m", "meeting_assistant", "serve", "--workspace", directory)


def dev() -> None:
    npm("exec", "--", "tauri", "dev")


def fetch_model(model: str) -> None:
    py("-m", "meeting_assistant.application.inference.catalog_cli", "fetch", model)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=[
            "bootstrap",
            "generate",
            "check",
            "test",
            "smoke",
            "audit",
            "build",
            "package",
            "release-check",
            "dev",
            "dev-engine",
            "fetch-model",
        ],
    )
    parser.add_argument("model", nargs="?")
    args = parser.parse_args()
    ARTIFACTS.mkdir(exist_ok=True)
    started = time.time()
    status = 1
    error = None
    try:
        if args.command == "fetch-model":
            if not args.model:
                raise GateError("fetch-model requires a catalog model ID")
            fetch_model(args.model)
        else:
            globals()[args.command.replace("-", "_")]()
        status = 0
    except (GateError, subprocess.SubprocessError, OSError) as exc:
        error = str(exc)
        print(f"GATE FAILED: {error}", file=sys.stderr)
    finally:
        record = {
            "command": args.command,
            "started_at": started,
            "finished_at": time.time(),
            "exit_code": status,
            "error": error,
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        with (ARTIFACTS / "commands.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
