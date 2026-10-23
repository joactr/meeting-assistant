"""Honest chunk-session scheduling for non-streaming local recognizers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Self

from .contracts import ProviderError, TranscriptHypothesis

if TYPE_CHECKING:
    from ..workers.client import WorkerClient


class ChunkASRSession:
    def __init__(
        self,
        worker: WorkerClient,
        *,
        sample_rate: int,
        origin_ms: int = 0,
        language: str | None = None,
        vocabulary: list[str] | None = None,
        max_seconds: int = 30,
    ) -> None:
        self.worker, self.rate, self.origin = worker, sample_rate, origin_ms
        self.language, self.vocabulary = language, vocabulary or []
        self.max_frames = sample_rate * max_seconds
        self.buffer = bytearray()
        self.frames = 0
        self.stream_id = str(uuid.uuid4())
        self.chunk = 0
        self.queue: asyncio.Queue[TranscriptHypothesis | None] = asyncio.Queue(maxsize=32)
        self.closed = False
        self.lock = asyncio.Lock()

    async def push(self, pcm_float32: bytes) -> None:
        if self.closed or len(pcm_float32) % 4:
            raise ProviderError("invalid_input", "Closed chunk stream or invalid float32 PCM")
        self.buffer.extend(pcm_float32)
        if len(self.buffer) // 4 >= self.max_frames:
            await self.flush()

    async def flush(self) -> dict[str, Any]:
        async with self.lock:
            if self.buffer:
                value = bytes(self.buffer)
                self.buffer.clear()
                async with self.worker.pcm(value, self.rate) as grant:
                    result = await self.worker.request(
                        "transcribe",
                        {
                            **grant,
                            "origin_ms": self.origin + self.frames * 1000 // self.rate,
                            "stream_id": self.stream_id,
                            "chunk_id": str(self.chunk),
                            "language": self.language,
                            "vocabulary": self.vocabulary,
                        },
                    )
                self.frames += len(value) // 4
                self.chunk += 1
                for row in result.get("hypotheses", []):
                    await self.queue.put(TranscriptHypothesis.model_validate(row))
            return {"acknowledged": True, "frame_count": self.frames, "native_streaming": False}

    async def events(self) -> AsyncIterator[TranscriptHypothesis]:
        while True:
            event = await self.queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        await self.flush()
        self.closed = True
        await self.queue.put(None)

    async def abort(self) -> None:
        self.buffer.clear()
        self.closed = True
        await self.queue.put(None)


class AzureSDKSession:
    def __init__(
        self,
        worker: WorkerClient,
        *,
        sample_rate: int,
        origin_ms: int = 0,
        vocabulary: list[str] | None = None,
        languages: list[str] | None = None,
        diarization: bool = False,
    ) -> None:
        self.worker, self.rate = worker, sample_rate
        self.configuration: dict[str, Any] = {
            "sample_rate": sample_rate,
            "origin_ms": origin_ms,
            "vocabulary": vocabulary or [],
            "languages": languages or [],
            "diarization": diarization,
        }
        self.queue: asyncio.Queue[TranscriptHypothesis | None] = asyncio.Queue(maxsize=128)
        self.acknowledged_frames = 0
        self.opened = False

    async def open(self) -> Self:
        await self.worker.request("open_stream", self.configuration, timeout=20)
        self.opened = True
        return self

    async def _outputs(self, result: dict[str, Any]) -> None:
        for row in result.get("hypotheses", []):
            if "error" in row:
                raise ProviderError(**row["error"])
            await self.queue.put(TranscriptHypothesis.model_validate(row))

    async def push(self, pcm_float32: bytes) -> None:
        if not self.opened:
            self.configuration["origin_ms"] += self.acknowledged_frames * 1000 // self.rate
            self.acknowledged_frames = 0
            await self.open()
        async with self.worker.pcm(pcm_float32, self.rate) as grant:
            await self._outputs(await self.worker.request("push", grant, timeout=20))
        self.acknowledged_frames += len(pcm_float32) // 4

    async def flush(self) -> dict[str, Any]:
        if not self.opened:
            return {"acknowledged": True}
        result = await self.worker.request("flush", {}, timeout=20)
        await self._outputs(result)
        self.opened = False
        return {"acknowledged": result["acknowledged"], "frame_count": self.acknowledged_frames}

    async def events(self) -> AsyncIterator[TranscriptHypothesis]:
        while True:
            try:
                event = await asyncio.wait_for(self.queue.get(), 0.25)
            except TimeoutError:
                if self.opened:
                    await self._outputs(await self.worker.request("poll", {}, timeout=10))
                continue
            if event is None:
                return
            yield event

    async def close(self) -> None:
        await self.flush()
        await self.queue.put(None)

    async def abort(self) -> None:
        if self.opened:
            await self.worker.request("close_stream", {}, timeout=5)
        self.opened = False
        await self.queue.put(None)
