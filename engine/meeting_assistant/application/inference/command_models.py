"""Public inference-command schemas; runtime SDK objects never cross this boundary."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from ...infrastructure.providers.contracts import Connection, EmbeddingBatch, ModelProfile, Value
from .service import TaskPolicy


class Empty(Value):
    pass


class PolicyReference(Value):
    policy_id: str = Field(min_length=1, max_length=200)
    scope: str | None = Field(default=None, max_length=500)
    data_classes: list[str] | None = Field(default=None, max_length=30)


class Selection(Value):
    connection: Connection | None = None
    connection_id: str | None = Field(default=None, min_length=1, max_length=200)
    profile: ModelProfile | None = None
    profile_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def selected(self) -> Self:
        if (self.connection is None and not self.connection_id) or (
            self.profile is None and not self.profile_id
        ):
            raise ValueError("Select both a connection and a profile")
        return self


class Test(Selection):
    scope: str = Field(default="connection_test", min_length=1, max_length=500)


class Models(Value):
    connection: Connection | None = None
    connection_id: str | None = Field(default=None, min_length=1, max_length=200)
    scope: str = Field(default="connection_test", min_length=1, max_length=500)

    @model_validator(mode="after")
    def selected(self) -> Self:
        if self.connection is None and not self.connection_id:
            raise ValueError("Select an existing connection")
        return self


class Generate(Value):
    task: Literal[
        "summary",
        "actions",
        "topics",
        "artifacts",
        "cues",
        "briefings",
        "minutes",
        "digest",
        "memory",
        "ask",
    ]
    evidence: list[dict[str, Any]] = Field(default_factory=list[dict[str, Any]], max_length=100000)
    policy: TaskPolicy | PolicyReference
    language: str = Field(default="auto", min_length=1, max_length=50)
    context: dict[str, Any] | None = None
    cancellation_token: str | None = Field(default=None, min_length=1, max_length=200)


class Embed(Value):
    batch: EmbeddingBatch
    policy: TaskPolicy | PolicyReference


class Cancel(Value):
    cancellation_token: str = Field(min_length=1, max_length=200)


class Download(Value):
    id: str = Field(min_length=1, max_length=200)
    accepted_license: bool = Field(strict=True)
    accepted_destination: bool = Field(strict=True)
    accepted_terms: bool = Field(default=False, strict=True)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=200)


class ImportModel(Value):
    template_id: str = Field(min_length=1, max_length=200)
    grant_id: str = Field(min_length=1, max_length=200)
    accepted_unreviewed: bool = Field(strict=True)
    accepted_license: bool = Field(strict=True)


class Remove(Value):
    id: str = Field(min_length=1, max_length=200)
    confirmed: bool = Field(strict=True)
    revision: str | None = Field(default=None, min_length=1, max_length=200)


class Prepare(Value):
    policy: TaskPolicy | PolicyReference


class SetupLlm(Value):
    adapter: str = Field(min_length=1, max_length=100)
    endpoint: str | None = Field(default=None, min_length=1, max_length=2000)
    secret_ref: str | None = Field(default=None, min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    context_window: int | None = Field(default=None, gt=0)


COMMAND_MODELS: dict[str, type[Value]] = {
    "providers.descriptors": Empty,
    "providers.capabilities": Selection,
    "providers.test": Test,
    "providers.models": Models,
    "inference.generate": Generate,
    "embeddings.generate": Embed,
    "inference.cancel": Cancel,
    "catalog.list": Empty,
    "catalog.download": Download,
    "catalog.import_model": ImportModel,
    "catalog.delete": Remove,
    "runtime.prepare": Prepare,
    "setup.status": Empty,
    "setup.llm": SetupLlm,
}


def command_schemas() -> dict[str, dict[str, Any]]:
    return {operation: model.model_json_schema() for operation, model in COMMAND_MODELS.items()}
