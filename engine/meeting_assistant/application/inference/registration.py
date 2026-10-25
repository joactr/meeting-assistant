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
from ...infrastructure.workers.developer import target
from ...infrastructure.workers.models import ModelRuntime
from .catalog import CatalogService
from .command_models import COMMAND_MODELS
from .policies import connection_value, profile_value, resolve_policy
from .service import InferenceService, maybe_await
from .setup import provision_local, setup_llm, setup_models, setup_status

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

    downloads: dict[str, dict[str, Any]] = {}  # catalog id -> in-flight/failed download state
    background: set[asyncio.Task[None]] = set()  # strong refs so tasks aren't collected mid-run

    def installed(entry: dict[str, Any]) -> bool:
        return (
            bool(entry.get("revision"))
            and (catalog.install_root / entry["id"] / entry["revision"]).is_dir()
        )

    def gpu_requirement(entry: dict[str, Any], by_id: dict[str, Any]) -> dict[str, Any] | None:
        # Signed platform runtimes inherit the GPU requirement of their source descriptor.
        source = by_id.get(entry["id"].removesuffix("-" + target()), {})
        return (entry.get("host_requirements") or source.get("host_requirements", {})).get("gpu")

    async def annotated() -> list[dict[str, Any]]:
        """Catalog entries with GPU eligibility and, per model, the runtime it will use."""
        entries = catalog.entries()
        by_id = {entry["id"]: entry for entry in entries}
        gpus: list[dict[str, Any]] | None = None
        for entry in entries:
            requirement = gpu_requirement(entry, by_id)
            if requirement:
                if gpus is None:
                    gpus = await asyncio.to_thread(detect_nvidia)
                entry["host_check"] = {"gpu": evaluate_gpu_requirement(requirement, gpus)}
        runtimes = [
            item
            for item in entries
            if item.get("role") == "runtime"
            and item.get("files")
            and not item.get("blocked_reason")
            and target() in item.get("platforms", [])
            and (gpu_requirement(item, by_id) is None or item["host_check"]["gpu"]["met"])
        ]
        for entry in entries:
            if entry.get("role") == "runtime":
                continue
            family = entry.get("runtime", {}).get("family")
            matching = [
                item for item in runtimes if item.get("runtime", {}).get("family") == family
            ]
            # An eligible GPU runtime wins over the CPU one.
            matching.sort(key=lambda item: "host_check" not in item)
            runtime = matching[0] if matching else None
            entry["runtime_id"] = runtime["id"] if runtime else None
            entry["gpu"] = bool(runtime and "host_check" in runtime)
            entry["installed"] = installed(entry) and (runtime is None or installed(runtime))
        for entry in entries:
            if entry["id"] in downloads:
                entry["download"] = dict(downloads[entry["id"]])
        return entries

    async def catalog_list(p: dict[str, Any]) -> dict[str, Any]:
        return {"items": await annotated()}

    register("catalog.list", catalog_list)

    async def download(p: dict[str, Any]) -> dict[str, Any]:
        token = await workspace.resolve_secret(p["secret_ref"]) if p.get("secret_ref") else None
        by_id = {entry["id"]: entry for entry in await annotated()}
        entry = by_id[catalog.entry(p["id"])["id"]]
        runtime = by_id.get(entry.get("runtime_id") or "")
        ids = [entry["id"]] + ([runtime["id"]] if runtime else [])
        needed = [by_id[i] for i in ids if not installed(by_id[i])]
        consent: dict[str, Any] = {
            "accepted_license": p.get("accepted_license", False),
            "accepted_destination": p.get("accepted_destination", False),
            "accepted_terms": p.get("accepted_terms", False),
            "token": token,
        }
        for wanted in needed:
            catalog.check(wanted["id"], **consent)
        if not needed:
            return {
                "item": {"id": entry["id"], "revision": entry["revision"], "activated": True},
                "items": [],
                "provisioned": provision_local(workspace, entry, runtime["id"], entry["gpu"])
                if runtime
                else None,
            }
        if any(i in downloads and downloads[i]["state"] != "failed" for i in ids):
            raise ProviderError("invalid_input", "This model is already downloading")
        state: dict[str, Any] = {
            "state": "downloading",
            "done": 0,
            "total": sum(f["bytes"] for wanted in needed for f in wanted["files"]),
            "error": None,
        }
        downloads.update(dict.fromkeys(ids, state))

        async def run() -> None:
            offset = 0
            try:
                for wanted in needed:

                    def progress(done: int, total: int, offset: int = offset) -> None:
                        state["done"] = offset + done
                        if state["done"] >= state["total"]:
                            state["state"] = "extracting"

                    item = await catalog.download(wanted["id"], progress=progress, **consent)
                    offset += sum(f["bytes"] for f in wanted["files"])
                    workspace.emit(
                        "catalog.activated", {"id": item["id"], "revision": item["revision"]}
                    )
                if runtime:
                    provision_local(workspace, entry, runtime["id"], entry["gpu"])
            except (AppError, ProviderError) as error:
                state.update(state="failed", error=error.message)
            else:
                for i in ids:
                    downloads.pop(i, None)

        task = asyncio.create_task(run())
        background.add(task)
        task.add_done_callback(background.discard)
        return {"started": True, "id": entry["id"]}

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

    register("setup.status", lambda p: setup_status(workspace, service))
    register("setup.llm", lambda p: setup_llm(workspace, p))
    register("setup.models", lambda p: setup_models(workspace, service, p))
