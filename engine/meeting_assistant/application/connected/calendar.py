from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import (
    all_items,
    authorized_connection,
    consent,
    existing,
    fail,
    fingerprint,
    instant,
    is_json_list,
    iso,
    native,
    privacy_guard,
    required,
    safe_https,
    save,
    vault_reference,
)
from .network import ProviderHTTP

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events.readonly",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
GRAPH_SCOPES = ["Calendars.ReadBasic", "offline_access"]


def event_time(value: dict[str, Any], fallback_zone: str = "UTC") -> tuple[str, str, bool]:
    raw = value.get("dateTime") or value.get("date")
    if not raw:
        fail("calendar_protocol", "Calendar event has no timestamp")
    zone = value.get("timeZone") or fallback_zone
    all_day = "dateTime" not in value
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone))
        except ZoneInfoNotFoundError:
            fail("calendar_timezone", "Provider supplied an unsupported timezone", timezone=zone)
    return iso(parsed), zone, all_day


def normalize_event(
    provider: str, calendar_id: str, connection_id: str, raw: dict[str, Any], calendar_zone: str
) -> dict[str, Any]:
    if provider == "google":
        raw_start = raw.get("start") or raw.get("originalStartTime")
        start, zone, all_day = (
            event_time(raw_start, calendar_zone) if raw_start else (None, calendar_zone, False)
        )
        end = event_time(raw["end"], zone)[0] if raw.get("end") else start
        attendees = [
            {
                "name": attendee.get("displayName"),
                "email": attendee.get("email"),
                "response": attendee.get("responseStatus"),
            }
            for attendee in raw.get("attendees", [])
        ]
        occurrence = raw.get("originalStartTime")
        url = raw.get("hangoutLink")
        if not url:
            url = next(
                (
                    entry.get("uri")
                    for entry in raw.get("conferenceData", {}).get("entryPoints", [])
                    if entry.get("entryPointType") == "video"
                ),
                None,
            )
        values = {
            "provider_event_id": raw["id"],
            "series_id": raw.get("recurringEventId"),
            "occurrence_id": fingerprint(occurrence) if occurrence else raw["id"],
            "title": raw.get("summary", "Untitled event"),
            "start": start,
            "end": end,
            "original_timezone": zone,
            "all_day": all_day,
            "cancelled": raw.get("status") == "cancelled",
            "attendees": attendees,
        }
    else:
        start, zone, all_day = event_time(raw["start"], calendar_zone)
        end = event_time(raw["end"], zone)[0]
        attendees = [
            {
                "name": entry.get("emailAddress", {}).get("name"),
                "email": entry.get("emailAddress", {}).get("address"),
                "response": entry.get("status", {}).get("response"),
            }
            for entry in raw.get("attendees", [])
        ]
        online_meeting: dict[str, Any] = raw.get("onlineMeeting") or {}
        url = online_meeting.get("joinUrl") or raw.get("onlineMeetingUrl")
        values = {
            "provider_event_id": raw["id"],
            "series_id": raw.get("seriesMasterId"),
            "occurrence_id": raw.get("originalStart") or raw["id"],
            "title": raw.get("subject", "Untitled event"),
            "start": start,
            "end": end,
            "original_timezone": raw.get("originalStartTimeZone") or zone,
            "all_day": bool(raw.get("isAllDay", all_day)),
            "cancelled": bool(raw.get("isCancelled")),
            "attendees": attendees,
        }
    if url:
        try:
            safe_https(url)
        except Exception:
            url = None
    return {
        "id": fingerprint([connection_id, calendar_id, raw["id"]]),
        "connection_id": connection_id,
        "calendar_id": calendar_id,
        "provider": provider,
        **values,
        "meeting_url": url,
        "etag": raw.get("etag") or raw.get("@odata.etag"),
    }


class CalendarService:
    def __init__(self, workspace: Workspace, http: ProviderHTTP) -> None:
        self.workspace = workspace
        self.http = http

    async def authorize(self, payload: dict[str, Any]) -> dict[str, Any]:
        privacy_guard(self.workspace)
        provider = required(payload, "provider")
        if provider not in ("google", "microsoft"):
            fail("invalid_provider", "Choose Google or Microsoft calendar")
        return await native(
            self.workspace,
            "oauth.authorize",
            {
                "provider": provider,
                "client_id": required(payload, "client_id"),
                "scopes": GOOGLE_SCOPES if provider == "google" else GRAPH_SCOPES,
                "tenant": payload.get("tenant", "common"),
            },
        )

    def connections(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [
                {key: value for key, value in item.items() if key != "secret_ref"}
                for item in all_items(self.workspace, "calendar_connections")
            ]
        }

    def connection_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = required(payload, "provider")
        if provider not in ("google", "microsoft", "manual"):
            fail("invalid_provider", "Unsupported calendar provider")
        identity = payload.get("id") or self.workspace.new_id()
        calendars = payload.get("calendar_ids", [])
        if not is_json_list(calendars) or any(
            not isinstance(item, str) or not item for item in calendars
        ):
            fail("invalid_input", "Select explicit calendar IDs")
        if provider != "manual":
            vault_reference(required(payload, "secret_ref"))
            required(payload, "client_id")
        timezone = payload.get("timezone", "UTC")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            fail("invalid_timezone", "Select an IANA timezone")
        if not 0 <= int(payload.get("reminder_minutes", 5)) <= 1440:
            fail("invalid_reminder", "Reminder offset must be from zero to 1440 minutes")
        data: dict[str, Any] = {
            "id": identity,
            "provider": provider,
            "client_id": payload.get("client_id"),
            "secret_ref": vault_reference(payload["secret_ref"]) if provider != "manual" else None,
            "calendar_ids": calendars,
            "timezone": timezone,
            "reminders_enabled": bool(payload.get("reminders_enabled", False)),
            "reminder_minutes": int(payload.get("reminder_minutes", 5)),
            "project_mapping": payload.get("project_mapping", {}),
            "enabled": True,
        }
        item = self.workspace.put(
            "calendar_connections",
            identity,
            data,
            expected_revision=payload.get("expected_revision"),
        )
        return {"item": {key: value for key, value in item.items() if key != "secret_ref"}}

    async def disconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        item = existing(self.workspace, "calendar_connections", required(payload, "id"))
        updated = self.workspace.put(
            "calendar_connections",
            item["id"],
            {**item, "enabled": False},
            expected_revision=required(payload, "expected_revision"),
        )
        revocation = (
            await native(
                self.workspace,
                "oauth.revoke",
                {"secret_ref": item["secret_ref"], "provider": item["provider"]},
            )
            if item.get("secret_ref")
            else None
        )
        return {
            "item": {key: value for key, value in updated.items() if key != "secret_ref"},
            "revocation": revocation,
        }

    async def calendars(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection = existing(
            self.workspace, "calendar_connections", required(payload, "connection_id")
        )
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["provider"],
            ["calendar"],
            {"connection_id": connection["id"]},
        )
        provider = connection["provider"]
        path = (
            "/calendar/v3/users/me/calendarList" if provider == "google" else "/v1.0/me/calendars"
        )
        params = (
            {"maxResults": 250}
            if provider == "google"
            else {"$select": "id,name,canEdit", "$top": 100}
        )
        output: list[dict[str, Any]] = []
        while path:
            response = await self.http.request(connection, "GET", path, params=params)
            data = response["json"]
            output.extend(
                {
                    "id": item["id"],
                    "title": item.get("summary") or item.get("name"),
                    "timezone": item.get("timeZone"),
                }
                for item in data.get("items" if provider == "google" else "value", [])
            )
            path, params = self._next(provider, path, params, data)
        return {"items": output}

    async def sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection = existing(
            self.workspace, "calendar_connections", required(payload, "connection_id")
        )
        if connection["provider"] == "manual":
            return {
                "items": all_items(
                    self.workspace, "calendar_events", {"connection_id": connection["id"]}
                ),
                "mode": "offline_manual",
            }
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["provider"],
            ["calendar"],
            {"connection_id": connection["id"]},
        )
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        start, end = now - timedelta(days=7), now + timedelta(days=30)
        events: list[dict[str, Any]] = []
        previous_sync: dict[str, Any] = self.workspace.get("calendar_sync", connection["id"]) or {}
        conditional: dict[str, dict[str, Any]] = {}
        for calendar_id in connection["calendar_ids"]:
            provider = connection["provider"]
            encoded = quote(calendar_id, safe="")
            if provider == "google":
                path = f"/calendar/v3/calendars/{encoded}/events"
                params = {
                    "timeMin": iso(start),
                    "timeMax": iso(end),
                    "singleEvents": "true",
                    "showDeleted": "true",
                    "maxResults": 2500,
                    "orderBy": "startTime",
                }
            else:
                path = f"/v1.0/me/calendars/{encoded}/calendarView"
                params = {
                    "startDateTime": iso(start),
                    "endDateTime": iso(end),
                    "$top": 100,
                    "$select": "id,subject,start,end,attendees,seriesMasterId,originalStart,isAllDay,isCancelled,originalStartTimeZone,type",
                }
            seen_pages: set[str] = set()
            query_hash = fingerprint([path, params])
            cached: dict[str, Any] = previous_sync.get("conditional", {}).get(calendar_id, {})
            etag = cached.get("etag") if cached.get("query_hash") == query_hash else None
            while path:
                page_key = fingerprint([path, params])
                if page_key in seen_pages:
                    fail("calendar_pagination", "Provider repeated a pagination cursor")
                seen_pages.add(page_key)
                headers = (
                    {"If-None-Match": etag}
                    if provider == "google" and etag and len(seen_pages) == 1
                    else None
                )
                response = await self.http.request(
                    connection, "GET", path, params=params, headers=headers
                )
                if response["status"] == 304:
                    events.extend(
                        item
                        for item in all_items(
                            self.workspace, "calendar_events", {"connection_id": connection["id"]}
                        )
                        if item.get("calendar_id") == calendar_id
                    )
                    conditional[calendar_id] = cached
                    break
                data = response["json"]
                if (
                    provider == "google"
                    and len(seen_pages) == 1
                    and (data.get("etag") or response["headers"].get("etag"))
                ):
                    conditional[calendar_id] = {
                        "query_hash": query_hash,
                        "etag": data.get("etag") or response["headers"]["etag"],
                    }
                for raw in data.get("items" if provider == "google" else "value", []):
                    events.append(
                        normalize_event(
                            provider,
                            calendar_id,
                            connection["id"],
                            raw,
                            data.get("timeZone") or connection["timezone"],
                        )
                    )
                path, params = self._next(provider, path, params, data)

        def commit(workspace: Workspace) -> dict[str, Any]:
            current_connection = existing(workspace, "calendar_connections", connection["id"])
            if (
                not current_connection.get("enabled")
                or current_connection["revision"] != connection["revision"]
            ):
                fail(
                    "connection_changed",
                    "The calendar connection changed while syncing; no partial snapshot was committed",
                )
            consent(
                workspace, connection["provider"], ["calendar"], {"connection_id": connection["id"]}
            )
            fetched = {event["id"] for event in events}
            for event in events:
                previous = workspace.get("calendar_events", event["id"])
                save(workspace, "calendar_events", event["id"], event, previous)
            for old in all_items(workspace, "calendar_events", {"connection_id": connection["id"]}):
                if old["id"] not in fetched:
                    workspace.delete(
                        "calendar_events", old["id"], expected_revision=old["revision"]
                    )
            previous = workspace.get("calendar_sync", connection["id"])
            return save(
                workspace,
                "calendar_sync",
                connection["id"],
                {
                    "id": connection["id"],
                    "connection_id": connection["id"],
                    "window_start": iso(start),
                    "window_end": iso(end),
                    "last_success_at": iso(now),
                    "next_sync_at": iso(now + timedelta(minutes=15)),
                    "event_count": len(events),
                    "conditional": conditional,
                },
                previous,
            )

        result = self.workspace.atomic(commit)
        self.workspace.emit(
            "calendar.synced", {"connection_id": connection["id"], "event_count": len(events)}
        )
        return {"item": result, "items": events}

    @staticmethod
    def _next(
        provider: str, path: str, params: dict[str, Any] | None, data: dict[str, Any]
    ) -> tuple[str | None, dict[str, Any] | None]:
        if provider == "google":
            return (
                (path, {**(params or {}), "pageToken": data["nextPageToken"]})
                if data.get("nextPageToken")
                else (None, None)
            )
        return data.get("@odata.nextLink"), None

    def events(self, payload: dict[str, Any]) -> dict[str, Any]:
        items = all_items(
            self.workspace,
            "calendar_events",
            {"connection_id": payload["connection_id"]} if payload.get("connection_id") else None,
        )
        return {"items": sorted(items, key=lambda item: (item.get("start") or "", item["id"]))}

    def manual_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = payload.get("id") or self.workspace.new_id()
        start, end = (
            iso(instant(required(payload, "start"))),
            iso(instant(required(payload, "end"))),
        )
        if start >= end:
            fail("invalid_time_range", "Calendar event end must follow start")
        connection_id = payload.get("connection_id") or "local-manual-calendar"
        connection = self.workspace.get("calendar_connections", connection_id)
        if connection is None:
            self.connection_save(
                {
                    "id": connection_id,
                    "provider": "manual",
                    "timezone": payload.get("timezone", "UTC"),
                }
            )
        elif connection["provider"] != "manual":
            fail(
                "manual_calendar_required",
                "Manual events cannot be inserted into a remote provider connection",
            )
        item = self.workspace.put(
            "calendar_events",
            identity,
            {
                "id": identity,
                "provider": "manual",
                "connection_id": connection_id,
                "calendar_id": "manual",
                "provider_event_id": identity,
                "occurrence_id": identity,
                "title": required(payload, "title"),
                "start": start,
                "end": end,
                "original_timezone": payload.get("timezone", "UTC"),
                "attendees": payload.get("attendees", []),
                "series_id": payload.get("series_id"),
                "cancelled": False,
            },
            expected_revision=payload.get("expected_revision"),
        )
        return {"item": item}

    def select(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = existing(self.workspace, "calendar_events", required(payload, "id"))
        if event.get("cancelled"):
            fail("event_cancelled", "Cancelled events cannot create a preparation draft")
        connection = (
            self.workspace.get("calendar_connections", event["connection_id"])
            if event.get("connection_id")
            else None
        )
        mapping: dict[str, Any] = (connection or {}).get("project_mapping", {})
        series_id = event.get("series_id")
        calendar_id = event.get("calendar_id")
        project_id = (mapping.get(series_id) if series_id is not None else None) or (
            mapping.get(calendar_id) if calendar_id is not None else None
        )
        prior = [
            meeting
            for meeting in all_items(self.workspace, "meetings")
            if event.get("series_id") and meeting.get("calendar_series_id") == event["series_id"]
        ]
        return {
            "draft": {
                "title": event["title"],
                "attendees": event.get("attendees", []),
                "calendar_event_id": event["id"],
                "calendar_series_id": event.get("series_id"),
                "project_id": project_id,
                "state": "draft",
            },
            "prior_meeting_ids": [meeting["id"] for meeting in prior],
            "requires_explicit_start": True,
        }

    async def reminders(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        sent: list[str] = []
        for connection in all_items(self.workspace, "calendar_connections"):
            if not connection.get("enabled") or not connection.get("reminders_enabled"):
                continue
            for event in all_items(
                self.workspace, "calendar_events", {"connection_id": connection["id"]}
            ):
                if event.get("cancelled") or not event.get("start"):
                    continue
                start = instant(event["start"])
                if (
                    not start - timedelta(minutes=connection.get("reminder_minutes", 5))
                    <= now
                    < start
                ):
                    continue
                key = fingerprint([event["id"], event["start"]])
                previous = self.workspace.get("calendar_reminders", key)
                if previous and previous.get("state") == "notified":
                    continue
                reminder = save(
                    self.workspace,
                    "calendar_reminders",
                    key,
                    {
                        "id": key,
                        "event_id": event["id"],
                        "state": "prepared",
                        "start": event["start"],
                    },
                    previous,
                )
                try:
                    await native(
                        self.workspace,
                        "native.notify",
                        {
                            "id": key,
                            "title": event["title"],
                            "body": "Open meeting preparation",
                            "action": {
                                "operation_id": "calendar.select",
                                "payload": {"id": event["id"]},
                            },
                        },
                    )
                except Exception as error:
                    save(
                        self.workspace,
                        "calendar_reminders",
                        key,
                        {
                            **reminder,
                            "state": "failed",
                            "error_code": getattr(error, "code", "notification_failed"),
                        },
                        reminder,
                    )
                    self.workspace.emit(
                        "calendar.reminder.failed",
                        {
                            "event_id": event["id"],
                            "code": getattr(error, "code", "notification_failed"),
                        },
                    )
                    continue
                save(
                    self.workspace,
                    "calendar_reminders",
                    key,
                    {**reminder, "state": "notified"},
                    reminder,
                )
                sent.append(event["id"])
        return {"items": sent}

    async def tick(self) -> None:
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        for connection in all_items(self.workspace, "calendar_connections"):
            if not connection.get("enabled") or connection["provider"] == "manual":
                continue
            state = self.workspace.get("calendar_sync", connection["id"])
            if state is None or instant(state["next_sync_at"]) <= now:
                try:
                    await self.sync({"connection_id": connection["id"]})
                except Exception as error:
                    previous = self.workspace.get("calendar_sync", connection["id"])
                    save(
                        self.workspace,
                        "calendar_sync",
                        connection["id"],
                        {
                            **(previous or {}),
                            "id": connection["id"],
                            "connection_id": connection["id"],
                            "next_sync_at": iso(now + timedelta(minutes=15)),
                            "last_error_code": getattr(error, "code", "sync_failed"),
                        },
                        previous,
                    )
                    self.workspace.emit(
                        "calendar.sync.failed",
                        {
                            "connection_id": connection["id"],
                            "code": getattr(error, "code", "sync_failed"),
                        },
                    )
        await self.reminders({})
