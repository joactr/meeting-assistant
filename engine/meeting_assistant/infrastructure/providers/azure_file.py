"""Azure Speech fast-transcription REST API, separate from continuous SDK."""

import asyncio
import json
import time
from typing import Any

import httpx

from .contracts import Connection, ModelProfile, ProviderError, TranscriptHypothesis, Word
from .llm import http_error, safe_endpoint


class AzureFileASR:
    def __init__(
        self, connection: Connection, profile: ModelProfile, secret: str, client: httpx.AsyncClient
    ) -> None:
        self.connection, self.profile, self.secret, self.client = (
            connection,
            profile,
            secret,
            client,
        )

    async def transcribe_file(
        self,
        audio: bytes,
        *,
        content_type: str,
        deadline: float,
        origin_ms: int = 0,
        vocabulary: list[str] | None = None,
        diarization: bool = False,
        cancelled: asyncio.Event | None = None,
    ) -> list[TranscriptHypothesis]:
        if vocabulary:
            raise ProviderError(
                "unsupported",
                "Azure fast file transcription does not advertise continuous SDK phrase-list support",
            )
        if cancelled and cancelled.is_set():
            raise ProviderError("cancelled", "File transcription cancelled before upload")
        base = safe_endpoint(self.connection.endpoint)
        definition = {
            "locales": self.profile.options.get("languages", ["en-US"]),
            "diarization": {"enabled": diarization},
        }
        headers = {"Ocp-Apim-Subscription-Key": self.secret}
        if self.connection.auth_scheme == "entra":
            headers = {"Authorization": "Bearer " + self.secret}
        response = await self.client.post(
            base + "/speechtotext/transcriptions:transcribe",
            params={"api-version": self.connection.api_version or "2025-10-15"},
            headers=headers,
            files={
                "audio": ("meeting.wav", audio, content_type),
                "definition": (None, json.dumps(definition), "application/json"),
            },
            timeout=max(0.1, deadline - time.monotonic()),
        )
        if response.is_error:
            raise http_error(response)
        try:
            data: dict[str, Any] = response.json()
            results: list[TranscriptHypothesis] = []
            for i, phrase in enumerate(data.get("phrases", [])):
                start = int(phrase["offsetMilliseconds"])
                end = start + int(phrase["durationMilliseconds"])
                words = [
                    Word(
                        text=word["text"],
                        start_ms=origin_ms + int(word["offsetMilliseconds"]),
                        end_ms=origin_ms
                        + int(word["offsetMilliseconds"])
                        + int(word["durationMilliseconds"]),
                        confidence=word.get("confidence"),
                        speaker=str(phrase["speaker"]) if "speaker" in phrase else None,
                    )
                    for word in phrase.get("words", [])
                ]
                results.append(
                    TranscriptHypothesis(
                        provider_stream_id="azure-file",
                        provider_result_id=str(i),
                        revision=0,
                        is_final=True,
                        start_ms=origin_ms + start,
                        end_ms=origin_ms + end,
                        text=phrase["text"],
                        detected_language=phrase.get("locale"),
                        confidence=phrase.get("confidence"),
                        timing_provenance="native_word" if words else "chunk_only",
                        words=words,
                    )
                )
            return results
        except (ValueError, KeyError, TypeError):
            raise ProviderError(
                "invalid_output", "Azure file result does not match pinned API generation"
            ) from None
