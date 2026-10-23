from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_unicode_cli_roundtrip_with_legacy_pipe_encoding(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    environment = {
        **os.environ,
        "PYTHONPATH": str(root / "engine"),
        "PYTHONIOENCODING": "cp1252:strict",
        "PYTHONUTF8": "0",
    }
    title = "Reunión — 日本語: 会議"
    command = [sys.executable, "-m", "meeting_assistant", "--workspace", str(tmp_path), "--json"]
    created = subprocess.run(
        [*command, "start", "--source", "text", "--title", title],
        env=environment,
        capture_output=True,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr.decode("utf-8", errors="replace")
    meeting = json.loads(created.stdout.decode("utf-8"))["item"]
    assert meeting["title"] == title

    destination = tmp_path / "meeting.md"
    exported = subprocess.run(
        [*command, "export", meeting["id"], "--format", "markdown", "--output", str(destination)],
        env=environment,
        capture_output=True,
        timeout=30,
    )
    assert exported.returncode == 0, exported.stderr.decode("utf-8", errors="replace")
    assert json.loads(exported.stdout.decode("utf-8"))["output"] == str(destination)
    assert destination.read_text(encoding="utf-8").splitlines()[0] == "# " + title
