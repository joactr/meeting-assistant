from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, TypedDict

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from urllib.parse import urlsplit

import httpx

from .common import (
    all_items,
    consent,
    existing,
    fail,
    fingerprint,
    native,
    privacy_guard,
    required,
    safe_https,
    save,
    vault_reference,
)


def recipient(value: object) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n"):
        fail("invalid_recipient", "Recipients cannot contain control characters")
    _, address = parseaddr(value)
    if not address or "@" not in address or address != value.strip():
        fail("invalid_recipient", "Use explicit email addresses without display-name syntax")
    return address


class MeetingSources(TypedDict):
    meeting: dict[str, Any]
    minutes: dict[str, Any] | None
    actions: list[dict[str, Any]]
    moments: list[dict[str, Any]]


class DeliveryService:
    def __init__(self, workspace: Workspace, client: httpx.AsyncClient | None = None) -> None:
        self.workspace = workspace
        self.client = client

    def _meeting_sources(self, meeting_id: str) -> MeetingSources:
        meeting = existing(self.workspace, "meetings", meeting_id)
        return {
            "meeting": meeting,
            "minutes": self.workspace.get("minutes_approved", meeting_id),
            "actions": all_items(self.workspace, "actions", {"meeting_id": meeting_id}),
            "moments": [
                item
                for item in all_items(self.workspace, "moments", {"meeting_id": meeting_id})
                if item.get("include_in_minutes")
            ],
        }

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = required(payload, "meeting_id")
        sources = self._meeting_sources(meeting_id)
        meeting = sources["meeting"]
        lines = ["# " + meeting["title"], ""]
        for section in (sources["minutes"] or {}).get("sections", []):
            if section.get("important_moment_id"):
                continue
            lines.extend(["## " + section.get("title", "Minutes"), "", section.get("text", ""), ""])
            if section.get("human_note"):
                lines.extend(["Human note: " + section["human_note"], ""])
        lines.extend(["## Actions", ""])
        lines.extend(
            "- [" + item.get("status", "open") + "] " + item["description"]
            for item in sources["actions"]
        )
        lines.extend(["", "## Important moments", ""])
        for item in sources["moments"]:
            lines.append("> " + item.get("quote_snapshot", ""))
            if item.get("note"):
                lines.append("Human note: " + item["note"])
        content = {
            "title": meeting["title"],
            "markdown": "\n".join(lines),
            "includes_transcript": False,
            "includes_audio": False,
        }
        identity = self.workspace.new_id()
        item = save(
            self.workspace,
            "delivery_previews",
            identity,
            {
                "id": identity,
                "meeting_id": meeting_id,
                "project_id": meeting.get("project_id"),
                "content": content,
                "source_fingerprint": fingerprint(sources),
                "source_revisions": {
                    "meeting": meeting["revision"],
                    "minutes": (sources["minutes"] or {}).get("revision"),
                },
                "preview_hash": fingerprint(content),
            },
        )
        return {"item": item}

    def list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [
                {key: value for key, value in item.items() if key != "secret_ref"}
                for item in all_items(self.workspace, "delivery_settings")
            ]
        }

    async def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not payload.get("reviewed"):
            fail(
                "review_required",
                "Confirm sending a small sample to the fixed reviewed destination",
            )
        settings = existing(self.workspace, "delivery_settings", required(payload, "id"))
        identity = fingerprint(
            ["sample", settings["id"], payload.get("operation_id") or settings["revision"]]
        )
        preview = self.workspace.get("delivery_previews", identity)
        if preview is None:
            content = {
                "title": "Meeting Assistant connection test",
                "markdown": "This is an explicitly requested Meeting Assistant delivery test. No meeting notes, transcript, audio or attendee data is included.",
            }
            preview = save(
                self.workspace,
                "delivery_previews",
                identity,
                {
                    "id": identity,
                    "meeting_id": settings.get("meeting_id"),
                    "project_id": settings.get("project_id"),
                    "sample": True,
                    "content": content,
                    "preview_hash": fingerprint(content),
                },
            )
        return await self.send(
            {
                "preview_id": identity,
                "delivery_id": settings["id"],
                "preview_hash": preview["preview_hash"],
                "reviewed": True,
                "authorize_duplicate_risk": bool(payload.get("authorize_duplicate_risk", False)),
            }
        )

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        transport = required(payload, "transport")
        if transport not in ("slack", "smtp", "local"):
            fail("invalid_delivery", "Choose local inbox, Slack webhook or SMTP")
        identity = payload.get("id") or self.workspace.new_id()
        previous = self.workspace.get("delivery_settings", identity)
        reference = payload.get("secret_ref") or (previous or {}).get("secret_ref")
        recipients = [recipient(item) for item in payload.get("recipients", [])]
        destination = payload.get("destination", "local inbox")
        if transport == "smtp":
            if not recipients:
                fail("invalid_recipient", "SMTP requires a fixed reviewed recipient list")
            if (
                not isinstance(destination, str)
                or not destination
                or any(char in destination for char in "\r\n/@")
            ):
                fail("invalid_destination", "SMTP destination must be the reviewed server hostname")
        if transport == "slack":
            if not isinstance(destination, str) or not destination.strip():
                fail(
                    "invalid_destination",
                    "Describe the reviewed Slack workspace/channel destination",
                )
        if not payload.get("project_id") and not payload.get("meeting_id"):
            fail("delivery_scope_required", "Select an explicit project or meeting for delivery")
        if payload.get("project_id"):
            existing(self.workspace, "projects", payload["project_id"])
        if payload.get("meeting_id"):
            existing(self.workspace, "meetings", payload["meeting_id"])
        data = {
            "id": identity,
            "transport": transport,
            "destination": destination,
            "recipients": recipients,
            "secret_ref": vault_reference(reference) if transport != "local" else None,
            "project_id": payload.get("project_id"),
            "meeting_id": payload.get("meeting_id"),
            "automatic": bool(payload.get("automatic", False)),
            "enabled": bool(payload.get("enabled", True)),
            "preview_hash": payload.get("preview_hash"),
        }
        binding_keys = (
            "transport",
            "destination",
            "recipients",
            "secret_ref",
            "project_id",
            "meeting_id",
        )
        if previous and any(previous.get(key) != data.get(key) for key in binding_keys):
            fail(
                "delivery_binding_changed",
                "Create a new reviewed destination when changing its transport, address, recipients, credential reference or project/meeting scope",
            )
        if data["automatic"]:
            if data.get("meeting_id") or not data.get("project_id"):
                fail(
                    "automatic_scope_invalid",
                    "Automatic delivery is available only for explicitly scheduled project digests",
                )
            preview = existing(self.workspace, "digest_runs", required(payload, "preview_id"))
            if (
                preview.get("project_id") != data["project_id"]
                or payload.get("preview_hash") != fingerprint(preview.get("content"))
                or not payload.get("reviewed")
            ):
                fail(
                    "delivery_preview_required",
                    "Review an exact digest preview for this project before authorizing automatic delivery",
                )
            data["consent_fingerprint"] = fingerprint(
                {
                    key: data[key]
                    for key in (
                        "transport",
                        "destination",
                        "recipients",
                        "project_id",
                        "secret_ref",
                    )
                }
            )
        item = self.workspace.put(
            "delivery_settings", identity, data, expected_revision=payload.get("expected_revision")
        )
        return {"item": {key: value for key, value in item.items() if key != "secret_ref"}}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_delivery = bool(payload.get("preview_id"))
        run = (
            existing(self.workspace, "delivery_previews", payload["preview_id"])
            if meeting_delivery
            else existing(self.workspace, "digest_runs", required(payload, "run_id"))
        )
        settings = existing(self.workspace, "delivery_settings", required(payload, "delivery_id"))
        scope_matches = (
            settings.get("meeting_id") == run.get("meeting_id")
            if settings.get("meeting_id")
            else bool(settings.get("project_id"))
            and settings.get("project_id") == run.get("project_id")
        )
        if not settings.get("enabled") or not scope_matches:
            fail(
                "delivery_scope_mismatch",
                "Delivery is disabled or outside the fixed authorized scope",
            )
        if meeting_delivery:
            if payload.get("preview_hash") != run["preview_hash"] or not payload.get("reviewed"):
                fail(
                    "review_required", "Review the exact persisted minutes preview before delivery"
                )
            if (
                not run.get("sample")
                and fingerprint(self._meeting_sources(run["meeting_id"]))
                != run["source_fingerprint"]
            ):
                fail("stale_preview", "Meeting edits changed the reviewed delivery preview")
        identity = fingerprint([run["id"], settings["id"]])
        operation = self.workspace.get("delivery_operations", identity)
        if operation and operation.get("state") == "delivered":
            return {"item": operation}
        if (
            operation
            and operation.get("state") in ("sending", "delivery-unknown")
            and not payload.get("authorize_duplicate_risk")
        ):
            fail(
                "delivery_unknown",
                "Delivery may already have occurred; explicit duplicate-risk authorization is required before retry",
            )
        if not settings.get("automatic") and not payload.get("reviewed"):
            fail("review_required", "Review the digest and fixed destination before sending")
        text = run["content"]["markdown"]
        if settings["transport"] == "local":
            return {
                "item": save(
                    self.workspace,
                    "delivery_operations",
                    identity,
                    {
                        "id": identity,
                        "run_id": run["id"],
                        "state": "delivered",
                        "transport": "local",
                    },
                    operation,
                )
            }
        privacy_guard(self.workspace)
        delivery_scope = {
            "project_id": settings.get("project_id"),
            "recipients": settings["recipients"],
            "delivery_id": settings["id"],
        }
        if meeting_delivery and run.get("meeting_id"):
            delivery_scope["meeting_id"] = run["meeting_id"]
        data_classes = (
            ["delivery_test"]
            if run.get("sample")
            else ["minutes" if meeting_delivery else "digest", "meeting_metadata"]
        )
        consent(self.workspace, settings["destination"], data_classes, delivery_scope)
        operation = save(
            self.workspace,
            "delivery_operations",
            identity,
            {
                "id": identity,
                "run_id": run["id"],
                "delivery_id": settings["id"],
                "transport": settings["transport"],
                "destination": settings["destination"],
                "recipients": settings["recipients"],
                "request_hash": fingerprint(
                    [settings["destination"], settings["recipients"], text]
                ),
                "state": "sending",
                "attempted_at_ms": self.workspace.now_ms(),
            },
            operation,
        )
        try:
            secret = (
                await native(
                    self.workspace, "vault.resolve", {"secret_ref": settings["secret_ref"]}
                )
            )["value"]
            current_settings = existing(self.workspace, "delivery_settings", settings["id"])
            if (
                not current_settings.get("enabled")
                or current_settings["revision"] != settings["revision"]
            ):
                fail(
                    "delivery_configuration_changed",
                    "The fixed delivery configuration changed before sending",
                )
            privacy_guard(self.workspace)
            consent(self.workspace, settings["destination"], data_classes, delivery_scope)
            if settings["transport"] == "slack":
                acknowledgement = await self._slack(secret, text)
            else:
                credential = json.loads(secret)
                acknowledgement = await asyncio.to_thread(
                    self._smtp, settings, credential, run["content"]["title"], text, identity
                )
        except (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            TimeoutError,
            smtplib.SMTPServerDisconnected,
        ):
            item = save(
                self.workspace,
                "delivery_operations",
                identity,
                {
                    **operation,
                    "state": "delivery-unknown",
                    "uncertainty": "The remote server may have accepted this message. No automatic resend will occur.",
                },
                operation,
            )
            return {"item": item}
        except Exception as error:
            details: dict[str, Any] = getattr(error, "details", None) or {}
            uncertain = bool(details.get("uncertain", False))
            save(
                self.workspace,
                "delivery_operations",
                identity,
                {
                    **operation,
                    "state": "delivery-unknown" if uncertain else "failed",
                    "error_code": getattr(error, "code", "delivery_failed"),
                },
                operation,
            )
            raise
        item = save(
            self.workspace,
            "delivery_operations",
            identity,
            {
                **operation,
                "state": "delivered",
                "acknowledgement": acknowledgement,
                "delivered_at_ms": self.workspace.now_ms(),
            },
            operation,
        )
        return {"item": item}

    async def _slack(self, webhook: str, text: str) -> dict[str, Any]:
        safe_https(webhook, hosts={"hooks.slack.com", "hooks.slack-gov.com"})
        if not urlsplit(webhook).path.startswith("/services/"):
            fail("invalid_webhook", "Use an actual Slack incoming-webhook URL")
        if self.client:
            response = await self.client.post(
                webhook, json={"text": text}, follow_redirects=False, timeout=30
            )
        else:
            async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
                response = await client.post(webhook, json={"text": text})
        if response.status_code != 200 or response.text.strip() != "ok":
            fail(
                "slack_not_acknowledged",
                "Slack did not acknowledge delivery",
                http_status=response.status_code,
                uncertain=response.status_code >= 500,
            )
        return {"http_status": 200, "provider": "slack", "response": "ok"}

    @staticmethod
    def _smtp(
        settings: dict[str, Any], credential: dict[str, Any], subject: str, body: str, identity: str
    ) -> dict[str, Any]:
        host = required(credential, "host")
        if host != settings["destination"]:
            fail(
                "delivery_destination_changed",
                "Vault SMTP hostname differs from the reviewed destination",
            )
        mode = credential.get("mode", "starttls")
        if mode not in ("starttls", "implicit_tls"):
            fail("smtp_tls_required", "SMTP requires verified STARTTLS or implicit TLS")
        sender = recipient(required(credential, "from"))
        if any(char in subject for char in "\r\n"):
            fail("invalid_subject", "Email subject cannot contain newlines")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = ", ".join(settings["recipients"])
        message["Subject"] = subject
        message["Message-ID"] = f"<{identity}@meeting-assistant.local>"
        message.set_content(body)
        context = ssl.create_default_context()
        port = int(credential.get("port", 465 if mode == "implicit_tls" else 587))
        if not 1 <= port <= 65535:
            fail("invalid_smtp_port", "SMTP port is invalid")
        connection = (
            smtplib.SMTP_SSL(host, port, timeout=30, context=context)
            if mode == "implicit_tls"
            else smtplib.SMTP(host, port, timeout=30)
        )
        try:
            connection.ehlo()
            if mode == "starttls":
                connection.starttls(context=context)
                connection.ehlo()
            if credential.get("username"):
                connection.login(credential["username"], required(credential, "password"))
            refused = connection.send_message(message)
            if refused:
                fail(
                    "smtp_partial_delivery",
                    "SMTP refused some recipients; other recipients may have received the message",
                    refused_recipient_count=len(refused),
                    uncertain=True,
                )
            return {
                "provider": "smtp",
                "accepted_recipient_count": len(settings["recipients"]),
                "message_id": message["Message-ID"],
            }
        finally:
            try:
                connection.quit()
            except (OSError, smtplib.SMTPException):
                connection.close()

    def reconcile(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = existing(self.workspace, "delivery_operations", required(payload, "id"))
        confirmation = payload.get("confirmation")
        if confirmation not in ("confirmed_received", "confirmed_not_received"):
            return {
                "item": operation,
                "reconciliation_available": False,
                "message": "Slack webhooks and SMTP provide no universal delivery lookup. Check the fixed destination, then record an explicit confirmation or authorize duplicate risk when retrying.",
            }
        if not payload.get("reviewed"):
            fail(
                "review_required",
                "Recording an external receipt confirmation requires explicit review",
            )
        state = "delivered" if confirmation == "confirmed_received" else "failed"
        return {
            "item": save(
                self.workspace,
                "delivery_operations",
                operation["id"],
                {
                    **operation,
                    "state": state,
                    "user_confirmation": confirmation,
                    "confirmed_at_ms": self.workspace.now_ms(),
                },
                operation,
            )
        }
