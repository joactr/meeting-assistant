"""Consent-first grounded inference orchestration; no SQL or implicit model download."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from pydantic import Field, ValidationError

from ...infrastructure.providers.contracts import (
    CancellationTokens,
    Connection,
    EmbeddingBatch,
    EmbeddingResult,
    LLMRequest,
    ModelProfile,
    ProviderError,
    Value,
    fingerprint,
)
from ...infrastructure.providers.registry import (
    CloudEmbedder,
    ProviderRegistry,
    resolve_capabilities,
)
from ...infrastructure.resources import resource_root
from .results import RESULT_MODELS, validate_sources

if TYPE_CHECKING:
    from ...infrastructure.providers.asr import CloudSpeechSession
    from ...infrastructure.providers.llm import NativeLLM
    from ...infrastructure.providers.local import AzureSDKSession, ChunkASRSession
    from ...infrastructure.workers.client import WorkerClient
    from ...infrastructure.workers.models import ModelRuntime

T = TypeVar("T")


class Budget(Value):
    deadline_seconds: float | None = Field(default=None, gt=0, le=3600)
    output_tokens: int | None = Field(default=None, gt=0, le=1000000)
    max_requests: int = Field(default=2, ge=1, le=10)
    max_cost: float | None = Field(default=None, ge=0)
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)


class Route(Value):
    connection: Connection
    profile: ModelProfile
    scope: str
    data_classes: list[str] = Field(default_factory=lambda: ["meeting_text"])


class TaskPolicy(Route):
    revision: int = Field(default=1, ge=0)
    budgets: Budget = Field(default_factory=Budget)
    fallbacks: list[Route] = Field(default_factory=list[Route], max_length=5)
    output_language: str | None = None


DEFAULTS = {
    "connection_test": (20, 128),
    "cues": (15, 1024),
    "summary": (60, 4096),
    "actions": (60, 4096),
    "topics": (300, 8192),
    "artifacts": (300, 8192),
    "memory": (300, 8192),
    "minutes": (300, 8192),
    "briefings": (300, 8192),
    "digest": (300, 8192),
    "ask": (120, 4096),
}


async def maybe_await[T](value: T | Awaitable[T]) -> T:
    return await value if inspect.isawaitable(value) else cast(T, value)


class InferenceService:
    def __init__(
        self,
        registry: ProviderRegistry,
        secret_resolver: Callable[[str], str | None | Awaitable[str | None]],
        consent_checker: Callable[[str, list[str], str], bool | Awaitable[bool]],
        *,
        prompt_root: Path | None = None,
        policy_resolver: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        model_runtime: ModelRuntime | None = None,
    ) -> None:
        self.registry, self.secret_resolver, self.consent_checker = (
            registry,
            secret_resolver,
            consent_checker,
        )
        self.prompt_root = prompt_root or resource_root() / "engine" / "prompts"
        self.policy_resolver = policy_resolver
        self.model_runtime = model_runtime
        self.tokens = CancellationTokens()
        self.connection_locks: dict[str, asyncio.Semaphore] = {}
        self.gpu_lock = asyncio.Semaphore(1)

    async def _worker(self, route: Route) -> WorkerClient:
        if self.model_runtime is not None:
            return await self.model_runtime.ensure_ready(route)
        worker = self.registry.worker_runtimes.get(route.profile.id)
        if worker is None:
            raise ProviderError(
                "model_missing",
                "Selected isolated runtime/model is not installed; explicit acquisition is required",
            )
        return worker

    async def _authorize(self, route: Route) -> str | None:
        # A loopback URL is not evidence of execution location (Ollama can route cloud models).
        if resolve_capabilities(route.connection, route.profile).execution != "local":
            allowed = await maybe_await(
                self.consent_checker(route.connection.destination, route.data_classes, route.scope)
            )
            if not allowed:
                raise ProviderError(
                    "permission", "This destination/data scope has not been consented"
                )
        if route.connection.secret_ref:
            secret = await maybe_await(self.secret_resolver(route.connection.secret_ref))
            if not secret:
                raise ProviderError("auth", "Selected credential reference is unavailable")
            return secret
        return None

    def cancel(self, token: str) -> bool:
        return self.tokens.cancel(token)

    def _policy(self, value: dict[str, Any]) -> TaskPolicy:
        try:
            resolved = self.policy_resolver(value) if self.policy_resolver is not None else value
            return TaskPolicy.model_validate(resolved)
        except ValidationError as exc:
            raise ProviderError(
                "invalid_input",
                "Task policy is invalid",
                details={
                    "fields": [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
                },
            ) from None

    async def generate(
        self,
        task: str,
        evidence: list[dict[str, Any]],
        policy: dict[str, Any],
        *,
        language: str = "auto",
        context: dict[str, Any] | None = None,
        cancellation_token: str | None = None,
    ) -> dict[str, Any]:
        if task not in RESULT_MODELS:
            raise ProviderError("unsupported", "Unknown structured analysis task")
        context = context or {}
        if task == "ask" and not evidence:
            return {
                "task": task,
                "schema_version": 1,
                "result": {
                    "answer": "Insufficient evidence in the selected scope.",
                    "citations": [],
                    "insufficient_evidence": True,
                },
                "prompt_version": "v1",
                "input_fingerprint": fingerprint({"evidence": [], "context": context}),
            }
        if task == "briefings" and not evidence:
            return {
                "task": task,
                "schema_version": 1,
                "result": {
                    "sections": [],
                    "warnings": ["No relevant selected inputs are available for a briefing."],
                },
                "prompt_version": "v1",
                "input_fingerprint": fingerprint({"evidence": [], "context": context}),
            }
        parsed = self._policy(policy)
        language = parsed.output_language or language
        if language != "auto" and not re.fullmatch(
            r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language
        ):
            raise ProviderError(
                "invalid_input", "Output language must be auto or a valid BCP-47 language tag"
            )
        if language == "auto":
            known = [item.get("detected_language", item.get("language")) for item in evidence]
            known = [item for item in known if item]
            language = (
                max(sorted(set(known)), key=known.count)
                if known
                else "preserve source language; language is unknown"
            )
        token = cancellation_token or str(uuid.uuid4())
        cancelled = self.tokens.register(token)
        defaults = DEFAULTS[task]
        deadline = time.monotonic() + (parsed.budgets.deadline_seconds or defaults[0])
        calls = 0
        estimated_spend = 0.0
        schema = RESULT_MODELS[task].model_json_schema()
        prompt = (self.prompt_root / task / "v1.txt").read_text(encoding="utf-8")
        prompt_fingerprint = fingerprint(
            {"task": task, "version": "v1", "text": prompt, "schema": schema}
        )
        evidence = [
            {
                **item,
                "source_id": str(item.get("source_id", item.get("id"))),
                "revision": item.get("revision", item.get("text_revision")),
            }
            for item in evidence
        ]
        if len({item["source_id"] for item in evidence}) != len(evidence):
            self.tokens.release(token)
            raise ProviderError("invalid_input", "Evidence source ids must be unique")
        input_fingerprint = fingerprint(
            {
                "task": task,
                "evidence": evidence,
                "context": context,
                "language": language,
                "policy": parsed.model_dump(mode="json"),
                "prompt": prompt_fingerprint,
            }
        )
        last_error: ProviderError | None = None
        try:
            for route in [
                Route(
                    connection=parsed.connection,
                    profile=parsed.profile,
                    scope=parsed.scope,
                    data_classes=parsed.data_classes,
                ),
                *parsed.fallbacks,
            ]:
                try:
                    # Historical Ask evidence needs its own data-class grant.
                    if task == "ask" and "historical_excerpts" not in route.data_classes:
                        raise ProviderError(
                            "permission",
                            "Ask requires explicit historical_excerpts consent, separate from current meeting text",
                        )
                    secret = await self._authorize(route)
                    llm = self.registry.resolve(route.connection, route.profile, secret)
                    capability = llm.capabilities
                    if not route.profile.context_tokens:
                        raise ProviderError(
                            "invalid_input",
                            "Model context capacity is unknown; supply/test it before budgeted inference",
                        )
                    cap = min(
                        parsed.budgets.output_tokens or defaults[1],
                        route.profile.output_tokens or parsed.budgets.output_tokens or defaults[1],
                    )
                    selected = self._select_evidence(
                        evidence,
                        context,
                        prompt,
                        schema,
                        language,
                        route.profile.context_tokens,
                        cap,
                        task,
                    )
                    payload = {
                        "output_language": language,
                        "context": context,
                        "evidence": selected,
                        "output_schema": schema,
                    }
                    message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                    estimated_input = len((prompt + message).encode("utf-8"))
                    b = parsed.budgets
                    if b.max_cost is not None:
                        if b.input_price_per_million is None or b.output_price_per_million is None:
                            raise ProviderError(
                                "invalid_input",
                                "Monetary caps require explicit input/output prices; no price is assumed",
                            )
                        estimate = (
                            estimated_input * b.input_price_per_million
                            + cap * b.output_price_per_million
                        ) / 1000000
                        if estimate > b.max_cost:
                            raise ProviderError(
                                "quota",
                                "Conservative estimated request cost exceeds configured cap",
                                details={"estimate": estimate, "estimate_only": True},
                            )
                    messages: list[dict[str, Any]] = [{"role": "user", "content": message}]
                    repairs = 0 if capability.structured_output.strict_schema else 1
                    for attempt in range(repairs + 1):
                        if calls >= parsed.budgets.max_requests:
                            raise ProviderError("quota", "Task request budget exhausted")
                        current_input = len(
                            (prompt + json.dumps(messages, ensure_ascii=False)).encode("utf-8")
                        )
                        if current_input * 1.1 + cap > route.profile.context_tokens:
                            raise ProviderError(
                                "quota",
                                "Request cannot reserve output and prompt overhead inside model context",
                            )
                        if parsed.budgets.max_cost is not None:
                            assert parsed.budgets.input_price_per_million is not None
                            assert parsed.budgets.output_price_per_million is not None
                            reserved = (
                                current_input * parsed.budgets.input_price_per_million
                                + cap * parsed.budgets.output_price_per_million
                            ) / 1000000
                            if estimated_spend + reserved > parsed.budgets.max_cost:
                                raise ProviderError(
                                    "quota",
                                    "Repair/fallback would exceed the same task monetary estimate cap",
                                )
                            estimated_spend += reserved
                        calls += 1
                        request = LLMRequest(
                            operation_id=token,
                            task=task,
                            policy_revision=parsed.revision,
                            system_text=prompt,
                            messages=messages,
                            output_schema=schema,
                            output_limit=cap,
                            deadline=deadline,
                            cancellation_token=token,
                        )
                        candidate = None
                        try:
                            candidate, usage = await self._collect(llm, request, cancelled)
                            result = (
                                RESULT_MODELS[task]
                                .model_validate(candidate)
                                .model_dump(mode="json")
                            )
                            validate_sources(result, selected)
                            interpretations = self._validate_task(task, result, selected, context)
                            return {
                                "task": task,
                                "schema_version": 1,
                                "result": result,
                                "prompt_version": "v1",
                                "prompt_fingerprint": prompt_fingerprint,
                                "input_fingerprint": input_fingerprint,
                                "policy_revision": parsed.revision,
                                "model_fingerprint": capability.fingerprint,
                                "selected_source_ids": [item["source_id"] for item in selected],
                                "usage": usage,
                                "cancellation_token": token,
                                "interpretations": interpretations,
                                "budget": {
                                    "requests": calls,
                                    "token_estimator": "conservative_utf8_bytes_v1",
                                    "cost_is_estimate": True,
                                },
                            }
                        except (ValidationError, ProviderError) as exc:
                            if isinstance(exc, ProviderError) and exc.code != "invalid_output":
                                raise
                            if attempt >= repairs:
                                raise ProviderError(
                                    "invalid_output",
                                    "Completed analysis failed strict schema or source validation; prior valid result must be retained",
                                ) from None
                            if candidate is not None:
                                messages.append(
                                    {
                                        "role": "assistant",
                                        "content": json.dumps(candidate, ensure_ascii=False),
                                    }
                                )
                            messages.append(
                                {
                                    "role": "user",
                                    "content": "Repair the prior output: emit exactly one complete JSON object satisfying the supplied exact schema and selected source references. Do not add evidence or invent missing facts. Return only the corrected JSON object.",
                                }
                            )
                            if (
                                len((prompt + json.dumps(messages, ensure_ascii=False)).encode())
                                * 1.1
                                + cap
                                > route.profile.context_tokens
                            ):
                                raise ProviderError(
                                    "quota", "Repair would exceed the same model context budget"
                                ) from None
                except ProviderError as exc:
                    last_error = exc
                    if exc.code in {
                        "cancelled",
                        "permission",
                        "auth",
                        "invalid_input",
                        "provider_refusal",
                        "invalid_output",
                    }:
                        raise
                    # Only explicitly listed, individually authorized routes can be attempted.
                    if time.monotonic() >= deadline:
                        raise
            raise last_error or ProviderError("unsupported", "No selected inference route")
        finally:
            self.tokens.release(token)

    @staticmethod
    def _select_evidence(
        evidence: list[dict[str, Any]],
        context: dict[str, Any],
        prompt: str,
        schema: dict[str, Any],
        language: str,
        context_tokens: int,
        output_cap: int,
        task: str,
    ) -> list[dict[str, Any]]:
        base = len(
            (
                prompt
                + json.dumps(
                    {"context": context, "output_schema": schema, "language": language},
                    ensure_ascii=False,
                )
            ).encode()
        )
        available = int((context_tokens - output_cap) / 1.1) - base
        if available <= 0:
            raise ProviderError(
                "quota", "Context cannot reserve schema, prompt, output and ten-percent overhead"
            )
        ordered = (
            evidence[:12]
            if task == "ask"
            else sorted(
                evidence,
                key=lambda item: (item.get("start_ms", 0), item["source_id"]),
                reverse=task == "cues",
            )
        )
        selected: list[dict[str, Any]] = []
        for item in ordered:
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) + 2
            if size <= available:
                selected.append(item)
                available -= size
        if evidence and not selected:
            raise ProviderError(
                "quota",
                "No complete source fits within selected context; choose a larger context or narrower source scope",
            )
        return selected

    async def _cancellable(
        self, awaitable: Coroutine[Any, Any, T], cancelled: asyncio.Event | None, deadline: float
    ) -> T:
        operation = asyncio.create_task(awaitable)
        cancel = asyncio.create_task(cancelled.wait()) if cancelled is not None else None
        try:
            waiters: list[asyncio.Task[T] | asyncio.Task[bool]] = [operation]
            if cancel is not None:
                waiters.append(cancel)
            done, _ = await asyncio.wait(
                waiters,
                timeout=max(0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if operation in done:
                return operation.result()
            raise ProviderError(
                "cancelled" if cancel in done else "timeout",
                "Stopped locally; remote processing or billing may continue",
                retryable=cancel not in done,
            )
        finally:
            if not operation.done():
                operation.cancel()
            if cancel is not None:
                cancel.cancel()
            draining: list[Awaitable[object]] = [operation]
            if cancel is not None:
                draining.append(cancel)
            await asyncio.gather(*draining, return_exceptions=True)

    async def _collect(
        self, llm: NativeLLM, request: LLMRequest, cancelled: asyncio.Event
    ) -> tuple[Any, dict[str, Any]]:
        async def consume() -> tuple[Any, dict[str, Any]]:
            candidate: Any = None
            usage: dict[str, Any] = {}
            async for event in llm.generate(request, cancelled):
                if event.type == "structured_candidate":
                    candidate = event.data["result"]
                elif event.type == "usage":
                    usage.update(event.data)
            if candidate is None:
                raise ProviderError(
                    "invalid_output", "Provider returned no complete structured candidate"
                )
            return candidate, usage

        lock = self.connection_locks.setdefault(llm.connection.id, asyncio.Semaphore(1))
        await self._cancellable(lock.acquire(), cancelled, request.deadline)
        try:
            return await self._cancellable(consume(), cancelled, request.deadline)
        finally:
            lock.release()

    @staticmethod
    def _validate_task(
        task: str,
        result: dict[str, Any],
        evidence: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        interpretations: list[dict[str, Any]] = []
        if task == "actions":
            names = {str(name).casefold() for name in context.get("resolved_people", [])}
            for item in evidence:
                if item.get("speaker_name"):
                    names.add(item["speaker_name"].casefold())
            action_keys: set[tuple[str, tuple[str, ...]]] = set()
            clean: list[dict[str, Any]] = []
            for candidate in result["candidates"]:
                if not any(ref.get("quote") for ref in candidate["source_refs"]):
                    raise ProviderError(
                        "invalid_output", "Action requires an explicit quoted commitment/request"
                    )
                if candidate["assignee"] and candidate["assignee"].casefold() not in names:
                    candidate["assignee"] = None
                from .deadlines import interpret_deadline

                selected_sources = {
                    str(item.get("source_id", item.get("id"))): item for item in evidence
                }
                if candidate["deadline_text"] and not any(
                    candidate["deadline_text"] in selected_sources[ref["source_id"]].get("text", "")
                    for ref in candidate["source_refs"]
                ):
                    raise ProviderError(
                        "invalid_output",
                        "Action deadline wording is not present in its cited evidence",
                    )
                interpretation = interpret_deadline(candidate["deadline_text"], context)
                candidate["due_date"] = interpretation["due_date"]
                if candidate["deadline_text"]:
                    interpretations.append(
                        {
                            "description": candidate["description"],
                            "deadline_text": candidate["deadline_text"],
                            **interpretation,
                        }
                    )
                key = (
                    " ".join(candidate["description"].casefold().split()),
                    tuple(sorted(ref["source_id"] for ref in candidate["source_refs"])),
                )
                if key not in action_keys:
                    action_keys.add(key)
                    clean.append(candidate)
            result["candidates"] = clean
        if task == "topics":
            by_title: dict[str, dict[str, Any]] = {
                topic["title"]: topic for topic in result["topics"]
            }
            for topic in result["topics"]:
                starts = [
                    ref["start_ms"] for ref in topic["source_refs"] if ref["start_ms"] is not None
                ]
                ends = [ref["end_ms"] for ref in topic["source_refs"] if ref["end_ms"] is not None]
                if (
                    not starts
                    or not ends
                    or topic["start_ms"] < min(starts)
                    or topic["end_ms"] > max(ends)
                ):
                    raise ProviderError(
                        "invalid_output", "Topic time range is not supported by its cited spans"
                    )
                seen = {topic["title"]}
                parent = topic["parent_key"]
                while parent is not None:
                    if parent not in by_title or parent in seen:
                        raise ProviderError(
                            "invalid_output", "Topic hierarchy contains an unknown parent or cycle"
                        )
                    seen.add(parent)
                    parent = by_title[parent]["parent_key"]
                if not context.get("topic_sentiment_enabled", False):
                    topic["sentiment"] = None
        if task == "minutes":
            ids = [section["id"] for section in result["sections"]]
            if len(ids) != len(set(ids)):
                raise ProviderError("invalid_output", "Minutes section ids are not unique")
            outline: list[Any] = context.get("outline", [])
            outline_ids = [
                cast(dict[str, Any], item)["id"]
                for item in outline
                if isinstance(item, dict) and cast(dict[str, Any], item).get("id")
            ]
            if outline_ids and ids != outline_ids:
                raise ProviderError(
                    "invalid_output", "Minutes must preserve the selected human outline ids/order"
                )
            body = "\n".join(section["body"] for section in result["sections"])
            for moment in context.get("important_moments", []):
                if moment.get("include_in_minutes"):
                    for field in ("quote_snapshot", "note"):
                        if moment.get(field) and moment[field] not in body:
                            raise ProviderError(
                                "invalid_output",
                                "Selected important-moment quote/note was not retained verbatim",
                            )
        return interpretations

    def embedding_descriptor(self, policy: dict[str, Any]) -> dict[str, Any]:
        parsed = self._policy(policy)
        caps = resolve_capabilities(parsed.connection, parsed.profile)
        local = parsed.connection.adapter == "e5-onnx"
        return {
            "fingerprint": caps.fingerprint,
            "dimensions": 384 if local else parsed.profile.dimensions,
            "preparation_version": "e5-prefix-attention-mean-l2-v1"
            if local
            else "raw-provider-l2-v1",
            "normalization": "l2",
            "embedding_tokens": caps.limits.embedding_tokens,
            "prefixes": {"query": "query: ", "document": "passage: "} if local else None,
            "tokenizer": parsed.profile.tokenizer
            or ("multilingual-e5-small-pinned" if local else None),
        }

    async def embed(
        self, batch: dict[str, Any] | EmbeddingBatch, policy: dict[str, Any]
    ) -> dict[str, Any]:
        parsed = self._policy(policy)
        caps = resolve_capabilities(parsed.connection, parsed.profile)
        secret = await self._authorize(parsed)
        value = batch if isinstance(batch, EmbeddingBatch) else EmbeddingBatch.model_validate(batch)
        if value.model_fingerprint != caps.fingerprint:
            raise ProviderError(
                "invalid_input",
                "Embedding request fingerprint differs from the selected model and preparation",
            )
        if parsed.connection.adapter != "e5-onnx":
            limit = caps.limits.embedding_tokens or parsed.profile.context_tokens
            if limit is None:
                raise ProviderError(
                    "invalid_input",
                    "Declare the selected embedding model's input token limit before budgeted requests",
                )
            token_upper_bounds = [len(text.encode("utf-8")) for text in value.texts]
            if any(count > limit for count in token_upper_bounds):
                raise ProviderError(
                    "quota",
                    "Embedding input exceeds its conservative UTF-8 token budget; split using the selected tokenizer",
                )
            if parsed.budgets.max_cost is not None:
                price = parsed.budgets.input_price_per_million
                if price is None:
                    raise ProviderError(
                        "unsupported",
                        "A monetary embedding budget requires an explicit input price",
                    )
                if (
                    sum(token_upper_bounds) * price * parsed.budgets.max_requests / 1000000
                    > parsed.budgets.max_cost
                ):
                    raise ProviderError(
                        "quota",
                        "Embedding requests and allowed retries exceed the configured monetary budget",
                    )
        if parsed.connection.adapter == "e5-onnx":
            worker = await self._worker(parsed)
            result = await worker.request(
                "embed", value.model_dump(), timeout=parsed.budgets.deadline_seconds or 120
            )
            return EmbeddingResult.model_validate(result).model_dump()
        llm = self.registry.resolve(parsed.connection, parsed.profile, secret)
        result = await CloudEmbedder(llm).embed(
            value,
            deadline=time.monotonic() + (parsed.budgets.deadline_seconds or 120),
            max_retries=parsed.budgets.max_requests - 1,
        )
        return result.model_dump()

    async def discover_models(
        self, connection: dict[str, Any], *, scope: str = "connection_test"
    ) -> list[dict[str, Any]]:
        # Discovery can disclose the connection destination even without meeting text.
        route = Route(
            connection=Connection.model_validate(connection),
            profile=ModelProfile(id="discovery", model="discovery", revision="discovery"),
            scope=scope,
            data_classes=["connection_test"],
        )
        secret = await self._authorize(route)
        return await self.registry.discover_models(route.connection, secret)

    async def prepare_runtime(self, policy: dict[str, Any]) -> dict[str, Any]:
        selected = self._policy(policy)
        await self._authorize(selected)
        worker = await self._worker(selected)
        return {
            "available": True,
            "capabilities": worker.capabilities,
            "model_fingerprint": worker.model_fingerprint,
        }

    async def test_connection(
        self, connection: dict[str, Any], profile: dict[str, Any], *, scope: str = "connection_test"
    ) -> dict[str, Any]:
        route = Route(
            connection=Connection.model_validate(connection),
            profile=ModelProfile.model_validate(profile),
            scope=scope,
            data_classes=["connection_test"],
        )
        secret = await self._authorize(route)
        caps = resolve_capabilities(route.connection, route.profile)
        if route.connection.adapter in {"deepgram", "assemblyai"}:
            from ...infrastructure.providers.asr import CloudASR

            if not secret:
                raise ProviderError("auth", "Selected cloud speech credential is unavailable")
            session = await CloudASR(
                route.connection, route.profile, secret, self.registry.client
            ).open_stream(sample_rate=16000)
            await session.abort()
            return {
                "available": True,
                "capabilities": caps.model_dump(),
                "probe": "actual-cloud-stream-handshake",
                "audio_uploaded": False,
                "outbound_destination": route.connection.destination,
            }
        if route.connection.adapter == "azure-speech":
            from ...infrastructure.providers.local import AzureSDKSession

            worker = await self._worker(route)
            session = AzureSDKSession(
                worker, sample_rate=16000, languages=route.profile.options.get("languages", [])
            )
            try:
                await session.open()
                await session.close()
                return {
                    "available": True,
                    "capabilities": caps.model_dump(),
                    "probe": "actual-sdk-cloud-session",
                    "audio_uploaded": False,
                    "outbound_destination": route.connection.destination,
                }
            finally:
                await worker.close()
        if route.connection.adapter in {
            "faster-whisper",
            "sherpa-parakeet",
            "community-1",
            "e5-onnx",
            "azure-speech",
        }:
            worker = await self._worker(route)
            return {
                "available": True,
                "capabilities": caps.model_dump(),
                "probe": "actual-worker-handshake",
                "runtime_capabilities": worker.capabilities,
                "outbound_destination": route.connection.destination,
            }
        if route.profile.options.get("role") == "embeddings":
            value = EmbeddingBatch(
                purpose="query",
                texts=["Explicit user connection test."],
                model_fingerprint=caps.fingerprint,
            )
            output = await CloudEmbedder(
                self.registry.resolve(route.connection, route.profile, secret)
            ).embed(value, deadline=time.monotonic() + 20, max_retries=0)
            return {
                "available": True,
                "capabilities": caps.model_dump(),
                "probe": "actual-embedding",
                "dimensions": output.dimensions,
                "outbound_destination": route.connection.destination,
            }
        llm = self.registry.resolve(route.connection, route.profile, secret)
        probe_schema = {
            "type": "object",
            "properties": {"connected": {"type": "boolean"}},
            "required": ["connected"],
            "additionalProperties": False,
        }
        request = LLMRequest(
            operation_id=str(uuid.uuid4()),
            task="connection_test",
            policy_revision=0,
            system_text='This is an explicit user connection test. Emit only the complete JSON object {"connected":true}.',
            messages=[{"role": "user", "content": "Connection test; no meeting data is included."}],
            output_schema=probe_schema,
            output_limit=min(128, route.profile.output_tokens or 128),
            deadline=time.monotonic() + 20,
            cancellation_token=str(uuid.uuid4()),
        )
        candidate: Any = None
        async for event in llm.generate(request):
            if event.type == "structured_candidate":
                candidate = event.data["result"]
        if (
            not isinstance(candidate, dict)
            or set(cast(dict[str, Any], candidate)) != {"connected"}
            or cast(dict[str, Any], candidate)["connected"] is not True
        ):
            raise ProviderError(
                "invalid_output", "Connection probe returned an invalid complete structured result"
            )
        return {
            "available": True,
            "capabilities": llm.capabilities.model_dump(),
            "probe": "actual-structured-generation",
            "outbound_destination": route.connection.destination,
        }

    async def close(self) -> None:
        for worker in self.registry.worker_runtimes.values():
            await worker.close()
        await self.registry.close()

    async def transcribe(
        self,
        pcm_float32: bytes,
        *,
        sample_rate: int,
        policy: dict[str, Any],
        origin_ms: int = 0,
        stream_id: str | None = None,
        chunk_id: str | None = None,
        vocabulary: list[str] | None = None,
        language: str | None = None,
        cancelled: asyncio.Event | None = None,
        on_hypothesis: Callable[[dict[str, Any]], None | Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        import io
        import wave
        from array import array

        from ...infrastructure.providers.asr import CloudASR
        from ...infrastructure.providers.azure_file import AzureFileASR
        from ...infrastructure.providers.contracts import TranscriptHypothesis

        parsed = self._policy(policy)
        secret = await self._authorize(parsed)
        adapter = parsed.connection.adapter
        if len(pcm_float32) % 4 or sample_rate <= 0:
            raise ProviderError(
                "invalid_input",
                "ASR requires mono little-endian float32 PCM and a valid sample rate",
            )
        if adapter in {"faster-whisper", "sherpa-parakeet"}:
            worker = await self._worker(parsed)
            async with worker.pcm(pcm_float32, sample_rate) as grant:
                output = await worker.request(
                    "transcribe",
                    {
                        **grant,
                        "origin_ms": origin_ms,
                        "stream_id": stream_id or str(uuid.uuid4()),
                        "chunk_id": chunk_id or str(uuid.uuid4()),
                        "language": None if not language or language == "auto" else language,
                        "vocabulary": vocabulary or [],
                    },
                    timeout=parsed.budgets.deadline_seconds or 120,
                    cancelled=cancelled,
                )
            return [
                TranscriptHypothesis.model_validate(row).model_dump()
                for row in output["hypotheses"]
            ]
        if adapter not in {"deepgram", "assemblyai", "azure-speech"}:
            raise ProviderError("unsupported", "Selected adapter is not an ASR protocol")
        if not secret:
            raise ProviderError("auth", "Cloud ASR requires an available credential")
        samples = array("f")
        samples.frombytes(pcm_float32)
        import sys

        if sys.byteorder != "little":
            samples.byteswap()
        import math

        if any(not math.isfinite(value) for value in samples):
            raise ProviderError("invalid_input", "Audio contains non-finite samples")
        integer = array("h", (round(max(-1, min(1, value)) * 32767) for value in samples))
        if sys.byteorder != "little":
            integer.byteswap()
        if on_hypothesis is not None:
            deadline = time.monotonic() + (parsed.budgets.deadline_seconds or 120)
            session = await self._cancellable(
                self.open_asr_stream(
                    policy=policy,
                    sample_rate=sample_rate,
                    origin_ms=origin_ms,
                    vocabulary=vocabulary,
                    language=language,
                    diarization=bool(parsed.profile.options.get("diarization", False)),
                ),
                cancelled,
                deadline,
            )
            finals: dict[tuple[str, str], dict[str, Any]] = {}
            data = pcm_float32 if adapter == "azure-speech" else integer.tobytes()
            chunk_bytes = max(1, sample_rate // 10) * (4 if adapter == "azure-speech" else 2)

            async def send_audio() -> None:
                for offset in range(0, len(data), chunk_bytes):
                    await session.push(data[offset : offset + chunk_bytes])
                await session.flush()
                await session.close()

            async def receive_hypotheses() -> None:
                async for hypothesis in session.events():
                    row = TranscriptHypothesis.model_validate(hypothesis).model_dump()
                    identity = (row["provider_stream_id"], row["provider_result_id"])
                    if row["is_final"]:
                        previous = finals.get(identity)
                        if previous is None or row["revision"] >= previous["revision"]:
                            finals[identity] = row
                    else:
                        await maybe_await(on_hypothesis(row))

            async def stream_audio() -> None:
                sender = asyncio.create_task(send_audio())
                receiver = asyncio.create_task(receive_hypotheses())
                try:
                    await asyncio.gather(sender, receiver)
                finally:
                    for task in (sender, receiver):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(sender, receiver, return_exceptions=True)

            try:
                await self._cancellable(stream_audio(), cancelled, deadline)
                return sorted(
                    finals.values(),
                    key=lambda row: (
                        row["start_ms"],
                        row["provider_stream_id"],
                        row["provider_result_id"],
                    ),
                )
            finally:
                await session.abort()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(integer.tobytes())
        provider = (
            AzureFileASR(parsed.connection, parsed.profile, secret, self.registry.client)
            if adapter == "azure-speech"
            else CloudASR(parsed.connection, parsed.profile, secret, self.registry.client)
        )
        output = await provider.transcribe_file(
            buffer.getvalue(),
            content_type="audio/wav",
            deadline=time.monotonic() + (parsed.budgets.deadline_seconds or 120),
            origin_ms=origin_ms,
            vocabulary=vocabulary or [],
            diarization=bool(parsed.profile.options.get("diarization", False)),
            cancelled=cancelled,
        )
        return [row.model_dump() for row in output]

    async def open_asr_stream(
        self,
        *,
        policy: dict[str, Any],
        sample_rate: int,
        origin_ms: int = 0,
        vocabulary: list[str] | None = None,
        language: str | None = None,
        diarization: bool = False,
    ) -> CloudSpeechSession | AzureSDKSession | ChunkASRSession:
        from ...infrastructure.providers.asr import CloudASR
        from ...infrastructure.providers.local import AzureSDKSession, ChunkASRSession

        parsed = self._policy(policy)
        secret = await self._authorize(parsed)
        if parsed.connection.adapter in {"faster-whisper", "sherpa-parakeet", "azure-speech"}:
            worker = await self._worker(parsed)
            if parsed.connection.adapter == "azure-speech":
                return await AzureSDKSession(
                    worker,
                    sample_rate=sample_rate,
                    origin_ms=origin_ms,
                    vocabulary=vocabulary,
                    languages=[language]
                    if language
                    else parsed.profile.options.get("languages", []),
                    diarization=diarization,
                ).open()
            return ChunkASRSession(
                worker,
                sample_rate=sample_rate,
                origin_ms=origin_ms,
                language=language,
                vocabulary=vocabulary,
            )
        if not secret:
            raise ProviderError("auth", "Selected cloud speech credential is unavailable")
        return await CloudASR(
            parsed.connection, parsed.profile, secret, self.registry.client
        ).open_stream(
            sample_rate=sample_rate,
            origin_ms=origin_ms,
            vocabulary=vocabulary,
            diarization=diarization,
        )

    async def diarize(
        self,
        *,
        pcm_float32: bytes | None = None,
        sample_rate: int = 16000,
        policy: dict[str, Any] | None = None,
        origin_ms: int = 0,
        final: bool = False,
        meeting: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if pcm_float32 is None or policy is None:
            raise ProviderError(
                "model_missing",
                "Diarization needs explicitly granted retained or bounded-window PCM plus a selected policy; no full recording is implied",
            )
        parsed = self._policy(policy)
        await self._authorize(parsed)
        worker = await self._worker(parsed)
        caps = resolve_capabilities(parsed.connection, parsed.profile)
        async with self.gpu_lock:
            async with worker.pcm(pcm_float32, sample_rate) as grant:
                return await worker.request(
                    "diarize_final" if final else "diarize",
                    {
                        **grant,
                        "origin_ms": origin_ms,
                        "model_fingerprint": caps.fingerprint,
                        "session_id": (meeting or {}).get("id", str(uuid.uuid4())),
                    },
                    timeout=parsed.budgets.deadline_seconds or 180,
                )

    async def count_vocabulary_tokens(self, profile: dict[str, Any], texts: list[str]) -> list[int]:
        if self.policy_resolver is None:
            worker = self.registry.worker_runtimes.get(profile["id"])
            if worker is None:
                raise ProviderError("model_missing", "The selected tokenizer worker is not ready")
        else:
            parsed = self._policy({"model_profile_id": profile["id"]})
            if parsed.connection.adapter not in {"faster-whisper", "e5-onnx"}:
                raise ProviderError(
                    "unsupported",
                    "This adapter does not supply a local authoritative tokenizer; use a visibly conservative byte bound",
                )
            worker = await self._worker(parsed)
        result = await worker.request("tokenize", {"texts": texts}, timeout=20)
        return result["counts"]

    async def tokenize(self, texts: list[str], *, policy: dict[str, Any]) -> dict[str, Any]:
        parsed = self._policy(policy)
        if parsed.connection.adapter != "e5-onnx":
            raise ProviderError(
                "unsupported",
                "Exact retrieval token offsets require the selected E5 tokenizer runtime",
            )
        worker = await self._worker(parsed)
        return await worker.request("tokenize", {"texts": texts}, timeout=20)

    async def preflight_asr(self, policy: dict[str, Any]) -> dict[str, Any]:
        parsed = self._policy(policy)
        caps = resolve_capabilities(parsed.connection, parsed.profile)
        if not caps.execution == "local" and not {"live_audio", "audio"}.intersection(
            parsed.data_classes
        ):
            raise ProviderError(
                "permission",
                "Cloud capture requires explicit live_audio data-class consent before Start",
            )
        await self._authorize(parsed)
        if parsed.connection.adapter == "azure-speech":
            from ...infrastructure.providers.local import AzureSDKSession

            worker = await self._worker(parsed)
            session = AzureSDKSession(
                worker, sample_rate=16000, languages=parsed.profile.options.get("languages", [])
            )
            try:
                await session.open()
                await session.close()
                return {
                    "available": True,
                    "capabilities": caps.model_dump(),
                    "probe": "actual-sdk-cloud-session",
                    "audio_uploaded": False,
                }
            finally:
                await worker.close()
        if parsed.connection.adapter in {"faster-whisper", "sherpa-parakeet"}:
            worker = await self._worker(parsed)
            return {
                "available": True,
                "capabilities": caps.model_dump(),
                "runtime_capabilities": worker.capabilities,
            }
        if parsed.connection.adapter not in {"deepgram", "assemblyai"}:
            raise ProviderError("unsupported", "Selected capture policy is not an ASR adapter")
        session = await self.open_asr_stream(policy=policy, sample_rate=16000)
        await session.abort()
        return {
            "available": True,
            "capabilities": caps.model_dump(),
            "probe": "actual-cloud-stream-handshake",
            "audio_uploaded": False,
        }
