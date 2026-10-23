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
            await pipeline.drain({"0": 2})
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
