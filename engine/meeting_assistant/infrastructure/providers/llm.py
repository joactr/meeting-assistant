"""Native HTTP wire adapters. No provider SDK enters application code."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import random
import time
from collections.abc import AsyncIterator, Mapping
from datetime import UTC
from typing import Any, Literal, TypedDict, Unpack, cast
from urllib.parse import urlsplit

import httpx

from .contracts import (
    Capabilities,
    Connection,
    LLMEvent,
    LLMRequest,
    ModelProfile,
    ProviderError,
    check_deadline,
    is_loopback,
)


class _RequestOptions(TypedDict, total=False):
    headers: Mapping[str, str]
    json: Any
    content: bytes


def _private_ip(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def safe_endpoint(endpoint: str) -> str:
    url = urlsplit(endpoint)
    if (
        url.scheme not in {"http", "https"}
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ProviderError(
            "invalid_input",
            "Endpoint must be an HTTP(S) origin/path without embedded credentials, query or fragment",
        )
    if url.scheme == "http" and not is_loopback(endpoint) and not _private_ip(url.hostname):
        raise ProviderError("permission", "Remote inference endpoints require HTTPS")
    return endpoint.rstrip("/")


def http_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    code = {
        400: "invalid_input",
        401: "auth",
        403: "permission",
        404: "model_missing",
        408: "timeout",
        413: "invalid_input",
        422: "invalid_input",
        429: "rate_limited",
    }.get(status, "network")
    retry = status in {408, 429, 500, 502, 503, 504}
    retry_after = response.headers.get("retry-after", "")
    provider_code = str(status)
    if response.is_stream_consumed:
        try:
            body: Any = response.json()
            error: Any = (
                cast(dict[str, Any], body).get("error", {}) if isinstance(body, dict) else {}
            )
            error_mapping = cast(dict[str, Any], error) if isinstance(error, dict) else {}
            raw_code = error_mapping.get("code", error_mapping.get("type"))
            if isinstance(raw_code, (str, int)) and len(str(raw_code)) <= 100:
                provider_code = str(raw_code)
            if raw_code in {"insufficient_quota", "billing_hard_limit_reached", "quota_exceeded"}:
                code, retry = "quota", False
        except (ValueError, TypeError):
            pass
    retry_seconds = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None
    if retry_after and retry_seconds is None:
        from datetime import datetime
        from email.utils import parsedate_to_datetime

        try:
            retry_seconds = max(
                0, (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds()
            )
        except (TypeError, ValueError, OverflowError):
            pass
    return ProviderError(
        code,
        f"Provider request failed (HTTP {status})",
        retryable=retry,
        provider_code=provider_code,
        retry_after=retry_seconds,
    )


async def json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    deadline: float,
    cancelled: asyncio.Event | None = None,
    max_retries: int | None = None,
    **kwargs: Unpack[_RequestOptions],
) -> dict[str, Any]:
    retries = min(
        2, max_retries if max_retries is not None else (2 if method.upper() == "GET" else 0)
    )
    for attempt in range(retries + 1):
        check_deadline(deadline, cancelled)
        try:
            response = await client.request(
                method, url, timeout=max(0.1, deadline - time.monotonic()), **kwargs
            )
            if response.is_error:
                raise http_error(response)
            return response.json()
        except httpx.TimeoutException:
            error = ProviderError("timeout", "Provider request timed out", retryable=True)
        except httpx.RequestError:
            error = ProviderError("network", "Provider transport failed", retryable=True)
        except ProviderError as exc:
            error = exc
        except (ValueError, KeyError) as exc:
            raise ProviderError("invalid_output", "Provider returned invalid JSON") from exc
        if attempt == retries or not error.retryable:
            raise error
        delay = max(error.retry_after or 0, 2**attempt + random.uniform(0, 0.2))
        if time.monotonic() + delay >= deadline:
            raise error
        await asyncio.sleep(delay)
    raise ProviderError("network", "Provider request failed")


async def sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    name = "message"
    data: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            raw = "\n".join(data)
            if raw == "[DONE]":
                return
            try:
                yield name, json.loads(raw)
            except ValueError as exc:
                raise ProviderError("invalid_output", "Malformed provider stream frame") from exc
            name, data = "message", []
    if data and "\n".join(data) != "[DONE]":
        try:
            yield name, json.loads("\n".join(data))
        except ValueError as exc:
            raise ProviderError("invalid_output", "Incomplete provider stream frame") from exc


class NativeLLM:
    def __init__(
        self,
        connection: Connection,
        profile: ModelProfile,
        capabilities: Capabilities,
        secret: str | None,
        client: httpx.AsyncClient,
    ):
        self.connection, self.profile, self.capabilities = connection, profile, capabilities
        self.secret, self.client = secret, client
        self.base = safe_endpoint(connection.endpoint)
        if urlsplit(self.base).path in {"", "/"}:
            suffix = {
                "openai": "/v1",
                "anthropic": "/v1",
                "azure-openai": "/openai/v1",
                "gemini": "/" + (connection.api_version or "v1beta"),
            }.get(connection.adapter, "")
            self.base += suffix

    def headers(self) -> dict[str, str]:
        adapter = self.connection.adapter
        if self.connection.auth_scheme != "none" and not self.secret:
            raise ProviderError("auth", "Selected connection has no available credential")
        headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
        if adapter == "anthropic":
            headers["anthropic-version"] = self.connection.api_version or "2023-06-01"
            if self.secret:
                headers["x-api-key"] = self.secret
        elif adapter == "gemini":
            if self.secret:
                headers["x-goog-api-key"] = self.secret
        elif adapter == "azure-openai" and self.connection.auth_scheme != "entra":
            if self.secret:
                headers["api-key"] = self.secret
        elif self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        elif self.connection.auth_scheme != "none":
            raise ProviderError("auth", "Selected connection has no available credential")
        return headers

    def payload(self, request: LLMRequest) -> tuple[str, dict[str, Any]]:
        adapter, model, schema = self.connection.adapter, self.profile.model, request.output_schema
        options = self.profile.options
        temperature = (
            options.get("temperature") if options.get("supports_temperature", False) else None
        )
        body: dict[str, Any]
        if adapter == "openai":
            body = {
                "model": model,
                "instructions": request.system_text,
                "input": request.messages,
                "max_output_tokens": request.output_limit,
                "stream": True,
                "store": False,
            }
            if schema and self.capabilities.structured_output.strict_schema:
                body["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": "meeting_result",
                        "schema": schema,
                        "strict": True,
                    }
                }
            path = "/responses"
        elif adapter in {"openai-compatible", "openrouter", "azure-openai"}:
            body = {
                "model": model,
                "messages": [{"role": "system", "content": request.system_text}, *request.messages],
                "stream": True,
            }
            body[options.get("output_cap_field", "max_tokens")] = request.output_limit
            if options.get("supports_stream_usage", False):
                body["stream_options"] = {"include_usage": True}
            if schema and self.capabilities.structured_output.strict_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "meeting_result", "schema": schema, "strict": True},
                }
            elif schema and self.capabilities.structured_output.json_mode:
                body["response_format"] = {"type": "json_object"}
            path = "/chat/completions"
        elif adapter == "anthropic":
            body = {
                "model": model,
                "system": request.system_text,
                "messages": request.messages,
                "max_tokens": request.output_limit,
                "stream": True,
            }
            if schema and self.capabilities.structured_output.strict_schema:
                body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
            path = "/messages"
        elif adapter == "gemini":
            body = {
                "systemInstruction": {"parts": [{"text": request.system_text}]},
                "contents": [
                    {
                        "role": "model" if m["role"] == "assistant" else "user",
                        "parts": [{"text": m["content"]}],
                    }
                    for m in request.messages
                ],
                "generationConfig": {"maxOutputTokens": request.output_limit},
            }
            if schema:
                body["generationConfig"]["responseMimeType"] = "application/json"
                if self.capabilities.structured_output.strict_schema:
                    body["generationConfig"]["responseJsonSchema"] = schema
            path = f"/models/{model}:streamGenerateContent?alt=sse"
        elif adapter == "ollama":
            body = {
                "model": model,
                "messages": [{"role": "system", "content": request.system_text}, *request.messages],
                "stream": True,
                "options": {"num_predict": request.output_limit},
                "keep_alive": options.get("keep_alive", "5m"),
            }
            if schema:
                body["format"] = (
                    schema if self.capabilities.structured_output.strict_schema else "json"
                )
            path = "/api/chat"
        else:
            raise ProviderError("unsupported", "Connection does not implement LLM generation")
        if temperature is not None:
            if adapter == "gemini":
                body["generationConfig"]["temperature"] = temperature
            elif adapter == "ollama":
                body["options"]["temperature"] = temperature
            else:
                body["temperature"] = temperature
        return self.base + path, body

    async def generate(
        self, request: LLMRequest, cancelled: asyncio.Event | None = None
    ) -> AsyncIterator[LLMEvent]:
        url, body = self.payload(request)
        text: list[str] = []
        complete, refusal = False, False
        try:
            check_deadline(request.deadline, cancelled)
            async with self.client.stream(
                "POST",
                url,
                json=body,
                headers=self.headers(),
                timeout=max(0.1, request.deadline - time.monotonic()),
            ) as response:
                if response.is_error:
                    raise http_error(response)
                if self.connection.adapter == "ollama":
                    frames = self._ndjson(response)
                else:
                    frames = sse(response)
                async for name, frame in frames:
                    check_deadline(request.deadline, cancelled)
                    if "error" in frame or name == "error":
                        raise ProviderError("network", "Provider stream reported an error")
                    events = self.normalize(name, frame)
                    for event in events:
                        if event.type == "text_delta":
                            text.append(event.data["text"])
                        if event.type == "refusal":
                            refusal = True
                        if event.type == "complete":
                            complete = True
                            if event.data.get("truncated"):
                                raise ProviderError(
                                    "invalid_output",
                                    "Provider output was truncated by its output limit",
                                )
                        if event.type != "complete":
                            yield event
                if refusal:
                    raise ProviderError(
                        "provider_refusal", "Provider refused the requested analysis"
                    )
                if not complete:
                    raise ProviderError("invalid_output", "Provider stream ended before completion")
                full_text = "".join(text)
                if request.output_schema is not None:
                    try:
                        result = json.loads(full_text)
                    except ValueError as exc:
                        raise ProviderError(
                            "invalid_output", "Completed provider output is not valid JSON"
                        ) from exc
                    yield LLMEvent(type="structured_candidate", data={"result": result})
                yield LLMEvent(type="complete", data={"text": full_text})
        except asyncio.CancelledError:
            raise ProviderError(
                "cancelled", "Stopped locally; provider may continue processing"
            ) from None
        except httpx.TimeoutException:
            raise ProviderError(
                "timeout", "Provider stream deadline exceeded", retryable=True
            ) from None
        except httpx.RequestError:
            raise ProviderError(
                "network",
                "Provider stream disconnected; no unacknowledged replay was attempted",
                retryable=True,
            ) from None

    async def _ndjson(self, response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        async for line in response.aiter_lines():
            if line:
                try:
                    yield "message", json.loads(line)
                except ValueError as exc:
                    raise ProviderError("invalid_output", "Malformed NDJSON frame") from exc

    def normalize(self, name: str, frame: dict[str, Any]) -> list[LLMEvent]:
        adapter = self.connection.adapter
        result: list[LLMEvent] = []

        def add(
            kind: Literal[
                "text_delta", "structured_candidate", "usage", "refusal", "complete", "error"
            ],
            **data: Any,
        ) -> None:
            result.append(LLMEvent(type=kind, data=data))

        if adapter == "openai":
            kind = frame.get("type", name)
            if kind == "response.output_text.delta":
                add("text_delta", text=frame.get("delta", ""))
            elif kind.startswith("response.refusal"):
                add("refusal", reason="provider_refusal")
            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                response = frame.get("response", {})
                if response.get("usage"):
                    add("usage", **response["usage"])
                add(
                    "complete",
                    truncated=kind != "response.completed",
                    status=response.get("status"),
                )
        elif adapter in {"openai-compatible", "openrouter", "azure-openai"}:
            if frame.get("usage"):
                add("usage", **frame["usage"])
            for choice in frame.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("content"):
                    add("text_delta", text=delta["content"])
                if delta.get("refusal") or choice.get("finish_reason") == "content_filter":
                    add("refusal", reason="provider_refusal")
                if choice.get("finish_reason"):
                    add("complete", truncated=choice["finish_reason"] == "length")
        elif adapter == "anthropic":
            kind = frame.get("type", name)
            if kind == "content_block_delta" and frame.get("delta", {}).get("type") == "text_delta":
                add("text_delta", text=frame["delta"]["text"])
            elif kind == "message_delta":
                if frame.get("usage"):
                    add("usage", **frame["usage"])
                stop = frame.get("delta", {}).get("stop_reason")
                if stop == "refusal":
                    add("refusal", reason="provider_refusal")
                if stop:
                    add(
                        "complete",
                        truncated=stop in {"max_tokens", "model_context_window_exceeded"},
                    )
            elif kind == "message_start" and frame.get("message", {}).get("usage"):
                add("usage", **frame["message"]["usage"])
        elif adapter == "gemini":
            if frame.get("promptFeedback", {}).get("blockReason"):
                add("refusal", reason="provider_refusal")
            for candidate in frame.get("candidates", []):
                for part in candidate.get("content", {}).get("parts", []):
                    if part.get("text") and not part.get("thought"):
                        add("text_delta", text=part["text"])
                stop = candidate.get("finishReason")
                if stop in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"}:
                    add("refusal", reason="provider_refusal")
                if stop:
                    add("complete", truncated=stop == "MAX_TOKENS")
            if frame.get("usageMetadata"):
                add("usage", **frame["usageMetadata"])
        else:
            if frame.get("message", {}).get("content"):
                add("text_delta", text=frame["message"]["content"])
            if frame.get("done"):
                add(
                    "usage",
                    input_tokens=frame.get("prompt_eval_count"),
                    output_tokens=frame.get("eval_count"),
                )
                add("complete", truncated=frame.get("done_reason") == "length")
        return result
