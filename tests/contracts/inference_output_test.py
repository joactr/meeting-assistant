"""Model output is repaired where safe, rejected with a reason where not, and in the right language."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from meeting_assistant.application.inference.results import prune_sources
from meeting_assistant.application.inference.service import InferenceService
from meeting_assistant.application.routing import output_language
from meeting_assistant.infrastructure.providers.contracts import ProviderError
from meeting_assistant.infrastructure.providers.registry import ProviderRegistry

POLICY = {
    "connection": {
        "id": "fixture",
        "adapter": "openai-compatible",
        "endpoint": "https://provider.invalid/v1",
        "auth_scheme": "none",
        "execution": "remote",
        "destination": "fixture",
    },
    "profile": {
        "id": "p",
        "model": "m",
        "revision": "r",
        "context_tokens": 32768,
        "output_tokens": 4096,
    },
    "scope": "meeting",
    "data_classes": ["meeting_text"],
}


def utterance(
    ident: str, text: str, start: int, end: int, language: str | None = None
) -> dict[str, Any]:
    return {
        "id": ident,
        "source_id": ident,
        "text": text,
        "start_ms": start,
        "end_ms": end,
        "revision": 1,
        "kind": "transcript",
        "detected_language": language,
    }


def ref(source_id: str, quote: str | None = None) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "revision": 1,
        "start_ms": 0,
        "end_ms": 1000,
        "quote": quote,
    }


def section(source_id: str, body: str) -> dict[str, Any]:
    return {
        "heading": "H",
        "body": body,
        "source_refs": [ref(source_id)],
        "provenance": "Transcript",
    }


def service(candidate: Any, seen: list[Any]) -> InferenceService:
    svc = InferenceService(ProviderRegistry(), lambda _: None, lambda *_: True)
    llm = SimpleNamespace(
        capabilities=SimpleNamespace(
            structured_output=SimpleNamespace(strict_schema=True), fingerprint="f"
        ),
        connection=SimpleNamespace(id="fixture"),
    )
    svc.registry.resolve = lambda *_: llm  # type: ignore[method-assign]

    async def collect(_llm: Any, request: Any, _cancelled: Any) -> tuple[Any, dict[str, Any]]:
        seen.append(request)
        return candidate, {}

    svc._collect = collect  # type: ignore[method-assign]
    return svc


async def test_output_language_is_instructed_and_follows_the_meeting() -> None:
    seen: list[Any] = []
    svc = service({"sections": [], "citations": []}, seen)
    evidence = [
        utterance("a", "Hola, revisamos el plan de lanzamiento.", 0, 30000, "es"),
        utterance("b", "ok", 30000, 31000, "ja"),
        utterance("c", "はい", 31000, 32000, "ja"),
        utterance("d", "そう", 32000, 33000, "ja"),
    ]
    await svc.generate("summary", evidence, POLICY, language="auto", context={})
    # Spanish dominates the speech time; three short noise bursts labelled "ja" must not win.
    assert "BCP-47 tag es" in seen[-1].system_text
    assert json.loads(seen[-1].messages[0]["content"])["output_language"] == "es"
    await svc.generate("summary", evidence, POLICY, language="ja", context={})
    assert "BCP-47 tag ja" in seen[-1].system_text
    # Without any detected language the model is told to follow the evidence, never left free.
    await svc.generate("summary", [utterance("a", "x", 0, 10)], POLICY, language="auto", context={})
    assert "language most of the evidence is written in" in seen[-1].system_text


def test_meeting_language_resolves_when_output_is_auto() -> None:
    assert output_language({"config_snapshot": {"output_language": "auto"}}) == "auto"
    assert (
        output_language({"config_snapshot": {"output_language": "auto", "meeting_language": "es"}})
        == "es"
    )
    assert (
        output_language({"config_snapshot": {"output_language": "en", "meeting_language": "es"}})
        == "en"
    )


async def test_bad_citations_are_dropped_not_fatal_and_failures_say_why() -> None:
    evidence = [utterance("u1", "hello there", 0, 1000, "en")]
    good, ghost = section("u1", "kept"), section("ghost", "dropped")
    svc = service({"sections": [good, ghost], "citations": [ref("ghost")]}, [])
    result = (await svc.generate("summary", evidence, POLICY, language="en", context={}))["result"]
    assert [s["body"] for s in result["sections"]] == ["kept"] and result["citations"] == []

    # A missing nullable key is null, not an error.
    action = {
        "description": "Send it",
        "priority": "normal",
        "source_refs": [ref("u1", "hello")],
    }
    svc = service({"candidates": [action]}, [])
    out = (await svc.generate("actions", evidence, POLICY, language="en", context={}))["result"]
    assert out["candidates"][0]["assignee"] is None

    svc = service({"sections": [{"heading": "H"}], "citations": []}, [])
    with pytest.raises(ProviderError) as caught:
        await svc.generate("summary", evidence, POLICY, language="en", context={})
    assert caught.value.code == "invalid_output" and caught.value.retryable
    assert "sections.0.body" in caught.value.message


def test_prune_removes_unsupported_items_only() -> None:
    evidence = [utterance("u1", "hello there", 0, 1000, "en")]
    candidate: dict[str, Any] = {
        "topics": [
            {"title": "ok", "start_ms": 0, "end_ms": 500, "source_refs": [ref("u1")]},
            {
                "title": "ghost",
                "start_ms": 0,
                "end_ms": 500,
                "source_refs": [ref("zzz")],
            },
        ]
    }
    prune_sources(candidate, evidence)
    assert [t["title"] for t in candidate["topics"]] == ["ok"]
