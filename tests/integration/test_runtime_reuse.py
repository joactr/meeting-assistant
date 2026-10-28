from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from meeting_assistant.application.inference.catalog import CatalogService
from meeting_assistant.infrastructure.providers.contracts import (
    Connection,
    ModelProfile,
    ProviderError,
)
from meeting_assistant.infrastructure.workers import models
from meeting_assistant.workspace import Workspace

RUNTIME, MODEL = "runtime-whisper-windows-x86_64", "whisper-small-multilingual"


def pack(root: Path, identity: str, revision: str, *, manifest: dict, resolved: dict) -> Path:
    folder = root / identity
    (folder / revision).mkdir(parents=True)
    (folder / "active.json").write_text(json.dumps({"revision": revision}))
    (folder / revision / "runtime-manifest.json").write_text(json.dumps(manifest))
    (folder / revision / "resolved-catalog.json").write_text(json.dumps(resolved))
    return folder / revision


def signed_runtime(root: Path, key: Ed25519PrivateKey, *, exe_hash: str) -> Path:
    exe = b"worker bytes"
    manifest = {
        "family": "whisper",
        "protocol_major": 1,
        "executable": "w.exe",
        "executable_sha256": exe_hash,
        "files": [{"path": "w.exe", "bytes": len(exe), "sha256": hashlib.sha256(exe).hexdigest()}],
    }
    descriptor = {"id": RUNTIME, "runtime_manifest": manifest}
    canonical = json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()
    descriptor["signature"] = base64.b64encode(key.sign(canonical)).decode()
    folder = pack(root, RUNTIME, "old", manifest=manifest, resolved=descriptor)
    (folder / "w.exe").write_bytes(exe)
    model = root / "m" / "r1"
    model.mkdir(parents=True)
    (model.parent / "active.json").write_text(json.dumps({"revision": "r1"}))
    (model / "resolved-catalog.json").write_text(json.dumps({"revision": "r1"}))
    return folder


async def test_ensure_ready_never_downloads_and_ignores_exe_hash(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    folder = signed_runtime(tmp_path, key, exe_hash="f" * 64)  # not this exe's hash
    started: list[str] = []

    class Worker:
        process = None

        def __init__(self, executable: Path, sha: str, family: str) -> None:
            started.append(family)

        async def start(self, *args: object) -> None:
            return None

    async def no_download(*args: object, **kwargs: object) -> None:
        raise AssertionError("ensure_ready must never download")

    monkeypatch.setattr(models, "WorkerClient", Worker)
    monkeypatch.setattr(CatalogService, "download", no_download)
    runtime = models.ModelRuntime(
        tmp_path,
        SimpleNamespace(worker_runtimes={}),
        lambda _: "",
        public_key=key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    )
    route = SimpleNamespace(
        connection=Connection(
            id="c",
            adapter="faster-whisper",
            endpoint="local://whisper",
            auth_scheme="none",
            execution="local",
            destination="local://whisper",
        ),
        profile=ModelProfile(
            id="p", model="m", revision="r1", options={"catalog_id": "m", "runtime_id": RUNTIME}
        ),
    )
    await runtime.ensure_ready(route)
    assert started == ["whisper"] and not runtime.damaged

    (folder / "w.exe").write_bytes(b"corrupted!!!")
    runtime.registry.worker_runtimes.clear()
    with pytest.raises(ProviderError, match="modified"):
        await runtime.ensure_ready(route)
    assert started == ["whisper"] and runtime.damaged == {RUNTIME}


@pytest.fixture
async def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


async def test_older_compatible_pack_counts_as_installed_until_damaged(workspace):
    root = workspace.root / "inference"
    items = (await workspace.adispatch("catalog.list", {}))["items"]
    model = next(i for i in items if i["id"] == MODEL)
    if not model["runtime_id"]:
        pytest.skip("no signed runtime for this platform")
    descriptor = next(i for i in items if i["id"] == model["runtime_id"])
    want = descriptor["runtime_manifest"]
    stale = {**want, "executable_sha256": "0" * 64, "files": []}  # an older build of the same pack
    pack(root, descriptor["id"], "older-revision", manifest=stale, resolved={})
    (root / MODEL / model["revision"]).mkdir(parents=True)

    async def listed() -> dict:
        return next(
            i for i in (await workspace.adispatch("catalog.list", {}))["items"] if i["id"] == MODEL
        )

    assert (await listed())["installed"] is True
    workspace.inference.model_runtime.damaged.add(descriptor["id"])
    assert (await listed())["installed"] is False
    protocol = json.dumps({**stale, "protocol_major": 2})
    (root / descriptor["id"] / "older-revision" / "runtime-manifest.json").write_text(protocol)
    workspace.inference.model_runtime.damaged.clear()
    assert (await listed())["installed"] is False
