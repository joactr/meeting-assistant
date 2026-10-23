"""Bounded rolling diarization and conservative meeting-local speaker matching."""

from __future__ import annotations

import asyncio
import math
import uuid
from collections import deque
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypedDict

from ...infrastructure.providers.contracts import ProviderError

if TYPE_CHECKING:
    from ...infrastructure.workers.client import WorkerClient


class KnownSpeaker(TypedDict):
    embedding: list[float]
    turns: list[dict[str, Any]]
    fingerprint: str


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right) or not left:
        return -1.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return dot / norm if norm else -1.0


class SpeakerReconciler:
    def __init__(
        self, model_fingerprint: str, *, threshold: float = 0.72, margin: float = 0.08
    ) -> None:
        self.model_fingerprint, self.threshold, self.margin = model_fingerprint, threshold, margin
        self.speakers: dict[str, KnownSpeaker] = {}

    def reconcile(self, result: dict[str, Any]) -> dict[str, Any]:
        if result["model_fingerprint"] != self.model_fingerprint:
            raise ProviderError(
                "invalid_input",
                "Speaker embeddings from distinct model fingerprints cannot be compared",
            )
        mapping: dict[str, str | None] = {}
        occupied: set[str] = set()
        embeddings: dict[str, list[float]] = result["speaker_embeddings"]
        for local, embedding in embeddings.items():
            turns: list[dict[str, Any]] = [
                turn for turn in result["exclusive_turns"] if turn["local_speaker_id"] == local
            ]
            duration = sum(turn["end_ms"] - turn["start_ms"] for turn in turns)
            # Insufficient voice evidence remains source-labelled, never adjacency-backfilled.
            if duration < 1000 or not all(math.isfinite(x) for x in embedding):
                mapping[local] = None
                continue
            candidates: list[tuple[float, int, bool, str]] = []
            for identity, known in self.speakers.items():
                if identity in occupied:
                    continue
                similarity = cosine(embedding, known["embedding"])
                temporal = sum(
                    max(0, min(a["end_ms"], b["end_ms"]) - max(a["start_ms"], b["start_ms"]))
                    for a in turns
                    for b in known["turns"]
                )
                contradictory = any(
                    max(a["start_ms"], b["start_ms"]) < min(a["end_ms"], b["end_ms"])
                    for a in turns
                    for other_id, other in self.speakers.items()
                    if other_id != identity
                    for b in other["turns"]
                )
                candidates.append((similarity, temporal, not contradictory, identity))
            candidates.sort(reverse=True)
            best = candidates[0] if candidates else None
            runner = candidates[1][0] if len(candidates) > 1 else -1
            identity = (
                best[3]
                if best
                and best[0] >= self.threshold
                and best[0] - runner >= self.margin
                and best[2]
                else str(uuid.uuid4())
            )
            if best and best[0] >= self.threshold and best[0] - runner < self.margin:
                mapping[local] = None
                continue
            mapping[local] = identity
            occupied.add(identity)
            self.speakers[identity] = {
                "embedding": embedding,
                "turns": turns,
                "fingerprint": self.model_fingerprint,
            }

        def mapped(turn: dict[str, Any]) -> dict[str, Any]:
            return {
                **turn,
                "meeting_speaker_id": mapping.get(turn["local_speaker_id"]),
                "identity_confidence": "suggestion"
                if mapping.get(turn["local_speaker_id"])
                else "unknown",
            }

        return {
            **result,
            "exclusive_turns": [mapped(turn) for turn in result["exclusive_turns"]],
            "turns": [mapped(turn) for turn in result["turns"]],
            "speaker_mapping": mapping,
        }


class RollingDiarizer:
    def __init__(
        self,
        worker: WorkerClient,
        model_fingerprint: str,
        *,
        sample_rate: int = 16000,
        window_seconds: int = 12,
        interval_seconds: int = 3,
        reconciler: SpeakerReconciler | None = None,
    ) -> None:
        self.worker, self.model_fingerprint = worker, model_fingerprint
        self.rate, self.window_frames, self.interval_frames = (
            sample_rate,
            sample_rate * window_seconds,
            sample_rate * interval_seconds,
        )
        self.chunks: deque[bytes] = deque()
        self.frames = 0
        self.total = 0
        self.last = 0
        self.lock = asyncio.Lock()
        self.reconciler = reconciler or SpeakerReconciler(model_fingerprint)

    async def push(
        self, pcm_float32: bytes, *, timeline_origin_ms: int = 0, session_id: str = "rolling"
    ) -> dict[str, Any] | None:
        if len(pcm_float32) % 4:
            raise ProviderError("invalid_input", "Rolling diarization requires float32 PCM")
        self.chunks.append(pcm_float32)
        count = len(pcm_float32) // 4
        self.frames += count
        self.total += count
        while self.frames > self.window_frames:
            first = self.chunks.popleft()
            excess = self.frames - self.window_frames
            first_frames = len(first) // 4
            if first_frames > excess:
                self.chunks.appendleft(first[excess * 4 :])
                self.frames -= excess
            else:
                self.frames -= first_frames
        if self.total - self.last < self.interval_frames or self.lock.locked():
            return None
        self.last = self.total
        async with self.lock:
            pcm = b"".join(self.chunks)
            origin = timeline_origin_ms + (self.total - self.frames) * 1000 // self.rate
            async with self.worker.pcm(pcm, self.rate) as grant:
                result = await self.worker.request(
                    "diarize",
                    {
                        **grant,
                        "origin_ms": origin,
                        "session_id": session_id,
                        "model_fingerprint": self.model_fingerprint,
                    },
                    timeout=180,
                )
            return self.reconciler.reconcile(result)

    async def finalize_available(
        self, *, timeline_origin_ms: int = 0, session_id: str = "final"
    ) -> dict[str, Any]:
        async with self.lock:
            if not self.chunks:
                return {
                    "exclusive_turns": [],
                    "turns": [],
                    "coverage": None,
                    "warning": "No retained or bounded-window audio is available",
                }
            pcm = b"".join(self.chunks)
            origin = timeline_origin_ms + (self.total - self.frames) * 1000 // self.rate
            async with self.worker.pcm(pcm, self.rate) as grant:
                result = await self.worker.request(
                    "diarize_final",
                    {
                        **grant,
                        "origin_ms": origin,
                        "session_id": session_id,
                        "model_fingerprint": self.model_fingerprint,
                    },
                    timeout=180,
                )
            return self.reconciler.reconcile(result)

    def clear(self) -> None:
        self.chunks.clear()
        self.frames = 0
