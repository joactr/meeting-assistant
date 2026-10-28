"""Bundled adapter registration and capability resolution without mutable globals."""

from __future__ import annotations

import asyncio
import math
import time
from typing import TYPE_CHECKING, Any, TypedDict

import httpx

from .contracts import (
    Capabilities,
    Connection,
    EmbeddingBatch,
    EmbeddingResult,
    ModelProfile,
    ProviderError,
    fingerprint,
    is_loopback,
)
from .llm import NativeLLM, json_request

if TYPE_CHECKING:
    from ..workers.client import WorkerClient


class _Descriptor(TypedDict):
    id: str
    roles: list[str]
    documentation_url: str


DESCRIPTORS: list[_Descriptor] = [
    {
        "id": "openai",
        "roles": ["llm", "embeddings"],
        "documentation_url": "https://platform.openai.com/docs/api-reference/responses",
    },
    {
        "id": "openai-compatible",
        "roles": ["llm", "embeddings"],
        "documentation_url": "https://platform.openai.com/docs/api-reference/chat",
    },
    {
        "id": "openrouter",
        "roles": ["llm"],
        "documentation_url": "https://openrouter.ai/docs/api-reference/chat-completion",
    },
    {
        "id": "azure-openai",
        "roles": ["llm", "embeddings"],
        "documentation_url": "https://learn.microsoft.com/azure/ai-foundry/openai/reference",
    },
    {
        "id": "anthropic",
        "roles": ["llm"],
        "documentation_url": "https://platform.claude.com/docs/en/api/messages",
    },
    {
        "id": "gemini",
        "roles": ["llm", "embeddings"],
        "documentation_url": "https://ai.google.dev/api/generate-content",
    },
    {
        "id": "ollama",
        "roles": ["llm", "embeddings"],
        "documentation_url": "https://docs.ollama.com/api",
    },
    {
        "id": "faster-whisper",
        "roles": ["asr"],
        "documentation_url": "https://github.com/SYSTRAN/faster-whisper",
    },
    {
        "id": "sherpa-parakeet",
        "roles": ["asr"],
        "documentation_url": "https://k2-fsa.github.io/sherpa/onnx/",
    },
    {
        "id": "fermion-phonon",
        "roles": ["asr"],
        "documentation_url": "https://huggingface.co/FermionResearch/Phonon-2.5",
    },
    {
        "id": "deepgram",
        "roles": ["asr"],
        "documentation_url": "https://developers.deepgram.com/reference/speech-to-text/listen-streaming",
    },
    {
        "id": "assemblyai",
        "roles": ["asr"],
        "documentation_url": "https://www.assemblyai.com/docs/streaming/api-spec/streaming-websocket",
    },
    {
        "id": "azure-speech",
        "roles": ["asr"],
        "documentation_url": "https://learn.microsoft.com/azure/ai-services/speech-service/",
    },
    {
        "id": "community-1",
        "roles": ["diarization"],
        "documentation_url": "https://huggingface.co/pyannote/speaker-diarization-community-1",
    },
    {
        "id": "e5-onnx",
        "roles": ["embeddings"],
        "documentation_url": "https://huggingface.co/intfloat/multilingual-e5-small",
    },
]


def descriptors() -> list[dict[str, Any]]:
    return [
        {
            **item,
            "adapter_version": "1.0.0",
            "auth_schemes": ["none"]
            if item["id"]
            in {
                "ollama",
                "faster-whisper",
                "sherpa-parakeet",
                "fermion-phonon",
                "community-1",
                "e5-onnx",
            }
            else ["api_key", "bearer"],
            "settings_schema": Connection.model_json_schema(),
            "model_discovery": {
                "supported": item["id"]
                in {"openai", "openai-compatible", "openrouter", "ollama", "gemini"},
                "manual_allowed": True,
            },
        }
        for item in DESCRIPTORS
    ]


def resolve_capabilities(connection: Connection, profile: ModelProfile) -> Capabilities:
    adapter, options = connection.adapter, profile.options
    if adapter not in {item["id"] for item in DESCRIPTORS}:
        raise ProviderError("unsupported", "Unknown adapter protocol")
    local = adapter in {
        "faster-whisper",
        "sherpa-parakeet",
        "fermion-phonon",
        "community-1",
        "e5-onnx",
    }
    preparation = "e5-prefix-attention-mean-l2-v1" if adapter == "e5-onnx" else "raw-provider-l2-v1"
    values: dict[str, Any] = {
        "fingerprint": fingerprint(
            {
                "connection": connection.model_dump(),
                "profile": profile.model_dump(),
                "adapter_version": "1.0.0",
                "embedding_preparation": preparation,
            }
        ),
        "api_version": connection.api_version or "configured",
        "execution": "local" if local else connection.execution,
        "limits": {
            "context_tokens": profile.context_tokens,
            "output_tokens": profile.output_tokens,
            "embedding_tokens": 512 if adapter == "e5-onnx" else options.get("embedding_tokens"),
        },
    }
    if adapter in {"openai-compatible", "ollama"}:
        # A URL proves "this device" only for loopback hosts; any other host is a remote destination.
        values["execution"] = "local" if is_loopback(connection.endpoint) else "remote"
        if adapter == "ollama" and values["execution"] == "local":
            # A loopback Ollama can still route to cloud models, so it must be confirmed.
            if not options.get("execution_confirmed", False):
                values["execution"] = "unknown"
    elif not local:
        values["execution"] = "remote"
    if adapter in {
        "openai",
        "openai-compatible",
        "openrouter",
        "azure-openai",
        "anthropic",
        "gemini",
        "ollama",
    }:
        values.update(
            streaming={"native": True, "interim": True, "revisions": False},
            structured_output={
                "strict_schema": bool(options.get("strict_schema", False)),
                "json_mode": bool(options.get("json_mode", False)),
                "prompt_only": not bool(options.get("strict_schema", False)),
            },
        )
    if adapter == "faster-whisper":
        values.update(
            languages={"select": True, "detect": True, "code_switch": False},
            timestamps={"word": True, "segment": True},
            confidence=False,
            vocabulary={"mode": "prompt_hint", "max_tokens": 223, "update_policy": "next_chunk"},
        )
    elif adapter == "sherpa-parakeet":
        values.update(
            languages={"detect": True, "code_switch": True},
            timestamps={"token": True, "segment": True},
        )
    elif adapter == "fermion-phonon":
        values.update(
            languages={"detect": True, "code_switch": True},
            timestamps={"word": True, "segment": True},
        )
    elif adapter == "deepgram":
        values.update(
            streaming={"native": True, "interim": True, "revisions": True},
            languages={"select": True, "detect": True, "code_switch": True},
            timestamps={"word": True, "segment": True},
            confidence=True,
            diarization={"available": True, "live": True, "late_revisions": False},
            vocabulary={
                "mode": "keyterms",
                "weighted": False,
                "max_tokens": 500,
                "update_policy": "next_stream",
            },
            cancellation={"cooperative": True, "flush": True},
        )
    elif adapter == "assemblyai":
        values.update(
            streaming={"native": True, "interim": True, "revisions": True},
            languages={"select": True, "detect": True},
            timestamps={"word": True, "segment": True},
            confidence=True,
            diarization={"available": True, "live": True, "late_revisions": True},
            vocabulary={
                "mode": "keyterms",
                "weighted": False,
                "max_terms": options.get("vocabulary_max_terms"),
                "update_policy": "next_stream",
            },
            cancellation={"cooperative": True, "flush": True, "remote_job_cancel": False},
        )
    elif adapter == "azure-speech":
        values.update(
            streaming={"native": True, "interim": True, "revisions": True},
            languages={
                "select": True,
                "detect": True,
                "code_switch": False,
                "supported": options.get("languages", []),
            },
            timestamps={"word": True, "segment": True},
            confidence=True,
            diarization={"available": True, "live": True, "late_revisions": True},
            vocabulary={
                "mode": "phrase_list",
                "weighted": True,
                "max_terms": 500,
                "update_policy": "next_stream",
            },
            cancellation={"cooperative": True, "flush": True},
        )
    elif adapter == "community-1":
        values.update(
            diarization={"available": True, "live": True, "late_revisions": True, "overlap": True}
        )
    return Capabilities.model_validate(values)


class CloudEmbedder:
    def __init__(self, llm: NativeLLM):
        self.adapter = llm

    async def embed(
        self,
        batch: EmbeddingBatch,
        *,
        deadline: float,
        cancelled: asyncio.Event | None = None,
        max_retries: int = 2,
    ) -> EmbeddingResult:
        llm, adapter = self.adapter, self.adapter.connection.adapter
        if batch.model_fingerprint != llm.capabilities.fingerprint:
            raise ProviderError(
                "invalid_input", "Embedding fingerprint differs from the selected profile"
            )
        model = llm.profile.model
        body: dict[str, Any]
        if adapter in {"openai", "openai-compatible", "azure-openai"}:
            url, body = (
                llm.base + "/embeddings",
                {"model": model, "input": batch.texts, "encoding_format": "float"},
            )
            if llm.profile.options.get("supports_dimensions", False) and llm.profile.dimensions:
                body["dimensions"] = llm.profile.dimensions
        elif adapter == "ollama":
            url, body = (
                llm.base + "/api/embed",
                {
                    "model": model,
                    "input": batch.texts,
                    "truncate": False,
                    "keep_alive": llm.profile.options.get("keep_alive", "5m"),
                },
            )
        elif adapter == "gemini":
            url = llm.base + f"/models/{model}:batchEmbedContents"
            body = {
                "requests": [
                    {
                        "model": f"models/{model}",
                        "content": {"parts": [{"text": text}]},
                        "taskType": "RETRIEVAL_QUERY"
                        if batch.purpose == "query"
                        else "RETRIEVAL_DOCUMENT",
                    }
                    for text in batch.texts
                ]
            }
            if llm.profile.dimensions:
                for request in body["requests"]:
                    request["outputDimensionality"] = llm.profile.dimensions
        else:
            raise ProviderError("unsupported", "This adapter does not supply embeddings")
        response = await json_request(
            llm.client,
            "POST",
            url,
            deadline=deadline,
            cancelled=cancelled,
            max_retries=max_retries,
            json=body,
            headers={**llm.headers(), "Accept": "application/json"},
        )
        try:
            vectors: list[list[Any]]
            if adapter in {"openai", "openai-compatible", "azure-openai"}:
                data: list[dict[str, Any]] = response["data"]
                entries = sorted(data, key=lambda row: row["index"])
                if [row["index"] for row in entries] != list(range(len(batch.texts))):
                    raise ValueError("Embedding indices do not match inputs")
                vectors = [row["embedding"] for row in entries]
            elif adapter == "gemini":
                vectors = [row["values"] for row in response["embeddings"]]
            else:
                vectors = response["embeddings"]
            dimensions = len(vectors[0])
            if (
                len(vectors) != len(batch.texts)
                or dimensions == 0
                or any(
                    len(row) != dimensions
                    or any(
                        isinstance(x, bool)
                        or not isinstance(x, (float, int))
                        or not math.isfinite(x)
                        for x in row
                    )
                    for row in vectors
                )
            ):
                raise ValueError("Invalid vector shape")
            if llm.profile.dimensions is not None and dimensions != llm.profile.dimensions:
                raise ValueError("Unexpected vector dimensions")
            normalized: list[list[float]] = []
            for row in vectors:
                norm = math.sqrt(sum(value * value for value in row))
                if not norm or not math.isfinite(norm):
                    raise ValueError("Embedding has zero or non-finite norm")
                normalized.append([value / norm for value in row])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise ProviderError(
                "invalid_output", "Embedding response does not match configured shape/order"
            ) from exc
        return EmbeddingResult(
            vectors_by_input_index=normalized,
            dimensions=dimensions,
            fingerprint=llm.capabilities.fingerprint,
            usage=response.get("usage"),
        )


class ProviderRegistry:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        worker_runtimes: dict[str, WorkerClient] | None = None,
    ) -> None:
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self.worker_runtimes: dict[str, WorkerClient] = worker_runtimes or {}

    def resolve(
        self, connection: Connection, profile: ModelProfile, secret: str | None = None
    ) -> NativeLLM:
        caps = resolve_capabilities(connection, profile)
        return NativeLLM(connection, profile, caps, secret, self.client)

    async def discover_models(
        self, connection: Connection, secret: str | None = None
    ) -> list[dict[str, Any]]:
        profile = ModelProfile(id="discovery", model="discovery", revision="discovery")
        llm = self.resolve(connection, profile, secret)
        path = "/api/tags" if connection.adapter == "ollama" else "/models"
        if connection.adapter not in {
            "openai",
            "openai-compatible",
            "openrouter",
            "ollama",
            "gemini",
        }:
            raise ProviderError(
                "unsupported",
                "Model discovery is not supplied by this adapter; enter the actual model/deployment",
            )
        result = await json_request(
            self.client,
            "GET",
            llm.base + path,
            deadline=time.monotonic() + 20,
            headers=llm.headers(),
        )
        return result.get("data", result.get("models", []))

    async def close(self) -> None:
        await self.client.aclose()
