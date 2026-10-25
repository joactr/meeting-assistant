"""Explicit-only acquisition, resumable hashes, signatures and safe activation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import tarfile
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ...infrastructure.providers.contracts import ProviderError

REVIEWED_HOSTS = {
    "huggingface.co",
    "cdn-lfs.huggingface.co",
    "cdn-lfs-us-1.huggingface.co",
    "cdn-lfs.hf.co",
    "cdn-lfs-us-1.hf.co",
    "cdn-lfs-eu-1.hf.co",
    "us.aws.cdn.hf.co",
    "cas-bridge.xethub.hf.co",
    "cas-server.xethub.hf.co",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


def safe_member(name: str) -> Path:
    value = PurePosixPath(name.replace("\\", "/"))
    if value.is_absolute() or ".." in value.parts or any(":" in part for part in value.parts):
        raise ProviderError("permission", "Archive member escapes its extraction grant")
    return Path(*value.parts)


def _sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def extract_verified(
    archive: Path, destination: Path, *, expanded_limit: int, max_files: int = 200000
) -> None:
    total, count = 0, 0

    def authorize(name: str, size: int) -> Path:
        nonlocal total, count
        path = destination / safe_member(name)
        total += size
        count += 1
        if count > max_files or total > expanded_limit:
            raise ProviderError("permission", "Archive extraction exceeds declared limits")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as source:
            for item in source.infolist():
                mode = (item.external_attr >> 16) & 0o170000
                if mode == 0o120000 or item.flag_bits & 1:
                    raise ProviderError(
                        "permission", "Links/encrypted members are forbidden in runtime archives"
                    )
                path = authorize(item.filename, item.file_size)
                if item.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    with source.open(item) as reader, path.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    if (item.external_attr >> 16) & 0o111:
                        path.chmod(path.stat().st_mode | 0o700)
    else:
        with tarfile.open(archive, "r:*") as source:
            for item in source:
                if not (item.isdir() or item.isfile()) or item.issym() or item.islnk():
                    raise ProviderError(
                        "permission", "Only regular files/directories may be extracted"
                    )
                path = authorize(item.name, item.size)
                if item.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    reader = source.extractfile(item)
                    assert reader is not None
                    with reader, path.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    if item.mode & 0o111:
                        path.chmod(path.stat().st_mode | 0o700)


class CatalogService:
    def __init__(
        self,
        catalog_root: Path,
        install_root: Path,
        client: httpx.AsyncClient,
        *,
        public_key: bytes | None = None,
        loaded_artifacts: Callable[[], set[str]] | None = None,
    ) -> None:
        self.catalog_root, self.install_root, self.client = catalog_root, install_root, client
        self.public_key = public_key
        self.loaded_artifacts: Callable[[], set[str]] = loaded_artifacts or (lambda: set[str]())

    def entries(self) -> list[dict[str, Any]]:
        bundled: list[dict[str, Any]] = [
            json.loads(path.read_text(encoding="utf-8"))
            for folder in ("models", "runtimes")
            for path in sorted((self.catalog_root / folder).glob("*.json"))
            if path.is_file()
        ]
        imported: list[dict[str, Any]] = []
        for pointer in sorted(self.install_root.glob("user-*/active.json")):
            active = json.loads(pointer.read_text(encoding="utf-8"))
            revision = active.get("revision", "")
            if len(revision) != 64 or any(
                character not in "0123456789abcdef" for character in revision
            ):
                continue
            descriptor = pointer.parent / revision / "resolved-catalog.json"
            if descriptor.is_file():
                item = json.loads(descriptor.read_text(encoding="utf-8"))
                if (
                    item.get("trust") == "user-supplied-unreviewed"
                    and item.get("role") != "runtime"
                ):
                    imported.append(item)
        return bundled + imported

    def entry(self, identity: str) -> dict[str, Any]:
        found = [item for item in self.entries() if item.get("id") == identity]
        if len(found) != 1:
            raise ProviderError("model_missing", "Reviewed catalog entry was not found")
        return found[0]

    async def _response(self, url: str, headers: dict[str, str]) -> httpx.Response:
        current = url
        for _ in range(6):
            parsed = urlsplit(current)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in REVIEWED_HOSTS
                or parsed.username
                or parsed.password
            ):
                raise ProviderError("permission", "Artifact redirect host is not reviewed")
            scoped_headers = {
                k: v
                for k, v in headers.items()
                if k.lower() != "authorization" or parsed.hostname == "huggingface.co"
            }
            request = self.client.build_request("GET", current, headers=scoped_headers, timeout=120)
            response = await self.client.send(request, stream=True, follow_redirects=False)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise ProviderError("network", "Artifact redirect has no destination")
                from urllib.parse import urljoin

                current = urljoin(current, location)
                continue
            return response
        raise ProviderError("network", "Artifact redirect limit exceeded")

    async def import_model(
        self, template_id: str, archive: Path, *, accepted_unreviewed: bool, accepted_license: bool
    ) -> dict[str, Any]:
        if not accepted_unreviewed or not accepted_license:
            raise ProviderError(
                "permission",
                "Model import requires explicit unreviewed-source and applicable-license acceptance",
            )
        template = self.entry(template_id)
        if template.get("role") == "runtime" or template.get("trust") == "user-supplied-unreviewed":
            raise ProviderError(
                "permission", "Choose a bundled model template; executable imports are forbidden"
            )
        return await asyncio.to_thread(self._import_model, template, archive)

    def _import_model(self, template: dict[str, Any], archive: Path) -> dict[str, Any]:
        expected = {file["path"] for file in template["files"]}
        if not expected or any(
            Path(name).suffix.lower()
            in {
                ".py",
                ".pyc",
                ".pyd",
                ".so",
                ".dll",
                ".dylib",
                ".exe",
                ".com",
                ".bat",
                ".cmd",
                ".ps1",
                ".sh",
            }
            for name in expected
        ):
            raise ProviderError(
                "permission",
                "Template contains executable artifacts and cannot be manually imported",
            )
        expanded_limit = min(
            20 * 1024**3, max(1024**2, sum(file["bytes"] for file in template["files"]) * 4)
        )
        self.install_root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.install_root).free < expanded_limit:
            raise ProviderError("quota", "Insufficient free space for bounded model extraction")
        identity = "user-" + str(uuid.uuid4())
        staging = self.install_root / (".import-" + identity)
        staging.mkdir(exist_ok=False)
        try:
            extract_verified(
                archive,
                staging,
                expanded_limit=expanded_limit,
                max_files=max(100, len(expected) * 3),
            )
            paths = [path for path in staging.rglob("*") if path.is_file()]
            if {path.relative_to(staging).as_posix() for path in paths} != expected:
                raise ProviderError(
                    "invalid_input",
                    "Imported model archive must contain exactly the selected template's file paths",
                )
            files: list[dict[str, Any]] = []
            executable_magic = {
                b"MZ",
                b"\x7fELF",
                b"\xfe\xed\xfa\xce",
                b"\xce\xfa\xed\xfe",
                b"\xfe\xed\xfa\xcf",
                b"\xcf\xfa\xed\xfe",
                b"\xca\xfe\xba\xbe",
            }
            for path in sorted(paths):
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    prefix = source.read(4)
                    if any(prefix.startswith(magic) for magic in executable_magic):
                        raise ProviderError(
                            "permission",
                            "Executable binaries cannot be imported as model artifacts",
                        )
                    digest.update(prefix)
                    while block := source.read(1024 * 1024):
                        digest.update(block)
                path.chmod(0o600)
                files.append(
                    {
                        "path": path.relative_to(staging).as_posix(),
                        "bytes": path.stat().st_size,
                        "sha256": digest.hexdigest(),
                        "url": None,
                    }
                )
            revision = hashlib.sha256(
                json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            entry = json.loads(json.dumps(template))
            entry.update(
                {
                    "id": identity,
                    "revision": revision,
                    "files": files,
                    "trust": "user-supplied-unreviewed",
                    "label": "User-supplied / unreviewed model",
                    "template_id": template["id"],
                    "upstream": {"source": "user-supplied archive", "revision": revision},
                    "license": {
                        "spdx": "LicenseRef-User-Supplied-Unreviewed",
                        "url": None,
                        "text": "Source, authorship and applicable license are user-supplied and have not been verified.",
                        "attribution": "User-supplied / unreviewed",
                    },
                    "gating": {"required": False, "user_confirms_rights": True},
                }
            )
            (staging / "resolved-catalog.json").write_text(
                json.dumps(entry, indent=2), encoding="utf-8"
            )
            folder = self.install_root / identity
            folder.mkdir(exist_ok=False)
            destination = folder / revision
            os.replace(staging, destination)
            (folder / "active.json").write_text(
                json.dumps({"revision": revision, "previous": None}), encoding="utf-8"
            )
            return {
                "id": identity,
                "revision": revision,
                "resolved_catalog": entry,
                "activated": True,
                "trust": "user-supplied-unreviewed",
            }
        except (tarfile.TarError, zipfile.BadZipFile):
            raise ProviderError(
                "invalid_input", "Choose a valid ZIP or tar model archive"
            ) from None
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def check(
        self,
        identity: str,
        *,
        accepted_license: bool,
        accepted_destination: bool,
        accepted_terms: bool = False,
        token: str | None = None,
    ) -> dict[str, Any]:
        """Validate that this entry may be acquired now; returns the catalog entry."""
        entry = self.entry(identity)
        if entry.get("trust") == "user-supplied-unreviewed":
            raise ProviderError(
                "invalid_input",
                "User-supplied models are activated by explicit archive import, not downloaded",
            )
        if entry.get("role") == "runtime" and entry.get("blocked_reason"):
            raise ProviderError(
                "model_missing",
                "This runtime has unresolved release prerequisites",
                details={"reason": entry["blocked_reason"]},
            )
        if not accepted_license or not accepted_destination:
            raise ProviderError(
                "permission",
                "Review license, size, destination and requirements before explicit acquisition",
            )
        if entry.get("gating", {}).get("required") and (not accepted_terms or not token):
            raise ProviderError(
                "auth", "This gated model requires the user's accepted upstream terms and HF token"
            )
        if not entry.get("files"):
            raise ProviderError(
                "model_missing",
                "Runtime release assets have not been built/signed; source descriptors do not authorize executable fallback",
            )
        return entry

    async def download(
        self,
        identity: str,
        *,
        accepted_license: bool,
        accepted_destination: bool,
        accepted_terms: bool = False,
        token: str | None = None,
        cancelled: asyncio.Event | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        entry = self.check(
            identity,
            accepted_license=accepted_license,
            accepted_destination=accepted_destination,
            accepted_terms=accepted_terms,
            token=token,
        )
        files = entry["files"]
        destination = self.install_root / identity / entry["revision"]
        if str(destination) in self.loaded_artifacts():
            raise ProviderError("permission", "Cannot overwrite an artifact loaded by active jobs")
        if destination.exists():
            raise ProviderError(
                "invalid_input", "This revision already exists; it will not be overwritten"
            )
        self.install_root.mkdir(parents=True, exist_ok=True)
        required = int(
            entry.get("minimum_resources", {}).get("free_disk_bytes")
            or sum(item["bytes"] for item in files) * 3
        )
        if shutil.disk_usage(self.install_root).free < required:
            raise ProviderError(
                "quota", "Not enough disk space for download plus extraction overhead"
            )
        staging = self.install_root / ".parts" / identity / entry["revision"]
        staging.mkdir(parents=True, exist_ok=True)
        resolved: list[dict[str, Any]] = []
        total = sum(item["bytes"] for item in files)
        base = 0

        def report(done: int) -> None:
            if progress:
                progress(done, total)

        for item in files:
            path = staging / safe_member(item["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            part = path.with_name(path.name + ".part")
            size = part.stat().st_size if part.exists() else 0
            report(base + size)
            headers = {"Range": f"bytes={size}-"} if size else {}
            if token:
                headers["Authorization"] = "Bearer " + token
            if size > item["bytes"]:
                raise ProviderError("invalid_output", "Partial artifact exceeds reviewed byte size")
            if size < item["bytes"]:
                response = await self._response(item["url"], headers)
                try:
                    if response.status_code in {401, 403}:
                        raise ProviderError(
                            "auth", "Upstream access terms/token do not permit this artifact"
                        )
                    if response.status_code not in {200, 206}:
                        raise ProviderError(
                            "network",
                            f"Artifact request failed (HTTP {response.status_code})",
                            retryable=True,
                        )
                    if (
                        size
                        and response.status_code == 206
                        and not response.headers.get("content-range", "").startswith(
                            f"bytes {size}-"
                        )
                    ):
                        raise ProviderError(
                            "invalid_output",
                            "Upstream resume range did not match the verified offset",
                        )
                    mode = "ab" if size and response.status_code == 206 else "wb"
                    written = size if mode == "ab" else 0
                    report(base + written)
                    with part.open(mode) as output:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if cancelled and cancelled.is_set():
                                raise ProviderError(
                                    "cancelled",
                                    "Download cancelled; bounded partial file retained for explicit resume",
                                )
                            written += len(chunk)
                            if written > item["bytes"]:
                                raise ProviderError(
                                    "invalid_output", "Artifact exceeds reviewed size"
                                )
                            output.write(chunk)
                            report(base + written)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    await response.aclose()
            if part.stat().st_size != item["bytes"]:
                raise ProviderError(
                    "invalid_output", "Artifact byte size did not match reviewed metadata"
                )
            digest = await asyncio.to_thread(_sha256, part)
            expected = item.get("sha256")
            if expected is None:
                # Only a small gated metadata object may resolve from an authoritative pinned Git blob.
                if (
                    not entry.get("gating", {}).get("required")
                    or item["bytes"] > 1024 * 1024
                    or not item.get("git_blob_sha1")
                ):
                    raise ProviderError(
                        "permission", "Reviewed SHA256 is missing; artifact cannot activate"
                    )
                raw = part.read_bytes()
                git_hash = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
                if git_hash != item["git_blob_sha1"]:
                    raise ProviderError(
                        "permission",
                        "Gated metadata differs from its authoritative pinned Git object",
                    )
            elif digest != expected:
                raise ProviderError("permission", "Artifact checksum verification failed")
            resolved.append({**item, "sha256": digest})
            os.replace(part, path)
            base += item["bytes"]
        resolved_entry = {**entry, "files": resolved, "blocked_reason": None}
        (staging / "resolved-catalog.json").write_text(
            json.dumps(resolved_entry, indent=2), encoding="utf-8"
        )
        if entry["role"] == "runtime":
            if self.public_key is None or not entry.get("signature"):
                raise ProviderError(
                    "permission",
                    "Executable runtime requires an embedded catalog public key and detached signature",
                )
            signed = json.dumps(
                {k: v for k, v in entry.items() if k != "signature"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            try:
                Ed25519PublicKey.from_public_bytes(self.public_key).verify(
                    base64.b64decode(entry["signature"]), signed
                )
            except Exception:
                raise ProviderError(
                    "permission", "Runtime catalog signature verification failed"
                ) from None
            archive = staging / safe_member(resolved[0]["path"])
            expanded = staging / "expanded"
            shutil.rmtree(expanded, ignore_errors=True)
            expanded.mkdir()
            await asyncio.to_thread(extract_verified, archive, expanded, expanded_limit=required)
            (expanded / "resolved-catalog.json").write_text(
                json.dumps(resolved_entry, indent=2), encoding="utf-8"
            )
            activation = expanded
        else:
            activation = staging
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(activation, destination)
        pointer = destination.parent / "active.json"
        temporary = pointer.with_name(".active-" + str(uuid.uuid4()) + ".json")
        temporary.write_text(
            json.dumps(
                {
                    "revision": entry["revision"],
                    "path": str(destination),
                    "previous": json.loads(pointer.read_text()).get("revision")
                    if pointer.exists()
                    else None,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporary, pointer)
        return {
            "id": identity,
            "revision": entry["revision"],
            "path": str(destination),
            "resolved_catalog": resolved_entry,
            "activated": True,
        }

    async def delete(
        self,
        identity: str,
        *,
        confirmed: bool,
        revision: str | None = None,
        release_artifact: Callable[[Path], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        entry = self.entry(identity)
        if not confirmed:
            raise ProviderError("permission", "Artifact removal requires explicit confirmation")
        folder = self.install_root / identity
        selected = revision or entry["revision"]
        if (
            not selected
            or "/" in selected
            or "\\" in selected
            or ":" in selected
            or selected in {".", ".."}
        ):
            raise ProviderError("invalid_input", "Invalid installed revision")
        target = folder / selected
        if not target.exists():
            raise ProviderError("model_missing", "Selected artifact revision is not installed")
        if not target.resolve().is_relative_to(self.install_root.resolve()) or target.is_symlink():
            raise ProviderError("permission", "Artifact removal escapes its installed grant")
        if release_artifact is not None:
            await release_artifact(target)
        if str(target) in self.loaded_artifacts():
            raise ProviderError("permission", "Dependent jobs have not drained this artifact")
        if not (
            (target / "resolved-catalog.json").exists()
            or (target / "runtime-manifest.json").exists()
        ):
            raise ProviderError("permission", "Refusing to remove an unrecognized user directory")
        shutil.rmtree(target)
        pointer = folder / "active.json"
        if (
            pointer.exists()
            and json.loads(pointer.read_text(encoding="utf-8")).get("revision") == selected
        ):
            pointer.unlink()
        return {"id": identity, "revision": selected, "deleted": True}
