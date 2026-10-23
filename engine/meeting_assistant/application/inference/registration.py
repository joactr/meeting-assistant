"""Engine command hooks; analysis persistence/promotion stays owned by core."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ...domain.errors import AppError
from ...domain.ports import CommandRegistry
from ...infrastructure.hardware import detect_nvidia, evaluate_gpu_requirement
from ...infrastructure.providers.contracts import Connection, ModelProfile, ProviderError
from ...infrastructure.providers.registry import ProviderRegistry, descriptors, resolve_capabilities
from ...infrastructure.resources import resource_root
from ...infrastructure.workers.models import ModelRuntime
from .catalog import CatalogService
from .command_models import COMMAND_MODELS
from .policies import connection_value, profile_value, resolve_policy
from .service import InferenceService, maybe_await

if TYPE_CHECKING:
    from ...workspace import Workspace


def register_workflows(workspace: Workspace, registry: CommandRegistry) -> None:
    service: InferenceService | None = workspace.inference
    if service is None:
        service = InferenceService(
            ProviderRegistry(),
            workspace.resolve_secret,
            workspace.check_consent,
            policy_resolver=lambda value: resolve_policy(workspace, value),
        )
        workspace.inference = service
    import base64
    import tomllib

    release_file = resource_root() / "release.toml"
    release = (
        tomllib.loads(release_file.read_text(encoding="utf-8")) if release_file.exists() else {}
    )
    key = release.get("updates", {}).get("catalog_public_key")
    public_key = base64.b64decode(key) if key else None
    runtime = ModelRuntime(
        workspace.root / "inference",
        service.registry,
        workspace.resolve_secret,
        public_key=public_key,
    )
    service.model_runtime = runtime
    catalog = CatalogService(
        resource_root() / "engine" / "catalog",
        workspace.root / "inference",
        service.registry.client,
        public_key=public_key,
        loaded_artifacts=runtime.loaded_artifacts,
    )

    def resources(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        connection = payload.get("connection") or workspace.get(
            "provider_connections", payload.get("connection_id", "")
        )
        profile = payload.get("profile") or workspace.get(
            "model_profiles", payload.get("profile_id", "")
        )
        if connection is None or profile is None:
            raise ProviderError("model_missing", "Select an existing connection and model profile")
        return connection_value(connection), profile_value(profile)

    def register(
        name: str,
        handler: Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]],
    ) -> None:
        async def translated(payload: dict[str, Any]) -> dict[str, Any]:
            try:
                value = (
                    COMMAND_MODELS[name]
                    .model_validate(payload)
                    .model_dump(mode="json", exclude_unset=True)
                )
                result = handler(value)
                return await maybe_await(result)
            except ProviderError as exc:
                raise AppError(
                    exc.code,
                    exc.message,
                    status=503
                    if exc.code in {"model_missing", "runtime_crash", "network"}
                    else 403
                    if exc.code in {"permission", "auth"}
                    else 422,
                    retryable=exc.retryable,
                    details={
                        **exc.details,
                        "provider_code": exc.provider_code,
                        "retry_after": exc.retry_after,
                        "provider_trace_id": exc.trace_id,
                    },
                ) from None
            except ValidationError as exc:
                raise AppError(
                    "invalid_input",
                    "Inference command payload is invalid",
                    status=422,
                    details={
                        "fields": [
                            ".".join(str(part) for part in item["loc"]) for item in exc.errors()
                        ]
                    },
                ) from None

        translated.__dict__["__command_model__"] = COMMAND_MODELS[name]
        registry[name] = translated

    register("providers.descriptors", lambda p: {"items": descriptors()})

    def capabilities(p: dict[str, Any]) -> dict[str, Any]:
        connection, profile = resources(p)
        return {
            "item": resolve_capabilities(
                Connection.model_validate(connection), ModelProfile.model_validate(profile)
            ).model_dump(),
            "validated_by_probe": False,
        }

    register("providers.capabilities", capabilities)

    async def test(p: dict[str, Any]) -> dict[str, Any]:
        connection, profile = resources(p)
        result = await service.test_connection(
            connection, profile, scope=p.get("scope", "connection_test")
        )
        stored = workspace.get("model_profiles", profile["id"])
        if stored:
            workspace.put(
                "model_profiles",
                stored["id"],
                {
                    "resolved_capabilities": result["capabilities"],
                    "connection_test_at": workspace.now_ms(),
                },
                expected_revision=stored["revision"],
            )
        return {"item": result}

    register("providers.test", test)

    async def models(p: dict[str, Any]) -> dict[str, Any]:
        supplied = p.get("connection") or workspace.get(
            "provider_connections", p.get("connection_id", "")
        )
        if supplied is None:
            raise ProviderError("invalid_input", "Select an existing connection")
        return {
            "items": await service.discover_models(
                connection_value(supplied), scope=p.get("scope", "connection_test")
            )
        }

    register("providers.models", models)

    async def generate(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": await service.generate(
                p["task"],
                p.get("evidence", []),
                p["policy"],
                language=p.get("language", "auto"),
                context=p.get("context"),
                cancellation_token=p.get("cancellation_token"),
            )
        }

    register("inference.generate", generate)

    async def embeddings(p: dict[str, Any]) -> dict[str, Any]:
        return {"item": await service.embed(p["batch"], p["policy"])}

    register("embeddings.generate", embeddings)
    register(
        "inference.cancel",
        lambda p: {
            "item": {
                "cancelled": service.cancel(p["cancellation_token"]),
                "remote_processing_may_continue": True,
            }
        },
    )

    async def catalog_list(p: dict[str, Any]) -> dict[str, Any]:
        entries = catalog.entries()
        if any("gpu" in entry.get("host_requirements", {}) for entry in entries):
            gpus = await asyncio.to_thread(detect_nvidia)
            for entry in entries:
                requirement = entry.get("host_requirements", {}).get("gpu")
                if requirement:
                    entry["host_check"] = {"gpu": evaluate_gpu_requirement(requirement, gpus)}
        return {"items": entries}

    register("catalog.list", catalog_list)

    async def download(p: dict[str, Any]) -> dict[str, Any]:
        token = await workspace.resolve_secret(p["secret_ref"]) if p.get("secret_ref") else None
        item = await catalog.download(
            p["id"],
            accepted_license=p.get("accepted_license", False),
            accepted_destination=p.get("accepted_destination", False),
            accepted_terms=p.get("accepted_terms", False),
            token=token,
        )
        workspace.emit("catalog.activated", {"id": item["id"], "revision": item["revision"]})
        return {"item": item}

    register("catalog.download", download)

    async def import_model(p: dict[str, Any]) -> dict[str, Any]:
        grant = await workspace.native(
            "file.resolve", {"grant_id": p["grant_id"], "purpose": "import_model"}
        )
        try:
            item = await catalog.import_model(
                p["template_id"],
                Path(grant["path"]),
                accepted_unreviewed=p["accepted_unreviewed"],
                accepted_license=p["accepted_license"],
            )
            workspace.emit(
                "catalog.activated",
                {"id": item["id"], "revision": item["revision"], "trust": item["trust"]},
            )
            return {"item": item}
        finally:
            await workspace.native("file.revoke", {"grant_id": p["grant_id"]})

    register("catalog.import_model", import_model)

    async def remove(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": await catalog.delete(
                p["id"],
                confirmed=p.get("confirmed", False),
                revision=p.get("revision"),
                release_artifact=runtime.release_artifact,
            )
        }

    register("catalog.delete", remove)

    async def prepare(p: dict[str, Any]) -> dict[str, Any]:
        return {"item": await service.prepare_runtime(p["policy"])}

    register("runtime.prepare", prepare)
