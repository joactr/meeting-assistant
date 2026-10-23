from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.ports import CommandHandler, CommandRegistry, HandlerResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .calendar import CalendarService
from .delivery import DeliveryService
from .digest import DigestService
from .issues import IssueService
from .network import ProviderHTTP
from .notes import NotesService
from .vocabulary import VocabularyService


class CommandPayload(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    expected_revision: int | None = None
    profile_id: str | None = None
    reviewed: bool = False
    authorize_duplicate_risk: bool = False
    automatic: bool = False
    enabled: bool = False
    apply: bool = False
    create_distinct: bool = False
    reminders_enabled: bool = False
    retry: bool = False
    read: bool = False
    priority: int = 0
    weekday: int = Field(default=4, ge=0, le=6)
    reminder_minutes: int = Field(default=5, ge=0, le=1440)
    aliases: list[str] = Field(default_factory=list)
    calendar_ids: list[str] = Field(default_factory=list)
    recipients: list[str] = Field(default_factory=list)
    repositories: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    assignees: list[str] = Field(default_factory=list)
    project_mapping: dict[str, str] = Field(default_factory=dict)


FIELD_TYPES: dict[str, Any] = {
    "id": str,
    "meeting_id": str,
    "project_id": str,
    "connection_id": str,
    "action_id": str,
    "provider": str,
    "client_id": str,
    "secret_ref": str,
    "repository": str,
    "request_hash": str,
    "expected_revision": int,
    "note_revision": int,
    "transcript_revision": int,
    "flow_id": str,
    "template_id": str,
    "run_id": str,
    "delivery_id": str,
    "transport": str,
    "name": str,
    "spelling": str,
    "timezone": str,
    "start": str,
    "end": str,
    "title": str,
    "blocks": list[dict[str, Any]],
    "sections": list[dict[str, Any]],
    "capabilities": dict[str, Any],
    "calendars": list[str],
}

# Required payload fields form the generated public command schema. Unknown template
# fields remain inert JSON data rather than an executable extension mechanism.
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "connected.capabilities": (),
    "vocabulary.list": (),
    "vocabulary.save": ("spelling",),
    "vocabulary.delete": ("id", "expected_revision"),
    "vocabulary.compile": (),
    "calendar.connection.authorize": ("provider", "client_id"),
    "calendar.connection.save": ("provider",),
    "calendar.connection.list": (),
    "calendar.connection.disconnect": ("id", "expected_revision"),
    "calendar.calendars": ("connection_id",),
    "calendar.sync": ("connection_id",),
    "calendar.events": (),
    "calendar.manual.save": ("title", "start", "end"),
    "calendar.select": ("id",),
    "calendar.reminders": (),
    "github.auth.begin": ("client_id",),
    "github.auth.poll": ("flow_id",),
    "github.auth.cancel": ("flow_id",),
    "github.connection.save": ("secret_ref",),
    "github.connection.list": (),
    "github.repositories": ("connection_id",),
    "github.issue.prepare": ("connection_id", "action_id", "repository"),
    "github.issue.send": ("id", "request_hash"),
    "github.issue.reconcile": ("id",),
    "github.issue.refresh": ("id",),
    "github.connection.disconnect": ("id", "expected_revision"),
    "notes.get": ("meeting_id",),
    "notes.save": ("meeting_id", "blocks"),
    "templates.list": (),
    "templates.save": ("name", "sections"),
    "templates.clone": ("id",),
    "templates.select": ("meeting_id", "template_id", "expected_revision"),
    "minutes.get": ("meeting_id",),
    "minutes.prepare": ("meeting_id", "note_revision"),
    "minutes.approve": ("id",),
    "minutes.edit": ("meeting_id", "sections", "expected_revision"),
    "digest.schedule.get": ("project_id",),
    "digest.schedule.save": ("project_id", "timezone"),
    "digest.preview": ("project_id",),
    "digest.run": ("project_id",),
    "digest.history": (),
    "digest.inbox": (),
    "digest.delivery.configure": ("transport", "project_id"),
    "digest.delivery.send": ("run_id", "delivery_id"),
    "digest.delivery.reconcile": ("id",),
    "delivery.list": (),
    "delivery.configure": ("transport",),
    "delivery.preview": ("meeting_id",),
    "delivery.send": ("delivery_id",),
    "delivery.reconcile": ("id",),
    "delivery.test": ("id",),
}


def command_model(operation: str, fields: tuple[str, ...]) -> type[CommandPayload]:
    definitions: dict[str, Any] = {field: (FIELD_TYPES[field], Field(...)) for field in fields}
    return create_model(
        "".join(part.title() for part in operation.split(".")) + "Payload",
        __base__=CommandPayload,
        **definitions,
    )


COMMAND_MODELS: dict[str, type[CommandPayload]] = {
    operation: command_model(operation, fields) for operation, fields in REQUIRED_FIELDS.items()
}


def command_schemas() -> dict[str, dict[str, Any]]:
    return {operation: model.model_json_schema() for operation, model in COMMAND_MODELS.items()}


class ConnectedWorkflows:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        http = ProviderHTTP(workspace)
        self.vocabulary = VocabularyService(workspace)
        self.calendar = CalendarService(workspace, http)
        self.issues = IssueService(workspace, http)
        self.notes = NotesService(workspace)
        self.notes.seed_templates()
        self.delivery = DeliveryService(workspace)
        self.digest = DigestService(workspace, self.delivery)
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def invalidate(self, meeting_id: str) -> None:
        self.notes.invalidate(meeting_id)
        self.digest.invalidate(meeting_id)

    def capabilities(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [
                {
                    "id": "G02",
                    "enabled": True,
                    "modes": [
                        "prompt_hint",
                        "decoder_bias",
                        "keyterms",
                        "phrase_list",
                        "custom_model",
                        "none",
                    ],
                    "requires_omission_acknowledgement": True,
                    "post_replacement": False,
                },
                {
                    "id": "G03",
                    "enabled": True,
                    "providers": ["google", "microsoft", "manual"],
                    "sync": "bounded_full_window",
                    "past_days": 7,
                    "future_days": 30,
                    "interval_minutes": 15,
                    "requires_public_client_registration": True,
                    "automatic_recording": False,
                },
                {
                    "id": "G04",
                    "enabled": True,
                    "provider": "github",
                    "auth": ["device", "fine_grained_pat"],
                    "requires_public_client_registration": True,
                    "uncertain_submission": "reconcile_before_retry",
                },
                {
                    "id": "G05",
                    "enabled": True,
                    "human_notes_preserved": True,
                    "generation": ["deterministic", "configured_inference"],
                    "provenance": ["Transcript", "Human note", "Both"],
                },
                {
                    "id": "G06",
                    "enabled": True,
                    "execution": "local_running_process",
                    "delivery": ["local", "slack", "smtp"],
                    "catchup": "coalesced",
                    "remote_exactly_once": False,
                },
            ],
            "native_broker_available": self.workspace.native_gateway is not None,
        }

    async def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="connected-workflow-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            for name, service in (("calendar", self.calendar), ("digest", self.digest)):
                try:
                    await service.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.workspace.emit(
                        "connected.scheduler.error",
                        {"workflow": name, "code": getattr(error, "code", "workflow_failed")},
                    )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=30)
            except TimeoutError:
                pass


def register_workflows(workspace: Workspace, registry: CommandRegistry) -> None:
    connected = ConnectedWorkflows(workspace)
    workspace.connected = connected
    handlers: CommandRegistry = {
        "connected.capabilities": connected.capabilities,
        "vocabulary.list": connected.vocabulary.list,
        "vocabulary.save": connected.vocabulary.save,
        "vocabulary.delete": connected.vocabulary.delete,
        "vocabulary.compile": connected.vocabulary.compile,
        "calendar.connection.authorize": connected.calendar.authorize,
        "calendar.connection.save": connected.calendar.connection_save,
        "calendar.connection.list": connected.calendar.connections,
        "calendar.connection.disconnect": connected.calendar.disconnect,
        "calendar.calendars": connected.calendar.calendars,
        "calendar.sync": connected.calendar.sync,
        "calendar.events": connected.calendar.events,
        "calendar.manual.save": connected.calendar.manual_save,
        "calendar.select": connected.calendar.select,
        "calendar.reminders": connected.calendar.reminders,
        "github.auth.begin": connected.issues.auth_begin,
        "github.auth.poll": connected.issues.auth_poll,
        "github.auth.cancel": connected.issues.auth_cancel,
        "github.connection.save": connected.issues.connection_save,
        "github.connection.list": connected.issues.connections,
        "github.repositories": connected.issues.repositories,
        "github.issue.prepare": connected.issues.prepare,
        "github.issue.send": connected.issues.send,
        "github.issue.reconcile": connected.issues.reconcile,
        "github.issue.refresh": connected.issues.refresh,
        "github.connection.disconnect": connected.issues.disconnect,
        "notes.get": connected.notes.get,
        "notes.save": connected.notes.save,
        "templates.list": connected.notes.templates,
        "templates.save": connected.notes.template_save,
        "templates.clone": connected.notes.template_clone,
        "templates.select": connected.notes.template_select,
        "minutes.get": connected.notes.minutes,
        "minutes.prepare": connected.notes.prepare,
        "minutes.approve": connected.notes.approve,
        "minutes.edit": connected.notes.edit,
        "digest.schedule.get": connected.digest.schedule_get,
        "digest.schedule.save": connected.digest.schedule_save,
        "digest.preview": connected.digest.preview,
        "digest.run": connected.digest.run,
        "digest.history": connected.digest.history,
        "digest.inbox": connected.digest.inbox,
        "digest.delivery.configure": connected.delivery.configure,
        "digest.delivery.send": connected.delivery.send,
        "digest.delivery.reconcile": connected.delivery.reconcile,
        "delivery.list": connected.delivery.list,
        "delivery.configure": connected.delivery.configure,
        "delivery.preview": connected.delivery.preview,
        "delivery.send": connected.delivery.send,
        "delivery.reconcile": connected.delivery.reconcile,
        "delivery.test": connected.delivery.test,
    }
    for operation, handler in handlers.items():
        if operation in registry:
            raise RuntimeError(f"Duplicate application operation: {operation}")
        model = COMMAND_MODELS[operation]

        def validated(
            payload: dict[str, Any],
            *,
            _handler: CommandHandler = handler,
            _model: type[CommandPayload] = model,
        ) -> HandlerResult | Awaitable[HandlerResult]:
            try:
                data = _model.model_validate(payload).model_dump(exclude_unset=True)
            except ValidationError as error:
                raise AppError(
                    "invalid_input",
                    "Command payload does not match its schema",
                    status=422,
                    details={
                        "fields": [
                            {"location": list(item["loc"]), "type": item["type"]}
                            for item in error.errors(include_input=False)
                        ]
                    },
                ) from error
            return _handler(data)

        validated.__name__ = operation.replace(".", "_")
        validated.__dict__["__command_model__"] = model
        registry[operation] = validated
