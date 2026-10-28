from __future__ import annotations

import asyncio
import struct
from pathlib import Path

import pytest
from meeting_assistant.application.capture import HEADER, AudioPipeline
from meeting_assistant.domain.model import new_id
from meeting_assistant.workspace import Workspace


@pytest.mark.parametrize("rate", [16000, 48000])
def test_partial_native_tail_reaches_drain_without_sample_loss(tmp_path: Path, rate: int) -> None:
    async def scenario() -> None:
        w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
        pipeline = AudioPipeline(w, "ab" * 32)
        writer = None
        try:
            meeting = w.dispatch(
                "meetings.create", {"title": "Boundary fixture", "source": "system"}
            )["item"]
            w.put(
                "capture_sources",
                new_id(),
                {
                    "meeting_id": meeting["id"],
                    "source_kind": "system",
                    "device_key": "synthetic",
                    "display_name": "Boundary fixture",
                    "epoch": 1,
                    "state": "active",
                    "source_index": 0,
                },
            )
            port = await pipeline.start()
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(bytes.fromhex("ab" * 32))
            frames = rate // 50
            values = [0.25] * frames + [0.5] * 173
            for sequence, samples in enumerate((values[:frames], values[frames:]), start=1):
                pcm = struct.pack(f"<{len(samples)}f", *samples)
                writer.write(
                    HEADER.pack(
                        44 + len(pcm),
                        1,
                        1,
                        0,
                        sequence,
                        1,
                        0,
                        rate,
                        1,
                        1,
                        len(samples),
                        (sequence - 1) * 20_000_000,
                    )
                )
                writer.write(pcm)
            await writer.drain()
            await pipeline.drain("unused", {"0": 2})
            packets = list(pipeline.buffers[(1, 0)])
            received = b"".join(packet.pcm for packet in packets)
            assert struct.unpack(f"<{len(values)}f", received) == tuple(values)
            assert [packet.frame_count for packet in packets] == [frames, 173]
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await pipeline.close()
            w.close()

    asyncio.run(scenario())


async def test_stopped_source_rejects_packets_despite_cached_lookup(tmp_path: Path) -> None:
    # The packet loop caches its source/meeting lookup; a stop written after the first packet
    # must still reject the next one instead of serving the stale "active" copy.
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    pipeline = AudioPipeline(w, "ab" * 32)
    meeting = w.dispatch("meetings.create", {"title": "Cache fixture", "source": "system"})["item"]
    source = w.put(
        "capture_sources",
        new_id(),
        {
            "meeting_id": meeting["id"],
            "source_kind": "system",
            "device_key": "synthetic",
            "display_name": "Cache fixture",
            "epoch": 1,
            "state": "active",
            "source_index": 0,
        },
    )
    pcm = struct.pack("<960f", *([0.1] * 960))

    def packet(sequence: int) -> bytes:
        header = HEADER.pack(44 + len(pcm), 1, 1, 0, sequence, 1, 0, 48000, 1, 1, 960, 0)
        return header + pcm

    port = await pipeline.start()
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(bytes.fromhex("ab" * 32) + packet(1))
        await writer.drain()
        for _ in range(100):
            if pipeline.buffers.get((1, 0)):
                break
            await asyncio.sleep(0.01)
        w.put(
            "capture_sources",
            source["id"],
            {"state": "stopped"},
            expected_revision=source["revision"],
        )
        writer.write(packet(2))
        await writer.drain()
        await asyncio.sleep(0.2)
        assert [p.sequence for p in pipeline.buffers[(1, 0)]] == [1]
    finally:
        writer.close()
        await pipeline.close()
        w.close()


async def test_diarization_after_cold_start_covers_the_meetings_first_audio(
    tmp_path: Path,
) -> None:
    # The speaker model unloads when idle, so the first window of a meeting can wait ~60s for a
    # cold start. The next window must reach back to the audio that first window missed, or
    # the meeting's first utterances are never labelled.
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    pipeline = AudioPipeline(w, "ab" * 32)
    meeting = w.dispatch("meetings.create", {"title": "Cold start", "source": "system"})["item"]
    w.put(
        "meetings",
        meeting["id"],
        {"config_snapshot": {**meeting["config_snapshot"], "diarization_policy": {"task": "x"}}},
        expected_revision=meeting["revision"],
    )
    w.put(
        "capture_sources",
        new_id(),
        {
            "meeting_id": meeting["id"],
            "source_kind": "system",
            "device_key": "synthetic",
            "display_name": "Cold start",
            "epoch": 1,
            "state": "active",
            "source_index": 0,
        },
    )
    cold = asyncio.Event()
    windows: list[tuple[int, float]] = []

    class Inference:
        async def diarize(self, *, pcm_float32: bytes, origin_ms: int, **_: object) -> dict:
            windows.append((origin_ms, len(pcm_float32) / 4 / 16000))
            if len(windows) == 1:
                await cold.wait()
            return {}

    w.inference = Inference()  # type: ignore[assignment]
    w.speaker_evidence.apply = lambda *_: {}  # type: ignore[method-assign]
    pcm = struct.pack("<960f", *([0.1] * 960))

    def packets(first: int, last: int) -> bytes:
        return b"".join(
            HEADER.pack(44 + len(pcm), 1, 1, 0, s, 1, 0, 48000, 1, 1, 960, (s - 1) * 20_000_000)
            + pcm
            for s in range(first, last + 1)
        )

    port = await pipeline.start()
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        # 40s of audio arrive while the first window waits on the cold model.
        writer.write(bytes.fromhex("ab" * 32) + packets(1, 2000))
        await writer.drain()
        for _ in range(500):
            if pipeline.last_sequences.get((1, 0)) == 2000:
                break
            await asyncio.sleep(0.01)
        cold.set()
        assert pipeline.diarization_task is not None
        await pipeline.diarization_task
        writer.write(packets(2001, 2010))
        await writer.drain()
        for _ in range(500):
            if len(windows) == 2:
                break
            await asyncio.sleep(0.01)
        assert pipeline.diarization_task is not None
        await pipeline.diarization_task
        # The second window starts where the first ended and reaches the newest audio.
        (first_origin, first_seconds), (origin_ms, seconds) = windows
        assert origin_ms <= first_origin + first_seconds * 1000, windows
        assert origin_ms + seconds * 1000 >= 40_000, windows
    finally:
        writer.close()
        await pipeline.close()
        w.close()
