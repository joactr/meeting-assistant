"""Run native acceptance through an actual installed application executable.

Deterministic: python tests/acceptance/native/smoke.py --installed <exe>
Hardware: add --device-key <key> --source mic|system --record-consent.
Use installed --native-acceptance devices to inspect stable IDs. Speak/play a known
sound; replay retained smoke segments. Permission denial, unplug, changed default,
RDP disconnect/reconnect and protected/exclusive playback must be exercised manually
and recorded with OS/build/device IDs. Recording-off must leave no media files.
macOS/Linux are experimental and untested until real hardware evidence is attached.
This script never substitutes synthetic provider output for real inference.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", required=True, type=Path)
    parser.add_argument("--device-key")
    parser.add_argument("--source", choices=("mic", "system"), default="mic")
    parser.add_argument("--record-consent", action="store_true")
    args = parser.parse_args()
    if not args.installed.is_absolute() or not args.installed.is_file():
        parser.error("--installed must be an absolute installed executable path")
    if args.device_key and not args.record_consent:
        parser.error("hardware capture stores audio and requires --record-consent")
    with tempfile.TemporaryDirectory(prefix="meeting-native-acceptance-") as temporary:
        output = Path(temporary) / "scenario"
        command = [str(args.installed), "--native-acceptance"]
        if args.device_key:
            command += ["capture-smoke", args.device_key, args.source, "5", str(output)]
        else:
            command += ["file-source-smoke", str(output)]
        result = subprocess.run(command, check=True, text=True, capture_output=True, timeout=90)
        evidence = json.loads(result.stdout)
        evidence["installed_executable"] = str(args.installed)
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
