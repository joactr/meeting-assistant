"""Standalone worker protocol: bounded JSON controls and shared-memory PCM.

This module intentionally imports only the standard library. Worker isolation is
for fault containment, not a hostile-code security sandbox.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import struct
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from queue import Empty, Queue
from typing import Any, BinaryIO, Literal, NotRequired, Protocol, Self, TypedDict, cast

MAX_CONTROL_BYTES = 4 * 1024 * 1024
PROTOCOL_MAJOR = 1


class ControlRequest(TypedDict):
    id: str
    operation: str
    payload: NotRequired[dict[str, Any]]


class ControlError(TypedDict):
    code: str
    message: str
    retryable: bool
    exception_type: NotRequired[str]


class ControlResponse(TypedDict):
    type: Literal["result", "error", "cancel_ack", "ready", "unloaded"]
    id: NotRequired[str | None]
    payload: NotRequired[dict[str, Any]]
    error: NotRequired[ControlError]


class WorkerRuntime(Protocol):
    def capabilities(self) -> dict[str, Any]: ...

    def run(
        self, operation: str, payload: dict[str, Any], cancelled: threading.Event
    ) -> dict[str, Any]: ...

    def close(self) -> None: ...


class RuntimeFailure(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def read_frame(stream: BinaryIO) -> ControlRequest | None:
    prefix = stream.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise ValueError("Truncated control prefix")
    size = struct.unpack("<I", prefix)[0]
    if not 0 < size <= MAX_CONTROL_BYTES:
        raise ValueError("Control frame exceeds limit")
    content = bytearray()
    while len(content) < size:
        part = stream.read(size - len(content))
        if not part:
            raise ValueError("Truncated control frame")
        content.extend(part)
    return cast(ControlRequest, json.loads(content))


def write_frame(stream: BinaryIO, value: dict[str, Any] | ControlRequest | ControlResponse) -> None:
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(encoded) > MAX_CONTROL_BYTES:
        raise ValueError("Result frame exceeds limit")
    stream.write(struct.pack("<I", len(encoded)))
    stream.write(encoded)
    stream.flush()


class PCMGrant:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.handle = payload["pcm_handle"]
        self.offset = int(payload.get("byte_offset", 0))
        self.length = int(payload["byte_length"])
        self.rate = int(payload["sample_rate"])
        self.frames = int(payload["frame_count"])
        self.memory: SharedMemory | None = None
        self.view: memoryview | None = None

    def __enter__(self) -> Self:
        if (
            self.offset < 0
            or self.length < 0
            or self.rate <= 0
            or self.frames < 0
            or self.length != self.frames * 4
            or self.length > 512 * 1024 * 1024
        ):
            raise ValueError("Invalid float32 mono PCM grant")
        self.memory = SharedMemory(name=self.handle, create=False)
        if self.offset + self.length > self.memory.size:
            self.memory.close()
            raise ValueError("PCM grant exceeds shared allocation")
        assert self.memory.buf is not None
        self.view = self.memory.buf[self.offset : self.offset + self.length]
        return self

    def __exit__(self, *_: object) -> None:
        if self.view is not None:
            self.view.release()
        if self.memory is not None:
            self.memory.close()


def serve(
    runtime_factory: Callable[[Path | None, dict[str, Any]], WorkerRuntime], family: str
) -> None:
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    output = sys.stdout.buffer
    sys.stdout = sys.stderr
    source = sys.stdin.buffer
    incoming: Queue[ControlRequest | None] = Queue(maxsize=32)

    def reader() -> None:
        try:
            while True:
                frame = read_frame(source)
                incoming.put(frame)
                if frame is None or frame.get("operation") == "shutdown":
                    return
        except Exception:
            incoming.put(None)

    threading.Thread(target=reader, daemon=True).start()
    runtime: WorkerRuntime | None = None
    active: concurrent.futures.Future[dict[str, Any]] | None = None
    active_id: str | None = None
    cancelled, last_used, residency = threading.Event(), time.monotonic(), 300
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=family)

    def send(value: ControlResponse) -> None:
        write_frame(output, value)

    try:
        while True:
            if active is not None and active.done():
                try:
                    result = active.result()
                    send({"id": active_id, "type": "result", "payload": result})
                except ModuleNotFoundError:
                    send(
                        {
                            "id": active_id,
                            "type": "error",
                            "error": {
                                "code": "model_missing",
                                "message": "Worker dependency is unavailable",
                                "retryable": False,
                            },
                        }
                    )
                except FileNotFoundError:
                    send(
                        {
                            "id": active_id,
                            "type": "error",
                            "error": {
                                "code": "model_missing",
                                "message": "Granted model files are missing",
                                "retryable": False,
                            },
                        }
                    )
                except InterruptedError:
                    send(
                        {
                            "id": active_id,
                            "type": "error",
                            "error": {
                                "code": "cancelled",
                                "message": "Worker operation cancelled",
                                "retryable": False,
                            },
                        }
                    )
                except RuntimeFailure as exc:
                    send(
                        {
                            "id": active_id,
                            "type": "error",
                            "error": {"code": exc.code, "message": exc.message, "retryable": False},
                        }
                    )
                except Exception as exc:
                    send(
                        {
                            "id": active_id,
                            "type": "error",
                            "error": {
                                "code": "invalid_input"
                                if isinstance(exc, ValueError)
                                else "runtime_crash",
                                "message": "Worker could not complete inference",
                                "retryable": False,
                                "exception_type": type(exc).__name__,
                            },
                        }
                    )
                active, active_id, last_used = None, None, time.monotonic()
            try:
                frame = incoming.get(timeout=0.05)
            except Empty:
                if (
                    runtime is not None
                    and active is None
                    and residency > 0
                    and time.monotonic() - last_used > residency
                ):
                    runtime.close()
                    runtime = None
                    send({"type": "unloaded", "payload": {"reason": "idle"}})
                continue
            if frame is None:
                break
            operation, request_id, payload = (
                frame.get("operation"),
                frame.get("id"),
                frame.get("payload", {}),
            )
            if operation == "cancel":
                cancelled.set()
                send({"id": request_id, "type": "cancel_ack", "payload": {"active_id": active_id}})
            elif operation == "shutdown":
                cancelled.set()
                break
            elif active is not None:
                send(
                    {
                        "id": request_id,
                        "type": "error",
                        "error": {
                            "code": "unsupported",
                            "message": "Worker already has an active operation",
                            "retryable": True,
                        },
                    }
                )
            elif operation == "initialize":
                if int(payload.get("protocol_major", 0)) != PROTOCOL_MAJOR:
                    raise ValueError("Worker protocol mismatch")
                model_dir = (
                    Path(payload["model_directory"]).resolve(strict=True)
                    if payload.get("model_directory")
                    else None
                )
                if model_dir and not model_dir.is_dir():
                    raise ValueError("Model grant must be a directory")
                expected_package = payload.get("package_sha256")
                executable_hash = hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest()
                if expected_package and expected_package != executable_hash:
                    raise ValueError("Runtime executable hash mismatch")
                try:
                    runtime = runtime_factory(model_dir, payload.get("options", {}))
                    residency = int(payload.get("residency_seconds", 300))
                    send(
                        {
                            "id": request_id,
                            "type": "ready",
                            "payload": {
                                "protocol_major": PROTOCOL_MAJOR,
                                "family": family,
                                "package_sha256": executable_hash,
                                "model_fingerprint": payload["model_fingerprint"],
                                "capabilities": runtime.capabilities(),
                            },
                        }
                    )
                except (ModuleNotFoundError, FileNotFoundError):
                    send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {
                                "code": "model_missing",
                                "message": "Granted runtime dependency or model is unavailable",
                                "retryable": False,
                            },
                        }
                    )
                except RuntimeFailure as exc:
                    # Curated, path-free, actionable message chosen by first-party worker code.
                    send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {"code": exc.code, "message": exc.message, "retryable": False},
                        }
                    )
                except Exception as exc:
                    send(
                        {
                            "id": request_id,
                            "type": "error",
                            "error": {
                                "code": "invalid_input"
                                if isinstance(exc, ValueError)
                                else "runtime_crash",
                                "message": "Selected runtime could not initialize",
                                "retryable": False,
                            },
                        }
                    )
            elif runtime is None:
                send(
                    {
                        "id": request_id,
                        "type": "error",
                        "error": {
                            "code": "model_missing",
                            "message": "Worker is unloaded; initialize a granted model first",
                            "retryable": False,
                        },
                    }
                )
            else:
                cancelled.clear()
                active_id = request_id
                active = executor.submit(runtime.run, operation, payload, cancelled)
    finally:
        cancelled.set()
        if runtime is not None and active is None:
            runtime.close()
        executor.shutdown(wait=False, cancel_futures=True)
