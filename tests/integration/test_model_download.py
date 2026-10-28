from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from meeting_assistant.application.inference.catalog import CatalogService
from meeting_assistant.workspace import Workspace

MODEL = "whisper-small-multilingual"
PAYLOAD = {"id": MODEL, "accepted_license": True, "accepted_destination": True}


@pytest.fixture
def workspace(tmp_path: Path):
    w = Workspace(tmp_path / "workspace" / "workspace.sqlite3")
    try:
        yield w
    finally:
        w.close()


async def entry_state(w: Workspace) -> dict | None:
    items = (await w.adispatch("catalog.list", {}))["items"]
    return next(item for item in items if item["id"] == MODEL).get("download")


async def settled(w: Workspace) -> dict | None:
    for _ in range(100):
        state = await entry_state(w)
        if state is None or state["state"] in {"failed"}:
            return state
        await asyncio.sleep(0.02)
    raise AssertionError("download never settled")


async def test_unexpected_error_fails_the_download_and_allows_retry(workspace, monkeypatch):
    calls = 0

    async def boom(self, identity, **kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("disk exploded")

    monkeypatch.setattr(CatalogService, "download", boom)
    assert (await workspace.adispatch("catalog.download", PAYLOAD))["started"]
    failed = await settled(workspace)
    assert failed and failed["state"] == "failed" and "disk exploded" in failed["error"]

    # A failed download must not block another attempt.
    assert (await workspace.adispatch("catalog.download", PAYLOAD))["started"]
    await settled(workspace)
    assert calls == 2
