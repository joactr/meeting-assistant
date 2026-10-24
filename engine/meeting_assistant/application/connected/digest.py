from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .common import (
    all_items,
    existing,
    fail,
    fingerprint,
    instant,
    is_json_list,
    iso,
    native,
    required,
    save,
)
from .delivery import DeliveryService


def local_boundary(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime:
    """Choose the first fold; advance gaps to the next real local minute."""
    naive = datetime.combine(day, time(hour, minute))
    for _ in range(24 * 60 + 1):
        candidates: list[datetime] = []
        for fold in (0, 1):
            local = naive.replace(tzinfo=zone, fold=fold)
            utc = local.astimezone(UTC)
            if utc.astimezone(zone).replace(tzinfo=None) == naive:
                candidates.append(utc)
        if candidates:
            return min(candidates)
        naive += timedelta(minutes=1)
    fail("invalid_timezone_rule", "No valid local time exists near this schedule boundary")


def boundary_for(rule: dict[str, Any], now: datetime, *, future: bool = False) -> datetime:
    zone = ZoneInfo(rule["timezone"])
    local_day = now.astimezone(zone).date()
    day = local_day - timedelta(days=(local_day.weekday() - rule["weekday"]) % 7)
    hour, minute = (int(part) for part in rule["time"].split(":"))
    candidate = local_boundary(day, hour, minute, zone)
    if future:
        if candidate <= now:
            candidate = local_boundary(day + timedelta(days=7), hour, minute, zone)
    elif candidate > now:
        candidate = local_boundary(day - timedelta(days=7), hour, minute, zone)
    return candidate


def semantic(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if key not in ("revision", "updated_at", "created_at", "updated_at_ms", "created_at_ms")
    }


def deterministic_delta(
    previous: dict[str, dict[str, Any]], current: dict[str, dict[str, Any]], period_end: datetime
) -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "decisions",
            "actions",
            "risks_blockers",
            "milestones_deliverables",
            "questions",
            "meetings",
        )
    }
    changes = 0
    keys = set(previous) | set(current)
    for key in sorted(keys):
        before, after = previous.get(key), current.get(key)
        changed = fingerprint(semantic(before or {})) != fingerprint(semantic(after or {}))
        if changed:
            changes += 1
        item = after or before
        assert item is not None
        source: dict[str, Any] = {
            "collection": item["collection"],
            "id": item["id"],
            "revision": item.get("revision"),
            "meeting_id": item.get("meeting_id"),
            "source_ids": item.get("source_ids", []),
        }
        kind = item.get("kind", item["collection"])
        state = item.get("status", item.get("state", "open"))
        delta = (
            "removed"
            if after is None
            else "new"
            if before is None
            else "superseded"
            if state == "superseded" and changed
            else "changed"
            if changed
            else "unchanged"
        )
        entry: dict[str, Any] = {
            "id": item["id"],
            "text": item.get("description")
            or item.get("text")
            or item.get("title")
            or item.get("question")
            or item.get("content")
            or "",
            "delta": delta,
            "status": state,
            "sources": [source],
        }
        if kind in ("decision", "decisions") and changed:
            sections["decisions"].append(entry)
        elif item["collection"] == "actions":
            due = item.get("due_date") or item.get("deadline")
            overdue = False
            if due and state not in ("completed", "done", "cancelled", "dismissed"):
                try:
                    overdue = date.fromisoformat(str(due)[:10]) < period_end.date()
                except ValueError:
                    overdue = False
            entry["category"] = (
                "removed"
                if after is None
                else "completed"
                if state in ("completed", "done")
                else "overdue"
                if overdue
                else "open"
            )
            entry["assignee"] = item.get("assignee_text") or item.get("assignee")
            entry["due_date"] = due
            if changed or entry["category"] in ("open", "overdue"):
                sections["actions"].append(entry)
        elif kind in ("risk", "blocker") and (changed or state not in ("resolved", "closed")):
            sections["risks_blockers"].append(entry)
        elif kind in ("milestone", "deliverable") and changed:
            sections["milestones_deliverables"].append(entry)
        elif item["collection"] == "questions" and state not in ("answered", "dismissed") and after:
            sections["questions"].append(entry)
        elif item["collection"] == "meetings" and changed:
            sections["meetings"].append(entry)
    return {"sections": sections, "change_count": changes, "no_change": changes == 0}


def render_digest(
    project: dict[str, Any], start: datetime, end: datetime, delta: dict[str, Any], *, catchup: bool
) -> dict[str, Any]:
    title = f"Project digest: {project.get('name') or project.get('title') or project['id']}"
    lines = ["# " + title, "", f"Period: {iso(start)} — {iso(end)}", ""]
    if catchup:
        lines.extend(
            [
                "Catch-up: missed local schedule boundaries have been coalesced into this one period.",
                "",
            ]
        )
    if delta["no_change"]:
        lines.extend(["No new meetings or state changes in this period.", ""])
    headings = {
        "decisions": "New, changed and superseded decisions",
        "actions": "Completed, open and overdue actions",
        "risks_blockers": "Explicit risks and blockers",
        "milestones_deliverables": "Milestones and deliverables",
        "questions": "Questions needing follow-up",
        "meetings": "Linked meeting sources",
    }
    for name, heading in headings.items():
        lines.extend(["## " + heading, ""])
        for entry in delta["sections"][name]:
            sources = "; ".join(
                f"{source['collection']}:{source['id']}@{source.get('revision', 0)}"
                + (f" (meeting {source['meeting_id']})" if source.get("meeting_id") else "")
                for source in entry["sources"]
            )
            lines.append(
                f"- [{entry.get('category', entry['delta'])}] {entry['text']} — Sources: {sources}"
            )
        if not delta["sections"][name]:
            lines.append("None recorded.")
        lines.append("")
    return {"title": title, "markdown": "\n".join(lines), **delta}


class DigestService:
    def __init__(self, workspace: Workspace, delivery: DeliveryService) -> None:
        self.workspace = workspace
        self.delivery = delivery
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def public_run(item: dict[str, Any]) -> dict[str, Any]:
        result = dict(item)
        for key in ("period_start", "period_end"):
            if isinstance(result.get(key), (int, float)):
                result[key] = iso(datetime.fromtimestamp(result[key] / 1000, UTC))
        result["preview_hash"] = fingerprint(result.get("content"))
        return result

    def schedule_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = required(payload, "project_id")
        existing(self.workspace, "projects", project_id)
        item = self.workspace.get("digest_schedules", project_id)
        return {
            "item": item
            or {
                "id": project_id,
                "project_id": project_id,
                "enabled": False,
                "weekday": 4,
                "time": "16:00",
                "timezone": "UTC",
                "revision": 0,
            }
        }

    def schedule_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = required(payload, "project_id")
        existing(self.workspace, "projects", project_id)
        timezone = required(payload, "timezone")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            fail("invalid_timezone", "Choose an IANA timezone")
        weekday = int(payload.get("weekday", 4))
        clock = payload.get("time", "16:00")
        try:
            parsed_clock = time.fromisoformat(clock)
            if (
                len(clock) != 5
                or parsed_clock.tzinfo
                or parsed_clock.second
                or not 0 <= weekday <= 6
            ):
                raise ValueError
        except (ValueError, TypeError):
            fail("invalid_schedule", "Use a weekday from 0 (Monday) to 6 and local HH:MM")
        old = self.workspace.get("digest_schedules", project_id)
        work_packages = payload.get("work_package_ids", [])
        if not is_json_list(work_packages):
            fail("invalid_scope", "Work package filters must be explicit IDs")
        for work_package_id in work_packages:
            work_package = existing(self.workspace, "work_packages", work_package_id)
            if work_package.get("project_id") != project_id:
                fail("invalid_scope", "Digest work packages must belong to the selected project")
        delivery_ids = payload.get("delivery_ids", [])
        for identity in delivery_ids:
            settings = existing(self.workspace, "delivery_settings", identity)
            if settings.get("project_id") != project_id or not settings.get("automatic"):
                fail(
                    "invalid_delivery",
                    "Scheduled delivery requires a reviewed automatic destination in this project",
                )
        rule: dict[str, Any] = {
            "id": project_id,
            "project_id": project_id,
            "enabled": bool(payload.get("enabled", False)),
            "weekday": weekday,
            "time": clock,
            "timezone": timezone,
            "work_package_ids": work_packages,
            "delivery_ids": delivery_ids,
            "policy": payload.get("policy"),
            "last_success_boundary": (old or {}).get("last_success_boundary"),
        }
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        changed_rule = any(
            (old or {}).get(key) != rule[key] for key in ("weekday", "time", "timezone")
        )
        rule["next_boundary"] = (
            iso(boundary_for(rule, now, future=True))
            if old is None or changed_rule or not old.get("enabled")
            else old["next_boundary"]
        )
        item = self.workspace.put(
            "digest_schedules", project_id, rule, expected_revision=payload.get("expected_revision")
        )
        return {
            "item": item,
            "execution": "Only while this application or daemon is running; missed boundaries are coalesced on next launch.",
        }

    def snapshot(
        self, rule: dict[str, Any], at: datetime | None = None
    ) -> dict[str, dict[str, Any]]:
        def rows(collection: str) -> list[dict[str, Any]]:
            return (
                self.workspace.snapshot_at(collection, int(at.timestamp() * 1000))
                if at is not None
                else all_items(self.workspace, collection)
            )

        meetings = {
            item["id"]: item
            for item in rows("meetings")
            if item.get("project_id") == rule["project_id"]
            and (
                not rule.get("work_package_ids")
                or item.get("work_package_id") in rule["work_package_ids"]
            )
        }
        result: dict[str, dict[str, Any]] = {}
        for collection in ("actions", "artifacts", "questions"):
            for item in rows(collection):
                if item.get("meeting_id") not in meetings:
                    if item.get("project_id") != rule["project_id"] or item.get("meeting_id"):
                        continue
                    if (
                        rule.get("work_package_ids")
                        and item.get("work_package_id") not in rule["work_package_ids"]
                    ):
                        continue
                result[collection + ":" + item["id"]] = {**item, "collection": collection}
        for meeting in meetings.values():
            result["meetings:" + meeting["id"]] = {
                "id": meeting["id"],
                "collection": "meetings",
                "meeting_id": meeting["id"],
                "revision": meeting["revision"],
                "title": meeting.get("title"),
                "transcript_revision": meeting.get("transcript_revision"),
                "note_revision": meeting.get("note_revision"),
                "state": meeting.get("state"),
            }
        return dict(sorted(result.items()))

    def _previous(self, rule: dict[str, Any]) -> dict[str, Any] | None:
        successes = [
            item
            for item in all_items(self.workspace, "digest_runs", {"project_id": rule["project_id"]})
            if item.get("state") == "generated"
            and not item.get("preview")
            and self.public_run(item).get("period_end") == rule.get("last_success_boundary")
        ]
        return successes[-1] if successes else None

    async def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = required(payload, "project_id")
        existing(self.workspace, "projects", project_id)
        rule = self.workspace.get("digest_schedules", project_id)
        if rule is None:
            rule = self.schedule_save(
                {"project_id": project_id, "timezone": "UTC", "enabled": False}
            )["item"]
        end = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        start = (
            instant(rule["last_success_boundary"])
            if rule.get("last_success_boundary")
            else end - timedelta(days=7)
        )
        return {
            "item": await self._generate(
                rule, start, end, self.workspace.new_id(), preview=True, catchup=False
            )
        }

    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        project_id = required(payload, "project_id")
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        async with lock:
            return await self._run(payload)

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule = existing(self.workspace, "digest_schedules", required(payload, "project_id"))
        if not rule.get("enabled"):
            fail(
                "schedule_disabled", "Enable the project schedule before running a scheduled digest"
            )
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        due = instant(rule["next_boundary"])
        if due > now:
            fail("schedule_not_due", "The next digest boundary has not occurred")
        end = boundary_for(rule, now)
        start = (
            instant(rule["last_success_boundary"])
            if rule.get("last_success_boundary")
            else end - timedelta(days=7)
        )
        identity = fingerprint([rule["id"], iso(end)])
        attempt = self.workspace.get("digest_runs", identity)
        if attempt and not payload.get("retry"):
            if (
                attempt.get("state") == "generation-failed"
                or attempt.get("state") == "generating"
                and rule.get("policy")
                and not attempt.get("content", {}).get("no_change")
            ):
                fail(
                    "digest_retry_required",
                    "The prior generation failed or was interrupted. Review its status before explicitly retrying.",
                    run_id=identity,
                )
        try:
            item = await self._generate(
                rule, start, end, identity, preview=False, catchup=due < end
            )
        except Exception as error:
            failed = self.workspace.get("digest_runs", identity)
            if failed and failed.get("state") == "generating":
                save(
                    self.workspace,
                    "digest_runs",
                    identity,
                    {
                        **failed,
                        "state": "generation-failed",
                        "error_code": getattr(error, "code", "generation_failed"),
                    },
                    failed,
                )
            raise
        if item["state"] != "generated":
            return {"item": item}
        latest = existing(self.workspace, "digest_schedules", rule["id"])
        if latest["revision"] != rule["revision"]:
            return {"item": item, "schedule_changed": True, "delivery_skipped": True}
        if latest.get("last_success_boundary") != iso(end):
            save(
                self.workspace,
                "digest_schedules",
                rule["id"],
                {
                    **latest,
                    "last_success_boundary": iso(end),
                    "next_boundary": iso(boundary_for(latest, end, future=True)),
                },
                latest,
            )
        delivery_ids: list[str] = rule.get("delivery_ids", []) if not item.get("stale") else []
        for delivery_id in delivery_ids:
            try:
                await self.delivery.send({"run_id": item["id"], "delivery_id": delivery_id})
            except Exception as error:
                self.workspace.emit(
                    "digest.delivery.failed",
                    {
                        "run_id": item["id"],
                        "delivery_id": delivery_id,
                        "code": getattr(error, "code", "delivery_failed"),
                    },
                )
        return {"item": item}

    async def _generate(
        self,
        rule: dict[str, Any],
        start: datetime,
        end: datetime,
        identity: str,
        *,
        preview: bool,
        catchup: bool,
    ) -> dict[str, Any]:
        existing_run = self.workspace.get("digest_runs", identity)
        if existing_run and existing_run.get("state") == "generated":
            return self.public_run(existing_run)
        project = existing(self.workspace, "projects", rule["project_id"])
        previous = self._previous(rule)
        snapshot = self.snapshot(rule, end)
        scope = {
            "project_id": rule["project_id"],
            "work_package_ids": rule.get("work_package_ids", []),
        }
        baseline = (
            previous["snapshot"]
            if previous and previous.get("scope") == scope
            else self.snapshot(rule, start)
        )
        delta = deterministic_delta(baseline, snapshot, end)
        content = render_digest(project, start, end, delta, catchup=catchup)
        run = save(
            self.workspace,
            "digest_runs",
            identity,
            {
                "id": identity,
                "project_id": rule["project_id"],
                "schedule_id": rule["id"],
                "state": "generating",
                "preview": preview,
                "period_start": iso(start),
                "period_end": iso(end),
                "catchup": catchup,
                "run_key": identity,
                "snapshot": snapshot,
                "snapshot_hash": fingerprint(snapshot),
                "scope": {
                    "project_id": rule["project_id"],
                    "work_package_ids": rule.get("work_package_ids", []),
                },
                "content": content,
            },
            existing_run,
        )
        generation = {"mode": "deterministic"}
        if rule.get("policy") and not delta["no_change"]:
            if self.workspace.inference is None:
                fail(
                    "inference_not_configured",
                    "A configured digest synthesis provider is unavailable",
                )
            evidence = [
                {
                    "id": key,
                    "source_id": key,
                    "text": item.get("description") or item.get("text") or item.get("title") or "",
                    "revision": item.get("revision"),
                    "meeting_id": item.get("meeting_id"),
                }
                for key, item in snapshot.items()
            ]
            generated = await self.workspace.inference.generate(
                "digest",
                evidence,
                rule["policy"],
                context={
                    "period_start": iso(start),
                    "period_end": iso(end),
                    "deterministic_delta": delta,
                },
            )
            result = generated.get("result", generated)
            claims: list[dict[str, Any]] = []
            for section in required(result, "sections"):
                references = section.get("source_refs", [])
                if not isinstance(section.get("body"), str) or not references:
                    fail("invalid_digest", "Every synthesized digest section needs cited text")
                for reference in references:
                    source = snapshot.get(reference.get("source_id"))
                    if source is None or reference.get("revision") != source.get("revision"):
                        fail(
                            "invalid_digest_citation",
                            "Digest synthesis cites an unknown source or revision",
                        )
                claims.append(
                    {
                        "text": section["body"],
                        "heading": section.get("heading"),
                        "source_ids": [reference["source_id"] for reference in references],
                        "source_refs": references,
                    }
                )
            content = dict(content, synthesis=claims)
            if claims:
                content["markdown"] = content["markdown"] + (
                    "\n## Cited synthesis\n\n"
                    + "\n".join(
                        "- " + claim["text"] + " — Sources: " + ", ".join(claim["source_ids"])
                        for claim in claims
                    )
                    + "\n"
                )
            generation = {
                "mode": "inference",
                "prompt_version": generated.get("prompt_version"),
                "input_fingerprint": generated.get("input_fingerprint"),
            }
        stale = fingerprint(self.snapshot(rule)) != run["snapshot_hash"]

        def commit(workspace: Workspace) -> dict[str, Any]:
            item = save(
                workspace,
                "digest_runs",
                identity,
                {
                    **run,
                    "state": "generated",
                    "content": content,
                    "generation": generation,
                    "stale": stale,
                    "generated_at_ms": workspace.now_ms(),
                },
                run,
            )
            if not preview:
                inbox = workspace.get("digest_inbox", identity)
                if inbox is None:
                    save(
                        workspace,
                        "digest_inbox",
                        identity,
                        {
                            "id": identity,
                            "run_id": identity,
                            "project_id": rule["project_id"],
                            "read": False,
                            "created_at_ms": workspace.now_ms(),
                        },
                    )
            return item

        item = self.workspace.atomic(commit)
        if not preview:
            self.workspace.emit(
                "digest.generated",
                {"run_id": identity, "project_id": rule["project_id"], "stale": stale},
                entity_id=identity,
            )
            if self.workspace.native_gateway is not None:
                await native(
                    self.workspace,
                    "native.notify",
                    {
                        "title": content["title"],
                        "body": "A project digest is ready in your local inbox",
                        "action": {
                            "operation_id": "digest.inbox",
                            "payload": {"project_id": rule["project_id"]},
                        },
                    },
                )
        return self.public_run(item)

    def history(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = {"project_id": payload["project_id"]} if payload.get("project_id") else None
        runs = [self.public_run(item) for item in all_items(self.workspace, "digest_runs", filters)]
        deliveries = all_items(self.workspace, "delivery_operations")
        return {
            "items": [
                {
                    **run,
                    "deliveries": [item for item in deliveries if item.get("run_id") == run["id"]],
                }
                for run in sorted(
                    runs, key=lambda item: (item["period_end"], item["id"]), reverse=True
                )
            ]
        }

    def inbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        filters = {"project_id": payload["project_id"]} if payload.get("project_id") else None
        if payload.get("id") and "read" in payload:
            item = existing(self.workspace, "digest_inbox", payload["id"])
            return {
                "item": self.workspace.put(
                    "digest_inbox",
                    item["id"],
                    {**item, "read": bool(payload["read"])},
                    expected_revision=required(payload, "expected_revision"),
                )
            }
        return {
            "items": [
                {**item, "run": self.workspace.get("digest_runs", item["run_id"])}
                for item in all_items(self.workspace, "digest_inbox", filters)
            ]
        }

    async def tick(self) -> None:
        now = datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)
        for rule in all_items(self.workspace, "digest_schedules"):
            if rule.get("enabled") and instant(rule["next_boundary"]) <= now:
                try:
                    await self.run({"project_id": rule["project_id"]})
                except Exception as error:
                    self.workspace.emit(
                        "digest.generation.failed",
                        {
                            "project_id": rule["project_id"],
                            "code": getattr(error, "code", "generation_failed"),
                        },
                    )
        for run in all_items(self.workspace, "digest_runs"):
            if run.get("state") != "generated" or run.get("stale"):
                continue
            scope = run.get("scope", {"project_id": run["project_id"]})
            current = self.snapshot(scope)
            old = run["snapshot"]
            affected = any(
                key not in current or fingerprint(current[key]) != fingerprint(item)
                for key, item in old.items()
            )
            if affected:
                save(
                    self.workspace,
                    "digest_runs",
                    run["id"],
                    {
                        **run,
                        "stale": True,
                        "stale_reason": "Selected evidence was edited or deleted; the historical digest is retained.",
                    },
                    run,
                )

    def invalidate(self, meeting_id: str) -> None:
        for run in all_items(self.workspace, "digest_runs"):
            if run.get("stale"):
                continue
            if any(
                item.get("meeting_id") == meeting_id for item in run.get("snapshot", {}).values()
            ):
                save(
                    self.workspace,
                    "digest_runs",
                    run["id"],
                    {
                        **run,
                        "stale": True,
                        "stale_reason": "Cited meeting evidence changed; historical content is retained.",
                    },
                    run,
                )

    def forget(self, meeting_id: str) -> None:
        """Meeting deletion: drop digest runs, inbox entries and history that carry its content."""
        for run in all_items(self.workspace, "digest_runs"):
            # ponytail: id substring over the serialized run (ids are unique random tokens);
            # store covered meeting ids on the run if runs get large.
            if meeting_id not in json.dumps(run):
                continue
            for collection in ("digest_inbox", "digest_runs"):
                item = self.workspace.get(collection, run["id"])
                if item:
                    self.workspace.delete(collection, run["id"], expected_revision=item["revision"])
            self.workspace.connection.execute(
                "DELETE FROM document_history WHERE entity_id=?", (run["id"],)
            )
