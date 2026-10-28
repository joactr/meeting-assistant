"""Offline scenarios use real Workspace persistence; HTTP fixtures test wire contracts only."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from meeting_assistant.application.connected.calendar import (
    CalendarService,
    event_time,
    normalize_event,
)
from meeting_assistant.application.connected.common import fingerprint, iso
from meeting_assistant.application.connected.delivery import DeliveryService, recipient
from meeting_assistant.application.connected.digest import (
    boundary_for,
    deterministic_delta,
    local_boundary,
)
from meeting_assistant.application.connected.issues import IssueService, marker
from meeting_assistant.application.connected.network import ProviderHTTP
from meeting_assistant.application.connected.notes import MINUTES_OUTLINE, validate_sections
from meeting_assistant.application.connected.vocabulary import compile_terms
from meeting_assistant.domain.errors import AppError
from meeting_assistant.domain.model import new_id
from meeting_assistant.workspace import Workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    fixed = int(datetime(2026, 10, 22, 12, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr("meeting_assistant.infrastructure.sqlite.store.now_ms", lambda: fixed)
    value = Workspace(tmp_path / "workspace.sqlite")
    monkeypatch.setattr(value, "now_ms", lambda: fixed)
    yield value
    value.close()


def project(workspace):
    return workspace.dispatch("projects.create", {"name": "Example project"})["item"]


def meeting(workspace, project_id=None):
    return workspace.dispatch(
        "meetings.create", {"title": "Example meeting", "project_id": project_id, "source": "mic"}
    )["item"]


def grant(workspace, destination, classes, scope):
    return workspace.dispatch(
        "consents.grant",
        {
            "purpose": "connected-test",
            "destination_fingerprint": destination,
            "data_classes": classes,
            "scope": scope,
        },
    )


def test_vocabulary_scope_priority_omissions_and_hash():
    terms = [
        {"id": "w", "spelling": "Acme", "priority": 100, "enabled": True},
        {
            "id": "p",
            "spelling": "ＡＣＭＥ",
            "project_id": "p1",
            "priority": 1,
            "enabled": True,
            "aliases": ["Acme Cloud"],
        },
        {"id": "a", "spelling": "Beta", "priority": 3, "enabled": True},
        {"id": "b", "spelling": "Gamma", "priority": 3, "enabled": True},
        {"id": "other", "spelling": "Private", "project_id": "p2", "priority": 1000},
    ]
    capability = {"vocabulary_mode": "keyterms", "vocabulary_max_terms": 2}
    result = compile_terms(terms, "p1", capability)
    assert [term["id"] for term in result["terms"]] == ["a", "b"]
    assert result["omitted"] == [{"id": "p", "spelling": "ＡＣＭＥ", "reason": "term_limit"}]
    assert result["acknowledgement_required"]
    accepted = compile_terms(terms, "p1", capability, acknowledgement=result["acknowledgement_key"])
    assert not accepted["acknowledgement_required"]
    assert accepted["effective_hash"] == result["effective_hash"]
    assert (
        compile_terms(list(reversed(terms)), "p1", capability)["effective_hash"]
        == result["effective_hash"]
    )


def test_vocabulary_unsupported_is_explicit():
    result = compile_terms([{"id": "a", "spelling": "Acme"}], None, {"vocabulary_mode": "none"})
    assert not result["terms"]
    assert result["omitted"][0]["reason"] == "unsupported_backend"
    assert result["acknowledgement_required"]


def test_all_day_and_offset_timezone_normalization():
    start, zone, all_day = event_time({"date": "2026-10-22", "timeZone": "America/New_York"})
    assert start == "2026-10-22T04:00:00Z"
    assert all_day and zone == "America/New_York"
    assert event_time({"dateTime": "2026-10-22T09:00:00-04:00"})[0] == "2026-10-22T13:00:00Z"


def test_google_recurring_cancellation_preserves_occurrence():
    event = normalize_event(
        "google",
        "cal",
        "conn",
        {
            "id": "instance",
            "recurringEventId": "series",
            "status": "cancelled",
            "originalStartTime": {"dateTime": "2026-10-22T10:00:00Z"},
        },
        "UTC",
    )
    assert event["cancelled"] and event["series_id"] == "series"
    assert event["start"] == "2026-10-22T10:00:00Z"
    assert event["occurrence_id"] != event["provider_event_id"]


def test_event_urls_cannot_execute_content():
    event = normalize_event(
        "google",
        "cal",
        "conn",
        {
            "id": "event",
            "start": {"date": "2026-10-22"},
            "end": {"date": "2026-10-23"},
            "hangoutLink": "javascript:alert(1)",
        },
        "UTC",
    )
    assert event["meeting_url"] is None


def test_manual_calendar_prepares_without_recording(workspace):
    event = workspace.dispatch(
        "calendar.manual.save",
        {
            "title": "Local planning",
            "start": "2026-10-22T13:00:00Z",
            "end": "2026-10-22T14:00:00Z",
            "attendees": [{"name": "Example"}],
        },
    )["item"]
    result = workspace.dispatch("calendar.select", {"id": event["id"]})
    assert result["requires_explicit_start"]
    assert result["draft"]["state"] == "draft"
    assert result["draft"]["title"] == "Local planning"
    assert workspace.list("meetings") == []


class FixtureVault:
    """Test-only private broker; never used by application runtime."""

    async def request(self, operation, payload):
        if operation == "vault.resolve":
            return {"value": json.dumps({"access_token": "synthetic-protocol-fixture"})}
        raise AssertionError(operation)


@pytest.mark.asyncio
async def test_google_bounded_full_window_paginates_atomically(workspace):
    workspace.native_gateway = FixtureVault()
    connection = workspace.dispatch(
        "calendar.connection.save",
        {
            "provider": "google",
            "client_id": "user-configured-test-id",
            "secret_ref": "oauth:00000000-0000-4000-8000-000000000001",
            "calendar_ids": ["selected@example.test"],
        },
    )["item"]
    grant(workspace, "google", ["calendar"], {"connection_id": connection["id"]})
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.params["singleEvents"] == "true"
        assert "timeMin" in request.url.params and "timeMax" in request.url.params
        assert "syncToken" not in request.url.params
        assert request.headers["authorization"].startswith("Bearer ")
        if "pageToken" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "one",
                            "summary": "First",
                            "start": {"dateTime": "2026-10-22T10:00:00Z"},
                            "end": {"dateTime": "2026-10-22T11:00:00Z"},
                        }
                    ],
                    "nextPageToken": "second",
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "two",
                        "summary": "Second",
                        "start": {"dateTime": "2026-10-23T10:00:00Z"},
                        "end": {"dateTime": "2026-10-23T11:00:00Z"},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = CalendarService(workspace, ProviderHTTP(workspace, client))
        result = await service.sync({"connection_id": connection["id"]})
    assert len(requests) == 2 and result["item"]["event_count"] == 2
    assert len(workspace.list("calendar_events")) == 2


@pytest.mark.asyncio
async def test_provider_rejects_cross_origin_pagination_before_credentials(workspace):
    service = ProviderHTTP(workspace)
    with pytest.raises(AppError) as error:
        await service.request(
            {"provider": "microsoft", "secret_ref": "never-resolve"},
            "GET",
            "https://attacker.example/steal",
        )
    assert error.value.code == "unsafe_pagination"


@pytest.mark.parametrize(
    ("day", "hour", "minute", "expected"),
    [
        (date(2026, 3, 8), 2, 30, "2026-03-08T07:00:00Z"),
        (date(2026, 11, 1), 1, 30, "2026-11-01T05:30:00Z"),
    ],
)
def test_digest_dst_boundary(day, hour, minute, expected):
    assert iso(local_boundary(day, hour, minute, ZoneInfo("America/New_York"))) == expected


def test_weekly_boundary_is_local_not_fixed_utc():
    rule = {"timezone": "America/New_York", "weekday": 4, "time": "16:00"}
    assert iso(boundary_for(rule, datetime(2026, 10, 30, 21, tzinfo=UTC))) == "2026-10-30T20:00:00Z"
    assert iso(boundary_for(rule, datetime(2026, 11, 6, 22, tzinfo=UTC))) == "2026-11-06T21:00:00Z"


def test_deterministic_delta_has_cited_completed_open_and_overdue():
    before = {
        "actions:a": {
            "id": "a",
            "collection": "actions",
            "description": "Ship",
            "status": "open",
            "revision": 1,
            "meeting_id": "m",
        }
    }
    after = {
        "actions:a": {**before["actions:a"], "status": "done", "revision": 2},
        "actions:b": {
            "id": "b",
            "collection": "actions",
            "description": "Review",
            "due_date": "2026-10-20",
            "status": "open",
            "revision": 1,
            "meeting_id": "m",
        },
    }
    result = deterministic_delta(before, after, datetime(2026, 10, 22, tzinfo=UTC))
    assert [item["category"] for item in result["sections"]["actions"]] == ["completed", "overdue"]
    assert result == deterministic_delta(
        before, dict(reversed(list(after.items()))), datetime(2026, 10, 22, tzinfo=UTC)
    )


def test_no_change_delta_does_not_claim_new_work():
    item = {
        "actions:a": {
            "id": "a",
            "collection": "actions",
            "description": "Open task",
            "status": "open",
            "revision": 1,
        }
    }
    result = deterministic_delta(item, item, datetime(2026, 10, 22, tzinfo=UTC))
    assert result["no_change"]
    assert result["sections"]["actions"][0]["category"] == "open"


@pytest.mark.asyncio
async def test_minutes_save_directly_and_preserve_human_text(workspace):
    item = meeting(workspace)
    saved = workspace.dispatch(
        "notes.save",
        {
            "meeting_id": item["id"],
            "expected_revision": 0,
            "blocks": [{"id": "outline-1", "heading": "Goals", "body": "A human-only observation"}],
        },
    )["item"]
    first = (
        await workspace.adispatch(
            "minutes.prepare", {"meeting_id": item["id"], "note_revision": saved["revision"]}
        )
    )["item"]
    second = (
        await workspace.adispatch(
            "minutes.prepare", {"meeting_id": item["id"], "note_revision": saved["revision"]}
        )
    )["item"]
    assert first["id"] == second["id"] == item["id"]
    assert workspace.get("minutes_approved", item["id"])["revision"] == second["revision"]
    assert second["revision"] > first["revision"]
    assert (
        workspace.get("human_notes", item["id"])["blocks"][0]["body"] == "A human-only observation"
    )
    assert first["sections"][0]["source"] == "Human note"
    assert first["sections"][0]["citations"] == []


@pytest.mark.asyncio
async def test_minutes_use_fixed_outline_without_template(workspace):
    item = meeting(workspace)
    assert "template_version_id" not in item and "template_snapshot" not in item
    # Every line from one audio source shares its source_id; the evidence id is the utterance.
    run = workspace.list("transcript_runs", {"meeting_id": item["id"]})[0]
    source = workspace.put(
        "capture_sources",
        new_id(),
        {
            "meeting_id": item["id"],
            "source_kind": "mic",
            "device_key": "synthetic",
            "display_name": "Mic",
            "epoch": 1,
            "state": "stopped",
            "source_index": 0,
        },
    )
    lines = [
        workspace.put(
            "utterances",
            new_id(),
            {
                "meeting_id": item["id"],
                "run_id": run["id"],
                "source_id": source["id"],
                "provider_result_id": f"r{start}",
                "start_ms": start,
                "end_ms": start + 500,
                "text": f"Line {start}",
                "is_final": True,
            },
        )
        for start in (0, 1000)
    ]
    seen = {}

    class Inference:
        async def generate(self, task, evidence, policy, *, language, context):
            seen.update(context, evidence=evidence)
            return {"result": {"sections": []}}

    workspace.inference = Inference()
    await workspace.adispatch(
        "minutes.prepare", {"meeting_id": item["id"], "note_revision": 0, "policy": {"x": 1}}
    )
    assert [block["id"] for block in seen["outline"]] == [s["id"] for s in MINUTES_OUTLINE]
    assert seen["outline_guidance"] == MINUTES_OUTLINE
    assert "template" not in seen
    assert [e["source_id"] for e in seen["evidence"]] == [line["id"] for line in lines]


def test_notes_cas_conflict_does_not_overwrite(workspace):
    item = meeting(workspace)
    payload = {
        "meeting_id": item["id"],
        "blocks": [{"id": "one", "body": "Original"}],
        "expected_revision": 0,
    }
    workspace.dispatch("notes.save", payload)
    with pytest.raises(AppError) as error:
        workspace.dispatch(
            "notes.save", {**payload, "blocks": [{"id": "one", "body": "Stale edit"}]}
        )
    assert error.value.code == "revision_conflict"
    assert workspace.get("human_notes", item["id"])["blocks"][0]["body"] == "Original"


def test_minutes_rejects_unknown_citations_and_false_provenance():
    with pytest.raises(AppError):
        validate_sections(
            [
                {
                    "id": "s",
                    "text": "Claim",
                    "source": "Transcript",
                    "citations": [{"source_id": "invented"}],
                }
            ],
            {},
            set(),
        )
    with pytest.raises(AppError):
        validate_sections(
            [{"id": "s", "text": "Claim", "source": "Both", "citations": [], "note_ids": ["n"]}],
            {},
            {"n"},
        )


@pytest.mark.asyncio
async def test_digest_preview_does_not_advance_schedule(workspace):
    item = project(workspace)
    rule = workspace.dispatch(
        "digest.schedule.save", {"project_id": item["id"], "timezone": "UTC", "enabled": False}
    )["item"]
    result = (await workspace.adispatch("digest.preview", {"project_id": item["id"]}))["item"]
    assert result["preview"] and result["content"]["no_change"]
    assert workspace.get("digest_schedules", item["id"])["revision"] == rule["revision"]
    assert not workspace.list("digest_inbox")
    assert result["generation"]["mode"] == "deterministic"


@pytest.mark.asyncio
async def test_slack_timeout_is_unknown_and_not_blindly_retried(workspace):
    item = project(workspace)
    run = (await workspace.adispatch("digest.preview", {"project_id": item["id"]}))["item"]
    setting = workspace.dispatch(
        "digest.delivery.configure",
        {
            "transport": "slack",
            "project_id": item["id"],
            "destination": "Example workspace #reviewed",
            "secret_ref": "credential:00000000-0000-4000-8000-000000000001",
        },
    )["item"]
    grant(
        workspace,
        setting["destination"],
        ["digest", "meeting_metadata"],
        {"project_id": item["id"], "recipients": [], "delivery_id": setting["id"]},
    )

    class WebhookVault:
        async def request(self, operation, payload):
            assert operation == "vault.resolve"
            return {"value": "https://hooks.slack.com/services/T/F/synthetic"}

    workspace.native_gateway = WebhookVault()
    attempts = []

    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        delivery = DeliveryService(workspace, client)
        result = await delivery.send(
            {"run_id": run["id"], "delivery_id": setting["id"], "reviewed": True}
        )
        assert result["item"]["state"] == "delivery-unknown"
        with pytest.raises(AppError) as error:
            await delivery.send(
                {"run_id": run["id"], "delivery_id": setting["id"], "reviewed": True}
            )
    assert error.value.code == "delivery_unknown"
    assert len(attempts) == 1


def test_delivery_recipients_reject_header_injection():
    for address in (
        "someone@example.test\r\nBcc: other@example.test",
        "Name <someone@example.test>",
        "not-an-address",
    ):
        with pytest.raises(AppError):
            recipient(address)


@pytest.mark.asyncio
async def test_issue_timeout_reconciles_paginated_marker_without_second_post(workspace):
    item = meeting(workspace)
    action = workspace.dispatch(
        "actions.create", {"meeting_id": item["id"], "description": "Review the release"}
    )["item"]
    connection = workspace.dispatch(
        "github.connection.save",
        {
            "secret_ref": "credential:00000000-0000-4000-8000-000000000001",
            "repositories": ["example/project"],
            "credential_kind": "pat",
        },
    )["item"]
    operation = workspace.dispatch(
        "github.issue.prepare",
        {
            "connection_id": connection["id"],
            "repository": "example/project",
            "action_id": action["id"],
        },
    )["item"]
    assert marker(operation["id"]) in operation["request"]["body"]
    assert operation["state"] == "prepared"
    assert fingerprint(operation["request"]) == operation["request_hash"]
    grant(
        workspace,
        "https://api.github.com",
        ["reviewed_issue", "meeting_metadata", "github_metadata"],
        {"connection_id": connection["id"], "repository": "example/project"},
    )
    workspace.native_gateway = FixtureVault()
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(request)
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if request.url.params.get("page") != "2":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "body": "Unrelated",
                        "html_url": "https://github.com/example/project/issues/1",
                    }
                ],
                headers={
                    "Link": '<https://api.github.com/repos/example/project/issues?page=2>; rel="next"'
                },
            )
        return httpx.Response(
            200,
            json=[
                {
                    "number": 2,
                    "body": operation["request"]["body"],
                    "html_url": "https://github.com/example/project/issues/2",
                    "labels": [],
                    "assignees": [],
                    "state": "open",
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        service = IssueService(workspace, ProviderHTTP(workspace, client))
        result = await service.send(
            {"id": operation["id"], "request_hash": operation["request_hash"], "reviewed": True}
        )
        assert result["item"]["state"] == "unknown"
        with pytest.raises(AppError) as error:
            await service.send(
                {"id": operation["id"], "request_hash": operation["request_hash"], "reviewed": True}
            )
        assert error.value.code == "reconciliation_required"
        reconciled = await service.reconcile({"id": operation["id"]})
        assert reconciled["item"]["state"] == "created"
        assert reconciled["item"]["remote_number"] == 2
        again = await service.send(
            {"id": operation["id"], "request_hash": operation["request_hash"], "reviewed": True}
        )
        assert again["existing"]
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_issue_reconcile_absence_remains_unknown(workspace):
    item = meeting(workspace)
    action = workspace.dispatch(
        "actions.create", {"meeting_id": item["id"], "description": "Follow up"}
    )["item"]
    connection = workspace.dispatch(
        "github.connection.save",
        {
            "secret_ref": "credential:00000000-0000-4000-8000-000000000001",
            "repositories": ["example/project"],
            "credential_kind": "pat",
        },
    )["item"]
    operation = workspace.dispatch(
        "github.issue.prepare",
        {
            "connection_id": connection["id"],
            "repository": "example/project",
            "action_id": action["id"],
        },
    )["item"]
    grant(
        workspace,
        "https://api.github.com",
        ["github_metadata"],
        {"connection_id": connection["id"], "repository": "example/project"},
    )
    workspace.native_gateway = FixtureVault()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))
    ) as client:
        result = await IssueService(workspace, ProviderHTTP(workspace, client)).reconcile(
            {"id": operation["id"]}
        )
    assert result["item"]["state"] == "unknown"


def test_strict_review_flags_cannot_be_strings(workspace):
    with pytest.raises(AppError) as error:
        workspace.dispatch("delivery.test", {"id": "missing", "reviewed": "false"})
    assert error.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_meeting_delivery_rejects_preview_after_source_edit(workspace):
    item = meeting(workspace)
    preview = workspace.dispatch("delivery.preview", {"meeting_id": item["id"]})["item"]
    settings = workspace.dispatch(
        "delivery.configure", {"transport": "local", "meeting_id": item["id"]}
    )["item"]
    current = workspace.get("meetings", item["id"])
    workspace.put(
        "meetings", item["id"], {"title": "Edited title"}, expected_revision=current["revision"]
    )
    with pytest.raises(AppError) as error:
        await workspace.adispatch(
            "delivery.send",
            {
                "preview_id": preview["id"],
                "delivery_id": settings["id"],
                "preview_hash": preview["preview_hash"],
                "reviewed": True,
            },
        )
    assert error.value.code == "stale_preview"


@pytest.mark.asyncio
async def test_digest_history_boundary_excludes_later_edits(workspace, monkeypatch):
    clock = [workspace.now_ms()]
    monkeypatch.setattr("meeting_assistant.infrastructure.sqlite.store.now_ms", lambda: clock[0])
    monkeypatch.setattr(workspace, "now_ms", lambda: clock[0])
    item = project(workspace)
    record = meeting(workspace, item["id"])
    action = workspace.dispatch(
        "actions.create", {"meeting_id": record["id"], "description": "Before boundary"}
    )["item"]
    boundary_ms = workspace.now_ms()
    clock[0] += 1000
    workspace.put(
        "actions",
        action["id"],
        {"description": "After boundary"},
        expected_revision=action["revision"],
    )
    snapshot = workspace.connected.digest.snapshot(
        {"project_id": item["id"]},
        datetime.fromtimestamp(boundary_ms / 1000, UTC),
    )
    assert snapshot["actions:" + action["id"]]["description"] == "Before boundary"


@pytest.mark.asyncio
async def test_digest_catchup_coalesces_and_persists_unique_boundary(workspace):
    item = project(workspace)
    rule = workspace.dispatch(
        "digest.schedule.save",
        {"project_id": item["id"], "timezone": "America/New_York", "enabled": True},
    )["item"]
    old_due = datetime(2026, 10, 2, 20, tzinfo=UTC)
    workspace.put(
        "digest_schedules",
        rule["id"],
        {"next_boundary": iso(old_due)},
        expected_revision=rule["revision"],
    )
    result = (await workspace.adispatch("digest.run", {"project_id": item["id"]}))["item"]
    assert result["catchup"]
    assert result["period_end"] == "2026-10-16T20:00:00Z"
    assert result["period_start"] == "2026-10-09T20:00:00Z"
    assert len(workspace.list("digest_runs")) == 1
    assert len(workspace.list("digest_inbox")) == 1
    with pytest.raises(AppError) as error:
        await workspace.adispatch("digest.run", {"project_id": item["id"]})
    assert error.value.code == "schedule_not_due"
    assert len(workspace.list("digest_runs")) == 1


@pytest.mark.asyncio
async def test_graph_full_window_follows_nextlink_without_delta_tokens(workspace):
    workspace.native_gateway = FixtureVault()
    connection = workspace.dispatch(
        "calendar.connection.save",
        {
            "provider": "microsoft",
            "client_id": "configured-test-registration",
            "secret_ref": "oauth:00000000-0000-4000-8000-000000000001",
            "calendar_ids": ["selected-calendar"],
        },
    )["item"]
    grant(workspace, "microsoft", ["calendar"], {"connection_id": connection["id"]})
    requests = []

    def respond(request):
        requests.append(request)
        assert request.headers["prefer"] == 'outlook.timezone="UTC"'
        assert "delta" not in request.url.path
        if len(requests) == 1:
            assert "startDateTime" in request.url.params and "endDateTime" in request.url.params
            return httpx.Response(
                200,
                json={
                    "value": [],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/calendars/selected-calendar/calendarView?$skiptoken=opaque",
                },
            )
        assert request.url.params["$skiptoken"] == "opaque"
        return httpx.Response(
            200,
            json={
                "value": [
                    {
                        "id": "instance",
                        "subject": "Graph planning",
                        "start": {"dateTime": "2026-10-22T13:00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-10-22T14:00:00", "timeZone": "UTC"},
                        "seriesMasterId": "series",
                        "originalStartTimeZone": "Eastern Standard Time",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await CalendarService(workspace, ProviderHTTP(workspace, client)).sync(
            {"connection_id": connection["id"]}
        )
    assert len(requests) == 2
    assert result["items"][0]["series_id"] == "series"
    assert result["items"][0]["original_timezone"] == "Eastern Standard Time"


@pytest.mark.parametrize(
    "reference",
    [
        "github_pat_never_store_this",
        "https://hooks.slack.com/services/synthetic",
        "credential:not-a-uuid",
    ],
)
def test_connection_rejects_credentials_disguised_as_vault_references(workspace, reference):
    with pytest.raises(AppError) as error:
        workspace.dispatch(
            "calendar.connection.save",
            {
                "provider": "google",
                "client_id": "user-configured-test-registration",
                "secret_ref": reference,
            },
        )
    assert error.value.code == "invalid_secret_reference"
    assert workspace.list("calendar_connections") == []


@pytest.mark.asyncio
async def test_saved_destination_enables_reviewed_automation_without_exposing_secret(workspace):
    item = project(workspace)
    preview = (await workspace.adispatch("digest.preview", {"project_id": item["id"]}))["item"]
    reference = "credential:00000000-0000-4000-8000-000000000001"
    public = workspace.dispatch(
        "digest.delivery.configure",
        {
            "transport": "smtp",
            "destination": "smtp.example.test",
            "recipients": ["reviewer@example.test"],
            "project_id": item["id"],
            "secret_ref": reference,
        },
    )["item"]
    assert "secret_ref" not in public
    updated = workspace.dispatch(
        "digest.delivery.configure",
        {
            **public,
            "expected_revision": public["revision"],
            "automatic": True,
            "reviewed": True,
            "preview_id": preview["id"],
            "preview_hash": preview["preview_hash"],
        },
    )["item"]
    assert updated["automatic"]
    assert "secret_ref" not in updated
    assert workspace.get("delivery_settings", public["id"])["secret_ref"] == reference
    with pytest.raises(AppError) as error:
        workspace.dispatch(
            "digest.delivery.configure",
            {
                **updated,
                "expected_revision": updated["revision"],
                "recipients": ["different@example.test"],
                "reviewed": True,
                "preview_id": preview["id"],
            },
        )
    assert error.value.code == "delivery_binding_changed"


@pytest.mark.asyncio
async def test_failed_digest_synthesis_requires_explicit_retry(workspace):
    item = project(workspace)
    record = meeting(workspace, item["id"])
    workspace.dispatch(
        "actions.create", {"meeting_id": record["id"], "description": "A reviewed source action"}
    )
    rule = workspace.dispatch(
        "digest.schedule.save",
        {
            "project_id": item["id"],
            "timezone": "UTC",
            "weekday": 3,
            "time": "12:00",
            "enabled": True,
            "policy": {"profile_id": "unavailable-user-configured-profile"},
        },
    )["item"]
    now = datetime.fromtimestamp(workspace.now_ms() / 1000, UTC)
    workspace.put(
        "digest_schedules",
        rule["id"],
        {"next_boundary": iso(now)},
        expected_revision=rule["revision"],
    )
    with pytest.raises(AppError):
        await workspace.adispatch("digest.run", {"project_id": item["id"]})
    assert workspace.list("digest_runs")[0]["state"] == "generation-failed"
    with pytest.raises(AppError) as second:
        await workspace.adispatch("digest.run", {"project_id": item["id"]})
    assert second.value.code == "digest_retry_required"


def test_offline_privacy_setting_from_ui_blocks_consented_egress(workspace):
    grant(workspace, "https://api.example.test", ["transcript"], "workspace")
    assert workspace.check_consent("https://api.example.test", ["transcript"], "workspace")
    saved = workspace.get("workspace_settings", "workspace")
    workspace.dispatch(
        "settings.save",
        {
            "expected_revision": saved["revision"],
            "settings": {"privacy": {"network_disabled": True}},
        },
    )
    with pytest.raises(AppError) as error:
        workspace.check_consent("https://api.example.test", ["transcript"], "workspace")
    assert error.value.code == "privacy_mode"


def test_deleting_person_or_meeting_purges_voice_vectors(workspace):
    from meeting_assistant.application.speakers import SpeakerEvidence

    vectors = SpeakerEvidence(workspace)
    person = workspace.dispatch("people.create", {"display_name": "Example person"})["item"]
    profile_object = vectors.store_vector([0.5] * 8, "profile-1")
    workspace.put(
        "voice_profiles",
        "profile-1",
        {
            "person_id": person["id"],
            "model_fingerprint": "m",
            "vector_object_ref": profile_object,
            "consent_at": 1,
            "matching_scope": {"scope": "workspace"},
        },
    )
    item = meeting(workspace)
    cluster_object = vectors.store_vector([0.25] * 8, "cluster-1")
    workspace.put(
        "speaker_clusters",
        "cluster-1",
        {
            "meeting_id": item["id"],
            "provider_session_id": "s",
            "provider_speaker_id": "0",
            "model_fingerprint": "m",
            "embedding_object_ref": cluster_object,
        },
    )
    workspace.dispatch(
        "people.delete", {"id": person["id"], "expected_revision": person["revision"]}
    )
    workspace.dispatch(
        "meetings.delete",
        {
            "id": item["id"],
            "expected_revision": workspace.get("meetings", item["id"])["revision"],
            "confirm": True,
        },
    )
    assert not workspace.list("objects")
    history = workspace.connection.execute(
        "SELECT COUNT(*) FROM document_history WHERE entity_id IN(?,?,?)",
        (profile_object, cluster_object, "profile-1"),
    ).fetchone()[0]
    assert history == 0
    purged = [
        path
        for job in workspace.list("jobs")
        if job["kind"] == "purge_media"
        for path in job["payload"]["paths"]
    ]
    assert len(purged) == 2


@pytest.mark.asyncio
async def test_deleting_meeting_removes_digests_built_from_it(workspace):
    item = project(workspace)
    kept = meeting(workspace, item["id"])
    gone = meeting(workspace, item["id"])
    workspace.dispatch("actions.create", {"meeting_id": gone["id"], "description": "Ship it"})
    workspace.dispatch("digest.schedule.save", {"project_id": item["id"], "timezone": "UTC"})
    run = (await workspace.adispatch("digest.preview", {"project_id": item["id"]}))["item"]
    workspace.dispatch(
        "meetings.delete",
        {
            "id": gone["id"],
            "expected_revision": workspace.get("meetings", gone["id"])["revision"],
            "confirm": True,
        },
    )
    assert workspace.get("digest_runs", run["id"]) is None
    assert workspace.get("meetings", kept["id"])
    history = workspace.connection.execute(
        "SELECT COUNT(*) FROM document_history WHERE entity_id=?", (run["id"],)
    ).fetchone()[0]
    assert history == 0
