"""Deterministic wire/value fixtures; never claim credentialed inference evidence."""

import io
import struct
import time

import httpx
import pytest
from meeting_assistant.application.inference.catalog import safe_member
from meeting_assistant.application.inference.results import RESULT_MODELS, validate_sources
from meeting_assistant.application.inference.speakers import SpeakerReconciler
from meeting_assistant.infrastructure.providers.asr import CloudSpeechSession
from meeting_assistant.infrastructure.providers.contracts import (
    Connection,
    LLMRequest,
    ModelProfile,
    ProviderError,
)
from meeting_assistant.infrastructure.providers.llm import NativeLLM, safe_endpoint
from meeting_assistant.infrastructure.providers.registry import (
    resolve_capabilities,
)
from meeting_assistant.infrastructure.workers.runtime import read_frame, write_frame
from pydantic import ValidationError


def values(adapter):
    connection = Connection(
        id="fixture",
        adapter=adapter,
        endpoint="https://provider.invalid/v1",
        auth_scheme="none",
        execution="remote",
        destination="fixture",
    )
    profile = ModelProfile(
        id="fixture-model",
        model="actual-user-model",
        revision="fixture-revision",
        context_tokens=32768,
        output_tokens=4096,
        options={"strict_schema": True},
    )
    return connection, profile


def request():
    return LLMRequest(
        operation_id="fixture",
        task="summary",
        policy_revision=1,
        system_text="Grounded fixture",
        messages=[{"role": "user", "content": "Selected evidence"}],
        output_schema={
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
        output_limit=128,
        deadline=time.monotonic() + 20,
        cancellation_token="fixture",
    )


@pytest.mark.parametrize(
    "adapter,path,schema_key",
    [
        ("openai", "/responses", "text"),
        ("openai-compatible", "/chat/completions", "response_format"),
        ("azure-openai", "/chat/completions", "response_format"),
        ("anthropic", "/messages", "output_config"),
        ("gemini", ":streamGenerateContent?alt=sse", "generationConfig"),
        ("ollama", "/api/chat", "format"),
    ],
)
def test_native_schema_field_is_not_interchanged(adapter, path, schema_key):
    connection, profile = values(adapter)
    with httpx.Client():
        client = httpx.AsyncClient()
        llm = NativeLLM(
            connection, profile, resolve_capabilities(connection, profile), None, client
        )
        url, body = llm.payload(request())
        assert url.endswith(path)
        assert schema_key in body
        if adapter == "anthropic":
            assert body["system"] == "Grounded fixture"
            assert body["max_tokens"] == 128
        if adapter == "openai":
            assert "response_format" not in body


def test_deepgram_final_and_speech_final_remain_distinct():
    connection, profile = values("deepgram")
    session = CloudSpeechSession(
        connection,
        profile,
        "fixture-not-a-real-key",
        sample_rate=16000,
        origin_ms=5000,
        vocabulary=[],
    )
    frame = {
        "type": "Results",
        "start": 1.125,
        "duration": 0.5,
        "is_final": True,
        "speech_final": False,
        "channel_index": [0],
        "channel": {
            "alternatives": [
                {
                    "transcript": "Yes",
                    "confidence": 0.91,
                    "words": [{"word": "Yes", "start": 1.125, "end": 1.625, "speaker": 0}],
                }
            ]
        },
    }
    result = session.normalize(frame)
    assert result.start_ms == 6125
    assert result.end_ms == 6625
    assert result.is_final is True
    assert result.provider_metadata["speech_final"] is False
    assert result.words[0].confidence is None
    assert session.normalize(frame).revision == 1


def test_assembly_turn_revisions_keep_provider_identity():
    connection, profile = values("assemblyai")
    session = CloudSpeechSession(
        connection, profile, "fixture", sample_rate=16000, origin_ms=900, vocabulary=[]
    )
    first = session.normalize(
        {
            "type": "Turn",
            "turn_order": 2,
            "transcript": "Plan",
            "end_of_turn": True,
            "words": [{"text": "Plan", "start": 100, "end": 300}],
        }
    )
    second = session.normalize(
        {
            "type": "Turn",
            "turn_order": 2,
            "transcript": "Plan.",
            "end_of_turn": True,
            "turn_is_formatted": True,
            "speaker_label": "A",
            "words": [{"text": "Plan.", "start": 100, "end": 300}],
        }
    )
    assert first.provider_result_id == second.provider_result_id
    assert second.revision == first.revision + 1
    assert second.start_ms == 1000
    assert second.words[0].speaker == "A"


def test_parakeet_never_advertises_word_confidence_or_bias():
    connection, profile = values("sherpa-parakeet")
    caps = resolve_capabilities(connection, profile)
    assert caps.timestamps.token and not caps.timestamps.word
    assert not caps.confidence
    assert caps.vocabulary.mode == "none"
    assert not caps.streaming.native


def test_phonon_is_local_with_word_timings_and_no_bias():
    connection, profile = values("fermion-phonon")
    caps = resolve_capabilities(connection, profile)
    assert caps.execution == "local"
    assert caps.timestamps.word and not caps.confidence
    assert caps.vocabulary.mode == "none"
    assert not caps.streaming.native


def test_cloud_api_cannot_be_declared_local_to_bypass_consent():
    connection, profile = values("openai")
    connection.execution = "local"
    assert resolve_capabilities(connection, profile).execution == "remote"


def test_ollama_loopback_is_not_local_evidence():
    connection, profile = values("ollama")
    connection.endpoint = "http://127.0.0.1:11434"
    connection.execution = "local"
    assert resolve_capabilities(connection, profile).execution == "unknown"


@pytest.mark.parametrize(
    "url",
    [
        "https://key:secret@example.com/v1",
        "http://remote.example/v1",
        "file:///tmp/model",
        "https://example.com/v1?key=secret",
    ],
)
def test_unsafe_endpoints_rejected(url):
    with pytest.raises(ProviderError):
        safe_endpoint(url)


def test_unknown_source_and_wrong_revision_are_invalid():
    evidence = [
        {
            "source_id": "u1",
            "revision": 3,
            "start_ms": 100,
            "end_ms": 200,
            "text": "I will send it.",
        }
    ]
    with pytest.raises(ProviderError):
        validate_sources(
            {
                "citations": [
                    {
                        "source_id": "invented",
                        "revision": 3,
                        "start_ms": 100,
                        "end_ms": 200,
                        "quote": None,
                    }
                ]
            },
            evidence,
        )
    with pytest.raises(ProviderError):
        validate_sources(
            {
                "citations": [
                    {
                        "source_id": "u1",
                        "revision": 2,
                        "start_ms": 100,
                        "end_ms": 200,
                        "quote": None,
                    }
                ]
            },
            evidence,
        )
    with pytest.raises(ProviderError):
        validate_sources(
            {
                "citations": [
                    {
                        "source_id": "u1",
                        "revision": 3,
                        "start_ms": 100,
                        "end_ms": 200,
                        "quote": "He agreed to a different date",
                    }
                ]
            },
            evidence,
        )


def test_result_schemas_forbid_unrecognized_output():
    with pytest.raises(ValidationError):
        RESULT_MODELS["ask"].model_validate(
            {
                "answer": "No evidence",
                "citations": [],
                "insufficient_evidence": True,
                "tool": "send",
            }
        )


def test_framed_worker_protocol_is_length_bounded():
    stream = io.BytesIO()
    write_frame(stream, {"id": "x", "operation": "cancel"})
    stream.seek(0)
    assert read_frame(stream) == {"id": "x", "operation": "cancel"}
    with pytest.raises(ValueError):
        read_frame(io.BytesIO(struct.pack("<I", 8 * 1024 * 1024)))


@pytest.mark.parametrize("path", ["../escape", "/absolute", "C:/escape", "dir/../../escape"])
def test_catalog_archive_traversal_is_rejected(path):
    with pytest.raises(ProviderError):
        safe_member(path)


def test_speaker_embedding_spaces_are_never_mixed():
    reconciler = SpeakerReconciler("model-a")
    with pytest.raises(ProviderError):
        reconciler.reconcile(
            {
                "model_fingerprint": "model-b",
                "speaker_embeddings": {},
                "exclusive_turns": [],
                "turns": [],
            }
        )
