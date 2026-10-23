from __future__ import annotations

import asyncio
import contextlib
import hashlib
import secrets
import struct
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from meeting_assistant.domain.audio import pcm16k
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

HEADER = struct.Struct("<IHBBQQIIHHIQ")


@dataclass(frozen=True)
class Packet:
    sequence: int
    epoch: int
    source_index: int
    sample_rate: int
    frame_count: int
    monotonic_ns: int
    pcm: bytes


class AudioPipeline:
    def __init__(self, workspace: Workspace, token: str) -> None:
        self.workspace = workspace
        self.token = bytes.fromhex(token)
        self.server: asyncio.Server | None = None
        self.buffers: dict[tuple[int, int], deque[Packet]] = {}
        self.last_sequences: dict[tuple[int, int], int] = {}
        self.tasks: set[asyncio.Task[None]] = set()
        self.total_bytes: dict[tuple[int, int], int] = {}
        self.next_diarization_ns: dict[tuple[int, int], int] = {}
        self.diarization_task: asyncio.Task[dict[str, Any]] | None = None
        self.diarization_disabled: set[tuple[int, int]] = set()
        self.pending_ms: dict[tuple[int, int], int] = {}
        self.pause_task: asyncio.Task[dict[str, Any]] | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=1_048_624)
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.diarization_task:
            await asyncio.gather(self.diarization_task, return_exceptions=True)
        self.buffers.clear()
        self.total_bytes.clear()

    async def drain(self, boundaries: dict[str, Any] | None = None) -> None:
        if boundaries:
            deadline = asyncio.get_running_loop().time() + 5
            while not all(
                any(
                    key[1] == int(index) and seen >= int(sequence)
                    for key, seen in self.last_sequences.items()
                )
                for index, sequence in boundaries.items()
            ):
                require(
                    asyncio.get_running_loop().time() < deadline,
                    "audio_boundary_missing",
                    "Engine has not received the native drain boundary.",
                    status=503,
                    retryable=True,
                )
                await asyncio.sleep(0.01)
        if self.tasks:
            await asyncio.gather(*list(self.tasks))
        if self.diarization_task:
            await asyncio.gather(self.diarization_task, return_exceptions=True)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            supplied = await asyncio.wait_for(reader.readexactly(32), 5)
            require(
                secrets.compare_digest(supplied, self.token),
                "unauthorized_audio",
                "Audio stream authentication failed.",
                status=401,
            )
            while True:
                header = await reader.readexactly(48)
                (
                    length,
                    version,
                    kind,
                    flags,
                    sequence,
                    epoch,
                    index,
                    rate,
                    channels,
                    format_,
                    frames,
                    monotonic,
                ) = HEADER.unpack(header)
                require(
                    version == 1 and kind == 1 and flags == 0,
                    "audio_protocol",
                    "Unsupported audio header.",
                    status=422,
                )
                require(
                    44 <= length <= 1_048_576
                    and channels == 1
                    and format_ == 1
                    and rate in (16000, 48000)
                    and 1 <= frames <= rate // 50
                    and length == 44 + frames * channels * 4,
                    "audio_dimensions",
                    "Invalid audio frame dimensions.",
                    status=422,
                )
                key = (epoch, index)
                last = self.last_sequences.get(key)
                require(
                    last is None or sequence > last,
                    "audio_sequence",
                    "Audio sequence is not monotonic.",
                    status=409,
                )
                source = self._source(epoch, index)
                require(
                    source is not None,
                    "audio_epoch",
                    "Audio frame does not belong to a current active source.",
                    status=409,
                )
                assert source is not None
                pcm = await reader.readexactly(length - 44)
                if last is not None and sequence != last + 1:
                    self._gap(
                        source,
                        "sequence_gap",
                        max(0, (monotonic // 1_000_000) - 20),
                        {"expected_sequence": last + 1, "received_sequence": sequence},
                    )
                packet = Packet(sequence, epoch, index, rate, frames, monotonic, pcm)
                for old_key in list(self.buffers):
                    if old_key[1] == index and old_key[0] != epoch:
                        self.buffers.pop(old_key, None)
                        self.total_bytes.pop(old_key, None)
                        self.last_sequences.pop(old_key, None)
                queue = self.buffers.setdefault(key, deque())
                queue.append(packet)
                self.last_sequences[key] = sequence
                self.total_bytes[key] = self.total_bytes.get(key, 0) + len(pcm)
                meeting = self.workspace.require_item("meetings", source["meeting_id"])
                if (
                    key not in self.diarization_disabled
                    and meeting["config_snapshot"].get("diarization")
                    and meeting["config_snapshot"].get("diarization_policy")
                    and monotonic >= self.next_diarization_ns.get(key, 0)
                    and (self.diarization_task is None or self.diarization_task.done())
                ):
                    self.next_diarization_ns[key] = monotonic + 3_000_000_000
                    self.diarization_task = asyncio.create_task(
                        self.diarize_source(source, key, final=False)
                    )

                    def diarization_done(
                        done: asyncio.Task[dict[str, Any]],
                        source_key: tuple[int, int] = key,
                        source_id: str = source["id"],
                        meeting_id: str = source["meeting_id"],
                    ) -> None:
                        if done.cancelled():
                            return
                        try:
                            done.result()
                        except Exception as exc:
                            self.diarization_disabled.add(source_key)
                            self.workspace.emit(
                                "capture.attention",
                                {
                                    "source_id": source_id,
                                    "code": getattr(exc, "code", "diarization_failed"),
                                    "message": str(exc)
                                    if hasattr(exc, "code")
                                    else "Diarization failed; existing transcript and manual identities are preserved.",
                                },
                                meeting_id=meeting_id,
                            )

                    self.diarization_task.add_done_callback(diarization_done)
                while queue and monotonic - queue[0].monotonic_ns > 30_000_000_000:
                    oldest = queue.popleft()
                    self.total_bytes[key] -= len(oldest.pcm)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except AppError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    def _source(self, epoch: int, index: int) -> dict[str, Any] | None:
        for source in self.workspace.list("capture_sources", {"epoch": epoch}):
            if source.get("source_index") == index and source["state"] in (
                "active",
                "paused",
                "starting",
            ):
                return source
        return None

    async def diarize_source(
        self, source: dict[str, Any], key: tuple[int, int], *, final: bool
    ) -> dict[str, Any]:
        w = self.workspace
        meeting = w.require_item("meetings", source["meeting_id"])
        packets = list(self.buffers.get(key, []))
        require(
            bool(packets),
            "audio_unavailable",
            "No retained or bounded-window audio is available for diarization.",
            status=503,
        )
        cutoff = packets[-1].monotonic_ns - 12_000_000_000
        packets = [packet for packet in packets if packet.monotonic_ns >= cutoff]
        pcm = await asyncio.to_thread(
            pcm16k, b"".join(packet.pcm for packet in packets), packets[0].sample_rate
        )
        origin_ms = max(0, packets[0].monotonic_ns // 1_000_000)
        assert w.inference is not None
        result = await w.inference.diarize(
            pcm_float32=pcm,
            sample_rate=16000,
            policy=meeting["config_snapshot"]["diarization_policy"],
            origin_ms=origin_ms,
            final=final,
            meeting=meeting,
        )
        return w.speaker_evidence.apply(meeting["id"], source["source_kind"], result)

    def _gap(
        self, source: dict[str, Any], reason: str, start_ms: int, details: dict[str, Any]
    ) -> None:
        w = self.workspace
        with w.transaction():
            w.put(
                "timeline_gaps",
                new_id(),
                {
                    "meeting_id": source["meeting_id"],
                    "source_id": source["id"],
                    "start_ms": start_ms,
                    "reason": reason,
                    **details,
                },
            )
            meeting = w.require_item("meetings", source["meeting_id"])
            w.put(
                "meetings",
                meeting["id"],
                {"health": "degraded"},
                expected_revision=meeting["revision"],
            )

    async def utterance(self, p: dict[str, Any]) -> dict[str, Any]:
        epoch = int(p["capture_epoch"])
        index = int(p["source_index"])
        source = self._source(epoch, index)
        require(
            source is not None,
            "audio_epoch",
            "Utterance belongs to an unavailable capture source.",
            status=409,
        )
        assert source is not None
        require(
            int(p.get("true_speech_samples", 4000)) >= 4000,
            "insufficient_speech",
            "Utterance has less than 250 ms true speech.",
            status=422,
        )
        packets = list(self.buffers.get((epoch, index), []))
        require(
            bool(packets),
            "audio_unavailable",
            "Utterance audio is no longer in the bounded memory window.",
            status=503,
        )
        start = int(p["start_ms"])
        end = int(p["end_ms"])
        require(
            0 <= start < end and end - start <= 13000,
            "invalid_span",
            "Utterance duration is invalid.",
            status=422,
        )
        packets = [
            packet
            for packet in packets
            if packet.monotonic_ns + packet.frame_count * 1_000_000_000 // packet.sample_rate
            > start * 1_000_000
            and packet.monotonic_ns < end * 1_000_000
        ]
        require(
            bool(packets)
            and all(
                right.sequence == left.sequence + 1
                for left, right in zip(packets, packets[1:], strict=False)
            ),
            "audio_range_missing",
            "Speech overlaps a captured audio gap; no fabricated contiguous audio is submitted.",
            status=503,
        )
        key = (epoch, index)
        if self.pending_ms.get(key, 0) + (end - start) > 30000:
            self.workspace.emit(
                "capture.backpressure",
                {
                    "source_id": source["id"],
                    "backlog_ms": self.pending_ms.get(key, 0),
                    "action": "pausing_capture",
                },
                meeting_id=source["meeting_id"],
            )
            if self.pause_task is None or self.pause_task.done():
                self.pause_task = asyncio.create_task(
                    self.workspace.adispatch(
                        "meetings.pause",
                        {
                            "meeting_id": source["meeting_id"],
                            "idempotency_key": "backpressure:" + new_id(),
                        },
                    )
                )
            while self.pending_ms.get(key, 0) + (end - start) > 30000 and self.tasks:
                await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        # Native positions are absolute meeting-timeline counters, including pauses.
        start_sample = int(p.get("start_sample", start * 16))
        start_frame = start_sample * (packets[0].sample_rate // 16000)
        first_frame = packets[0].monotonic_ns * packets[0].sample_rate // 1_000_000_000
        require(
            start_frame >= first_frame - 3,
            "audio_range_missing",
            "Speech start was evicted from the bounded memory window.",
            status=503,
        )
        offset = max(0, start_frame - first_frame)
        count = (end - start) * (packets[0].sample_rate // 1000)
        pcm = b"".join(packet.pcm for packet in packets)
        pcm = pcm[offset * 4 : (offset + count) * 4]
        require(
            len(pcm) >= max(4, count * 4 - 3840),
            "audio_range_missing",
            "Utterance audio range is incomplete; a gap is recorded instead of fabricated speech.",
            status=503,
        )

        async def run() -> None:
            w = self.workspace
            meeting = w.require_item("meetings", source["meeting_id"])
            policy = meeting["config_snapshot"].get("asr_policy")
            require(
                w.inference is not None and bool(policy),
                "asr_unavailable",
                "Selected ASR profile/runtime is not ready.",
                status=503,
            )
            assert w.inference is not None
            assert policy is not None
            converted = await asyncio.to_thread(pcm16k, pcm, packets[0].sample_rate)
            compiled: dict[str, Any] = meeting["config_snapshot"].get("vocabulary", {})
            terms: list[dict[str, Any] | str] = compiled.get("terms", [])
            vocabulary: list[str] = (
                [compiled["prompt_hint"]]
                if compiled.get("prompt_hint")
                else [
                    hint
                    for term in terms
                    for hint in (
                        term.get("hints", [term.get("spelling", term.get("term", ""))])
                        if isinstance(term, dict)
                        else [term]
                    )
                ]
            )

            async def on_hypothesis(hypothesis: dict[str, Any]) -> None:
                await w.adispatch(
                    "transcript.ingest",
                    {
                        **hypothesis,
                        "meeting_id": source["meeting_id"],
                        "source_id": source["id"],
                        "capture_epoch": epoch,
                        "provider_stream_id": hypothesis.get("provider_stream_id", source["id"]),
                    },
                )

            results = await w.inference.transcribe(
                converted,
                sample_rate=16000,
                policy=policy,
                origin_ms=start,
                stream_id=source["id"],
                chunk_id=str(p.get("utterance_id", new_id())),
                vocabulary=vocabulary,
                language=meeting["config_snapshot"].get("meeting_language"),
                on_hypothesis=on_hypothesis,
            )
            for result in results:
                await w.adispatch(
                    "transcript.ingest",
                    {
                        **result,
                        "meeting_id": source["meeting_id"],
                        "source_id": source["id"],
                        "capture_epoch": epoch,
                        "provider_stream_id": result.get("provider_stream_id", source["id"]),
                    },
                )

        self.pending_ms[key] = self.pending_ms.get(key, 0) + (end - start)
        task = asyncio.create_task(run())
        self.tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            self.tasks.discard(done)
            self.pending_ms[key] = max(0, self.pending_ms.get(key, 0) - (end - start))
            try:
                done.result()
            except Exception as exc:
                w = self.workspace
                with w.transaction():
                    self._gap(
                        source,
                        "asr_failed",
                        start,
                        {
                            "end_ms": end,
                            "error_code": exc.code if isinstance(exc, AppError) else "asr_failed",
                        },
                    )
                    w.emit(
                        "capture.attention",
                        {
                            "source_id": source["id"],
                            "code": exc.code if isinstance(exc, AppError) else "asr_failed",
                        },
                        meeting_id=source["meeting_id"],
                    )

        task.add_done_callback(completed)
        return {"accepted": True, "source_id": source["id"], "start_ms": start, "end_ms": end}


def register_capture(w: Workspace, r: CommandRegistry) -> None:
    async def utterance(p: dict[str, Any]) -> dict[str, Any]:
        require(
            w.audio_pipeline is not None,
            "audio_unavailable",
            "Audio ingress is not running.",
            status=503,
        )
        assert w.audio_pipeline is not None
        return await w.audio_pipeline.utterance(p)

    r["capture.utterance"] = utterance

    def source_event(p: dict[str, Any]) -> dict[str, Any]:
        candidates = w.list("capture_sources", {"meeting_id": p["meeting_id"]})
        source = next((s for s in candidates if s.get("source_index") == p["source_index"]), None)
        require(source is not None, "source_missing", "Capture source not found.", status=404)
        assert source is not None
        with w.transaction():
            item = w.put(
                "capture_sources",
                source["id"],
                {"state": p["state"]},
                expected_revision=source["revision"],
            )
            if p["state"] == "lost":
                m = w.require_item("meetings", p["meeting_id"])
                w.put("meetings", m["id"], {"health": "degraded"}, expected_revision=m["revision"])
                w.put(
                    "timeline_gaps",
                    new_id(),
                    {
                        "meeting_id": m["id"],
                        "source_id": source["id"],
                        "start_ms": p.get("start_ms", m["elapsed_ms"]),
                        "reason": p.get("reason", "source_lost"),
                    },
                )
            return {"item": item}

    r["capture.source_event"] = source_event

    def commit(p: dict[str, Any]) -> dict[str, Any]:
        meeting = w.require_item("meetings", p["meeting_id"])
        require(
            meeting["config_snapshot"].get("recording", False),
            "recording_disabled",
            "Durable audio is disabled for this meeting.",
            status=403,
        )
        relative = "media/" + p["relative_path"]
        target = (w.root / relative).resolve()
        require(
            target.name == p["relative_path"]
            and target.parent == (w.root / "media").resolve()
            and target.is_file(),
            "unsafe_media_path",
            "Committed media must be a private workspace file.",
            status=422,
        )
        require(
            target.stat().st_size == p["bytes"]
            and hashlib.sha256(target.read_bytes()).hexdigest() == p["sha256"],
            "media_integrity",
            "Native media commit failed hash verification.",
            status=422,
        )
        mid = p.get("media_id", new_id())
        existing = w.get("media_segments", mid)
        if existing:
            return {"item": existing}
        source_id = p.get("source_id")
        if source_id and w.get("capture_sources", source_id) is None:
            source = next(
                (
                    s
                    for s in w.list("capture_sources", {"meeting_id": meeting["id"]})
                    if s.get("source_index") == p.get("source_index")
                ),
                None,
            )
            source_id = source["id"] if source else None
        retention = meeting["config_snapshot"].get("retention_days", 7)
        return {
            "item": w.put(
                "media_segments",
                mid,
                {
                    "meeting_id": meeting["id"],
                    "source_id": source_id,
                    "relative_path": relative,
                    "codec": p.get("codec", "ogg_opus"),
                    "rate": p.get("sample_rate", 48000),
                    "channels": 1,
                    "start_ms": p.get("start_ms", int(p.get("start_ns", 0)) // 1000000),
                    "duration_ms": int(p["frame_count"]) * 1000 // int(p.get("sample_rate", 48000)),
                    "sample_count": p["frame_count"],
                    "pre_skip_samples": p.get("pre_skip", 0),
                    "bytes": p["bytes"],
                    "sha256": p["sha256"],
                    "state": "committed",
                    "retain_until": now_ms() + int(retention) * 86400000 if retention else None,
                },
            )
        }

    r["media.commit"] = commit
