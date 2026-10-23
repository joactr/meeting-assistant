"""Translate persisted resource fields once at the application boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...infrastructure.providers.contracts import ProviderError

if TYPE_CHECKING:
    from ...workspace import Workspace


def connection_value(item: dict[str, Any]) -> dict[str, Any]:
    if "adapter" in item:
        return item
    settings = item.get("nonsecret_settings", {})
    return {
        "id": item["id"],
        "adapter": item["adapter_id"],
        "endpoint": item["endpoint"],
        "secret_ref": item.get("secret_ref"),
        "auth_scheme": item.get("auth_kind", "none"),
        "execution": item.get("declared_execution", "unknown"),
        "region": item.get("region"),
        "api_version": settings.get("api_version"),
        "destination": settings.get(
            "destination_fingerprint", item.get("destination_fingerprint", item["endpoint"])
        ),
    }


def profile_value(item: dict[str, Any]) -> dict[str, Any]:
    if "model" in item:
        return item
    options = {**item.get("options", {}), "role": item.get("role", "llm")}
    caps = item.get("resolved_capabilities", {})
    limits = caps.get("limits", {})
    return {
        "id": item["id"],
        "model": item["model_id"],
        "revision": item.get("artifact_fingerprint")
        or options.get("revision")
        or f"user-profile-revision-{item.get('revision', 1)}",
        "options": options,
        "context_tokens": item.get("context_window")
        or options.get("context_tokens")
        or limits.get("context_tokens"),
        "output_tokens": options.get("output_tokens") or limits.get("output_tokens"),
        "dimensions": options.get("dimensions"),
        "tokenizer": options.get("tokenizer"),
    }


def resolve_policy(workspace: Workspace, value: dict[str, Any]) -> dict[str, Any]:
    if value.get("policy_id"):
        stored = workspace.get("task_policies", value["policy_id"])
        if stored is None:
            raise ProviderError("invalid_input", "Selected task policy no longer exists")
        value = {**stored, **{k: v for k, v in value.items() if k != "policy_id"}}
    if value.get("connection") and value.get("profile"):
        return {
            **value,
            "connection": connection_value(value["connection"]),
            "profile": profile_value(value["profile"]),
        }
    profile_id = value.get("model_profile_id", value.get("profile_id"))
    if not profile_id:
        raise ProviderError("invalid_input", "Task policy must select an explicit model profile")
    profile = workspace.get("model_profiles", profile_id)
    if profile is None:
        raise ProviderError("model_missing", "Selected model profile no longer exists")
    connection = workspace.get("provider_connections", profile["connection_id"])
    if connection is None:
        raise ProviderError("model_missing", "Selected provider connection no longer exists")
    fallbacks: list[dict[str, Any]] = []
    for fallback_id in value.get("fallback_ids", []):
        fallback = resolve_policy(
            workspace,
            {
                "model_profile_id": fallback_id,
                "scope": value.get("scope", "workspace"),
                "data_classes": value.get("data_classes", ["meeting_text"]),
            },
        )
        fallbacks.append(
            {key: fallback[key] for key in ("connection", "profile", "scope", "data_classes")}
        )
    return {
        "connection": connection_value(connection),
        "profile": profile_value(profile),
        "scope": value.get("scope", "workspace"),
        "data_classes": value.get("data_classes", ["meeting_text"]),
        "revision": value.get("revision", 1),
        "output_language": value.get("language", value.get("output_language")),
        "fallbacks": fallbacks,
        "budgets": value.get(
            "budgets",
            {
                "deadline_seconds": value["deadline_ms"] / 1000
                if value.get("deadline_ms")
                else None,
                "output_tokens": value.get("token_limit"),
                "max_requests": value.get("max_requests", 2),
                "max_cost": value.get("budget_limit") if value.get("budget_limit", 0) > 0 else None,
                "input_price_per_million": value.get("input_price_per_million"),
                "output_price_per_million": value.get("output_price_per_million"),
            },
        ),
    }
