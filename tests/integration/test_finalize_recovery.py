"""A failed AI task never strands a meeting; recovery and error reporting stay actionable."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from meeting_assistant.infrastructure.providers.contracts import ProviderError
from meeting_assistant.workspace import Workspace

EMPTY: dict[str, dict[str, Any]] = {
    "summary": {"sections": [], "citations": []},
    "actions": {"candidates": []},
    "topics": {"topics": []},
    "artifacts": {
        "decisions": [],
        "risks": [],
        "milestones": [],
        "deliverables": [],
        "follow_ups": [],
    },
    "memory": {"sections": [], "warnings": []},
    "minutes": {"sections": []},
}


class Inference:
    def __init__(self, failing: set[str]) -> None:
        self.failing = failing

    async def generate(
        self, task: str, evidence: Any, policy: Any, *, language: str, context: Any
    ) -> dict[str, Any]:
        if task in self.failing:
            raise ProviderError(
                "invalid_output",
                "The model's summary output was rejected (sections.0.body: Field required).",
                retryable=True,
            )
        return {"result": EMPTY[task]}


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


def interrupted_meeting(w: Workspace) -> dict[str, Any]:
    meeting = w.dispatch(
        "meetings.create",
        {"title": "Recovery", "source": "text", "config": {"ai_policy": {"x": 1}}},
    )["item"]
    return w.put(
        "meetings",
        meeting["id"],
        {"state": "interrupted"},
        expected_revision=meeting["revision"],
    )


async def until(predicate: Any) -> None:
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.05)


def statuses(w: Workspace, meeting_id: str) -> dict[str, dict[str, Any]]:
    items = w.dispatch("analysis.status", {"meeting_id": meeting_id})["items"]
    return {item["task"]: item for item in items}


async def test_failed_ai_task_still_completes_meeting_and_is_retryable(
    workspace: Workspace,
) -> None:
    w = workspace
    meeting = interrupted_meeting(w)
    model = Inference({"summary"})
    w.inference = model  # type: ignore[assignment]
    await w.adispatch("meetings.stop", {"meeting_id": meeting["id"], "idempotency_key": "stop"})
    loop = asyncio.create_task(w._job_loop())
    try:
        await until(lambda: w.require_item("meetings", meeting["id"])["state"] == "complete")
        done = w.require_item("meetings", meeting["id"])
        assert done["health"] == "degraded"
        jobs = w.list("jobs", {"kind": "finalize"})
        assert all(j["status"] in ("succeeded", "skipped") for j in jobs)
        rows = statuses(w, meeting["id"])
        assert rows["summary"]["status"] == "failed"
        assert rows["summary"]["error"].startswith("The model's summary output was rejected")
        assert rows["summary"]["error_code"] == "invalid_output"
        assert rows["actions"]["status"] == "succeeded"
        assert rows["minutes"]["status"] == "succeeded"

        # Retrying while the model still fails re-runs the earlier attempt instead of no-op.
        first = (await w.adispatch("analysis.generate", {"meeting_id": meeting["id"]}))["job"]
        await until(lambda: w.require_item("jobs", first["id"])["status"] == "failed")
        again = (await w.adispatch("analysis.generate", {"meeting_id": meeting["id"]}))["job"]
        assert again["id"] == first["id"] and again["status"] == "queued"
        await until(lambda: w.require_item("jobs", first["id"])["status"] == "failed")
        model.failing.clear()
        await w.adispatch("analysis.generate", {"meeting_id": meeting["id"]})
        await until(lambda: statuses(w, meeting["id"])["summary"]["status"] == "succeeded")
        assert statuses(w, meeting["id"])["summary"]["error"] is None
    finally:
        w._stopping = True
        await loop


def test_processing_meeting_without_live_job_is_recovered(tmp_path: Path) -> None:
    path = tmp_path / "workspace" / "workspace.sqlite3"
    w = Workspace(path)
    meeting = interrupted_meeting(w)
    asyncio.run(w.adispatch("meetings.stop", {"meeting_id": meeting["id"], "idempotency_key": "a"}))
    drain = next(j for j in w.list("jobs", {"kind": "finalize"}) if j["stage"] == "drain")
    w.put(
        "jobs",
        drain["id"],
        {"status": "failed", "error": {"code": "x", "message": "boom"}},
        expected_revision=drain["revision"],
    )
    assert w.require_item("meetings", meeting["id"])["state"] == "processing"
    w.close()

    w = Workspace(path)  # engine restart: nothing is running, a job is failed
    try:
        assert w.require_item("meetings", meeting["id"])["state"] == "attention_required"
        asyncio.run(
            w.adispatch("meetings.stop", {"meeting_id": meeting["id"], "idempotency_key": "b"})
        )
        assert w.require_item("meetings", meeting["id"])["state"] == "processing"
        assert w.require_item("jobs", drain["id"])["status"] == "queued"
        noop = asyncio.run(
            w.adispatch("meetings.stop", {"meeting_id": meeting["id"], "idempotency_key": "c"})
        )
        assert noop["item"]["state"] == "processing"
    finally:
        w.close()
