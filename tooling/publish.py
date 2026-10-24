"""Assemble verified GitHub draft assets; publication requires an explicit flag.

Never invoked by local build/package. Requires a protected release token and real
publisher configuration. Drafts are intentionally retained after a failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import release


class GitHub:
    def __init__(self, repository: str, token: str) -> None:
        self.repository = repository
        self.token = token

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "api.github.com",
            "uploads.github.com",
        }:
            raise RuntimeError("Refusing to send release credentials to an untrusted host")
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": content_type,
                "User-Agent": "meeting-assistant-release",
            },
        )

        # Requests with credentials must not follow redirects to asset/CDN hosts.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect()).open(request, timeout=120) as response:
            return response.read()

    def json(self, path: str, *, method: str = "GET", value: dict | None = None) -> dict:
        body = None if value is None else json.dumps(value).encode()
        return json.loads(
            self.request(
                f"https://api.github.com/repos/{self.repository}/{path}", method=method, body=body
            )
        )

    def upload(self, release_id: int, path: Path) -> dict:
        size = path.stat().st_size
        expected_digest = "sha256:" + release.digest(path)
        endpoint = f"/repos/{self.repository}/releases/{release_id}/assets?name={urllib.parse.quote(path.name, safe='')}"
        connection = http.client.HTTPSConnection("uploads.github.com", timeout=120)
        try:
            connection.putrequest("POST", endpoint)
            for key, value in {
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
                "User-Agent": "meeting-assistant-release",
            }.items():
                connection.putheader(key, value)
            connection.endheaders()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    connection.send(chunk)
            response = connection.getresponse()
            if response.status != 201:
                raise RuntimeError(f"GitHub upload returned HTTP {response.status}; draft retained")
            asset = json.loads(response.read(1024 * 1024))
        finally:
            connection.close()
        if asset.get("digest") != expected_digest or asset.get("size") != size:
            raise RuntimeError(f"Stored GitHub asset failed digest/size check: {path.name}")
        self.verify_download(asset["id"], size, expected_digest)
        return {"id": asset["id"], "sha256": expected_digest, "bytes": size}

    def verify_download(self, asset_id: int, size: int, expected_digest: str) -> None:
        # API asset downloads may redirect to GitHub's CDN. Never forward tokens.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        url = f"https://api.github.com/repos/{self.repository}/releases/assets/{asset_id}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/octet-stream",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "meeting-assistant-release",
            },
        )
        try:
            response = urllib.request.build_opener(NoRedirect()).open(request, timeout=120)
        except urllib.error.HTTPError as error:
            if error.code != 302:
                raise
            location = error.headers.get("Location", "")
            parsed = urllib.parse.urlsplit(location)
            error.close()
            if parsed.scheme != "https" or parsed.hostname not in {
                "release-assets.githubusercontent.com",
                "objects.githubusercontent.com",
            }:
                raise RuntimeError("Unexpected GitHub asset download redirect") from None
            response = urllib.request.build_opener(NoRedirect()).open(location, timeout=120)
        actual_digest = hashlib.sha256()
        received = 0
        with response:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > size:
                    raise RuntimeError("Downloaded asset exceeds expected size")
                actual_digest.update(chunk)
        if received != size or "sha256:" + actual_digest.hexdigest() != expected_digest:
            raise RuntimeError("Downloaded release asset failed byte-for-byte verification")


def verify_runtime_catalogs(config: dict) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    repository = config["publisher"]["repository"]
    version = config["version"]
    key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(config["updates"]["catalog_public_key"], validate=True)
    )
    catalogs = list((release.ROOT / "artifacts" / "runtimes").rglob("*catalog.json"))
    windows_families = set()
    for path in catalogs:
        descriptor = json.loads(path.read_text(encoding="utf-8"))
        if (
            descriptor.get("role") != "runtime"
            or descriptor.get("revision") != version
            or descriptor.get("blocked_reason")
        ):
            raise RuntimeError(f"Runtime descriptor is not release-ready: {path.name}")
        canonical = json.dumps(
            {name: value for name, value in descriptor.items() if name != "signature"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            key.verify(base64.b64decode(descriptor["signature"], validate=True), canonical)
        except (InvalidSignature, TypeError, KeyError) as error:
            raise RuntimeError(f"Runtime catalog signature failed: {path.name}") from error
        if len(descriptor.get("files", [])) != 1:
            raise RuntimeError("Runtime catalog must identify its actual archive")
        file = descriptor["files"][0]
        if Path(file["path"]).name != file["path"]:
            raise RuntimeError("Runtime archive name escapes its catalog directory")
        archive = path.parent / file["path"]
        expected_url = (
            f"https://github.com/{repository}/releases/download/v{version}/{archive.name}"
        )
        actual_digest = release.digest(archive)
        audit = descriptor.get("license_audit") or {}
        if (
            file.get("url") != expected_url
            or file.get("bytes") != archive.stat().st_size
            or file.get("sha256") != actual_digest
        ):
            raise RuntimeError(f"Runtime archive metadata mismatch: {archive.name}")
        if (
            audit.get("approved") is not True
            or audit.get("artifact_sha256") != actual_digest
            or not audit.get("reviewer")
            or not audit.get("evidence")
        ):
            raise RuntimeError(f"Runtime artifact redistribution review missing: {archive.name}")
        manifest = descriptor.get("runtime_manifest", {})
        if manifest.get("weights_included") is not False:
            raise RuntimeError("Runtime artifacts must not silently bundle model weights")
        if manifest.get("platform") == "windows-x86_64":
            windows_families.add(manifest.get("family"))
    if windows_families != {
        "whisper",
        "sherpa",
        "phonon",
        "pyannote",
        "embeddings",
        "azure-speech",
    }:
        raise RuntimeError("All six reviewed, signed Windows CPU runtime packages are required")


def assemble(*, publish: bool) -> None:
    release.check()
    config = tomllib.loads((release.ROOT / "release.toml").read_text(encoding="utf-8"))
    verify_runtime_catalogs(config)
    repository = config["publisher"]["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Real public repository required")
    token = os.environ.get("GH_RELEASE_TOKEN")
    if not token:
        raise RuntimeError("GH_RELEASE_TOKEN is required; never pass it on the command line")
    version = config["version"]
    tag = f"v{version}"
    github = GitHub(repository, token)
    # Fail on an existing tag/release rather than overwrite a published asset.
    releases = json.loads(
        github.request(f"https://api.github.com/repos/{repository}/releases?per_page=100")
    )
    if any(item.get("tag_name") == tag for item in releases):
        raise RuntimeError(f"Release {tag} already exists; no asset replacement permitted")
    github.json(f"git/ref/tags/{urllib.parse.quote(tag, safe='')}")
    artifacts = release.packaged_files()
    artifacts += [
        p.with_name(p.name + ".sig") for p in artifacts if p.with_name(p.name + ".sig").is_file()
    ]
    artifacts += [
        p for p in release.OUTPUT.iterdir() if p.is_file() and p.suffix in {".json", ".txt"}
    ]
    runtimes = release.ROOT / "artifacts" / "runtimes"
    if runtimes.exists():
        artifacts += [
            p
            for p in runtimes.rglob("*")
            if p.is_file()
            and (p.suffix == ".zip" or p.name.endswith("catalog.json") or p.name.endswith(".sig"))
        ]
    by_name: dict[str, Path] = {}
    for path in artifacts:
        if path.name in by_name:
            raise RuntimeError(f"Duplicate release asset name: {path.name}")
        by_name[path.name] = path
    manifest = json.loads((release.OUTPUT / "latest.json").read_text(encoding="utf-8"))
    for entry in manifest["platforms"].values():
        parsed = urllib.parse.urlsplit(entry["url"])
        expected = f"/{repository}/releases/download/{tag}/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or not parsed.path.startswith(expected)
        ):
            raise RuntimeError("Updater manifest has an unexpected version-tagged URL")
        if urllib.parse.unquote(parsed.path[len(expected) :]) not in by_name:
            raise RuntimeError("Updater manifest references a missing local asset")
    draft = github.json(
        "releases",
        method="POST",
        value={
            "tag_name": tag,
            "name": f"Meeting Assistant {version}",
            "body": str(manifest.get("notes", "")),
            "draft": True,
            "prerelease": "-" in version,
        },
    )
    release_id = draft["id"]
    uploaded = {}
    for name, path in by_name.items():
        uploaded[name] = github.upload(release_id, path)
    record = {
        "repository": repository,
        "release_id": release_id,
        "tag": tag,
        "assets": uploaded,
        "published": False,
    }
    if publish:
        result = github.json(f"releases/{release_id}", method="PATCH", value={"draft": False})
        if result.get("draft") is not False:
            raise RuntimeError("GitHub did not confirm release publication")
        record["published"] = True
    (release.OUTPUT / "github-assembly.json").write_text(
        json.dumps(record, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{'Published' if publish else 'Assembled draft'} {tag}; {len(uploaded)} asset downloads verified"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Explicitly authorize creating a remote GitHub draft",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="Explicitly authorize publishing the fully staged draft",
    )
    args = parser.parse_args()
    if not args.allow_network:
        parser.error(
            "Creating a remote draft requires --allow-network; local package never publishes"
        )
    try:
        assemble(publish=args.publish)
    except (OSError, ValueError, RuntimeError) as exc:
        # No request headers or tokens are included in these messages.
        print(f"Release assembly failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
