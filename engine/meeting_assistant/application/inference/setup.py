"""First-run provisioning: turn a downloaded model or a chosen LLM into a ready task route."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from ...domain.model import new_id, now_ms
from ...infrastructure.providers.contracts import Connection, ModelProfile, ProviderError
from ...infrastructure.providers.registry import DESCRIPTORS, resolve_capabilities
from ...infrastructure.workers.models import FAMILIES
from ..routing import task_policy
from .policies import connection_value, profile_value
from .service import InferenceService

if TYPE_CHECKING:
    from ...workspace import Workspace

OLLAMA = "http://127.0.0.1:11434"
FIXED_ENDPOINTS = {
    "ollama": OLLAMA,
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
}
BEARER_LISTING = {"openai", "openrouter", "openai-compatible"}
LOCAL_ROLES = ("asr", "diarization", "embeddings")
ADAPTERS = {family: adapter for adapter, family in FAMILIES.items()}


def ensure(
    w: Workspace, collection: str, match: dict[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    """Create the row identified by `match`, or bring an existing one up to `data`."""
    for row in w.list(collection):
        if all(row.get(key) == value for key, value in match.items()):
            if all(row.get(key) == value for key, value in data.items()):
                return row
            return w.put(collection, row["id"], data, expected_revision=row["revision"])
    return w.put(collection, new_id(), {**match, **data})


def provision_local(
    w: Workspace, entry: dict[str, Any], runtime_id: str, gpu: bool
) -> dict[str, Any] | None:
    """Connection, profile and global task policy (switched to this model) for an installed local model."""
    role = entry.get("role")
    adapter = ADAPTERS.get(entry.get("runtime", {}).get("family", ""))
    if role not in LOCAL_ROLES or adapter is None:
        return None
    with w.transaction():
        connection = ensure(
            w,
            "provider_connections",
            {"adapter_id": adapter, "endpoint": "local://" + adapter},
            {"display_name": adapter, "auth_kind": "none", "declared_execution": "local"},
        )
        profile = ensure(
            w,
            "model_profiles",
            {"connection_id": connection["id"], "role": role, "model_id": entry["id"]},
            {
                "options": {
                    "catalog_id": entry["id"],
                    "revision": entry["revision"],
                    "runtime_id": runtime_id,
                    **({"device": "cuda"} if gpu else {}),
                }
            },
        )
        policy = ensure(
            w,
            "task_policies",
            {"task": role, "project_id": None},
            {"model_profile_id": profile["id"]},
        )
        policy_id = policy["id"]
    return {"connection_id": connection["id"], "profile_id": profile["id"], "policy_id": policy_id}


def current_choice(w: Workspace, tasks: tuple[str, ...]) -> dict[str, Any] | None:
    """The profile a task is routed to, as {profile_id, model_id, label, gpu, adapter}."""
    for task in tasks:
        route = task_policy(w, task)
        policy = (
            w.get("task_policies", route["policy_id"]) if route and "policy_id" in route else None
        )
        profile = (
            w.get(
                "model_profiles", policy.get("model_profile_id") or policy.get("profile_id") or ""
            )
            if policy
            else None
        )
        connection = w.get("provider_connections", profile["connection_id"]) if profile else None
        if profile and connection:
            return {
                "profile_id": profile["id"],
                "model_id": profile["model_id"],
                "label": profile["model_id"],
                "gpu": profile.get("options", {}).get("device") == "cuda",
                "adapter": connection["adapter_id"],
            }
    return None


async def setup_status(w: Workspace, service: InferenceService) -> dict[str, Any]:
    ollama: dict[str, Any] = {"available": False, "models": []}
    try:
        response = await service.registry.client.get(OLLAMA + "/api/tags", timeout=1.0)
        response.raise_for_status()
        ollama = {"available": True, "models": [m["name"] for m in response.json()["models"]]}
    except Exception:  # noqa: BLE001 - an absent or odd Ollama only means "not available"
        pass
    asr = current_choice(w, ("asr",))
    return {
        "asr_ready": task_policy(w, "asr") is not None,
        "llm_ready": task_policy(w, "summary") is not None,
        "ollama": ollama,
        "asr": asr and {**asr, "language_selectable": FAMILIES.get(asr["adapter"]) == "whisper"},
        "diarization": current_choice(w, ("diarization",)),
        "llm": current_choice(w, ("summary", "general")),
    }


async def setup_models(
    w: Workspace, service: InferenceService, p: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """List the models a provider offers: [{id, name?, context_length?}]. Unsupported adapters list none."""
    adapter = p["adapter"]
    if adapter != "ollama" and adapter not in BEARER_LISTING:
        return {"items": []}
    base = (
        FIXED_ENDPOINTS["openrouter"]
        if adapter == "openrouter"
        else p.get("endpoint") or FIXED_ENDPOINTS.get(adapter)
    )
    if not base:
        raise ProviderError("invalid_input", "This provider needs an endpoint")
    headers: dict[str, str] = {}
    if adapter != "ollama":
        if not p.get("secret_ref"):
            raise ProviderError("invalid_input", "Store an API key to list models")
        headers["Authorization"] = f"Bearer {await w.resolve_secret(p['secret_ref'])}"
    url = base.rstrip("/") + ("/api/tags" if adapter == "ollama" else "/models")
    try:
        response = await service.registry.client.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        body: dict[str, Any] = response.json()
    except httpx.HTTPStatusError as exc:
        code = "invalid_input" if exc.response.status_code in (401, 403) else "unavailable"
        raise ProviderError(
            code, f"Provider refused the model list ({exc.response.status_code})"
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ProviderError("unavailable", "Could not reach the provider to list models") from exc
    rows: list[dict[str, Any]] = body.get("models" if adapter == "ollama" else "data") or []
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
            continue
        model_id = row.get("name" if adapter == "ollama" else "id")
        if not isinstance(model_id, str) or not model_id:
            continue
        item: dict[str, Any] = {"id": model_id}
        if isinstance(row.get("name"), str) and adapter != "ollama":
            item["name"] = row["name"]
        if isinstance(row.get("context_length"), int):
            item["context_length"] = row["context_length"]
        items.append(item)
    return {"items": items}


def setup_llm(w: Workspace, p: dict[str, Any]) -> dict[str, Any]:
    adapter = p["adapter"]
    if not any(d["id"] == adapter and "llm" in d["roles"] for d in DESCRIPTORS):
        raise ProviderError("invalid_input", "Choose a supported AI provider")
    endpoint = FIXED_ENDPOINTS.get(adapter) if adapter == "openrouter" else None
    endpoint = endpoint or p.get("endpoint") or FIXED_ENDPOINTS.get(adapter)
    if not endpoint:
        raise ProviderError("invalid_input", "This provider needs an endpoint")
    loopback = urlparse(endpoint).hostname in ("127.0.0.1", "localhost", "::1")
    ollama_local = adapter == "ollama" and loopback
    with w.transaction():
        connection = ensure(
            w,
            "provider_connections",
            {"adapter_id": adapter, "endpoint": endpoint},
            {
                "display_name": adapter,
                "auth_kind": "bearer" if p.get("secret_ref") else "none",
                "secret_ref": p.get("secret_ref"),
                "declared_execution": "local" if ollama_local else "remote",
            },
        )
        profile = ensure(
            w,
            "model_profiles",
            {"connection_id": connection["id"], "role": "llm", "model_id": p["model"]},
            {
                "options": {"execution_confirmed": True} if ollama_local else {},
                "context_window": p.get("context_window", 8192),
            },
        )
        ensure(
            w,
            "task_policies",
            {"task": "general", "project_id": None},
            {"model_profile_id": profile["id"]},
        )
        destination = connection_value(connection)["destination"]
        local = (
            resolve_capabilities(
                Connection.model_validate(connection_value(connection)),
                ModelProfile.model_validate(profile_value(profile)),
            ).execution
            == "local"
        )
        route = task_policy(w, "general")
        if not local and route:
            classes, scope = route["data_classes"], route["scope"]
            if not any(
                c["destination_fingerprint"] == destination
                and c.get("revoked_at") is None
                and c["scope"] == scope
                and set(classes) <= set(c["data_classes"])
                for c in w.list("consents")
            ):
                w.put(
                    "consents",
                    new_id(),
                    {
                        "purpose": "summaries",
                        "destination_fingerprint": destination,
                        "data_classes": classes,
                        "scope": scope,
                        "accepted_at": now_ms(),
                    },
                )
    return {"connection_id": connection["id"], "profile_id": profile["id"]}
