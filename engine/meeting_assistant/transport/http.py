from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical
from meeting_assistant.domain.ports import JSONDocument
from pydantic import ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

PRIVATE_OPERATIONS = frozenset(
    {
        "meetings.acknowledge",
        "capture.utterance",
        "capture.source_event",
        "capture.gap",
        "media.commit",
        "job.media_import",
        "job.retranscribe",
        "job.embedding_index",
        "job.memory_compress",
    }
)
READ_SUFFIXES = (
    ".list",
    ".get",
    ".read",
    ".history",
    ".inbox",
    ".status",
    ".descriptors",
    ".capabilities",
    ".events",
    ".attention",
    ".snapshot",
    ".analytics",
    ".diff",
    ".preview",
)


class Authorization:
    def __init__(self, token: str, *, allowed_origins: set[str] | None = None) -> None:
        require(
            len(token) >= 64,
            "weak_token",
            "Engine requires a random 256-bit launch token.",
            status=422,
        )
        self.tokens = {
            hashlib.sha256(token.encode()).digest(): frozenset(
                {"host", "read", "write", "diagnostics", "files"}
            )
        }
        self.allowed_origins = allowed_origins or set()

    def grant(self, token: str, scopes: set[str]) -> None:
        require(
            scopes <= {"read", "write", "diagnostics", "files"},
            "invalid_scope",
            "Public grants cannot acquire native host authority.",
            status=422,
        )
        self.tokens[hashlib.sha256(token.encode()).digest()] = frozenset(scopes)

    def authenticate(
        self, headers: Mapping[str, str], client: str | None, operation: str
    ) -> frozenset[str]:
        require(
            client in ("127.0.0.1", "::1", "testclient"),
            "remote_client",
            "Only loopback clients are accepted.",
            status=403,
        )
        host = headers.get("host", "")
        parsed = urlsplit("//" + host)
        require(
            parsed.hostname in ("127.0.0.1", "localhost", "::1", "testserver"),
            "invalid_host",
            "Host must identify this loopback engine.",
            status=403,
        )
        origin = headers.get("origin")
        require(
            origin is None or origin in self.allowed_origins,
            "origin_rejected",
            "Browser origins cannot access the local engine.",
            status=403,
        )
        authorization = headers.get("authorization", "")
        require(
            authorization.startswith("Bearer "),
            "unauthorized",
            "A bearer capability token is required.",
            status=401,
        )
        supplied = hashlib.sha256(authorization[7:].encode()).digest()
        scopes = None
        for digest, grant in self.tokens.items():
            if secrets.compare_digest(digest, supplied):
                scopes = grant
        require(
            scopes is not None,
            "unauthorized",
            "Bearer capability is invalid or revoked.",
            status=401,
        )
        assert scopes is not None
        required = (
            "host"
            if operation in PRIVATE_OPERATIONS
            else "diagnostics"
            if operation == "diagnostics" or operation.startswith("diagnostics.")
            else "files"
            if operation.startswith(("backup.", "legacy.", "media.import", "media.retranscribe"))
            else "read"
            if operation.endswith(READ_SUFFIXES) or operation in ("search.query", "events")
            else "write"
        )
        require(
            required in scopes,
            "scope_forbidden",
            "This capability does not permit the requested operation.",
            status=403,
            details={"required_scope": required},
        )
        return scopes


class _RequestGuard:
    def __init__(self, app: ASGIApp, authorization: Authorization) -> None:
        self.app = app
        self.authorization = authorization

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        body: bytes | None = None
        try:
            health = request.url.path in ("/api/v1/health", "/health")
            # Health reveals no workspace text or credentials, but still has loopback Host checks.
            if not health:
                operation = (
                    request.path_params.get("operation_id") or request.url.path.rsplit("/", 1)[-1]
                )
                if "/commands/" in request.url.path:
                    operation = request.url.path.split("/commands/", 1)[1]
                self.authorization.authenticate(
                    request.headers, request.client.host if request.client else None, operation
                )
            else:
                require(
                    request.client is not None
                    and request.client.host in ("127.0.0.1", "::1", "testclient"),
                    "remote_client",
                    "Health is loopback only.",
                    status=403,
                )
                require(
                    urlsplit("//" + request.headers.get("host", "")).hostname
                    in ("127.0.0.1", "localhost", "::1", "testserver"),
                    "invalid_host",
                    "Invalid health Host.",
                    status=403,
                )
                require(
                    request.headers.get("origin") is None,
                    "origin_rejected",
                    "Browser origins cannot access health.",
                    status=403,
                )
            if request.method in ("POST", "PUT", "PATCH"):
                require(
                    request.headers.get("content-type", "").split(";")[0] == "application/json",
                    "json_required",
                    "Commands require application/json.",
                    status=400,
                )
                length = request.headers.get("content-length")
                if length:
                    require(
                        length.isdigit() and int(length) <= 1048576,
                        "request_too_large",
                        "JSON requests are limited to 1 MiB.",
                        status=413,
                    )
                data = bytearray()
                async for chunk in request.stream():
                    require(
                        len(data) + len(chunk) <= 1048576,
                        "request_too_large",
                        "JSON requests are limited to 1 MiB.",
                        status=413,
                    )
                    data.extend(chunk)
                body = bytes(data)
                del data
        except AppError as exc:
            await JSONResponse(exc.as_dict(), status_code=exc.status)(scope, receive, send)
            return

        if body is None:
            await self.app(scope, receive, send)
            return

        async def replay_body() -> Message:
            nonlocal body
            if body is None:
                return await receive()
            message: Message = {"type": "http.request", "body": body, "more_body": False}
            body = None
            return message

        await self.app(scope, replay_body, send)


def create_app(
    workspace: Workspace,
    token: str,
    *,
    manage_lifespan: bool = True,
    authorization: Authorization | None = None,
) -> FastAPI:
    auth = authorization or Authorization(token)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        if manage_lifespan:
            await workspace.start()
        try:
            yield
        finally:
            if manage_lifespan:
                await workspace.stop()

    app = FastAPI(
        title="Meeting Assistant local API",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.workspace = workspace
    app.state.authorization = auth

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(exc.as_dict(), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            AppError(
                "validation_error",
                "Request validation failed.",
                status=422,
                details={"fields": [list(error["loc"]) for error in exc.errors()]},
            ).as_dict(),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            AppError(
                "internal_error",
                "An unexpected engine error occurred. See local diagnostics by trace ID.",
                status=500,
                details={"exception_type": type(exc).__name__},
            ).as_dict(),
            status_code=500,
        )

    app.add_middleware(_RequestGuard, authorization=auth)

    @app.get("/api/v1/health")
    async def health() -> JSONDocument:
        return {
            "protocol_major": 1,
            "build_id": workspace.build_id,
            "ready": not workspace.stopping,
            "heartbeat_at_ms": workspace.now_ms(),
            "capabilities": {
                "keyword_search": True,
                "capture": workspace.native_gateway is not None,
            },
        }

    @app.get("/api/v1/diagnostics")
    async def diagnostics() -> JSONDocument:
        return {
            "build_id": workspace.build_id,
            "schema_version": workspace.connection.execute("PRAGMA user_version").fetchone()[0],
            "job_counts": {
                row[0]: row[1]
                for row in workspace.connection.execute(
                    "SELECT status,COUNT(*) FROM jobs GROUP BY status"
                )
            },
            "sensitive_data_included": False,
        }

    async def execute(operation_id: str, payload: JSONDocument, request: Request) -> JSONResponse:
        auth.authenticate(
            request.headers, request.client.host if request.client else None, operation_id
        )
        require(
            not any(key.startswith("_") for key in payload),
            "private_field",
            "Private execution fields are not accepted over HTTP.",
            status=403,
        )
        handler = workspace.registry.get(operation_id)
        model = getattr(handler, "__command_model__", None)
        if model:
            try:
                payload = model.model_validate(payload).model_dump(exclude_none=True)
            except ValidationError as exc:
                raise AppError(
                    "validation_error",
                    "Command payload validation failed.",
                    status=422,
                    details={"fields": [list(e["loc"]) for e in exc.errors()]},
                ) from exc
        try:
            result = await workspace.adispatch(operation_id, payload)
        except KeyError as exc:
            raise AppError(
                "missing_field",
                "Required command field is missing.",
                status=422,
                details={"field": str(exc)},
            ) from exc
        except (TypeError, ValueError) as exc:
            raise AppError(
                "invalid_value",
                "Command contains an invalid field value.",
                status=422,
                details={"exception_type": type(exc).__name__},
            ) from exc
        return JSONResponse(
            result,
            status_code=202 if result.get("status") == "accepted" or result.get("job") else 200,
        )

    @app.post("/api/v1/commands/{operation_id}", operation_id="application_command")
    async def command(operation_id: str, request: Request) -> JSONResponse:
        try:
            payload: object = await request.json()
        except json.JSONDecodeError as exc:
            raise AppError("malformed_json", "Command JSON is malformed.", status=400) from exc
        require(
            isinstance(payload, dict),
            "invalid_payload",
            "Command payload must be an object.",
            status=422,
        )
        return await execute(operation_id, cast(JSONDocument, payload), request)

    @app.get("/api/v1/meetings")
    async def meetings(request: Request) -> JSONResponse:
        return await execute("meetings.list", dict(request.query_params), request)

    @app.post("/api/v1/meetings")
    async def create_meeting(request: Request) -> JSONResponse:
        return await execute("meetings.create", await request.json(), request)

    @app.get("/api/v1/meetings/{meeting_id}")
    async def get_meeting(meeting_id: str, request: Request) -> JSONResponse:
        return await execute("meetings.get", {"id": meeting_id}, request)

    @app.patch("/api/v1/meetings/{meeting_id}")
    async def patch_meeting(meeting_id: str, request: Request) -> JSONResponse:
        p: JSONDocument = await request.json()
        p["id"] = meeting_id
        if request.headers.get("if-match"):
            p["expected_revision"] = int(request.headers["if-match"].strip('"'))
        return await execute("meetings.update", p, request)

    @app.delete("/api/v1/meetings/{meeting_id}")
    async def delete_meeting(meeting_id: str, request: Request) -> JSONResponse:
        return await execute(
            "meetings.delete",
            {
                "id": meeting_id,
                "expected_revision": int(request.headers.get("if-match", "0").strip('"')),
                "confirm": request.query_params.get("confirm") == "true",
            },
            request,
        )

    @app.post("/api/v1/meetings/{meeting_id}/{action}")
    async def meeting_action(meeting_id: str, action: str, request: Request) -> JSONResponse:
        require(
            action in ("start", "pause", "resume", "stop", "retry"),
            "unknown_operation",
            "Meeting action does not exist.",
            status=404,
        )
        return await execute(
            "meetings." + action, {**await request.json(), "meeting_id": meeting_id}, request
        )

    @app.get("/api/v1/meetings/{meeting_id}/transcript")
    async def get_transcript(meeting_id: str, request: Request) -> JSONResponse:
        return await execute(
            "transcript.list", {"meeting_id": meeting_id, **dict(request.query_params)}, request
        )

    @app.get("/api/v1/jobs")
    async def jobs(request: Request) -> JSONResponse:
        return await execute("jobs.list", {}, request)

    @app.get("/api/v1/search")
    async def search(request: Request) -> JSONResponse:
        return await execute("search.query", dict(request.query_params), request)

    @app.post("/api/v1/ask")
    async def ask(request: Request) -> JSONResponse:
        return await execute("ask.query", await request.json(), request)

    @app.post("/api/v1/ask/stream")
    async def ask_stream(request: Request) -> StreamingResponse:
        auth.authenticate(
            request.headers, request.client.host if request.client else None, "ask.query"
        )
        result = await workspace.adispatch("ask.query", await request.json())

        async def stream() -> AsyncGenerator[str]:
            yield "event: answer\ndata: " + canonical(result) + "\n\n"
            yield "event: complete\ndata: {}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    @app.websocket("/api/v1/events")
    async def events(socket: WebSocket) -> None:
        live = None
        try:
            auth.authenticate(
                socket.headers, socket.client.host if socket.client else None, "events"
            )
            after = int(socket.query_params.get("after_seq", "0"))
            require(after >= 0, "invalid_cursor", "Event sequence cannot be negative.", status=422)
            meeting_id = socket.query_params.get("meeting_id")
            await socket.accept()
            live = workspace.subscribe_live()
            while not workspace.stopping:
                batch = workspace.events(after, meeting_id, limit=200)
                if batch["resync_required"]:
                    await socket.send_json(
                        {"type": "resync_required", "watermark": batch["watermark"]}
                    )
                    await socket.close(code=4009, reason="resync_required")
                    return
                for event in batch["items"]:
                    try:
                        await asyncio.wait_for(socket.send_json(event), 2)
                    except TimeoutError:
                        await socket.close(code=4009, reason="slow_consumer_resync")
                        return
                    after = event["seq"]
                # Filtered streams advance over unrelated durable events without leaking their payloads.
                if not batch["items"]:
                    after = max(after, batch["watermark"])
                while not live.empty():
                    event = live.get_nowait()
                    if meeting_id and event["meeting_id"] != meeting_id:
                        continue
                    if workspace.get("meetings", event["meeting_id"]) is None:
                        continue
                    try:
                        await asyncio.wait_for(socket.send_json(event), 2)
                    except TimeoutError:
                        await socket.close(code=4009, reason="slow_consumer_resync")
                        return
                await asyncio.sleep(0.1)
        except AppError as exc:
            await socket.close(code=4401 if exc.status == 401 else 4403, reason=exc.code)
        except (WebSocketDisconnect, ConnectionError):
            return
        finally:
            if live is not None:
                workspace.unsubscribe_live(live)

    return app
