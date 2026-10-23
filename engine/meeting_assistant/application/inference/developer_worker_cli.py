"""Explicitly trust/install a first-party frozen worker for a developer build only.

This is native-code installation, NOT model import, catalog download, signature
verification, artifact license audit, release approval or executable discovery.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ...infrastructure.providers.contracts import ProviderError
from ...infrastructure.workers.developer import install


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser(
        "install", help="Create a private immutable developer native-code grant"
    )
    command.add_argument("--package-directory", type=Path, required=True)
    command.add_argument("--registry", type=Path, required=True)
    command.add_argument("--workspace", type=Path, required=True)
    command.add_argument("--model-id", required=True)
    command.add_argument("--model-revision", required=True)
    command.add_argument("--accept-native-code", action="store_true")
    command.add_argument("--accept-license", action="store_true")
    command.add_argument("--accept-destination", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "action": "install-first-party-developer-native-code",
                "package": str(args.package_directory),
                "private_registry": str(args.registry),
                "workspace_scope": str(args.workspace),
                "model": {"id": args.model_id, "revision": args.model_revision},
                "license_inventory_to_review": str(
                    args.package_directory / "third-party-notices.json"
                ),
                "warning": "Trusted as native code. Local developer artifact; unsigned/unreviewed for official distribution. Not a hostile-code sandbox. No official release approval.",
            },
            indent=2,
        ),
        file=sys.stderr,
    )
    try:
        result = install(
            args.package_directory,
            args.registry,
            args.workspace,
            args.model_id,
            args.model_revision,
            accept_native_code=args.accept_native_code,
            accept_license=args.accept_license,
            accept_destination=args.accept_destination,
        )
    except ProviderError as error:
        print(json.dumps({"code": error.code, "message": error.message}), file=sys.stderr)
        raise SystemExit(2) from None
    except (OSError, ValueError, KeyError, TypeError):
        print(
            json.dumps(
                {
                    "code": "permission",
                    "message": "Developer worker installation metadata/path is unavailable or invalid",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
