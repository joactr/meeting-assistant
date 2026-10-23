from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import os
import secrets
import socket
import sys
from pathlib import Path

import httpx
import uvicorn
from platformdirs import user_data_path

from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, new_id
from meeting_assistant.domain.ports import JSONDocument
from meeting_assistant.workspace import Workspace


class NativeGateway:
    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token

    async def request(self, operation: str, payload: JSONDocument) -> JSONDocument:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120, connect=5), follow_redirects=False, trust_env=False
        ) as client:
            response = await client.post(
                f"http://127.0.0.1:{self.port}/rpc",
                headers={"Authorization": "Bearer " + self.token},
                json={"operation_id": operation, "payload": payload},
            )
        try:
            body: JSONDocument = response.json()
        except ValueError as exc:
            raise AppError(
                "native_protocol", "Native gateway returned invalid JSON.", status=503
            ) from exc
        if response.is_error:
            raise AppError(
                body.get("code", "native_error"),
                body.get("message", "Native operation failed."),
                status=response.status_code,
                retryable=body.get("retryable", False),
                details=body.get("details"),
            )
        return body


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meeting-assistant",
        description="Local-first meeting notebook engine and scoped automation CLI.",
    )
    p.add_argument("--config", type=Path)
    p.add_argument("--workspace", type=Path)
    p.add_argument("--json", action="store_true")
    p.add_argument("--token-file", type=Path)
    p.add_argument("--url")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--host-stdio", action="store_true")
    commands = p.add_subparsers(dest="command")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--host-stdio", action="store_true")
    serve.add_argument("--grant-token-file", type=Path)
    serve.add_argument(
        "--scope", action="append", choices=["read", "write", "diagnostics", "files"], default=[]
    )
    start = commands.add_parser("start")
    start.add_argument("--title", required=True)
    start.add_argument("--source", choices=["mic", "system", "both", "text"], default="both")
    start.add_argument("--project-id")
    start.add_argument("--device-key", action="append", default=[])
    for name in ("stop", "pause", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("meeting_id")
        sub.add_argument("--idempotency-key")
    commands.add_parser("status")
    imp = commands.add_parser("import")
    imp.add_argument("path", type=Path)
    imp.add_argument("--accept", action="store_true")
    imp.add_argument("--source-stopped", action="store_true")
    for name in ("search", "ask"):
        sub = commands.add_parser(name)
        sub.add_argument("query")
        sub.add_argument("--project-id")
        sub.add_argument("--meeting-id", action="append", default=[])
        sub.add_argument("--mode", choices=["keyword", "semantic", "hybrid"], default="hybrid")
    exp = commands.add_parser("export")
    exp.add_argument("meeting_id")
    exp.add_argument(
        "--format", choices=["markdown", "pdf", "srt", "json", "email"], default="markdown"
    )
    exp.add_argument("--output", type=Path)
    exp.add_argument("--include-transcript", action="store_true")
    back = commands.add_parser("backup")
    back.add_argument("output", type=Path)
    back.add_argument("--plain-warning-accepted", action="store_true")
    back.add_argument("--recipient")
    back.add_argument("--passphrase", action="store_true")
    back.add_argument("--include-media", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("path", type=Path)
    restore.add_argument("--passphrase", action="store_true")
    commands.add_parser("models")
    commands.add_parser("providers")
    return p


async def serve(args: argparse.Namespace, configuration: JSONDocument) -> int:
    from meeting_assistant.application.capture import AudioPipeline
    from meeting_assistant.transport.http import Authorization, create_app

    native = None
    build_id = str(configuration.get("build_id", "1.0.0-beta.1"))
    if args.host_stdio:
        frame = json.loads(sys.stdin.readline(1048576))
        require(
            frame.get("protocol_major") == 1,
            "protocol_mismatch",
            "Native host protocol is incompatible.",
            status=409,
        )
        token = frame["token"]
        workspace_path = Path(frame["workspace"])
        build_id = frame["build_id"]
        if frame.get("host_port"):
            native = NativeGateway(int(frame["host_port"]), token)
    else:
        token_file = args.token_file or configuration.get("token_file")
        require(
            token_file is not None,
            "token_required",
            "Engine-only serve requires an explicit owner-readable --token-file.",
            status=422,
        )
        assert token_file is not None
        token = Path(token_file).read_text().strip()
        workspace_path = args.workspace or Path(
            configuration.get("workspace", user_data_path("Meeting Assistant", "MeetingAssistant"))
        )
    require(
        len(token) == 64 and all(c in "0123456789abcdef" for c in token),
        "invalid_token",
        "Token file or launch pipe must contain 32 random bytes encoded as lowercase hex.",
        status=422,
    )
    database = (
        workspace_path
        if workspace_path.suffix in (".db", ".sqlite3", ".sqlite")
        else workspace_path / "workspace.sqlite3"
    )
    workspace = Workspace(database, build_id=build_id, native_gateway=native)
    auth = Authorization(token)
    if getattr(args, "grant_token_file", None):
        grant = secrets.token_hex(32)
        target = args.grant_token_file
        with target.open("x", encoding="utf-8") as output:
            output.write(grant + "\n")
        target.chmod(0o600)
        auth.grant(grant, set(args.scope or ["read"]))
    pipeline = AudioPipeline(workspace, token)
    workspace.audio_pipeline = pipeline
    audio_port = await pipeline.start()
    app = create_app(workspace, token, manage_lifespan=False, authorization=auth)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", int(args.port or configuration.get("port", 0))))
    sock.listen(128)
    sock.setblocking(False)
    await workspace.start()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=sock.getsockname()[1],
            log_config=None,
            access_log=False,
            log_level="warning",
        )
    )
    ready = {
        "protocol_major": 1,
        "build_id": build_id,
        "pid": os.getpid(),
        "http_port": sock.getsockname()[1],
        "audio_port": audio_port,
        "capabilities": {
            "capture": native is not None,
            "keyword_search": True,
            "deterministic_export": True,
            "native_gateway": native is not None,
        },
    }

    async def monitor_shutdown() -> None:
        await workspace.shutdown_requested.wait()
        server.should_exit = True

    shutdown_monitor = asyncio.create_task(monitor_shutdown())
    print(canonical(ready), flush=True)
    try:
        await server.serve(sockets=[sock])
    finally:
        shutdown_monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_monitor
        for meeting in workspace.list("meetings"):
            if meeting["state"] in ("recording", "paused", "starting", "stopping"):
                try:
                    await workspace.adispatch(
                        "meetings.stop",
                        {"meeting_id": meeting["id"], "idempotency_key": "shutdown-" + new_id()},
                    )
                except AppError:
                    current = workspace.require_item("meetings", meeting["id"])
                    workspace.put(
                        "meetings",
                        meeting["id"],
                        {
                            "state": "interrupted",
                            "interrupted_reason": "Shutdown could not receive native preservation acknowledgement.",
                        },
                        expected_revision=current["revision"],
                    )
        await pipeline.close()
        await workspace.stop()
        sock.close()
    return 0


async def run(args: argparse.Namespace) -> int:
    configuration: JSONDocument = json.loads(args.config.read_text()) if args.config else {}
    if args.command == "serve" or args.host_stdio:
        return await serve(args, configuration)
    require(
        args.command is not None, "command_required", "Specify an operation or serve.", status=422
    )
    operation = ""
    payload: JSONDocument = {}
    if args.command == "status":
        operation = "workspace.status"
    elif args.command in ("stop", "pause", "resume"):
        operation = "meetings." + args.command
        payload = {
            "meeting_id": args.meeting_id,
            "idempotency_key": args.idempotency_key or new_id(),
        }
    elif args.command == "import":
        operation = "legacy.import" if args.accept else "legacy.preview"
        payload = {
            "_trusted_path": str(args.path.resolve()),
            "confirm": args.accept,
            "source_stopped": args.source_stopped,
        }
    elif args.command in ("search", "ask"):
        operation = "search.query" if args.command == "search" else "ask.query"
        payload = {
            "query": args.query,
            "project_id": args.project_id,
            "meeting_ids": args.meeting_id,
            "mode": args.mode,
        }
    elif args.command == "export":
        operation = "export.create"
        payload = {
            "meeting_id": args.meeting_id,
            "format": args.format,
            "include_transcript": args.include_transcript,
        }
    elif args.command == "backup":
        operation = "backup.create"
        payload = {
            "plain_warning_accepted": args.plain_warning_accepted,
            "recipient": args.recipient,
            "include_media": args.include_media,
        }
        if args.passphrase:
            import getpass

            payload["_passphrase"] = getpass.getpass("Backup passphrase: ")
    elif args.command == "restore":
        operation = "backup.restore"
        payload = {"_trusted_path": str(args.path.resolve())}
        if args.passphrase:
            import getpass

            payload["_passphrase"] = getpass.getpass("Backup passphrase: ")
    elif args.command == "models":
        operation = "catalog.list"
    elif args.command == "providers":
        operation = "providers.descriptors"
    elif args.command == "start":
        operation = "meetings.create"
        payload = {"title": args.title, "source": args.source, "project_id": args.project_id}
    token_file = args.token_file or configuration.get("token_file")
    if args.url or configuration.get("url"):
        require(
            token_file is not None,
            "token_required",
            "Remote loopback CLI needs --token-file.",
            status=422,
        )
        assert token_file is not None
        require(
            not any(k.startswith("_") for k in payload),
            "native_grant_required",
            "File operations over HTTP require a native file grant. Run offline with an exclusive workspace for local file operations.",
            status=422,
        )
        url = args.url or configuration["url"]
        require(
            url.startswith("http://127.0.0.1:"),
            "invalid_url",
            "CLI API URL must be explicit loopback.",
            status=422,
        )
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(
                url + "/api/v1/commands/" + operation,
                json=payload,
                headers={"Authorization": "Bearer " + Path(token_file).read_text().strip()},
            )
            result: JSONDocument = response.json()
            if response.is_error:
                raise AppError(
                    result.get("code", "operation_failed"),
                    result.get("message", "Operation failed."),
                    status=response.status_code,
                    details=result.get("details"),
                )
    else:
        root = args.workspace or Path(
            configuration.get("workspace", user_data_path("Meeting Assistant", "MeetingAssistant"))
        )
        w = Workspace(root / "workspace.sqlite3")
        try:
            result = await w.adispatch(operation, payload)
        finally:
            w.close()
    if args.command == "start" and args.source != "text":
        raise AppError(
            "native_capture_required",
            "Named draft created. Use the desktop/native host for live capture; engine-only mode does not pretend to record.",
            status=503,
            details=result,
        )
    if args.command in ("backup", "export") and getattr(args, "output", None):
        target = args.output
        with target.open("xb") as output:
            output.write(base64.b64decode(result["content"]))
        result = {key: value for key, value in result.items() if key != "content"}
        result["output"] = str(target)
    print(canonical(result) if args.json else json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    # Host frames and machine-readable CLI output use UTF-8 even on Windows pipes.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except AppError as exc:
        print(canonical(exc.as_dict()), file=sys.stderr)
        return (
            3
            if exc.status == 409
            else 4
            if exc.status == 503
            else 2
            if exc.status in (400, 422)
            else 1
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(canonical(AppError("invocation_failed", str(exc)).as_dict()), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
