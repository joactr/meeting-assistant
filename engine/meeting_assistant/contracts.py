from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model
from pydantic.fields import FieldInfo

from meeting_assistant.domain.ports import CommandRegistry, JSONDocument

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


class CommandPayload(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    recording_enabled: bool | None = None
    recording_notice_acknowledged: bool | None = None
    temporary_decode_consent: bool | None = None
    retain_audio: bool | None = None
    plain_warning_accepted: bool | None = None
    include_media: bool | None = None
    include_transcript: bool | None = None
    confirm: bool | None = None
    file_grant: str | None = None
    grant_id: str | None = None
    passphrase_secret_ref: str | None = None
    policy: dict[str, Any] | None = None
    meeting_ids: list[str] | None = Field(default=None, max_length=200)
    limit: int | None = Field(default=None, ge=1, le=200)
    cursor: str | None = None


class ApplicationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation_id: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any]


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    retryable: bool
    details: Any = None
    trace_id: str


class EventEnvelope(BaseModel):
    protocol_version: Literal[1] = 1
    seq: int = Field(ge=1)
    event_id: str
    type: str
    meeting_id: str | None = None
    entity_id: str | None = None
    revision: int | None = None
    occurred_at_ms: int
    data: dict[str, Any]


class Resource(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    revision: int = Field(ge=1)
    created_at: int
    updated_at: int


class ResourcePage(BaseModel):
    items: list[Resource]
    next_cursor: str | None = None


class ReadyFrame(BaseModel):
    protocol_major: Literal[1] = 1
    build_id: str
    pid: int
    http_port: int = Field(ge=1, le=65535)
    audio_port: int = Field(ge=1, le=65535)
    capabilities: dict[str, Any]


FIELD_TYPES: dict[str, type[object]] = {
    "id": str,
    "meeting_id": str,
    "run_id": str,
    "utterance_id": str,
    "speaker_id": str,
    "source_id": str,
    "target_id": str,
    "title": str,
    "text": str,
    "query": str,
    "collection": str,
    "task": str,
    "language": str,
    "expected_revision": int,
    "start_ms": int,
    "end_ms": int,
    "timestamp_ms": int,
    "idempotency_key": str,
    "reservation_id": str,
    "data": dict[str, Any],
    "changes": dict[str, Any],
    "source_index": int,
    "capture_epoch": int,
}
FIELD_TYPES.update(
    {"person_id": str, "meeting_speaker_id": str, "confirm": bool, "source_stopped": bool}
)
CORE_REQUIRED = {
    "meetings.create": ("title",),
    "meetings.get": ("id",),
    "meetings.update": ("id", "expected_revision"),
    "meetings.delete": ("id", "expected_revision"),
    "meetings.start": ("meeting_id", "idempotency_key"),
    "meetings.vocabulary_check": ("meeting_id",),
    "meetings.pause": ("meeting_id", "idempotency_key"),
    "meetings.resume": ("meeting_id", "idempotency_key"),
    "meetings.stop": ("meeting_id", "idempotency_key"),
    "shutdown.cancel": ("reservation_id",),
    "shutdown.commit": ("reservation_id",),
    "transcript.list": ("meeting_id",),
    "transcript.ingest": ("meeting_id", "start_ms", "end_ms", "text"),
    "transcript.edit": ("id", "text", "expected_revision"),
    "transcript.split": ("id", "timestamp_ms", "expected_revision"),
    "transcript.reassign": ("id", "speaker_id", "expected_revision"),
    "transcript.diff": ("run_id",),
    "transcript.promote": ("run_id", "expected_revision"),
    "speakers.list": ("meeting_id",),
    "speakers.rename": ("id", "expected_revision"),
    "speakers.merge": ("source_id", "target_id", "expected_revision"),
    "speakers.split": ("id", "expected_revision"),
    "speakers.analytics": ("meeting_id",),
    "analysis.generate": ("meeting_id",),
    "analysis.list": ("meeting_id",),
    "analysis.status": ("meeting_id",),
    "analysis.read": ("id",),
    "analysis.promote": ("id", "expected_revision"),
    "language.set": ("meeting_id", "language", "expected_revision"),
    "search.query": ("query",),
    "ask.query": ("query",),
    "export.create": ("meeting_id",),
    "entity.list": ("collection",),
    "entity.get": ("collection", "id"),
    "entity.create": ("collection", "data"),
    "entity.update": ("collection", "id", "expected_revision"),
    "entity.delete": ("collection", "id", "expected_revision"),
    "capture.utterance": ("source_index", "capture_epoch", "start_ms", "end_ms"),
    "capture.source_event": ("meeting_id", "source_index"),
    "search.reindex": ("idempotency_key",),
    "briefings.prepare": ("meeting_id",),
    "memory.compress": ("meeting_id", "idempotency_key"),
    "media.import": ("idempotency_key",),
    "media.retranscribe": ("meeting_id", "idempotency_key"),
    "media.delete": ("id", "expected_revision"),
    "voice_profiles.create": ("person_id", "meeting_speaker_id"),
    "voice_profiles.delete": ("id", "expected_revision"),
    "voice_profiles.adopt": ("person_id", "speaker_id", "expected_revision"),
    "legacy.import": ("confirm", "source_stopped"),
    "diagnostics.export": ("confirm",),
    "retention.purge": ("confirm",),
}


def _payload_model(operation: str, fields: tuple[str, ...]) -> type[CommandPayload]:
    field_definitions: dict[str, tuple[type[object], FieldInfo]] = {
        field: (FIELD_TYPES[field], Field(...)) for field in fields
    }
    return create_model(
        "".join(part.title() for part in operation.split(".")) + "Payload",
        __base__=CommandPayload,
        __config__=None,
        __doc__=None,
        __module__=__name__,
        __validators__=None,
        __namespace__=None,
        __cls_kwargs__=None,
        __qualname__=None,
        **field_definitions,
    )


CORE_MODELS: dict[str, type[CommandPayload]] = {
    operation: _payload_model(operation, fields) for operation, fields in CORE_REQUIRED.items()
}


def source_root() -> Path:
    return Path(__file__).resolve().parents[2]


def schema_catalog(workspace: Workspace) -> dict[str, JSONDocument]:
    return {
        operation: (
            cast(type[BaseModel] | None, getattr(handler, "__command_model__", None))
            or CORE_MODELS.get(operation)
            or CommandPayload
        ).model_json_schema()
        for operation, handler in sorted(workspace.registry.items())
    }


def attach_core_models(registry: CommandRegistry) -> None:
    for operation, model in CORE_MODELS.items():
        handler = registry.get(operation)
        if handler is not None and not hasattr(handler, "__command_model__"):
            handler.__dict__["__command_model__"] = model


TS = """// Generated by python -m meeting_assistant.contracts generate.
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };
export interface ApplicationCommand { operation_id: string; payload: Record<string, JsonValue>; }
export interface ErrorEnvelope { code: string; message: string; retryable: boolean; details: JsonValue; trace_id: string; }
export interface EventEnvelope { protocol_version: 1; seq: number; event_id: string; type: string; meeting_id: string | null; entity_id: string | null; revision: number | null; occurred_at_ms: number; data: Record<string, JsonValue>; }
export interface Resource { id: string; revision: number; created_at: number; updated_at: number; [key: string]: JsonValue; }
export interface ResourcePage { items: Resource[]; next_cursor: string | null; }
export interface ReadyFrame { protocol_major: 1; build_id: string; pid: number; http_port: number; audio_port: number; capabilities: Record<string, JsonValue>; }
export type Command = ApplicationCommand;
export type DomainEvent = EventEnvelope;
"""
RUST = """// Generated by python -m meeting_assistant.contracts generate.
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApplicationCommand {
    pub operation_id: String,
    pub payload: Map<String, Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ErrorEnvelope {
    pub code: String,
    pub message: String,
    pub retryable: bool,
    pub details: Value,
    pub trace_id: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EventEnvelope {
    pub protocol_version: u16,
    pub seq: u64,
    pub event_id: String,
    #[serde(rename = "type")]
    pub event_type: String,
    pub meeting_id: Option<String>,
    pub entity_id: Option<String>,
    pub revision: Option<u64>,
    pub occurred_at_ms: u64,
    pub data: Map<String, Value>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReadyFrame {
    pub protocol_major: u16,
    pub build_id: String,
    pub pid: u32,
    pub http_port: u16,
    pub audio_port: u16,
    pub capabilities: Map<String, Value>,
}
"""


def artifacts(workspace: Workspace) -> dict[str, str]:
    from meeting_assistant.transport.http import PRIVATE_OPERATIONS

    catalog = schema_catalog(workspace)
    public = {
        k: v for k, v in catalog.items() if k not in PRIVATE_OPERATIONS and not k.startswith("job.")
    }
    models = {
        model.__name__: model.model_json_schema()
        for model in (
            ApplicationCommand,
            ErrorEnvelope,
            EventEnvelope,
            Resource,
            ResourcePage,
            ReadyFrame,
        )
    }
    schemas: JSONDocument = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "protocol_major": 1,
        "models": models,
        "commands": public,
    }
    paths: JSONDocument = {
        f"/api/v1/commands/{operation}": {
            "post": {
                "operationId": operation,
                "security": [{"bearerAuth": []}],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": schema}},
                },
                "responses": {
                    "200": {"description": "Committed application response"},
                    "202": {"description": "Durable job accepted"},
                    "409": {
                        "description": "State, revision or idempotency conflict",
                        "content": {"application/json": {"schema": models["ErrorEnvelope"]}},
                    },
                },
            }
        }
        for operation, schema in public.items()
    }
    openapi: JSONDocument = {
        "openapi": "3.1.0",
        "info": {"title": "Meeting Assistant application operations", "version": "1"},
        "paths": paths,
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": models,
        },
    }

    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    rust = (
        RUST
        + "\npub const OPERATION_IDS: &[&str] = &[\n"
        + "".join("    " + json.dumps(op) + ",\n" for op in public)
        + "];\n"
    )
    return {
        "commands.json": dump(sorted(public)),
        "schema.json": dump(schemas),
        "openapi.json": dump(openapi),
        "events.schema.json": dump(models["EventEnvelope"]),
        "types.ts": TS,
        "contracts.rs": rust,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["generate", "check"])
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    destination = args.output or source_root() / "engine" / "contracts"
    from meeting_assistant.workspace import Workspace

    with tempfile.TemporaryDirectory(prefix="meeting-contracts-") as temp:
        workspace = Workspace(Path(temp) / "workspace.sqlite3")
        try:
            outputs = artifacts(workspace)
        finally:
            workspace.close()
    differences: list[str] = []
    for name, content in outputs.items():
        target = destination / name
        if args.action == "check":
            if not target.is_file() or target.read_text(encoding="utf-8") != content:
                differences.append(name)
        else:
            destination.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    if differences:
        print("Contract drift: " + ", ".join(differences), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
