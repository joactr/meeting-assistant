"""Inventory license files from the *actual* isolated build environment."""

from __future__ import annotations

import hashlib
import json
from importlib.metadata import distributions
from pathlib import Path


def main():
    packages = []
    for distribution in sorted(distributions(), key=lambda value: value.metadata.get("Name", "")):
        license_files = []
        for file in distribution.files or []:
            lowered = str(file).lower()
            if any(part in lowered for part in ("license", "copying", "notice", "copyright")):
                path = Path(distribution.locate_file(file))
                if not path.is_file():
                    continue
                raw = path.read_bytes()
                license_files.append(
                    {
                        "path": str(file),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "text": raw.decode("utf-8", errors="replace"),
                    }
                )
        packages.append(
            {
                "name": distribution.metadata["Name"],
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_metadata": distribution.metadata.get("License"),
                "license_files": license_files,
            }
        )
    native = {}
    try:
        import av

        native["pyav_linked_libraries"] = av.library_versions
    except ImportError:
        pass
    print(
        json.dumps(
            {
                "schema_version": 1,
                "packages": packages,
                "native": native,
                "audit_status": "inventory-only; artifact-level legal/codec review remains required",
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
