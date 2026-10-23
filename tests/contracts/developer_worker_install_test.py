"""Native-code registration boundaries; fixtures are never executed as workers."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from meeting_assistant.infrastructure.providers.contracts import (
    Connection,
    ModelProfile,
    ProviderError,
)
from meeting_assistant.infrastructure.workers import developer
from meeting_assistant.infrastructure.workers.models import ModelRuntime


@pytest.fixture
def grant(tmp_path, monkeypatch):
    root = tmp_path / "source"
    family = root / "workers/whisper"
    family.mkdir(parents=True)
    (family / "worker.py").write_text("# first-party fixture; never executed\n")
    (family / "uv.lock").write_text("# frozen fixture\n")
    protocol = root / "engine/meeting_assistant/infrastructure/workers/runtime.py"
    protocol.parent.mkdir(parents=True)
    protocol.write_text("# protocol fixture\n")
    (root / "release.toml").write_text('version = "1.0.0-beta.1"\n[publisher]\nrepository = ""\n')
    monkeypatch.setattr(developer, "resource_root", lambda: root)
    workspace = tmp_path / "workspace"
    revision = "exact-model-revision"
    model = workspace / "inference/reviewed-model" / revision
    model.mkdir(parents=True)
    (model / "model.bin").write_bytes(b"model fixture; never deserialized")
    descriptor = {
        "id": "reviewed-model",
        "role": "asr",
        "revision": revision,
        "blocked_reason": None,
        "platforms": [developer.target()],
        "runtime": {"family": "whisper", "version_range": ">=1.0.0,<2.0.0"},
        "files": [
            {
                "path": "model.bin",
                "bytes": (model / "model.bin").stat().st_size,
                "sha256": developer.digest(model / "model.bin"),
            }
        ],
    }
    catalog = root / "catalog/models"
    catalog.mkdir(parents=True)
    (catalog / "reviewed-model.json").write_text(json.dumps(descriptor))
    (model / "resolved-catalog.json").write_text(json.dumps(descriptor))
    (model.parent / "active.json").write_text(json.dumps({"revision": revision}))
    package = tmp_path / "build/package"
    package.mkdir(parents=True)
    name = "meeting-whisper.exe" if os.name == "nt" else "meeting-whisper"
    executable = package / name
    executable.write_bytes(b"native fixture; never executed")
    if os.name != "nt":
        executable.chmod(0o700)
    proof = developer.provenance("whisper")
    manifest = {
        "schema_version": 1,
        "family": "whisper",
        "version": proof["version"],
        "platform": developer.target(),
        "protocol_major": 1,
        "executable": name,
        "executable_sha256": developer.digest(executable),
        "files": [
            {
                "path": name,
                "bytes": executable.stat().st_size,
                "sha256": developer.digest(executable),
            }
        ],
        "build": {
            "python": "3.12.10",
            **{key: value for key, value in proof.items() if key != "version"},
        },
        "weights_included": False,
        "platform_validation": "fixture; not inference evidence",
    }
    (package / "runtime-manifest.json").write_text(json.dumps(manifest))
    return SimpleNamespace(
        root=root,
        workspace=workspace,
        model=model,
        revision=revision,
        package=package,
        registry=tmp_path / "private-registry",
        manifest=manifest,
    )


def install(grant, **overrides):
    args = {"accept_native_code": True, "accept_license": True, "accept_destination": True}
    args.update(overrides)
    return developer.install(
        grant.package, grant.registry, grant.workspace, "reviewed-model", grant.revision, **args
    )


def load(grant, result, **overrides):
    args = {
        "registry": grant.registry,
        "identity": result["developer_worker_id"],
        "workspace": grant.workspace,
        "family": "whisper",
        "options": result["profile_options"],
        "revision": grant.revision,
    }
    args.update(overrides)
    return developer.load(**args)


def test_private_immutable_grant_is_not_an_official_catalog_install(grant):
    result = install(grant)
    package, manifest, model = load(grant, result)
    assert package != grant.package and manifest == grant.manifest and model == grant.model
    assert result["provenance"]["trust"] == "first-party-developer-built-local"
    assert result["provenance"]["official_catalog"] is False
    assert result["provenance"]["release_ready"] is False
    assert not (grant.workspace / "inference/runtime-whisper").exists()
    with pytest.raises(ProviderError, match="will not be overwritten"):
        install(grant)


@pytest.mark.parametrize(
    "acceptance", ["accept_native_code", "accept_license", "accept_destination"]
)
def test_each_explicit_install_permission_is_required(grant, acceptance):
    with pytest.raises(ProviderError, match="Explicit native-code"):
        install(grant, **{acceptance: False})
    assert not grant.registry.exists()


@pytest.mark.parametrize("tamper", ["runtime", "extra-library", "manifest", "model", "provenance"])
def test_modified_installed_files_or_provenance_cannot_launch(grant, tamper):
    result = install(grant)
    destination = Path(result["destination"])
    if tamper == "runtime":
        (destination / "package" / grant.manifest["executable"]).write_bytes(b"changed")
    elif tamper == "extra-library":
        (destination / "package/unregistered.dll").write_bytes(b"extra native payload")
    elif tamper == "manifest":
        manifest = {**grant.manifest, "protocol_major": 2}
        (destination / "package/runtime-manifest.json").write_text(json.dumps(manifest))
    elif tamper == "model":
        (grant.model / "model.bin").write_bytes(b"changed model")
    else:
        receipt = json.loads((destination / "provenance.json").read_text())
        receipt["trust"] = "signed-official-catalog"
        (destination / "provenance.json").write_text(json.dumps(receipt))
    with pytest.raises(ProviderError):
        load(grant, result)


def test_scope_and_explicit_launch_configuration_are_required(grant):
    result = install(grant)
    with pytest.raises(ProviderError, match="launch configuration"):
        load(grant, result, registry=None)
    with pytest.raises(ProviderError, match="provenance/scope"):
        load(grant, result, workspace=grant.workspace.parent / "other-workspace")
    with pytest.raises(ProviderError, match="exact model/profile"):
        load(grant, result, revision="different-revision")
    with pytest.raises(ProviderError, match="separate from workspace"):
        developer.registry_path(grant.workspace / "uploads/registry", grant.workspace)


def test_unreviewed_uploaded_model_cannot_authorize_worker_installation(grant):
    descriptor = json.loads((grant.root / "catalog/models/reviewed-model.json").read_text())
    descriptor["trust"] = "user-supplied-unreviewed"
    (grant.root / "catalog/models/reviewed-model.json").write_text(json.dumps(descriptor))
    with pytest.raises(ProviderError, match="reviewed model"):
        install(grant)


def test_source_lock_provenance_and_publisher_boundary_cannot_be_bypassed(grant):
    original = (grant.root / "workers/whisper/uv.lock").read_bytes()
    (grant.root / "workers/whisper/uv.lock").write_bytes(b"different lock")
    with pytest.raises(ProviderError, match="source/lock/protocol"):
        install(grant)
    (grant.root / "workers/whisper/uv.lock").write_bytes(original)
    result = install(grant)
    (grant.root / "release.toml").write_text(
        'version = "1.0.0-beta.1"\n[publisher]\nrepository = "actual-owner/actual-project"\n'
    )
    with pytest.raises(ProviderError, match="publisher-configured"):
        load(grant, result)


def test_broadened_registry_permissions_are_rejected(grant):
    result = install(grant)
    if os.name == "nt":
        subprocess.run(
            [
                str(Path(os.environ["SYSTEMROOT"]) / "System32/icacls.exe"),
                str(grant.registry),
                "/grant",
                "*S-1-1-0:(OI)(CI)F",
            ],
            check=True,
            capture_output=True,
        )
    else:
        grant.registry.chmod(0o755)
    try:
        with pytest.raises(ProviderError, match="private|owner-only"):
            load(grant, result)
    finally:
        developer.private(grant.registry, establish=True)


async def test_profile_cannot_mix_signed_and_developer_runtime_selection(grant, monkeypatch):
    monkeypatch.setenv(developer.REGISTRY_ENV, str(grant.registry))
    runtime = ModelRuntime(
        grant.workspace / "inference", SimpleNamespace(worker_runtimes={}), lambda _: ""
    )
    route = SimpleNamespace(
        connection=Connection(
            id="local",
            adapter="faster-whisper",
            endpoint="local://whisper",
            auth_scheme="none",
            execution="local",
            destination="local://whisper",
        ),
        profile=ModelProfile(
            id="profile",
            model="reviewed-model",
            revision=grant.revision,
            options={"developer_worker_id": "0" * 64, "runtime_id": "official"},
        ),
    )
    with pytest.raises(ProviderError, match="not both"):
        await runtime.ensure_ready(route)


def test_developer_package_cannot_satisfy_signed_runtime_validation(grant):
    result = install(grant)
    runtime = ModelRuntime(
        grant.workspace / "inference", SimpleNamespace(worker_runtimes={}), lambda _: ""
    )
    official = grant.workspace / "inference/runtime-whisper" / grant.revision
    official.parent.mkdir(parents=True)
    shutil.copytree(Path(result["destination"]) / "package", official)
    (official.parent / "active.json").write_text(json.dumps({"revision": grant.revision}))
    (official / "resolved-catalog.json").write_text(json.dumps({"signature": None}))
    with pytest.raises(ProviderError, match="trusted catalog signature"):
        runtime._active("runtime-whisper", runtime=True)
