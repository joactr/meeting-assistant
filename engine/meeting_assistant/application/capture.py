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
        # End of the last window that diarized successfully; audio after it is not labelled yet.
        self.diarized_ns: dict[tuple[int, int], int] = {}
        self.diarization_task: asyncio.Task[dict[str, Any]] | None = None
        self.diarization_disabled: set[tuple[int, int]] = set()
        self.pending_ms: dict[tuple[int, int], int] = {}
        self.misses: dict[tuple[int, int], int] = {}
        # Sources whose audio socket is open: their packets can still arrive.
        self.streaming: set[tuple[int, int]] = set()
        # (epoch, index) -> (workspace.capture_version, source, meeting) for the packet loop.
        self.lookups: dict[
            tuple[int, int], tuple[int, dict[str, Any] | None, dict[str, Any] | None]
        ] = {}

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

    async def drain(self, meeting_id: str, boundaries: dict[str, Any] | None = None) -> None:
        """Wait for native boundaries and in-flight ASR. A boundary that never arrives becomes a
        recorded gap: pause/stop must still transition, otherwise native and engine disagree."""
        missing: list[int] = []
        if boundaries:
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                missing = [
                    int(index)
                    for index, sequence in boundaries.items()
                    if not any(
                        key[1] == int(index) and seen >= int(sequence)
                        for key, seen in self.last_sequences.items()
                    )
                ]
                if not missing or asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.01)
        if self.tasks:
            # Failed ASR tasks already recorded their own gaps.
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.diarization_task:
            await asyncio.gather(self.diarization_task, return_exceptions=True)
        for source in self.workspace.list("capture_sources", {"meeting_id": meeting_id}):
            if source.get("source_index") in missing:
                last = [
                    q[-1].monotonic_ns
                    for k, q in self.buffers.items()
                    if k[1] == source["source_index"] and q
                ]
                self.record_gap(source, "drain_incomplete", last[0] // 1_000_000 if last else 0, {})

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        streamed: set[tuple[int, int]] = set()
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
                # The frame is read before any per-packet check so the stream stays framed: one
                # stale packet (old epoch, replayed sequence) is dropped, not the shared socket,
                # which would stop every source with it.
                pcm = await reader.readexactly(length - 44)
                last = self.last_sequences.get(key)
                if last is not None and sequence <= last:
                    continue
                # 50 packets/s per source: read the source and meeting again only after one of
                # them was written (pause, resume, retry, stop, settings), not on every packet.
                cached = self.lookups.get(key)
                if cached is None or cached[0] != self.workspace.capture_version:
                    found = self._source(epoch, index)
                    cached = (
                        self.workspace.capture_version,
                        found,
                        found and self.workspace.require_item("meetings", found["meeting_id"]),
                    )
                    self.lookups[key] = cached
                source, meeting = cached[1], cached[2]
                if source is None or meeting is None:
                    continue
                if last is not None and sequence != last + 1:
                    self.record_gap(
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
                streamed.add(key)
                self.streaming.add(key)
                queue.append(packet)
                self.last_sequences[key] = sequence
                self.total_bytes[key] = self.total_bytes.get(key, 0) + len(pcm)
                diarize = bool(
                    key not in self.diarization_disabled
                    and meeting["config_snapshot"].get("diarization_policy")
                    # With both sources the mic is the local user ("Me"); only split the other side.
                    and not (
                        source["source_kind"] == "mic"
                        and meeting["config_snapshot"].get("source") == "both"
                    )
                )
                if (
                    diarize
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
                            # One bad window (the installed pyannote pack fails a window where a
                            # speaker talks too briefly to embed) must not end labelling.
                            if getattr(exc, "code", None) == "invalid_input":
                                return
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
                # Audio not yet diarized stays up to 120s (~23 MB at 48 kHz): a cold speaker
                # model takes ~60s to start, and the meeting's first words must still be labelled.
                while (
                    queue
                    and monotonic - queue[0].monotonic_ns > 30_000_000_000
                    and (
                        not diarize
                        or queue[0].monotonic_ns < self.diarized_ns.get(key, 0)
                        or monotonic - queue[0].monotonic_ns > 120_000_000_000
                    )
                ):
                    oldest = queue.popleft()
                    self.total_bytes[key] -= len(oldest.pcm)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except AppError:
            pass
        finally:
            self.streaming.difference_update(streamed)
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
        # From the end of the last labelled window, so audio a failed or cold-start window
        # missed is diarized late instead of never; at least 12s for speaker context.
        cutoff = min(packets[-1].monotonic_ns - 12_000_000_000, self.diarized_ns.get(key, 0))
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
        applied = w.speaker_evidence.apply(meeting["id"], source["source_kind"], result)
        self.diarized_ns[key] = packets[-1].monotonic_ns
        return applied

    @staticmethod
    def _slice(packets: list[Packet], p: dict[str, Any], start: int, end: int) -> tuple[bytes, int]:
        """Cut an utterance's PCM out of the bounded packet window, never fabricating audio."""
        require(
            bool(packets),
            "audio_unavailable",
            "Utterance audio is no longer in the bounded memory window.",
            status=503,
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
        rate = packets[0].sample_rate
        # Native positions are absolute meeting-timeline counters, including pauses.
        start_frame = int(p.get("start_sample", start * 16)) * (rate // 16000)
        first_frame = packets[0].monotonic_ns * rate // 1_000_000_000
        require(
            start_frame >= first_frame - 3,
            "audio_range_missing",
            "Speech start was evicted from the bounded memory window.",
            status=503,
        )
        offset = max(0, start_frame - first_frame)
        count = (end - start) * (rate // 1000)
        pcm = b"".join(packet.pcm for packet in packets)[offset * 4 : (offset + count) * 4]
        require(
            len(pcm) >= max(4, count * 4 - 3840),
            "audio_range_missing",
            "Utterance audio range is incomplete; a gap is recorded instead of fabricated speech.",
            status=503,
        )
        return pcm, rate

    async def _await_audio(self, key: tuple[int, int], end_ms: int) -> None:
        """Packets and the utterance request reach the engine on separate connections; when the
        loop is busy (transcription, speaker model) the request can arrive before the audio it
        names. Wait for the stream to catch up instead of dropping the speech as a gap."""
        deadline = asyncio.get_running_loop().time() + 10
        # ponytail: 20 ms polling; an arrival condition if this ever shows up in profiles.
        while key in self.streaming and asyncio.get_running_loop().time() < deadline:
            queue = self.buffers.get(key)
            last = queue[-1] if queue else None
            if last and (
                last.monotonic_ns + last.frame_count * 1_000_000_000 // last.sample_rate
                >= end_ms * 1_000_000
            ):
                return
            await asyncio.sleep(0.02)

    def record_gap(
        self, source: dict[str, Any], reason: str, start_ms: int, details: dict[str, Any]
    ) -> None:
        w = self.workspace
        with w.transaction():
            end_ms = details.get("end_ms")
            # Consecutive drops of the same kind extend one gap instead of stacking rows, so a
            # long bad stretch stays one readable badge and the gap list stays short.
            previous = w.connection.execute(
                "SELECT id FROM timeline_gaps WHERE source_id=? AND reason=? AND end_ms IS NOT NULL"
                " AND end_ms>=? AND start_ms<=? ORDER BY start_ms DESC LIMIT 1",
                (source["id"], reason, start_ms - 2000, start_ms),
            ).fetchone()
            if previous is not None and end_ms is not None:
                gap = w.require_item("timeline_gaps", previous["id"])
                if end_ms > gap["end_ms"]:
                    w.put(
                        "timeline_gaps",
                        gap["id"],
                        {"end_ms": end_ms},
                        expected_revision=gap["revision"],
                    )
                return
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
        start = int(p["start_ms"])
        end = int(p["end_ms"])
        require(
            # The longest-line setting goes to 30 s; the VAD may overrun it by one 32 ms window.
            0 <= start < end and end - start <= 31000,
            "invalid_span",
            "Utterance duration is invalid.",
            status=422,
        )
        key = (epoch, index)
        await self._await_audio(key, end)
        try:
            pcm, rate = self._slice(list(self.buffers.get(key, [])), p, start, end)
        except AppError as exc:
            # Dropped speech is a visible gap; repeated drops mean transcription has stalled.
            self.record_gap(source, exc.code, start, {"end_ms": end})
            self.misses[key] = self.misses.get(key, 0) + 1
            if self.misses[key] == 3:
                self.workspace.emit(
                    "capture.attention",
                    {"source_id": source["id"], "code": "asr_stalled", "message": exc.message},
                    meeting_id=source["meeting_id"],
                )
            raise
        self.misses.pop(key, None)
        # A slow transcriber (CPU only, speaker model on the same cores) must not stop capture:
        # speech past a minute of backlog is skipped as a visible gap and transcription catches
        # up by itself, instead of pausing the meeting until someone presses Resume.
        if self.pending_ms.get(key, 0) > 60000:
            self.record_gap(source, "asr_backlog", start, {"end_ms": end})
            self.workspace.emit(
                "capture.backpressure",
                {"source_id": source["id"], "backlog_ms": self.pending_ms[key]},
                meeting_id=source["meeting_id"],
            )
            return {"accepted": False, "source_id": source["id"], "start_ms": start, "end_ms": end}

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
            converted = await asyncio.to_thread(pcm16k, pcm, rate)
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
                    self.record_gap(
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

    def gap(p: dict[str, Any]) -> dict[str, Any]:
        """Native host reports speech it could not hand over (request failed or timed out)."""
        pipeline = w.audio_pipeline
        require(pipeline is not None, "audio_unavailable", "Audio is unavailable.", status=503)
        assert pipeline is not None
        candidates = w.list("capture_sources", {"meeting_id": p["meeting_id"]})
        source = next((s for s in candidates if s.get("source_index") == p["source_index"]), None)
        require(source is not None, "source_missing", "Capture source not found.", status=404)
        assert source is not None
        start, end = int(p["start_ms"]), int(p["end_ms"])
        # The engine already records a gap for drops it detects itself; don't stack a second.
        covered = w.connection.execute(
            "SELECT 1 FROM timeline_gaps WHERE source_id=? AND start_ms<=? AND end_ms>=?",
            (source["id"], start, end),
        ).fetchone()
        if covered is None:
            pipeline.record_gap(source, "utterance_rejected", start, {"end_ms": end})
        return {"recorded": covered is None}

    r["capture.gap"] = gap

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
