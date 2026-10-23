"""Real streaming/file speech APIs with independent result revisions."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any, Self
from urllib.parse import urlencode, urlsplit

import httpx
from websockets.asyncio.client import ClientConnection, connect

from .contracts import Connection, ModelProfile, ProviderError, TranscriptHypothesis, Word
from .llm import json_request, safe_endpoint


class CloudSpeechSession:
    def __init__(
        self,
        connection: Connection,
        profile: ModelProfile,
        secret: str,
        *,
        sample_rate: int,
        origin_ms: int,
        vocabulary: list[str],
        diarization: bool = False,
    ):
        self.connection, self.profile, self.secret = connection, profile, secret
        self.sample_rate, self.origin_ms = sample_rate, origin_ms
        self.vocabulary, self.diarization = vocabulary, diarization
        self.stream_id = str(uuid.uuid4())
        self.revisions: defaultdict[str, int] = defaultdict(int)
        self.socket: ClientConnection | None = None
        self._flushed = asyncio.Event()
        self._closed = asyncio.Event()
        self._events: asyncio.Queue[TranscriptHypothesis | ProviderError | None] = asyncio.Queue(
            maxsize=128
        )
        self._receiver: asyncio.Task[None] | None = None

    async def open(self) -> Self:
        adapter = self.connection.adapter
        base = safe_endpoint(self.connection.endpoint)
        if adapter == "assemblyai":
            explicit = self.profile.options.get("streaming_endpoint")
            if explicit:
                base = safe_endpoint(explicit)
            elif urlsplit(base).hostname in {"api.assemblyai.com", "api.eu.assemblyai.com"}:
                region = self.connection.region or (
                    "eu" if urlsplit(base).hostname == "api.eu.assemblyai.com" else "global"
                )
                regional_base = {
                    "global": "https://streaming.assemblyai.com",
                    "us": "https://streaming.us.assemblyai.com",
                    "eu": "https://streaming.eu.assemblyai.com",
                }.get(region)
                if regional_base is None:
                    raise ProviderError(
                        "invalid_input", "AssemblyAI streaming residency must be global, us or eu"
                    )
                base = regional_base
        parsed = urlsplit(base)
        websocket_base = (
            ("wss" if parsed.scheme == "https" else "ws") + "://" + parsed.netloc + parsed.path
        )
        query: list[tuple[str, str]] | dict[str, str | int]
        if adapter == "deepgram":
            query = [
                ("model", self.profile.model),
                ("encoding", "linear16"),
                ("sample_rate", str(self.sample_rate)),
                ("channels", "1"),
                ("interim_results", "true"),
                ("diarize", str(self.diarization).lower()),
            ]
            query.extend(("keyterm", term) for term in self.vocabulary)
            if self.profile.options.get("language"):
                query.append(("language", self.profile.options["language"]))
            url, headers = (
                websocket_base + "/v1/listen?" + urlencode(query),
                {"Authorization": "Token " + self.secret},
            )
        elif adapter == "assemblyai":
            query = {
                "sample_rate": self.sample_rate,
                "speech_model": self.profile.model,
                "format_turns": "true",
                "keyterms_prompt": json.dumps(self.vocabulary),
                "speaker_labels": str(self.diarization).lower(),
            }
            url, headers = (
                websocket_base + "/v3/ws?" + urlencode(query),
                {"Authorization": self.secret},
            )
            if self.connection.api_version:
                headers["AssemblyAI-Version"] = self.connection.api_version
        else:
            raise ProviderError(
                "unsupported", "Cloud stream adapter not available for this protocol"
            )
        try:
            self.socket = await connect(
                url,
                additional_headers=headers,
                max_size=4 * 1024 * 1024,
                open_timeout=20,
                close_timeout=5,
                max_queue=16,
            )
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise ProviderError(
                "auth" if status in {401, 403} else "network",
                "Speech streaming connection failed",
                retryable=status not in {401, 403},
            ) from None
        self._receiver = asyncio.create_task(self._receive())
        return self

    async def push(self, pcm: bytes) -> None:
        if self.socket is None:
            raise ProviderError("runtime_crash", "Speech session is not open")
        if len(pcm) % 2:
            raise ProviderError("invalid_input", "Expected mono signed 16-bit PCM")
        await self.socket.send(pcm)

    async def _receive(self) -> None:
        assert self.socket is not None
        try:
            async for raw in self.socket:
                frame: dict[str, Any] = json.loads(raw)
                if frame.get("type") in {"Error", "error"} or frame.get("error"):
                    raise ProviderError("network", "Speech provider reported a stream error")
                result = self.normalize(frame)
                if result is not None:
                    await self._events.put(result)
                if (
                    frame.get("from_finalize")
                    or frame.get("type") in {"Termination"}
                    or (frame.get("type") == "Turn" and frame.get("end_of_turn"))
                ):
                    self._flushed.set()
        except asyncio.CancelledError:
            raise
        except ProviderError as exc:
            await self._events.put(exc)
        except Exception:
            await self._events.put(
                ProviderError("network", "Speech stream ended unexpectedly", retryable=True)
            )
        finally:
            self._closed.set()
            await self._events.put(None)

    def normalize(self, frame: dict[str, Any]) -> TranscriptHypothesis | None:
        metadata: dict[str, Any]
        if self.connection.adapter == "deepgram":
            if frame.get("type") != "Results":
                return None
            alternatives = frame.get("channel", {}).get("alternatives", [])
            if not alternatives:
                return None
            alternative = alternatives[0]
            start = self.origin_ms + round(frame.get("start", 0) * 1000)
            end = start + round(frame.get("duration", 0) * 1000)
            result_id = (
                f"{frame.get('channel_index', [0])[0]}:{round(frame.get('start', 0) * 1000)}"
            )
            words = [
                Word(
                    text=w.get("punctuated_word", w["word"]),
                    start_ms=self.origin_ms + round(w["start"] * 1000),
                    end_ms=self.origin_ms + round(w["end"] * 1000),
                    confidence=w.get("confidence"),
                    speaker=str(w["speaker"]) if "speaker" in w else None,
                )
                for w in alternative.get("words", [])
            ]
            final, text, confidence = (
                bool(frame.get("is_final")),
                alternative.get("transcript", ""),
                alternative.get("confidence"),
            )
            metadata = {
                "speech_final": bool(frame.get("speech_final")),
                "from_finalize": bool(frame.get("from_finalize")),
            }
        else:
            if frame.get("type") != "Turn":
                return None
            result_id = str(frame["turn_order"])
            words = [
                Word(
                    text=w["text"],
                    start_ms=self.origin_ms + int(w["start"]),
                    end_ms=self.origin_ms + int(w["end"]),
                    confidence=w.get("confidence"),
                    speaker=str(w.get("speaker", frame.get("speaker_label")))
                    if w.get("speaker", frame.get("speaker_label")) is not None
                    else None,
                )
                for w in frame.get("words", [])
            ]
            start = min((w.start_ms for w in words), default=self.origin_ms)
            end = max((w.end_ms for w in words), default=start)
            final, text, confidence = (
                bool(frame.get("end_of_turn")),
                frame.get("transcript", ""),
                None,
            )
            metadata = {
                "turn_is_formatted": bool(frame.get("turn_is_formatted")),
                "speaker_revision": frame.get("speaker_label"),
                "end_of_turn": final,
            }
        revision = self.revisions[result_id]
        self.revisions[result_id] += 1
        return TranscriptHypothesis(
            provider_stream_id=self.stream_id,
            provider_result_id=result_id,
            revision=revision,
            is_final=final,
            start_ms=start,
            end_ms=end,
            text=text,
            confidence=confidence,
            timing_provenance="native_word" if words else "chunk_only",
            words=words,
            provider_metadata=metadata,
        )

    async def events(self) -> AsyncIterator[TranscriptHypothesis]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    async def flush(self) -> dict[str, Any]:
        assert self.socket is not None
        self._flushed.clear()
        await self.socket.send(
            json.dumps(
                {"type": "Finalize" if self.connection.adapter == "deepgram" else "ForceEndpoint"}
            )
        )
        try:
            await asyncio.wait_for(self._flushed.wait(), 20)
        except TimeoutError:
            raise ProviderError(
                "timeout", "Speech provider did not acknowledge boundary flush", retryable=True
            ) from None
        return {"acknowledged": True, "stream_id": self.stream_id}

    async def close(self) -> None:
        if self.socket:
            await self.socket.send(
                json.dumps(
                    {
                        "type": "CloseStream"
                        if self.connection.adapter == "deepgram"
                        else "Terminate"
                    }
                )
            )
            try:
                await asyncio.wait_for(self._closed.wait(), 5)
            except TimeoutError:
                pass
            await self.socket.close()
        if self._receiver and not self._receiver.done():
            self._receiver.cancel()

    async def abort(self) -> None:
        if self.socket:
            await self.socket.close()
        if self._receiver:
            self._receiver.cancel()


class CloudASR:
    def __init__(
        self, connection: Connection, profile: ModelProfile, secret: str, client: httpx.AsyncClient
    ):
        self.connection, self.profile, self.secret, self.client = (
            connection,
            profile,
            secret,
            client,
        )
        self.base = safe_endpoint(connection.endpoint)

    async def open_stream(
        self,
        *,
        sample_rate: int,
        origin_ms: int = 0,
        vocabulary: list[str] | None = None,
        diarization: bool = False,
    ) -> CloudSpeechSession:
        return await CloudSpeechSession(
            self.connection,
            self.profile,
            self.secret,
            sample_rate=sample_rate,
            origin_ms=origin_ms,
            vocabulary=vocabulary or [],
            diarization=diarization,
        ).open()

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
        if self.connection.adapter == "deepgram":
            query = [
                ("model", self.profile.model),
                ("diarize", str(diarization).lower()),
                ("utterances", "true"),
            ]
            query.extend(("keyterm", v) for v in vocabulary or [])
            data = await json_request(
                self.client,
                "POST",
                self.base + "/v1/listen?" + urlencode(query),
                deadline=deadline,
                cancelled=cancelled,
                content=audio,
                headers={"Authorization": "Token " + self.secret, "Content-Type": content_type},
            )
            outputs: list[TranscriptHypothesis] = []
            for index, utterance in enumerate(data.get("results", {}).get("utterances", [])):
                words = [
                    Word(
                        text=w.get("punctuated_word", w["word"]),
                        start_ms=origin_ms + round(w["start"] * 1000),
                        end_ms=origin_ms + round(w["end"] * 1000),
                        confidence=w.get("confidence"),
                        speaker=str(w["speaker"]) if "speaker" in w else None,
                    )
                    for w in utterance.get("words", [])
                ]
                outputs.append(
                    TranscriptHypothesis(
                        provider_stream_id=data.get("metadata", {}).get("request_id", "file"),
                        provider_result_id=str(index),
                        revision=0,
                        is_final=True,
                        start_ms=origin_ms + round(utterance["start"] * 1000),
                        end_ms=origin_ms + round(utterance["end"] * 1000),
                        text=utterance["transcript"],
                        confidence=utterance.get("confidence"),
                        timing_provenance="native_word" if words else "chunk_only",
                        words=words,
                    )
                )
            return outputs
        if self.connection.adapter != "assemblyai":
            raise ProviderError("unsupported", "Selected adapter has no file speech API")
        headers = {"Authorization": self.secret}
        uploaded = await json_request(
            self.client,
            "POST",
            self.base + "/v2/upload",
            deadline=deadline,
            cancelled=cancelled,
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=audio,
        )
        body: dict[str, Any] = {
            "audio_url": uploaded["upload_url"],
            "speaker_labels": diarization,
            "speech_models": [self.profile.options.get("file_model", self.profile.model)],
        }
        if vocabulary:
            body["word_boost"] = vocabulary
            body["boost_param"] = "default"
        job = await json_request(
            self.client,
            "POST",
            self.base + "/v2/transcript",
            deadline=deadline,
            cancelled=cancelled,
            headers=headers,
            json=body,
        )
        job_id = job["id"]
        try:
            while job.get("status") not in {"completed", "error"}:
                await asyncio.sleep(min(2, max(0, deadline - time.monotonic())))
                job = await json_request(
                    self.client,
                    "GET",
                    self.base + "/v2/transcript/" + job_id,
                    deadline=deadline,
                    cancelled=cancelled,
                    headers=headers,
                )
            if job.get("status") == "error":
                raise ProviderError(
                    "invalid_output",
                    "AssemblyAI file transcription failed",
                    details={"remote_job_id": job_id},
                )
            utterances: list[dict[str, Any]] = job.get("utterances") or [
                {
                    "start": 0,
                    "end": round(job.get("audio_duration", 0) * 1000),
                    "text": job.get("text") or "",
                    "words": job.get("words") or [],
                }
            ]
            return [
                TranscriptHypothesis(
                    provider_stream_id=job_id,
                    provider_result_id=str(i),
                    revision=0,
                    is_final=True,
                    start_ms=origin_ms + u["start"],
                    end_ms=origin_ms + u["end"],
                    text=u["text"],
                    confidence=u.get("confidence"),
                    timing_provenance="native_word" if u.get("words") else "chunk_only",
                    words=[
                        Word(
                            text=w["text"],
                            start_ms=origin_ms + w["start"],
                            end_ms=origin_ms + w["end"],
                            confidence=w.get("confidence"),
                            speaker=w.get("speaker"),
                        )
                        for w in u.get("words", [])
                    ],
                )
                for i, u in enumerate(utterances)
            ]
        except ProviderError as exc:
            exc.details["remote_job_id"] = job_id
            exc.details["remote_cancellation_supported"] = False
            raise
