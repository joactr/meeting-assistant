from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.transcripts import all_items
from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import canonical, now_ms
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

CATEGORIES = frozenset(
    {"expired_media", "candidates", "diagnostics", "events", "cache", "voice_profiles"}
)


def register_operations(w: Workspace, r: CommandRegistry) -> None:
    def diagnostics(p: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": {
                "protocol_major": 1,
                "build_id": w.build_id,
                "schema_version": w.connection.execute("PRAGMA user_version").fetchone()[0],
                "database_integrity": w.connection.execute("PRAGMA quick_check").fetchone()[0],
                "job_counts": {
                    row[0]: row[1]
                    for row in w.connection.execute(
                        "SELECT status,COUNT(*) FROM jobs GROUP BY status"
                    )
                },
                "capture_sources": [
                    {"id": s["id"], "state": s["state"], "source_kind": s["source_kind"]}
                    for s in w.list("capture_sources")
                ],
                "privacy_mode": bool(w.settings().get("privacy_mode")),
                "included_data": ["IDs", "timings", "states", "codes"],
                "excluded_data": [
                    "transcript",
                    "audio",
                    "prompts",
                    "keys",
                    "attendees",
                    "vault references",
                ],
                "no_analytics": True,
            }
        }

    r["diagnostics.get"] = diagnostics

    async def diagnostic_export(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("confirm") is True,
            "confirmation_required",
            "Review and explicitly export the local support bundle.",
            status=422,
        )
        content = (canonical(diagnostics(p)) + "\n").encode()
        result = {
            "filename": "meeting-assistant-diagnostics.json",
            "encoding": "base64",
            "content": base64.b64encode(content).decode(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "sensitive_content_included": False,
        }
        if p.get("file_grant"):
            await w.native(
                "file.write",
                {
                    "grant_id": p["file_grant"],
                    "content_base64": result["content"],
                    "filename": result["filename"],
                    "media_type": "application/json",
                },
            )
            result["saved"] = True
        return result

    r["diagnostics.export"] = diagnostic_export

    def preview(p: dict[str, Any]) -> dict[str, Any]:
        raw_selected: object = p.get("categories", ["expired_media"])
        require(
            isinstance(raw_selected, list) and set(cast(list[str], raw_selected)) <= CATEGORIES,
            "invalid_retention_category",
            "Unknown purge category.",
            status=422,
        )
        selected = cast(list[str], raw_selected)
        expired = [
            media
            for media in all_items(w, "media_segments")
            if media.get("retain_until") is not None
            and media["retain_until"] <= now_ms()
            and not media["pinned"]
            and media["state"] != "purged"
        ]
        candidates = [
            candidate
            for candidate in all_items(w, "analysis_results", {"state": "candidate"})
            if candidate["created_at"] < now_ms() - 30 * 86400000
            and not candidate.get("pinned", False)
        ]
        counts = {
            "expired_media": len(expired),
            "candidates": len(candidates),
            "events": w.connection.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0],
            "voice_profiles": len(all_items(w, "voice_profiles")),
            "diagnostics": len(list((w.root / "logs").glob("*.log")))
            if (w.root / "logs").is_dir()
            else 0,
            "cache": len(list((w.root / "cache").glob("*"))) if (w.root / "cache").is_dir() else 0,
        }
        return {
            "categories": selected,
            "counts": {key: counts[key] for key in selected},
            "bytes": sum(media["bytes"] for media in expired) if "expired_media" in selected else 0,
            "transcripts_deleted": False,
            "warning": "Unlinking and SQLite compaction are not secure erasure guarantees for SSDs or backups.",
        }

    r["retention.preview"] = preview

    def purge(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("confirm") is True,
            "confirmation_required",
            "Explicit purge confirmation is required.",
            status=422,
        )
        plan = preview(p)
        selected = plan["categories"]
        paths: list[str] = []
        with w.transaction():
            if "expired_media" in selected:
                for media in all_items(w, "media_segments"):
                    if (
                        media.get("retain_until") is not None
                        and media["retain_until"] <= now_ms()
                        and not media["pinned"]
                        and media["state"] not in ("purged", "purge_pending")
                    ):
                        w.put(
                            "media_segments",
                            media["id"],
                            {"state": "purge_pending"},
                            expected_revision=media["revision"],
                        )
                        paths.append(media["relative_path"])
            if "candidates" in selected:
                for candidate in all_items(w, "analysis_results", {"state": "candidate"}):
                    if candidate["created_at"] < now_ms() - 30 * 86400000 and not candidate.get(
                        "pinned", False
                    ):
                        w.delete(
                            "analysis_results",
                            candidate["id"],
                            expected_revision=candidate["revision"],
                        )
                        w.connection.execute(
                            "DELETE FROM document_history WHERE entity_id=?", (candidate["id"],)
                        )
                for collection in ("minutes_candidates",):
                    for candidate in all_items(w, collection):
                        if (
                            candidate["created_at"] < now_ms() - 30 * 86400000
                            and candidate.get("state") == "candidate"
                        ):
                            w.delete(
                                collection, candidate["id"], expected_revision=candidate["revision"]
                            )
                            w.connection.execute(
                                "DELETE FROM document_history WHERE entity_id=?", (candidate["id"],)
                            )
            if "events" in selected:
                w.connection.execute("DELETE FROM outbox_events")
            if "voice_profiles" in selected:
                for profile in all_items(w, "voice_profiles"):
                    r["voice_profiles.delete"](
                        {"id": profile["id"], "expected_revision": profile["revision"]}
                    )
            for category, directory in (("diagnostics", "logs"), ("cache", "cache")):
                if category in selected and (w.root / directory).is_dir():
                    for path in (w.root / directory).rglob("*"):
                        if path.is_file() and path.resolve().is_relative_to(w.root):
                            paths.append(str(path.relative_to(w.root)))
            job = w.queue_job("purge_media", {"paths": paths}) if paths else None
            w.emit("retention.purged", {"categories": selected, "pending_files": len(paths)})
        return {**plan, "job": job, "purged": True}

    r["retention.purge"] = purge
