"""Resolve signed catalog runtimes or explicitly installed developer worker grants."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..providers.contracts import Connection, ModelProfile, ProviderError
from ..providers.registry import resolve_capabilities
from .client import WorkerClient
from .developer import launch_registry
from .developer import load as load_developer_worker

if TYPE_CHECKING:
    from ..providers.registry import ProviderRegistry


class _ModelRoute(Protocol):
    @property
    def connection(self) -> Connection: ...

    @property
    def profile(self) -> ModelProfile: ...


FAMILIES = {
    "faster-whisper": "whisper",
    "sherpa-parakeet": "sherpa",
    "fermion-phonon": "phonon",
    "community-1": "pyannote",
    "e5-onnx": "embeddings",
    "azure-speech": "azure-speech",
}


class ModelRuntime:
    def __init__(
        self,
        install_root: Path,
        registry: ProviderRegistry,
        secret_resolver: Callable[[str], str | Awaitable[str]],
        *,
        public_key: bytes | None = None,
    ) -> None:
        self.install_root, self.registry, self.secret_resolver = (
            install_root,
            registry,
            secret_resolver,
        )
        self.public_key = public_key
        self.developer_registry = launch_registry(install_root.parent)
        # Per profile: a cold speaker-model start (~80s) must not block a running ASR worker.
        self.locks: dict[str, asyncio.Lock] = {}
        self.grants: dict[str, dict[str, Path | None]] = {}
        # Runtime ids whose installed pack failed signature/integrity checks; only an explicit
        # user download replaces them (never ensure_ready).
        self.damaged: set[str] = set()

    def installed_manifest(self, identity: str) -> tuple[str, dict[str, Any]] | None:
        """(revision, runtime manifest) of the active pack, whatever its revision; no hashing."""
        try:
            folder = self.install_root / identity
            revision: str = json.loads((folder / "active.json").read_text(encoding="utf-8"))[
                "revision"
            ]
            manifest: dict[str, Any] = json.loads(
                (folder / revision / "runtime-manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return revision, manifest

    def _active(
        self, identity: str | None, *, runtime: bool = False
    ) -> tuple[Path, dict[str, Any]]:
        if (
            not identity
            or "/" in identity
            or "\\" in identity
            or ":" in identity
            or identity in {".", ".."}
        ):
            raise ProviderError("invalid_input", "Select an explicit reviewed catalog artifact id")
        folder = self.install_root / identity
        pointer = folder / "active.json"
        if not pointer.exists():
            raise ProviderError(
                "model_missing", "Selected model/runtime has not been explicitly downloaded"
            )
        active: dict[str, Any] = json.loads(pointer.read_text(encoding="utf-8"))
        path = folder / active["revision"]
        if not path.resolve().is_relative_to(self.install_root.resolve()) or not path.is_dir():
            raise ProviderError("permission", "Installed artifact grant is invalid")
        marker = path / ("runtime-manifest.json" if runtime else "resolved-catalog.json")
        if not marker.exists():
            raise ProviderError("model_missing", "Installed artifact manifest is unavailable")
        manifest: dict[str, Any] = json.loads(marker.read_text(encoding="utf-8"))
        if runtime:
            import base64

            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            signed_path = path / "resolved-catalog.json"
            if self.public_key is None or not signed_path.exists():
                raise ProviderError(
                    "permission", "Installed runtime has no trusted catalog signature"
                )
            descriptor: dict[str, Any] = json.loads(signed_path.read_text(encoding="utf-8"))
            try:
                canonical = json.dumps(
                    {key: value for key, value in descriptor.items() if key != "signature"},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                Ed25519PublicKey.from_public_bytes(self.public_key).verify(
                    base64.b64decode(descriptor["signature"]), canonical
                )
            except Exception:
                raise ProviderError(
                    "permission", "Installed runtime catalog signature was modified"
                ) from None
            if descriptor.get("blocked_reason") or descriptor.get("runtime_manifest") != manifest:
                raise ProviderError(
                    "permission",
                    "Installed runtime manifest differs from the signed reviewed descriptor",
                )
        return path, manifest

    async def ensure_ready(self, route: _ModelRoute) -> WorkerClient:
        async with self.locks.setdefault(route.profile.id, asyncio.Lock()):
            profile, connection = route.profile, route.connection
            family = FAMILIES.get(connection.adapter)
            if family is None:
                raise ProviderError("unsupported", "Adapter does not use a local isolated runtime")
            developer = "developer_worker_id" in profile.options
            developer_model = None
            current = self.registry.worker_runtimes.get(profile.id)
            caps = resolve_capabilities(connection, profile)
            if (
                current is not None
                and current.process is not None
                and current.process.returncode is None
            ):
                if current.model_fingerprint != caps.fingerprint:
                    if current.lock.locked():
                        raise ProviderError(
                            "permission", "Selected runtime is serving an active policy revision"
                        )
                    await current.close()
                else:
                    return current
            runtime_id = profile.options.get("runtime_id")
            if developer:
                if "runtime_id" in profile.options:
                    raise ProviderError(
                        "invalid_input",
                        "Select either a signed runtime_id or a developer_worker_id, not both",
                    )
                if profile.model != profile.options.get("catalog_id"):
                    raise ProviderError(
                        "invalid_input",
                        "Developer profile model must name its registered reviewed catalog id",
                    )
                try:
                    runtime_dir, manifest, developer_model = load_developer_worker(
                        self.developer_registry,
                        profile.options["developer_worker_id"],
                        self.install_root.parent,
                        family,
                        profile.options,
                        profile.revision,
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    raise ProviderError(
                        "permission", "Developer worker grant is unavailable or invalid"
                    ) from None
            else:
                try:
                    runtime_dir, manifest = self._active(runtime_id, runtime=True)
                except ProviderError as error:
                    if error.code == "permission":
                        self.damaged.add(str(runtime_id))
                    raise
            if manifest.get("family") != family or manifest.get("protocol_major") != 1:
                raise ProviderError(
                    "unsupported", "Installed runtime family/protocol does not match this profile"
                )
            executable = (runtime_dir / manifest["executable"]).resolve()
            if not executable.is_relative_to(runtime_dir.resolve()):
                raise ProviderError("permission", "Runtime executable escapes its installed grant")
            if not developer:

                def verify() -> None:
                    # Lexical check only: resolve() per file cost ~0.5ms (15k files in the speaker
                    # pack) and, contending for the GIL with a busy capture loop, most of a cold
                    # start. The hash below is what proves the bytes.
                    for item in manifest.get("files", []):
                        relative = Path(item["path"])
                        if relative.anchor or ".." in relative.parts:
                            raise ProviderError(
                                "permission", "Installed runtime file grant is invalid"
                            )
                        try:
                            with (runtime_dir / relative).open("rb") as source:
                                size = os.fstat(source.fileno()).st_size
                                digest = hashlib.file_digest(source, "sha256").hexdigest()
                        except OSError:
                            raise ProviderError(
                                "permission", "Installed runtime file grant is invalid"
                            ) from None
                        if size != item["bytes"] or digest != item["sha256"]:
                            raise ProviderError("permission", "Installed runtime file was modified")

                # Hashing a runtime pack takes seconds to a minute; on the loop it starves the
                # engine heartbeat and the native watchdog kills the engine mid-capture.
                try:
                    await asyncio.to_thread(verify)
                except ProviderError:
                    self.damaged.add(str(runtime_id))
                    raise
                self.damaged.discard(runtime_id)
            model_dir = developer_model
            if family != "azure-speech" and not developer:
                model_dir, model = self._active(profile.options.get("catalog_id"))
                if profile.revision != model["revision"]:
                    raise ProviderError(
                        "invalid_input",
                        "Profile revision does not match the selected exact model artifact revision",
                    )
            options = {**profile.options, "model_fingerprint": caps.fingerprint}
            if family == "azure-speech":
                if not connection.secret_ref:
                    raise ProviderError(
                        "auth", "Azure Speech requires an operation-scoped credential"
                    )
                secret = self.secret_resolver(connection.secret_ref)
                import inspect

                options["key"] = await secret if inspect.isawaitable(secret) else secret
                options["region"] = connection.region
            worker = WorkerClient(executable, manifest["executable_sha256"], family)
            await worker.start(model_dir, caps.fingerprint, options)
            self.registry.worker_runtimes[profile.id] = worker
            self.grants[profile.id] = {"runtime": runtime_dir, "model": model_dir}
            return worker

    def state(self, profile_id: str) -> str:
        """Worker state for the UI: ready (running), loading (ensure_ready starting it) or stopped."""
        worker = self.registry.worker_runtimes.get(profile_id)
        if worker is not None and worker.process is not None and worker.process.returncode is None:
            return "ready"
        lock = self.locks.get(profile_id)
        return "loading" if lock is not None and lock.locked() else "stopped"

    def loaded_artifacts(self) -> set[str]:
        return {
            str(path)
            for grant in self.grants.values()
            for path in grant.values()
            if path is not None
        }

    async def release_artifact(self, path: Path) -> None:
        for profile, grant in list(self.grants.items()):
            if path in grant.values():
                worker = self.registry.worker_runtimes[profile]
                if worker.lock.locked():
                    raise ProviderError(
                        "permission",
                        "Artifact is serving active inference; wait for dependent jobs to drain",
                    )
                await worker.close()
                self.registry.worker_runtimes.pop(profile, None)
                self.grants.pop(profile, None)
