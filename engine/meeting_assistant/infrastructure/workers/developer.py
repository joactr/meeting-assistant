"""Explicit first-party developer grants, separate from the signed runtime catalog.

The registry is native code trusted by the account, not an upload/import format.
Publisher-configured builds cannot use it. No archive is extracted or discovered.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ..providers.contracts import ProviderError
from ..resources import resource_root

REGISTRY_ENV = "MEETING_ASSISTANT_DEVELOPER_WORKER_REGISTRY"
FAMILIES = {"whisper", "sherpa", "pyannote", "embeddings", "azure-speech"}
TRUST = "first-party-developer-built-local"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-[0-9A-Za-z.-]+)?\Z")


def denied(message: str) -> ProviderError:
    return ProviderError("permission", message)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise denied("Developer artifact metadata grant is invalid")
    try:
        value = json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise denied("Developer artifact metadata is invalid") from None
    if not isinstance(value, dict):
        raise denied("Developer artifact metadata must be an object")
    return cast(dict[str, Any], value)


def private(path: Path, *, establish: bool = False) -> None:
    """Require owner-only storage; Windows chmod alone does not restrict a DACL."""
    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
        raise denied("Developer registry cannot contain links or junctions")
    if os.name != "nt":
        if establish:
            path.chmod(0o700 if path.is_dir() or os.access(path, os.X_OK) else 0o600)
        if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise denied("Developer registry must be private to the current account")
        return
    # Protected DACL: owner rights and SYSTEM only, inheritable by copied files.
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    convert.restype = wintypes.BOOL
    render = security.ConvertSecurityDescriptorToStringSecurityDescriptorW
    render.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    render.restype = wintypes.BOOL
    set_security = security.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    set_security.restype = wintypes.BOOL
    get_security = security.GetFileSecurityW
    get_security.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    get_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    expected = "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
    if establish:
        descriptor = ctypes.c_void_p()
        if not convert(expected, 1, ctypes.byref(descriptor), None):
            raise denied("Cannot construct private developer registry permissions")
        try:
            if not set_security(str(path), 0x80000004, descriptor):
                raise denied("Cannot protect developer registry permissions")
        finally:
            kernel.LocalFree(descriptor)
    size = wintypes.DWORD()
    get_security(str(path), 4, None, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if not get_security(str(path), 4, buffer, size.value, ctypes.byref(size)):
        raise denied("Cannot inspect developer registry permissions")
    rendered = ctypes.c_void_p()
    if not render(buffer, 1, 4, ctypes.byref(rendered), None):
        raise denied("Cannot inspect developer registry DACL")
    try:
        pointer = rendered.value
        if not pointer or ctypes.wstring_at(pointer) != expected:
            raise denied("Developer registry must retain its owner-only protected DACL")
    finally:
        kernel.LocalFree(rendered)


def provenance(family: str, lock_sha256: str | None = None) -> dict[str, str]:
    if family not in FAMILIES:
        raise denied("Unknown first-party worker family")
    root = resource_root()
    release = tomllib.loads((root / "release.toml").read_text(encoding="utf-8"))
    if release.get("publisher", {}).get("repository"):
        raise denied("Developer workers are disabled in publisher-configured builds")
    if getattr(sys, "frozen", False):
        sources = root / "developer-worker-sources"
        worker = sources / family / "worker.py"
        locks = [sources / name / "uv.lock" for name in (family, family + "-cuda")]
        protocol = sources / "runtime.py"
    else:
        worker = root / "workers" / family / "worker.py"
        locks = [root / "workers" / name / "uv.lock" for name in (family, family + "-cuda")]
        protocol = root / "engine/meeting_assistant/infrastructure/workers/runtime.py"
    # A hardware variant shares the worker source/protocol but has its own frozen lock.
    present = [path for path in locks if path.is_file() and not path.is_symlink()]
    lock = next((path for path in present if digest(path) == lock_sha256), None) or (
        present[0] if present and present[0] == locks[0] else None
    )
    if lock is None or not all(
        path.is_file() and not path.is_symlink() for path in (worker, lock, protocol)
    ):
        raise denied("This build does not include first-party developer worker provenance")
    return {
        "source_sha256": digest(worker),
        "worker_lock_sha256": digest(lock),
        "protocol_source_sha256": digest(protocol),
        "version": release["version"],
    }


def registry_path(raw: str | Path, workspace: Path) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise denied("Developer registry requires an explicit absolute launch path")
    resolved = path.resolve()
    if resolved.is_relative_to(workspace.resolve()) or workspace.resolve().is_relative_to(resolved):
        raise denied("Developer native-code grants must be separate from workspace/upload data")
    if path != resolved:
        raise denied("Developer registry path must be canonical and cannot traverse links")
    return resolved


def launch_registry(workspace: Path) -> Path | None:
    raw = os.environ.get(REGISTRY_ENV)
    return registry_path(raw, workspace) if raw else None


def target() -> str:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}.get(platform.system())
    machine = {"AMD64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(
        platform.machine()
    )
    return f"{system}-{machine}"


def inventory(root: Path, files: Any, *, metadata: set[str]) -> None:
    if not isinstance(files, list):
        raise denied("Developer artifact requires an exact bounded file inventory")
    entries = cast(list[Any], files)
    if not entries or len(entries) > 20000:
        raise denied("Developer artifact requires an exact bounded file inventory")
    expected: set[str] = set()
    folded: set[str] = set()
    for raw_item in entries:
        if not isinstance(raw_item, dict):
            raise denied("Invalid artifact file inventory")
        item = cast(dict[str, Any], raw_item)
        name = item.get("path")
        if not isinstance(name, str):
            raise denied("Invalid artifact relative file path")
        relative = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or ":" in name
            or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in name.split("/"))
            or name.casefold() in folded
            or name in metadata
        ):
            raise denied("Artifact inventory contains unsafe or duplicate paths")
        expected.add(name)
        folded.add(name.casefold())
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            raise denied("Artifact file escapes or is missing from its grant")
        if (
            type(item.get("bytes")) is not int
            or item["bytes"] < 0
            or not _HASH.fullmatch(str(item.get("sha256", "")))
        ):
            raise denied("Artifact inventory requires exact sizes and SHA256 hashes")
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise denied("Developer artifact file was modified")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise denied("Artifact grants cannot contain links or junctions")
        if path.is_file():
            name = path.relative_to(root).as_posix()
            if name not in metadata:
                actual.add(name)
    if actual != expected:
        raise denied("Artifact contains unregistered files")


def validate_manifest(package: Path) -> dict[str, Any]:
    manifest = document(package / "runtime-manifest.json")
    required = {
        "schema_version",
        "family",
        "version",
        "platform",
        "protocol_major",
        "executable",
        "executable_sha256",
        "files",
        "build",
        "weights_included",
        "platform_validation",
    }
    if (
        set(manifest) != required
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or type(manifest["protocol_major"]) is not int
        or manifest["protocol_major"] != 1
        or manifest["weights_included"] is not False
    ):
        raise denied("Unsupported first-party runtime manifest schema/protocol")
    build_value = manifest["build"]
    if not isinstance(build_value, dict):
        raise denied("Missing first-party frozen-lock build provenance")
    build = cast(dict[str, Any], build_value)
    first_party = provenance(manifest["family"], str(build.get("worker_lock_sha256")))
    if set(build) != {
        "python",
        "worker_lock_sha256",
        "source_sha256",
        "protocol_source_sha256",
    }:
        raise denied("Missing first-party frozen-lock build provenance")
    if re.fullmatch(r"3\.12\.\d+", str(build["python"])) is None or any(
        build[key] != first_party[key] for key in first_party if key != "version"
    ):
        raise denied("Runtime does not match this build's first-party worker source/lock/protocol")
    if manifest["version"] != first_party["version"] or manifest["platform"] != target():
        raise denied("Developer worker version/target does not match this application build")
    executable = "meeting-" + manifest["family"] + (".exe" if os.name == "nt" else "")
    if manifest["executable"] != executable:
        raise denied("Only the named first-party frozen worker executable is permitted")
    inventory(package, manifest["files"], metadata={"runtime-manifest.json"})
    if not any(
        item["path"] == executable and item["sha256"] == manifest["executable_sha256"]
        for item in manifest["files"]
    ):
        raise denied("Worker executable hash is absent from the exact file inventory")
    return manifest


def compatible(version: str, expression: str) -> bool:
    """Developer prereleases use their release triple; never a release approval."""
    match = _VERSION.fullmatch(version)
    if match is None:
        return False
    current = tuple(map(int, match.groups()))
    for clause in expression.split(","):
        bound = re.fullmatch(r"\s*(>=|<=|>|<|==)(\d+)\.(\d+)\.(\d+)\s*", clause)
        if bound is None:
            return False
        op, *parts = bound.groups()
        other = tuple(map(int, parts))
        if not {
            ">=": current >= other,
            "<=": current <= other,
            ">": current > other,
            "<": current < other,
            "==": current == other,
        }[op]:
            return False
    return True


def model_grant(
    install_root: Path, catalog_id: str, revision: str, manifest: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9-]*", catalog_id)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", revision)
        or revision in {".", ".."}
    ):
        raise denied("Developer worker requires an explicit reviewed model id/revision")
    reviewed = document(resource_root() / "engine/catalog/models" / (catalog_id + ".json"))
    if (
        reviewed.get("role") == "runtime"
        or reviewed.get("trust")
        or reviewed.get("revision") != revision
        or reviewed.get("blocked_reason")
    ):
        raise denied("Developer registration requires a bundled reviewed model revision")
    runtime = reviewed.get("runtime", {})
    if (
        runtime.get("family") != manifest["family"]
        or manifest["platform"] not in reviewed.get("platforms", [])
        or not compatible(manifest["version"], runtime.get("version_range", ""))
    ):
        raise denied(
            "Selected model is incompatible with the developer runtime family/version/target"
        )
    folder = install_root / catalog_id
    active = document(folder / "active.json")
    model = folder / revision
    if active.get("revision") != revision or not model.resolve().is_relative_to(
        install_root.resolve()
    ):
        raise denied("Selected exact model revision is not active in this workspace")
    installed = document(model / "resolved-catalog.json")
    if installed != {**reviewed, "blocked_reason": None}:
        raise denied("Installed model metadata differs from the reviewed catalog")
    inventory(model, reviewed["files"], metadata={"resolved-catalog.json"})
    return model, {
        "catalog_id": catalog_id,
        "revision": revision,
        "descriptor_sha256": digest(model / "resolved-catalog.json"),
    }


def install(
    package: Path,
    registry: Path,
    workspace: Path,
    catalog_id: str,
    revision: str,
    *,
    accept_native_code: bool,
    accept_license: bool,
    accept_destination: bool,
) -> dict[str, Any]:
    if not (accept_native_code and accept_license and accept_destination):
        raise denied(
            "Explicit native-code trust, license and private destination acceptance are required"
        )
    registry = registry_path(registry, workspace)
    package = package.resolve(strict=True)
    if registry.is_relative_to(package) or package.is_relative_to(registry):
        raise denied("Developer registry must be separate from build artifacts")
    manifest = validate_manifest(package)
    model_dir, model = model_grant(workspace / "inference", catalog_id, revision, manifest)
    if registry.is_relative_to(model_dir) or model_dir.is_relative_to(registry):
        raise denied("Developer registry must be separate from model data")
    receipt = {
        "schema_version": 1,
        "trust": TRUST,
        "authorization": "explicit-cli-native-code-license-destination-acceptance",
        "official_catalog": False,
        "release_ready": False,
        "runtime_manifest_sha256": digest(package / "runtime-manifest.json"),
        "provenance": provenance(manifest["family"], manifest["build"]["worker_lock_sha256"]),
        "model": model,
        "workspace": str(workspace.resolve()),
    }
    identity = hashlib.sha256(canonical(receipt)).hexdigest()
    registry.mkdir(parents=True, exist_ok=True, mode=0o700)
    private(registry, establish=True)
    destination = registry / identity
    if destination.exists():
        raise denied("This immutable developer grant already exists; it will not be overwritten")
    if shutil.disk_usage(registry).free < sum(item["bytes"] for item in manifest["files"]) * 2:
        raise ProviderError(
            "quota", "Insufficient space for private developer runtime installation"
        )
    staging = Path(tempfile.mkdtemp(prefix=".install-", dir=registry))
    try:
        private(staging, establish=True)
        shutil.copytree(package, staging / "package")
        (staging / "provenance.json").write_bytes(canonical(receipt))
        for path in [staging, *staging.rglob("*")]:
            private(path, establish=True)
        copied = validate_manifest(staging / "package")
        if (
            copied != manifest
            or digest(staging / "package/runtime-manifest.json")
            != receipt["runtime_manifest_sha256"]
        ):
            raise denied("Runtime build changed during installation")
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "developer_worker_id": identity,
        "registry": str(registry),
        "destination": str(destination),
        "provenance": receipt,
        "profile_options": {
            "developer_worker_id": identity,
            "catalog_id": catalog_id,
            "revision": revision,
        },
    }


def load(
    registry: Path | None,
    identity: object,
    workspace: Path,
    family: str,
    options: dict[str, Any],
    revision: str,
) -> tuple[Path, dict[str, Any], Path]:
    if registry is None:
        raise denied(
            "Developer worker requires an explicit developer registry launch configuration"
        )
    registry = registry_path(registry, workspace)
    if not isinstance(identity, str) or not _HASH.fullmatch(identity):
        raise denied("Developer worker id must be an immutable CLI registration hash")
    private(registry)
    folder = registry / identity
    private(folder)
    receipt_path = folder / "provenance.json"
    receipt = document(receipt_path)
    fields = {
        "schema_version",
        "trust",
        "authorization",
        "official_catalog",
        "release_ready",
        "runtime_manifest_sha256",
        "provenance",
        "model",
        "workspace",
    }
    if (
        set(receipt) != fields
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != 1
        or receipt.get("authorization") != "explicit-cli-native-code-license-destination-acceptance"
    ):
        raise denied("Developer worker provenance authorization schema is invalid")
    if (
        digest(receipt_path) != identity
        or receipt.get("trust") != TRUST
        or receipt.get("official_catalog") is not False
        or receipt.get("release_ready") is not False
        or receipt.get("workspace") != str(workspace.resolve())
    ):
        raise denied("Developer worker provenance/scope was modified")
    for path in folder.rglob("*"):
        private(path)
    package = folder / "package"
    manifest = validate_manifest(package)
    if (
        manifest["family"] != family
        or receipt.get("provenance") != provenance(family, manifest["build"]["worker_lock_sha256"])
        or digest(package / "runtime-manifest.json") != receipt.get("runtime_manifest_sha256")
    ):
        raise denied("Developer worker manifest/provenance does not match the explicit grant")
    model = receipt.get("model", {})
    if options.get("catalog_id") != model.get("catalog_id") or revision != model.get("revision"):
        raise denied("Developer worker is not registered for this exact model/profile revision")
    model_dir, actual = model_grant(
        workspace / "inference", model["catalog_id"], revision, manifest
    )
    if actual != model:
        raise denied("Registered model descriptor was modified")
    return package, manifest, model_dir
