"""Versioned, SDK-independent inference boundary values."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid")


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        provider_code: str | None = None,
        retry_after: float | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
        self.provider_code, self.retry_after = provider_code, retry_after
        self.details, self.trace_id = details or {}, str(uuid.uuid4())

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "provider_code": self.provider_code,
            "retry_after": self.retry_after,
            "details": self.details,
            "trace_id": self.trace_id,
        }


class Languages(Value):
    select: bool = False
    detect: bool = False
    code_switch: bool = False
    supported: list[str] = Field(default_factory=list)


class Streaming(Value):
    native: bool = False
    interim: bool = False
    revisions: bool = False


class Timestamps(Value):
    word: bool = False
    token: bool = False
    segment: bool = False


class Diarization(Value):
    available: bool = False
    live: bool = False
    late_revisions: bool = False
    overlap: bool = False


class Vocabulary(Value):
    mode: Literal[
        "prompt_hint", "decoder_bias", "keyterms", "phrase_list", "custom_model", "none"
    ] = "none"
    weighted: bool = False
    max_terms: int | None = None
    max_tokens: int | None = None
    update_policy: Literal["live", "next_chunk", "next_stream", "next_meeting", "unsupported"] = (
        "unsupported"
    )


class StructuredOutput(Value):
    strict_schema: bool = False
    json_mode: bool = False
    prompt_only: bool = True


class Cancellation(Value):
    cooperative: bool = True
    flush: bool = False
    remote_job_cancel: bool = False


class Limits(Value):
    context_tokens: int | None = Field(default=None, gt=0)
    output_tokens: int | None = Field(default=None, gt=0)
    audio_bytes: int | None = Field(default=None, gt=0)
    audio_seconds: float | None = Field(default=None, gt=0)
    embedding_tokens: int | None = Field(default=None, gt=0)


class Capabilities(Value):
    fingerprint: str
    api_version: str
    execution: Literal["local", "remote", "unknown"] = "unknown"
    languages: Languages = Field(default_factory=Languages)
    streaming: Streaming = Field(default_factory=Streaming)
    timestamps: Timestamps = Field(default_factory=Timestamps)
    confidence: bool = False
    diarization: Diarization = Field(default_factory=Diarization)
    vocabulary: Vocabulary = Field(default_factory=Vocabulary)
    structured_output: StructuredOutput = Field(default_factory=StructuredOutput)
    cancellation: Cancellation = Field(default_factory=Cancellation)
    limits: Limits = Field(default_factory=Limits)


class Connection(Value):
    id: str
    adapter: str
    endpoint: str
    secret_ref: str | None = None
    auth_scheme: Literal["bearer", "api_key", "entra", "none"] = "bearer"
    execution: Literal["local", "remote", "unknown"] = "unknown"
    region: str | None = None
    api_version: str | None = None
    destination: str


class ModelProfile(Value):
    id: str
    model: str
    revision: str
    options: dict[str, Any] = Field(default_factory=dict)
    context_tokens: int | None = Field(default=None, gt=0)
    output_tokens: int | None = Field(default=None, gt=0)
    dimensions: int | None = Field(default=None, gt=0)
    tokenizer: str | None = None


class Word(Value):
    text: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1)
    speaker: str | None = None


class SpeakerSpan(Value):
    speaker: str | None
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    overlap: bool = False
    confidence: float | None = Field(default=None, ge=0, le=1)


class TranscriptHypothesis(Value):
    provider_stream_id: str
    provider_result_id: str
    revision: int = Field(ge=0)
    is_final: bool
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    detected_language: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    timing_provenance: Literal["native_word", "native_token", "derived_word", "chunk_only"]
    words: list[Word] = Field(default_factory=list[Word])
    speaker_spans: list[SpeakerSpan] = Field(default_factory=list[SpeakerSpan])
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("End precedes start")
        return self


class AudioSpan(Value):
    meeting_id: str
    source_id: str
    epoch: int = Field(ge=0)
    start_sample: int = Field(ge=0)
    sample_rate: int = Field(gt=0)
    frame_count: int = Field(ge=0)
    timeline_start_ns: int = Field(ge=0)
    pcm_handle: str
    byte_offset: int = Field(default=0, ge=0)
    byte_length: int = Field(ge=0)
    processing_profile: str


class DiarizationTurn(Value):
    provider_session_id: str
    local_speaker_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    overlap: bool = False
    confidence: float | None = None
    embedding_ref: str | None = None


class LLMRequest(Value):
    operation_id: str
    task: str
    policy_revision: int = Field(ge=0)
    system_text: str
    messages: list[dict[str, Any]]
    output_schema: dict[str, Any] | None = None
    output_limit: int = Field(gt=0)
    deadline: float
    cancellation_token: str


class LLMEvent(Value):
    type: Literal["text_delta", "structured_candidate", "usage", "refusal", "complete", "error"]
    data: dict[str, Any]


class EmbeddingBatch(Value):
    purpose: Literal["query", "document"]
    texts: list[str] = Field(min_length=1, max_length=2048)
    model_fingerprint: str


class EmbeddingResult(Value):
    vectors_by_input_index: list[list[float]]
    dimensions: int = Field(gt=0)
    fingerprint: str
    usage: dict[str, Any] | None = None


class CancellationTokens:
    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def register(self, token: str) -> asyncio.Event:
        event = asyncio.Event()
        self._events[token] = event
        return event

    def cancel(self, token: str) -> bool:
        event = self._events.get(token)
        if event is not None:
            event.set()
        return event is not None

    def release(self, token: str) -> None:
        self._events.pop(token, None)


def check_deadline(deadline: float, cancelled: asyncio.Event | None = None) -> None:
    if cancelled is not None and cancelled.is_set():
        raise ProviderError("cancelled", "Operation was cancelled; remote processing may continue")
    if time.monotonic() >= deadline:
        raise ProviderError("timeout", "Inference deadline exceeded", retryable=True)
