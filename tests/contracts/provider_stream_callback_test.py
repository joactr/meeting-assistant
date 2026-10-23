"""Deterministic stream boundary fixture; does not exercise a real provider."""

import asyncio

from meeting_assistant.application.inference.service import InferenceService
from meeting_assistant.infrastructure.providers.contracts import (
    Connection,
    ModelProfile,
    TranscriptHypothesis,
)
from meeting_assistant.infrastructure.providers.registry import ProviderRegistry


def test_cloud_interim_callback_is_ephemeral_and_latest_final_returns_once():
    async def scenario():
        connection = Connection(
            id="fixture",
            adapter="deepgram",
            endpoint="https://provider.invalid",
            secret_ref="fixture-secret",
            destination="fixture",
            execution="remote",
        )
        profile = ModelProfile(id="fixture-profile", model="fixture", revision="fixture")
        observations = []

        class StreamFixture:
            def __init__(self):
                self.queue = asyncio.Queue()

            def hypothesis(self, text, revision, final):
                return TranscriptHypothesis(
                    provider_stream_id="actual-stream-fixture",
                    provider_result_id="result",
                    revision=revision,
                    is_final=final,
                    start_ms=0,
                    end_ms=100,
                    text=text,
                    timing_provenance="chunk_only",
                )

            async def push(self, pcm):
                assert len(pcm) == 3200
                await self.queue.put(self.hypothesis("Plan", 0, False))
                await self.queue.put(self.hypothesis("Plan", 1, True))

            async def flush(self):
                await self.queue.put(self.hypothesis("Plan.", 2, True))

            async def close(self):
                await self.queue.put(None)

            async def abort(self):
                observations.append("closed")

            async def events(self):
                while True:
                    item = await self.queue.get()
                    if item is None:
                        return
                    yield item

        stream = StreamFixture()

        async def open_stream(**kwargs):
            return stream

        async def on_hypothesis(row):
            observations.append(row)

        service = InferenceService(
            ProviderRegistry(),
            lambda ref: "fixture-not-a-real-key",
            lambda destination, classes, scope: True,
        )
        service.open_asr_stream = open_stream
        policy = {
            "connection": connection.model_dump(),
            "profile": profile.model_dump(),
            "scope": "meeting-live",
            "data_classes": ["live_audio"],
        }
        try:
            finals = await service.transcribe(
                b"\0" * 6400, sample_rate=16000, policy=policy, on_hypothesis=on_hypothesis
            )
            assert len(finals) == 1
            assert finals[0]["text"] == "Plan."
            assert finals[0]["revision"] == 2
            interim = [item for item in observations if isinstance(item, dict)]
            assert len(interim) == 1
            assert interim[0]["is_final"] is False
            assert interim[0]["provider_result_id"] == finals[0]["provider_result_id"]
            assert observations[-1] == "closed"
        finally:
            await service.close()

    asyncio.run(scenario())
