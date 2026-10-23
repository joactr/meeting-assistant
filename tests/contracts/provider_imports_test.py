"""Deterministic safety contracts, not credentialed or real-model inference evidence."""

import asyncio
import hashlib
import json
import zipfile

import httpx
import pytest
from meeting_assistant.application.inference.catalog import CatalogService
from meeting_assistant.application.inference.command_models import Download, ImportModel
from meeting_assistant.application.inference.deadlines import interpret_deadline
from meeting_assistant.infrastructure.providers.contracts import ProviderError
from pydantic import ValidationError


def template(root, content):
    models = root / "catalog" / "models"
    models.mkdir(parents=True)
    value = {
        "id": "fixture-template",
        "role": "embeddings",
        "revision": "fixture-source-revision",
        "files": [
            {
                "path": "model.onnx",
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "url": "https://huggingface.co/fixture/model.onnx",
            }
        ],
    }
    (models / "fixture.json").write_text(json.dumps(value), encoding="utf-8")
    archive = root / "user.zip"
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("model.onnx", content)
    return archive


def test_manual_import_computes_hash_and_labels_unreviewed(tmp_path):
    content = b"Non-executable deterministic fixture bytes; not a model."
    archive = template(tmp_path, content)

    async def scenario():
        async with httpx.AsyncClient() as client:
            service = CatalogService(tmp_path / "catalog", tmp_path / "inference", client)
            result = await service.import_model(
                "fixture-template", archive, accepted_unreviewed=True, accepted_license=True
            )
            entry = service.entry(result["id"])
            assert entry["trust"] == "user-supplied-unreviewed"
            assert entry["files"][0]["sha256"] == hashlib.sha256(content).hexdigest()
            assert entry["files"][0]["url"] is None
            assert (
                tmp_path / "inference" / result["id"] / result["revision"] / "model.onnx"
            ).read_bytes() == content
            with pytest.raises(ProviderError) as failure:
                await service.download(
                    result["id"], accepted_license=True, accepted_destination=True
                )
            assert failure.value.code == "invalid_input"

    asyncio.run(scenario())


@pytest.mark.parametrize("content", [b"MZfixture", b"\x7fELFfixture", b"\xcf\xfa\xed\xfefixture"])
def test_manual_import_never_activates_disguised_executables(tmp_path, content):
    archive = template(tmp_path, content)

    async def scenario():
        async with httpx.AsyncClient() as client:
            service = CatalogService(tmp_path / "catalog", tmp_path / "inference", client)
            with pytest.raises(ProviderError) as failure:
                await service.import_model(
                    "fixture-template", archive, accepted_unreviewed=True, accepted_license=True
                )
            assert failure.value.code == "permission"
            assert not list((tmp_path / "inference").glob("user-*/active.json"))
            assert not list((tmp_path / "inference").glob(".import-*"))

    asyncio.run(scenario())


def test_import_consent_requires_actual_json_booleans():
    with pytest.raises(ValidationError):
        ImportModel(
            template_id="model", grant_id="grant", accepted_unreviewed="true", accepted_license=True
        )
    with pytest.raises(ValidationError):
        Download(id="model", accepted_license=True, accepted_destination="yes")


@pytest.mark.parametrize("wording", ["tomorrow", "mañana"])
def test_relative_deadline_uses_meeting_date_not_rerun(wording):
    result = interpret_deadline(
        wording, {"meeting_local_date": "2025-02-28", "meeting_timezone": "UTC"}
    )
    assert result["due_date"] == "2025-03-01"


def test_ambiguous_deadline_stays_null_without_meeting_timezone():
    assert interpret_deadline("tomorrow", {"meeting_local_date": "2025-02-28"})["due_date"] is None
    assert (
        interpret_deadline(
            "next month", {"meeting_local_date": "2025-02-28", "meeting_timezone": "UTC"}
        )["due_date"]
        is None
    )
