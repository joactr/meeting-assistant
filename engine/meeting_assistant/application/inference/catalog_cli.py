"""Explicit acquisition of a reviewed model; never install executable runtimes here."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from platformdirs import user_data_path

from ...infrastructure.providers.contracts import ProviderError
from ...infrastructure.resources import resource_root
from .catalog import CatalogService


class AcquisitionArguments(argparse.Namespace):
    identity: str
    install_root: Path
    accept_license: bool
    accept_destination: bool
    accept_terms: bool
    token_env: str | None


async def acquire(args: AcquisitionArguments) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=60, follow_redirects=False, trust_env=False) as client:
        catalog = CatalogService(resource_root() / "engine" / "catalog", args.install_root, client)
        entry = catalog.entry(args.identity)
        if entry["role"] == "runtime":
            raise ProviderError(
                "permission", "Executable runtimes require the application's signed runtime catalog"
            )
        review = {
            "id": entry["id"],
            "revision": entry["revision"],
            "source": entry["source"],
            "download_bytes": sum(item["bytes"] for item in entry["files"]),
            "destination": str(args.install_root / entry["id"] / entry["revision"]),
            "license": entry["license"],
            "gating": entry.get("gating", {}),
        }
        print(json.dumps(review, indent=2, ensure_ascii=True), file=sys.stderr)
        accepted = args.accept_license and args.accept_destination
        if not accepted and sys.stdin.isatty():
            accepted = (
                input("After reviewing this license and destination, type ACQUIRE: ") == "ACQUIRE"
            )
        if not accepted:
            raise ProviderError(
                "permission",
                "Explicit --accept-license and --accept-destination are required for noninteractive acquisition",
            )
        return await catalog.download(
            args.identity,
            accepted_license=True,
            accepted_destination=True,
            accepted_terms=args.accept_terms,
            token=os.environ.get(args.token_env) if args.token_env else None,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Review and acquire an exact catalog model revision")
    fetch.add_argument("identity")
    fetch.add_argument(
        "--install-root",
        type=Path,
        default=user_data_path("Meeting Assistant", "MeetingAssistant") / "inference",
    )
    fetch.add_argument("--accept-license", action="store_true")
    fetch.add_argument("--accept-destination", action="store_true")
    fetch.add_argument("--accept-terms", action="store_true")
    fetch.add_argument(
        "--token-env",
        help="Name of an explicitly selected HF credential environment variable; never its value",
    )
    args = parser.parse_args(namespace=AcquisitionArguments())
    try:
        result = asyncio.run(acquire(args))
    except ProviderError as error:
        print(json.dumps({"code": error.code, "message": str(error)}), file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
