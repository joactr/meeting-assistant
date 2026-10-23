"""Exercise real engine HTTP, native numbered PCM, and browser surfaces."""

from __future__ import annotations

import json
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from run import ROOT, environment, executable, npm, run
from websockets.sync.client import connect


def engine_smoke(folder: Path) -> None:
    token = secrets.token_hex(32)
    workspace = folder / "workspace"
    process = subprocess.Popen(
        [sys.executable, "-m", "meeting_assistant", "--host-stdio"],
        cwd=ROOT,
        env=environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    ready: queue.Queue[str] = queue.Queue()
    diagnostics: list[str] = []

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            ready.put(line)

    def read_errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            diagnostics.append(line)

    threads = [
        threading.Thread(target=read_output, daemon=True),
        threading.Thread(target=read_errors, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        assert process.stdin is not None
        process.stdin.write(
            json.dumps(
                {
                    "token": token,
                    "protocol_major": 1,
                    "build_id": "1.0.0-beta.1",
                    "workspace": str(workspace),
                    "host_port": None,
                }
            )
            + "\n"
        )
        process.stdin.flush()
        deadline = time.monotonic() + 30
        frame = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Engine exited before readiness: " + "".join(diagnostics))
            try:
                line = ready.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if "http_port" in candidate:
                frame = candidate
                break
        if frame is None or frame.get("protocol_major") != 1:
            raise RuntimeError("No compatible engine ready frame: " + "".join(diagnostics))
        base = f"http://127.0.0.1:{frame['http_port']}"

        def command(
            operation: str, payload: dict, *, authenticated: bool = True
        ) -> tuple[int, dict]:
            headers = {"Content-Type": "application/json"}
            if authenticated:
                headers["Authorization"] = "Bearer " + token
            request = urllib.request.Request(
                base + "/api/v1/commands/" + operation,
                data=json.dumps(payload).encode(),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        status, _ = command("workspace.status", {}, authenticated=False)
        if status != 401:
            raise RuntimeError(f"Unauthenticated command was not rejected: {status}")
        status, created = command(
            "meetings.create", {"title": "Independent runtime smoke", "source": "mic"}
        )
        if status not in {200, 201}:
            raise RuntimeError(f"Meeting creation failed: {created}")
        meeting = created["item"]
        status, updated = command(
            "meetings.update",
            {
                "id": meeting["id"],
                "expected_revision": meeting["revision"],
                "title": "Persisted smoke correction",
            },
        )
        if status != 200 or updated["item"]["title"] != "Persisted smoke correction":
            raise RuntimeError(f"Persisted edit failed: {updated}")
        status, _ = command(
            "meetings.update",
            {
                "id": meeting["id"],
                "expected_revision": meeting["revision"],
                "title": "Stale overwrite",
            },
        )
        if status != 409:
            raise RuntimeError("Stale revision did not produce conflict")
        status, listed = command("meetings.list", {})
        if status != 200 or not any(
            row["id"] == meeting["id"] and row["title"] == "Persisted smoke correction"
            for row in listed["items"]
        ):
            raise RuntimeError("Persisted edit not visible through real HTTP list")
        status, _ = command("search.query", {"query": "missing-evidence", "mode": "keyword"})
        if status != 200:
            raise RuntimeError("Keyword-only search unavailable without models")
        with connect(
            base.replace("http://", "ws://", 1) + "/api/v1/events",
            additional_headers={"Authorization": "Bearer " + token},
            open_timeout=10,
        ):
            status, reservation = command("shutdown.prepare", {"idempotency_key": "smoke-prepare"})
            if status != 200 or reservation["blockers"]:
                raise RuntimeError(f"Idle engine shutdown was unexpectedly blocked: {reservation}")
            status, committed = command(
                "shutdown.commit",
                {
                    "reservation_id": reservation["reservation_id"],
                    "idempotency_key": "smoke-commit",
                },
            )
            if status != 200 or committed.get("safe_to_exit") is not True:
                raise RuntimeError(f"Engine did not acknowledge preservation: {committed}")
            if process.wait(timeout=10) != 0:
                raise RuntimeError("Engine failed to exit cleanly with an active event subscriber")
        print(
            "PASS: authenticated real engine HTTP, persisted edit, stale CAS rejection, keyword-only search, preservation-acknowledged shutdown with active events"
        )
    finally:
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=2)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
        if diagnostics:
            print("".join(diagnostics), file=sys.stderr)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="meeting-assistant-smoke-") as directory:
        folder = Path(directory)
        engine_smoke(folder)
        run(
            executable("cargo"),
            "run",
            "--locked",
            "-p",
            "native-host",
            "--bin",
            "native-acceptance",
            "--",
            "file-source-smoke",
            str(folder / "native"),
        )
        npm("run", "smoke", "--workspace", "apps/desktop")
    print(
        "Runtime smoke passed. Real hardware/provider/installed-update acceptance remains separate."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
