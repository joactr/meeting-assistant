from __future__ import annotations

from pathlib import Path

import pytest
from meeting_assistant.application.inference.setup import setup_llm
from meeting_assistant.domain.errors import AppError
from meeting_assistant.infrastructure.providers.contracts import (
    Connection,
    ModelProfile,
    normalize_endpoint,
)
from meeting_assistant.infrastructure.providers.registry import resolve_capabilities
from meeting_assistant.workspace import Workspace


def execution(adapter: str, endpoint: str) -> str:
    connection = Connection(
        id="c", adapter=adapter, endpoint=endpoint, auth_scheme="none", destination=endpoint
    )
    return resolve_capabilities(connection, ModelProfile(id="p", model="m", revision="r")).execution


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://localhost:8080", "local"),
        ("127.0.0.5:8080", "local"),
        ("http://[::1]:8080", "local"),
        ("10.0.0.5:8080", "remote"),
        ("http://127.example.com", "remote"),
    ],
)
def test_openai_compatible_is_local_only_on_loopback(endpoint: str, expected: str):
    assert execution("openai-compatible", endpoint) == expected


def test_endpoint_without_scheme_is_prefixed_and_garbage_is_rejected():
    assert normalize_endpoint("192.168.1.9:8080") == "http://192.168.1.9:8080"
    assert normalize_endpoint("https://api.example.com/v1") == "https://api.example.com/v1"
    for bad in ("", "http://", ":8080", "host:notaport"):
        with pytest.raises(AppError) as failure:
            normalize_endpoint(bad)
        assert (failure.value.code, failure.value.status) == ("invalid_endpoint", 422)


def test_setup_needs_explicit_consent_for_remote_endpoint(tmp_path: Path):
    w = Workspace(tmp_path / "workspace.sqlite3")
    try:
        payload = {"adapter": "openai-compatible", "endpoint": "10.0.0.5:8080", "model": "m"}
        with pytest.raises(AppError) as failure:
            setup_llm(w, payload)
        assert (failure.value.code, failure.value.status) == ("consent_required", 403)
        assert w.list("provider_connections") == [] and w.list("consents") == []

        setup_llm(w, {**payload, "accepted_destination": True})
        assert [c["destination_fingerprint"] for c in w.list("consents")] == [
            "http://10.0.0.5:8080"
        ]

        setup_llm(w, {**payload, "endpoint": "localhost:8080"})
        assert len(w.list("consents")) == 1
    finally:
        w.close()
