from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn, TypeGuard

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from urllib.parse import urlsplit
from uuid import UUID

from meeting_assistant.domain.errors import AppError


def is_json_list(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def fail(code: str, message: str, **details: Any) -> NoReturn:
    raise AppError(code, message, details=details)


def required(payload: dict[str, Any], key: str) -> Any:
    value = payload.get(key)
    if value is None or value == "":
        fail("invalid_input", f"{key} is required", field=key)
    return value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def instant(value: str | datetime) -> datetime:
    result = (
        datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    )
    if result.tzinfo is None:
        fail("timezone_required", "An explicit UTC offset is required")
    return result.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def all_items(
    workspace: Workspace, collection: str, filters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = workspace.list(collection, filters, limit=200, offset=offset)
        result.extend(page)
        if len(page) < 200:
            return result
        offset += len(page)


def existing(workspace: Workspace, collection: str, identity: str) -> dict[str, Any]:
    item = workspace.get(collection, identity)
    if item is None:
        raise AppError(
            "not_found",
            "The requested record does not exist",
            status=404,
            details={"collection": collection, "id": identity},
        )
    return item


def save(
    workspace: Workspace,
    collection: str,
    identity: str,
    data: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return workspace.put(
        collection, identity, data, expected_revision=previous["revision"] if previous else None
    )


def safe_https(value: object, *, hosts: set[str] | None = None) -> str:
    if not isinstance(value, str) or any(ord(char) < 33 for char in value) or "\\" in value:
        fail("unsafe_url", "External URLs cannot contain whitespace, controls or backslashes")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port in (None, 443)
        hostname = parsed.hostname.encode("idna").decode("ascii") if parsed.hostname else ""
    except (ValueError, UnicodeError):
        fail("unsafe_url", "External URL host or port is malformed")
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not valid_port
    ):
        fail("unsafe_url", "Only credential-free HTTPS URLs on the standard port are accepted")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        if len(hostname) > 253 or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in hostname.rstrip(".").split(".")
        ):
            fail("unsafe_url", "External URL host is malformed")
    if hosts is not None and hostname.lower() not in hosts:
        fail("unsafe_url", "The URL host is not allowed")
    return value


async def native(workspace: Workspace, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    return await workspace.native(operation, payload)


def consent(
    workspace: Workspace, destination: str, data_classes: list[str], scope: dict[str, Any]
) -> None:
    workspace.check_consent(destination, data_classes, scope)


def authorized_connection(
    workspace: Workspace,
    connection: dict[str, Any],
    destination: str,
    data_classes: list[str],
    scope: dict[str, Any],
) -> dict[str, Any]:
    consent(workspace, destination, data_classes, scope)
    return {
        **connection,
        "_request_consent": {
            "destination": destination,
            "data_classes": data_classes,
            "scope": scope,
        },
    }


def privacy_guard(workspace: Workspace) -> None:
    if workspace.offline():
        fail("privacy_mode", "External requests are disabled by privacy mode")


def vault_reference(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64:
        fail("invalid_secret_reference", "Use a native vault reference, never a credential value")
    prefix, separator, identifier = value.partition(":")
    try:
        parsed = UUID(identifier)
    except ValueError:
        fail(
            "invalid_secret_reference",
            "Use an opaque OAuth or credential reference returned by the native host",
        )
    if (
        separator != ":"
        or prefix not in ("oauth", "credential")
        or str(parsed) != identifier.lower()
    ):
        fail(
            "invalid_secret_reference",
            "Use an opaque OAuth or credential reference returned by the native host",
        )
    return value
