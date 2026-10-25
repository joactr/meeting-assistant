"""Supervised, explicitly granted executables; no PATH/interpreter fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import struct
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any, TypedDict

from ..providers.contracts import ProviderError
from .runtime import MAX_CONTROL_BYTES, ControlError, ControlRequest


class _ProcessOptions(TypedDict, total=False):
    creationflags: int
    start_new_session: bool


class WorkerClient:
    def __init__(
        self,
        executable: Path,
        package_sha256: str,
        family: str,
        *,
        startup_timeout: float = 120,
        shutdown_grace: float = 5,
    ) -> None:
        self.executable, self.package_sha256, self.family = executable, package_sha256, family
        self.startup_timeout = 180 if family == "pyannote" else startup_timeout
        self.shutdown_grace = shutdown_grace
        self.process: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._buffers: dict[str, SharedMemory] = {}
        self.capabilities: dict[str, Any] | None = None
        self.model_fingerprint: str | None = None

    async def start(
        self,
        model_directory: Path | None,
        model_fingerprint: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        executable = self.executable.resolve(strict=True)
        with executable.open("rb") as source:
            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if not executable.is_file() or actual_hash != self.package_sha256:
            raise ProviderError(
                "permission", "Worker executable does not match the reviewed package hash"
            )
        safe_env = {
            k: v
            for k, v in os.environ.items()
            if k.upper()
            in {
                "SYSTEMROOT",
                "WINDIR",
                "TEMP",
                "TMP",
                "LOCALAPPDATA",
                "APPDATA",
                "HOME",
                "USERPROFILE",
                "LANG",
                "LC_ALL",
                "PATH",
                "PROGRAMFILES",
                "PROGRAMW6432",
            }
        }
        safe_env.update(
            PYANNOTE_METRICS_ENABLED="0",
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            PYTHONNOUSERSITE="1",
        )
        flags: _ProcessOptions = (
            {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}
        )
        self.process = await asyncio.create_subprocess_exec(
            str(executable),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=safe_env,
            **flags,
        )
        self._reader = asyncio.create_task(self._read())
        # Windows: loading a DLL whose static CRT probes the std handles (scipy's OpenBLAS in
        # the pyannote pack) blocks behind the worker's pending synchronous stdin read until a
        # frame arrives. Harmless "cancel" frames while initializing keep that read completing.
        # ponytail: drop once worker packs import heavy modules before starting their reader.
        nudge = asyncio.create_task(self._nudge())
        try:
            result = await self.request(
                "initialize",
                {
                    "protocol_major": 1,
                    "package_sha256": self.package_sha256,
                    "model_directory": str(model_directory.resolve(strict=True))
                    if model_directory
                    else None,
                    "model_fingerprint": model_fingerprint,
                    "options": options or {},
                    "residency_seconds": (options or {}).get("residency_seconds", 300),
                },
                timeout=self.startup_timeout,
            )
            if (
                result.get("protocol_major") != 1
                or result.get("package_sha256") != self.package_sha256
                or result.get("model_fingerprint") != model_fingerprint
            ):
                raise ProviderError(
                    "runtime_crash", "Worker handshake did not match requested runtime/model"
                )
            self.capabilities, self.model_fingerprint = result["capabilities"], model_fingerprint
            return result
        except BaseException:
            await self.terminate()
            raise
        finally:
            nudge.cancel()

    async def _nudge(self) -> None:
        frame = json.dumps({"id": "nudge", "operation": "cancel", "payload": {}}).encode()
        while self.process is not None and self.process.stdin is not None:
            await asyncio.sleep(2)
            try:
                self.process.stdin.write(struct.pack("<I", len(frame)) + frame)
            except (ConnectionError, RuntimeError):
                return

    async def _read(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                prefix = await self.process.stdout.readexactly(4)
                size = struct.unpack("<I", prefix)[0]
                if not 0 < size <= MAX_CONTROL_BYTES:
                    raise ValueError("Invalid worker control frame")
                frame: dict[str, Any] = json.loads(await self.process.stdout.readexactly(size))
                if frame.get("type") == "unloaded":
                    self.capabilities, self.model_fingerprint = None, None
                    continue
                request_id = frame.get("id")
                future = self._pending.pop(request_id, None) if request_id is not None else None
                if future is None or future.done():
                    continue
                if frame["type"] == "error":
                    error: ControlError = frame["error"]
                    future.set_exception(
                        ProviderError(
                            error["code"], error["message"], retryable=error.get("retryable", False)
                        )
                    )
                else:
                    future.set_result(frame.get("payload", {}))
        except (asyncio.IncompleteReadError, ValueError, ConnectionError):
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        ProviderError(
                            "runtime_crash", "Inference worker stopped unexpectedly", retryable=True
                        )
                    )
            self._pending.clear()

    async def _send(
        self, operation: str, payload: dict[str, Any]
    ) -> tuple[str, asyncio.Future[dict[str, Any]]]:
        if self.process is None or self.process.returncode is not None:
            raise ProviderError("runtime_crash", "Inference worker is not running")
        assert self.process.stdin is not None
        request_id = str(uuid.uuid4())
        request: ControlRequest = {"id": request_id, "operation": operation, "payload": payload}
        frame = json.dumps(request, allow_nan=False).encode()
        if len(frame) > MAX_CONTROL_BYTES:
            raise ProviderError("invalid_input", "Worker control request exceeds bounds")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        self.process.stdin.write(struct.pack("<I", len(frame)) + frame)
        await self.process.stdin.drain()
        return request_id, future

    async def request(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        timeout: float = 120,
        cancelled: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        async with self.lock:
            request_id, future = await self._send(operation, payload)
            cancellation = asyncio.create_task(cancelled.wait()) if cancelled is not None else None
            try:
                waiters: list[asyncio.Future[dict[str, Any]] | asyncio.Task[bool]] = [future]
                if cancellation is not None:
                    waiters.append(cancellation)
                done, _ = await asyncio.wait(
                    waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if future in done:
                    return future.result()
                _, cancel_ack = await self._send("cancel", {"operation_id": request_id})
                try:
                    await asyncio.wait_for(
                        asyncio.gather(asyncio.shield(future), cancel_ack), self.shutdown_grace
                    )
                except (TimeoutError, ProviderError):
                    await self.terminate()
                raise ProviderError(
                    "cancelled" if cancellation in done else "timeout",
                    "Worker operation cancelled"
                    if cancellation in done
                    else "Worker exceeded its deadline",
                    retryable=cancellation not in done,
                )
            finally:
                if cancellation:
                    cancellation.cancel()
                self._pending.pop(request_id, None)

    @asynccontextmanager
    async def pcm(self, pcm_float32: bytes, sample_rate: int) -> AsyncGenerator[dict[str, Any]]:
        if len(pcm_float32) % 4 or sample_rate <= 0:
            raise ProviderError("invalid_input", "Expected mono float32 PCM")
        memory = SharedMemory(create=True, size=max(1, len(pcm_float32)))
        self._buffers[memory.name] = memory
        assert memory.buf is not None
        memory.buf[: len(pcm_float32)] = pcm_float32
        try:
            yield {
                "pcm_handle": memory.name,
                "byte_offset": 0,
                "byte_length": len(pcm_float32),
                "frame_count": len(pcm_float32) // 4,
                "sample_rate": sample_rate,
            }
        finally:
            self._buffers.pop(memory.name, None)
            memory.close()
            memory.unlink()

    async def terminate(self) -> None:
        for future in self._pending.values():
            future.cancel()
        self._pending.clear()
        if self.process is not None:
            if self.process.returncode is None:
                self.process.kill()
            await self.process.wait()
            if self.process.stdin is not None:
                self.process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await self.process.stdin.wait_closed()
        if self._reader:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        for memory in list(self._buffers.values()):
            memory.close()
            memory.unlink()
        self._buffers.clear()

    async def close(self) -> None:
        try:
            if self.process is not None and self.process.returncode is None:
                request_id, future = await self._send("shutdown", {})
                self._pending.pop(request_id, None)
                future.cancel()
                if self.process.stdin is not None:
                    self.process.stdin.close()
                await asyncio.wait_for(self.process.wait(), self.shutdown_grace)
        finally:
            await self.terminate()
