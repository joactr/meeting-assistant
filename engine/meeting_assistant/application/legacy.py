from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import tempfile
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.transcripts import assign_speaker
from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, fingerprint, new_id, now_ms
from meeting_assistant.domain.ports import JSONDocument

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

SUPPORTED = frozenset(
    {
        "sessions",
        "meetings",
        "utterances",
        "transcripts",
        "segments",
        "transcript_segments",
        "actions",
        "action_items",
        "speakers",
        "meeting_speakers",
        "projects",
        "work_packages",
        "taxonomy_nodes",
        "questions",
        "topics",
        "artifacts",
        "project_artifacts",
        "important_moments",
        "bookmarks",
        "summaries",
        "minutes",
        "memory_segments",
        "briefings",
        "meeting_briefings",
        "templates",
        "settings",
        "workspace_settings",
        "voice_profiles",
        "speaker_profiles",
        "words",
        "meeting_finalizations",
        "final_analysis_promotions",
        "schema_version",
    }
)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextlib.contextmanager
def source_snapshot(path: Path) -> Generator[tuple[sqlite3.Connection, str]]:
    require(path.is_file(), "source_missing", "Granted legacy source does not exist.", status=404)
    require(
        path.stat().st_size <= 4 * 1024**3,
        "source_too_large",
        "Legacy database exceeds 4 GiB.",
        status=413,
    )
    original = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        with tempfile.TemporaryDirectory(prefix="ma-legacy-snapshot-") as temp:
            snapshot = Path(temp) / "source.sqlite3"
            destination = sqlite3.connect(snapshot)
            try:
                original.backup(destination)
            finally:
                destination.close()
            fingerprint_ = hash_file(snapshot)
            readonly = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True)
            readonly.row_factory = sqlite3.Row
            try:
                require(
                    readonly.execute("PRAGMA quick_check").fetchone()[0] == "ok",
                    "source_corrupt",
                    "Legacy SQLite integrity validation failed.",
                    status=422,
                )
                yield readonly, fingerprint_
            finally:
                readonly.close()
    finally:
        original.close()


def source_tables(source: sqlite3.Connection) -> set[str]:
    return {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def report_preview(source: sqlite3.Connection, source_fingerprint: str) -> JSONDocument:
    tables = source_tables(source)
    require(
        bool(tables & {"sessions", "meetings"}),
        "unsupported_legacy",
        "No recognized sessions or meetings table.",
        status=422,
    )
    counts: dict[str, int] = {
        table: source.execute("SELECT COUNT(*) FROM " + quote_identifier(table)).fetchone()[0]
        for table in sorted(tables & SUPPORTED)
    }
    columns: dict[str, list[str]] = {
        table: [
            row[1] for row in source.execute("PRAGMA table_info(" + quote_identifier(table) + ")")
        ]
        for table in sorted(tables & SUPPORTED)
    }
    return {
        "source_fingerprint": source_fingerprint,
        "source_schema": columns,
        "counts": counts,
        "missing_optional_tables": sorted(
            {"words", "speaker_profiles", "questions", "topics", "templates"} - tables
        ),
        "requires_source_stopped_for_final_cutover": True,
        "secrets_imported": False,
        "rejected": [],
        "conflicts": [],
        "source_unchanged": True,
    }


def legacy_preview(path: Path) -> JSONDocument:
    with source_snapshot(path) as (source, source_fingerprint):
        return report_preview(source, source_fingerprint)


def milliseconds(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def decode_json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def import_legacy(w: Workspace, path: Path, p: JSONDocument) -> JSONDocument:
    require(
        p.get("source_stopped") is True and p.get("confirm") is True,
        "import_confirmation_required",
        "Stop the source app and explicitly accept the reviewed import.",
        status=422,
    )
    with source_snapshot(path) as (source, source_fingerprint):
        preview = report_preview(source, source_fingerprint)
        previous = w.list("legacy_imports", {"source_fingerprint": source_fingerprint})
        if previous:
            return {"item": previous[0], "already_imported": True}
        if p.get("source_fingerprint"):
            require(
                p["source_fingerprint"] == source_fingerprint,
                "source_changed",
                "Legacy source changed after preview. Review it again.",
                status=409,
            )
        tables = source_tables(source)
        mapping: dict[str, dict[str, str]] = {}
        counts: dict[str, int] = {}
        rejected: list[JSONDocument] = []
        warnings: list[str] = []
        run_by_meeting: dict[str, str] = {}
        speaker_by_label: dict[tuple[str, str], str] = {}

        def rows(table: str) -> Generator[JSONDocument]:
            if table in tables:
                for row in source.execute("SELECT * FROM " + quote_identifier(table)):
                    yield dict(row)

        def old_id(row: JSONDocument) -> str:
            return str(row.get("id", row.get("session_id", new_id())))

        def mapped(kind: str, value: Any) -> str | None:
            return mapping.get(kind, {}).get(str(value)) if value is not None else None

        def meeting_id(row: JSONDocument) -> str | None:
            return mapped("meetings", row.get("meeting_id", row.get("session_id")))

        def reject(table: str, row: JSONDocument, code: str) -> None:
            rejected.append({"table": table, "source_id": old_id(row), "code": code})

        def save(
            kind: str, row: JSONDocument, data: JSONDocument, *, key: str | None = None
        ) -> JSONDocument | None:
            try:
                item = w.put(kind, new_id(), data)
                mapping.setdefault(kind, {})[key or old_id(row)] = item["id"]
                counts[kind] = counts.get(kind, 0) + 1
                return item
            except (AppError, ValueError, TypeError) as exc:
                reject(kind, row, exc.code if isinstance(exc, AppError) else "invalid_value")
                return None

        def imported_result(mid: str, kind: str, payload: Any) -> str:
            meeting = w.require_item("meetings", mid)
            run = w.put(
                "analysis_runs",
                new_id(),
                {
                    "meeting_id": mid,
                    "task": kind,
                    "input_revision": meeting["transcript_revision"],
                    "note_revision": meeting["note_revision"],
                    "watermark": meeting["transcript_revision"],
                    "policy_hash": meeting["policy_hash"],
                    "status": "succeeded",
                    "provider_fingerprint": "legacy-import",
                    "model_fingerprint": "legacy-unidentified",
                    "prompt_version": "legacy-preserved",
                },
            )
            result = w.put(
                "analysis_results",
                new_id(),
                {
                    "run_id": run["id"],
                    "kind": kind,
                    "payload": payload,
                    "state": "promoted",
                    "language": "auto",
                    "content_hash": fingerprint(payload),
                    "promoted_at": now_ms(),
                },
            )
            counts["analysis_results"] = counts.get("analysis_results", 0) + 1
            return result["id"]

        with w.transaction():
            for row in rows("projects"):
                save(
                    "projects",
                    row,
                    {
                        "name": row.get("name", row.get("title", "Imported project")),
                        "description": row.get("description", ""),
                    },
                )
            taxonomy = list(rows("taxonomy_nodes"))
            pending = list(taxonomy)
            while pending:
                ready = [
                    row
                    for row in pending
                    if row.get("parent_id") is None or mapped("taxonomy", row.get("parent_id"))
                ]
                if not ready:
                    for row in pending:
                        reject("taxonomy_nodes", row, "unresolved_parent")
                    break
                for row in ready:
                    parent = mapped("taxonomy", row.get("parent_id"))
                    node_type = str(row.get("node_type", "project")).casefold()
                    if parent and node_type in ("subcategory", "work_package", "workpackage"):
                        root_project = mapped("taxonomy_project", row.get("parent_id")) or parent
                        item = save(
                            "work_packages",
                            row,
                            {
                                "project_id": root_project,
                                "name": row.get("name", "Imported work package"),
                                "description": "Imported legacy taxonomy scope.",
                            },
                        )
                    else:
                        item = save(
                            "projects",
                            row,
                            {
                                "name": row.get("name", "Imported project"),
                                "description": "Imported taxonomy " + node_type,
                                "legacy_parent_id": parent,
                            },
                        )
                        root_project = item["id"] if item else None
                    if item:
                        assert root_project is not None
                        mapping.setdefault("taxonomy", {})[old_id(row)] = item["id"]
                        mapping.setdefault("taxonomy_project", {})[old_id(row)] = root_project
                    pending.remove(row)
            for row in rows("work_packages"):
                project = mapped("projects", row.get("project_id")) or mapped(
                    "taxonomy_project", row.get("project_id")
                )
                if project:
                    save(
                        "work_packages",
                        row,
                        {
                            "project_id": project,
                            "name": row.get("name", "Imported work package"),
                            "description": row.get("description", ""),
                        },
                    )
                else:
                    reject("work_packages", row, "unresolved_project")
            for row in rows("templates"):
                template = save(
                    "templates",
                    row,
                    {
                        "name": row.get("name", "Imported template"),
                        "sections": decode_json(
                            row.get("section_schema_json", row.get("sections_json")), []
                        ),
                        "legacy_fields": {k: v for k, v in row.items() if k not in ("id", "name")},
                    },
                )
                if template:
                    version = w.put(
                        "template_versions",
                        new_id(),
                        {
                            "template_id": template["id"],
                            "version": 1,
                            "section_schema": {"sections": template.get("sections", [])},
                            "extraction_policy": decode_json(row.get("extraction_policy_json"), {}),
                            "content_hash": fingerprint(template.get("sections", [])),
                        },
                    )
                    w.put(
                        "templates",
                        template["id"],
                        {"current_version_id": version["id"]},
                        expected_revision=template["revision"],
                    )
            meeting_table = "meetings" if "meetings" in tables else "sessions"
            for row in rows(meeting_table):
                ended = milliseconds(row.get("ended_at_ms", row.get("ended_at")))
                started = milliseconds(row.get("started_at_ms", row.get("started_at")))
                status = str(
                    row.get("state", row.get("status", "complete" if ended else "interrupted"))
                )
                state = (
                    "complete"
                    if status in ("complete", "completed", "finished")
                    and (ended is not None or "status" in row or "state" in row)
                    else "interrupted"
                )
                project = (
                    mapped("projects", row.get("project_id"))
                    or mapped("taxonomy_project", row.get("project_id"))
                    or mapped("taxonomy_project", row.get("category_id"))
                )
                package = mapped("work_packages", row.get("work_package_id")) or mapped(
                    "work_packages", row.get("subcategory_id")
                )
                config = decode_json(
                    row.get("config_snapshot", row.get("config_snapshot_json")), {}
                )
                preferences = {
                    key: config[key]
                    for key in ("always_on_top", "tray", "theme", "language", "output_language")
                    if key in config
                }
                sanitized = {
                    "source": "text",
                    "recording": False,
                    "legacy_device_reselection_required": True,
                    "legacy_provider_reselection_required": True,
                    "imported_preferences": preferences,
                }
                item = save(
                    "meetings",
                    row,
                    {
                        "title": row.get("title", row.get("name", "Imported meeting")),
                        "state": state,
                        "health": "degraded" if state == "interrupted" else "healthy",
                        "started_at": started,
                        "ended_at": ended,
                        "elapsed_ms": max(0, (ended or started or 0) - (started or 0)),
                        "project_id": project,
                        "work_package_id": package,
                        "config_snapshot": sanitized,
                        "policy_hash": fingerprint(sanitized),
                        "interrupted_reason": "Imported unfinished capture; preserved text can be finalized. Audio not present in the source is not recoverable."
                        if state == "interrupted"
                        else None,
                        "legacy_source_id": old_id(row),
                        "legacy_template": row.get("template"),
                    },
                )
                if item:
                    run = w.put(
                        "transcript_runs",
                        new_id(),
                        {
                            "meeting_id": item["id"],
                            "source": "import",
                            "state": "active",
                            "provider_fingerprint": "legacy-import",
                            "model_fingerprint": "legacy-unidentified",
                        },
                    )
                    run_by_meeting[item["id"]] = run["id"]
            for table in ("speakers", "meeting_speakers"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    label = str(row.get("speaker_label", row.get("id", "unknown")))
                    speaker = save(
                        "meeting_speakers",
                        row,
                        {
                            "meeting_id": mid,
                            "display_name": row.get("display_name", row.get("name", "Unknown")),
                            "source_kind": row.get("source_kind", "system"),
                            "color_token": row.get("color", "speaker-1"),
                            "manual_name_lock": 1,
                        },
                        key=mid + ":" + old_id(row),
                    )
                    if speaker:
                        speaker_by_label[(mid, label)] = speaker["id"]
            transcript_table = next(
                (
                    table
                    for table in ("utterances", "transcript_segments", "transcripts", "segments")
                    if table in tables
                ),
                None,
            )
            if transcript_table:
                for row in rows(transcript_table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(transcript_table, row, "unresolved_meeting")
                        continue
                    text = (
                        row.get("edited_text")
                        or row.get("corrected_text")
                        or row.get("text", row.get("content", ""))
                    )
                    start = row.get("start_ms", int(float(row.get("start", 0)) * 1000))
                    end = row.get("end_ms", int(float(row.get("end", start / 1000)) * 1000))
                    utterance = save(
                        "utterances",
                        row,
                        {
                            "meeting_id": mid,
                            "run_id": run_by_meeting[mid],
                            "provider_stream_id": "legacy-import",
                            "provider_result_id": old_id(row),
                            "start_ms": start,
                            "end_ms": end,
                            "text": text,
                            "language": row.get("language"),
                            "confidence": row.get("confidence"),
                            "is_final": int(bool(row.get("is_final", True))),
                            "manual_text_lock": int(
                                bool(row.get("edited", row.get("manual_text_lock", False)))
                                or bool(row.get("edited_text"))
                                or bool(row.get("corrected_text"))
                            ),
                            "timing_provenance": "legacy-preserved",
                        },
                    )
                    if not utterance:
                        continue
                    label = str(
                        row.get(
                            "speaker_label", row.get("speaker_id", row.get("speaker", "Unknown"))
                        )
                    )
                    sid = speaker_by_label.get((mid, label))
                    if not sid:
                        speaker = save(
                            "meeting_speakers",
                            {"id": mid + ":" + label},
                            {
                                "meeting_id": mid,
                                "display_name": label
                                if label.casefold() not in ("unknown", "none", "null", "")
                                else "Unknown",
                                "source_kind": "mic"
                                if label.casefold() in ("me", "mic", "microphone")
                                else "system",
                                "manual_name_lock": 1,
                            },
                            key=mid + ":" + label,
                        )
                        sid = speaker["id"] if speaker else None
                        if sid:
                            speaker_by_label[(mid, label)] = sid
                    if sid:
                        assign_speaker(w, utterance, sid, manual=True)
                    words: object = decode_json(
                        row.get("word_timestamps", row.get("words_json")), []
                    )
                    for ordinal, raw_word in enumerate(
                        cast(list[object], words) if isinstance(words, list) else []
                    ):
                        if not isinstance(raw_word, dict):
                            reject("words", row, "invalid_word")
                            continue
                        word = cast(JSONDocument, raw_word)
                        a = word.get(
                            "start_ms",
                            round(float(word["start"]) * 1000)
                            if word.get("start") is not None
                            else None,
                        )
                        b = word.get(
                            "end_ms",
                            round(float(word["end"]) * 1000)
                            if word.get("end") is not None
                            else None,
                        )
                        if a is not None and (b is None or not start - 20 <= a <= b <= end + 20):
                            reject("words", row, "invalid_span")
                            continue
                        w.connection.execute(
                            "INSERT INTO words VALUES(?,?,?,?,?,?,?)",
                            (
                                utterance["id"],
                                ordinal,
                                word.get("word", word.get("text", "")),
                                a,
                                b,
                                word.get("confidence", word.get("probability")),
                                sid,
                            ),
                        )
                        counts["words"] = counts.get("words", 0) + 1
                    w.put(
                        "utterance_revisions",
                        new_id(),
                        {
                            "utterance_id": utterance["id"],
                            "actor": "import",
                            "before": {},
                            "after": utterance,
                            "revision": 1,
                        },
                    )
            for row in rows("words"):
                uid = mapped("utterances", row.get("utterance_id", row.get("segment_id")))
                if not uid:
                    reject("words", row, "unresolved_utterance")
                    continue
                u = w.require_item("utterances", uid)
                a = row.get("start_ms")
                b = row.get("end_ms")
                if a is not None and (
                    b is None or not u["start_ms"] - 20 <= a <= b <= u["end_ms"] + 20
                ):
                    reject("words", row, "invalid_span")
                    continue
                ordinal = row.get(
                    "ordinal",
                    w.connection.execute(
                        "SELECT COUNT(*) FROM words WHERE utterance_id=?", (uid,)
                    ).fetchone()[0],
                )
                w.connection.execute(
                    "INSERT OR IGNORE INTO words VALUES(?,?,?,?,?,?,NULL)",
                    (
                        uid,
                        ordinal,
                        row.get("text", row.get("word", "")),
                        a,
                        b,
                        row.get("confidence"),
                    ),
                )
            for table in ("actions", "action_items"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    status_aliases: dict[str | None, str] = {
                        "completed": "done",
                        "pending": "open",
                        "in-progress": "in_progress",
                        "cleared": "dismissed",
                    }
                    status = status_aliases.get(row.get("status"), row.get("status", "open"))
                    action = save(
                        "actions",
                        row,
                        {
                            "meeting_id": mid,
                            "stable_origin_key": "legacy:" + table + ":" + old_id(row),
                            "description": row.get(
                                "description", row.get("text", "Imported action")
                            ),
                            "status": status,
                            "priority": row.get("priority") or "normal",
                            "assignee_text": row.get("assignee_text", row.get("assignee")),
                            "due_date": row.get("due_date"),
                            "deadline_text": row.get("deadline", row.get("deadline_text")),
                            "timezone": row.get("timezone"),
                            "manual_fields": [
                                "description",
                                "status",
                                "priority",
                                "assignee_text",
                                "due_date",
                                "deadline_text",
                            ],
                        },
                    )
                    uid = mapped(
                        "utterances", row.get("source_segment_id", row.get("utterance_id"))
                    )
                    if action and uid:
                        u = w.require_item("utterances", uid)
                        w.put(
                            "entity_sources",
                            new_id(),
                            {
                                "action_id": action["id"],
                                "meeting_id": mid,
                                "utterance_id": uid,
                                "source_revision": u["text_revision"],
                                "start_ms": u["start_ms"],
                                "end_ms": u["end_ms"],
                                "quote_snapshot": u["text"],
                            },
                        )
            for table in ("important_moments", "bookmarks"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    save(
                        "important_moments",
                        row,
                        {
                            "meeting_id": mid,
                            "utterance_id": mapped(
                                "utterances", row.get("utterance_id", row.get("segment_id"))
                            ),
                            "start_ms": row.get("start_ms", 0),
                            "label": row.get("label", row.get("title", "Important moment")),
                            "quote_snapshot": row.get("quote_snapshot", row.get("quote", "")),
                            "note": row.get("note", ""),
                            "include_in_minutes": int(bool(row.get("include_in_minutes", True))),
                            "status": row.get("status", "open"),
                            "priority": row.get("priority") or "normal",
                            "assignee": row.get("assignee"),
                            "due_date": row.get("due_date"),
                        },
                    )
            for row in rows("questions"):
                mid = meeting_id(row)
                if not mid:
                    reject("questions", row, "unresolved_meeting")
                    continue
                save(
                    "questions",
                    row,
                    {
                        "meeting_id": mid,
                        "utterance_id": mapped(
                            "utterances", row.get("utterance_id", row.get("segment_id"))
                        ),
                        "text": row.get("text", row.get("question_text", row.get("question", ""))),
                        "status": row.get(
                            "status", "answered" if row.get("is_answered") else "open"
                        ),
                        "answered_at": milliseconds(row.get("answered_at")),
                    },
                )
            for row in rows("topics"):
                mid = meeting_id(row)
                if not mid:
                    reject("topics", row, "unresolved_meeting")
                    continue
                topic = save(
                    "topics",
                    row,
                    {
                        "meeting_id": mid,
                        "title": row.get("title", row.get("name", "Topic")),
                        "start_ms": row.get("start_ms", 0),
                        "end_ms": row.get("end_ms", row.get("start_ms", 0)),
                        "sentiment": row.get("sentiment"),
                    },
                )
                if topic:
                    for child in decode_json(row.get("subtopics_json"), []):
                        value: JSONDocument = (
                            cast(JSONDocument, child)
                            if isinstance(child, dict)
                            else {"title": str(child)}
                        )
                        save(
                            "topics",
                            {"id": old_id(row) + ":" + str(cast(object, child))},
                            {
                                "meeting_id": mid,
                                "parent_id": topic["id"],
                                "title": value.get("title", value.get("name", "Subtopic")),
                                "start_ms": value.get("start_ms", topic["start_ms"]),
                                "end_ms": value.get("end_ms", topic["end_ms"]),
                            },
                        )
            for table in ("artifacts", "project_artifacts"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    kind = str(row.get("kind", row.get("artifact_type", "follow_up"))).replace(
                        "-", "_"
                    )
                    kind = {
                        "decisions": "decision",
                        "risks": "risk",
                        "milestones": "milestone",
                        "deliverables": "deliverable",
                        "followups": "follow_up",
                    }.get(kind, kind)
                    save(
                        "artifacts",
                        row,
                        {
                            "meeting_id": mid,
                            "project_id": w.require_item("meetings", mid).get("project_id"),
                            "kind": kind,
                            "title": row.get("title", row.get("description", "Imported outcome")),
                            "description": row.get("description", ""),
                            "status": row.get("status", "open"),
                            "owner_text": row.get("owner"),
                            "date": row.get("date", row.get("due_date")),
                            "payload": decode_json(row.get("metadata_json"), {}),
                        },
                    )
            for table in ("summaries", "minutes"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    kind = str(
                        row.get("summary_type", "minutes" if table == "minutes" else "summary")
                    )
                    kind = "minutes" if "minute" in kind else "summary"
                    content = decode_json(
                        row.get("content", row.get("payload_json")),
                        {"text": row.get("content", row.get("text", ""))},
                    )
                    result_id = imported_result(mid, kind, content)
                    if kind == "minutes":
                        approved = w.get("minutes_approved", mid)
                        w.put(
                            "minutes_approved",
                            mid,
                            {
                                "meeting_id": mid,
                                "state": "approved",
                                "legacy_preserved": True,
                                "sections": [
                                    {
                                        "id": new_id(),
                                        "title": "Imported minutes",
                                        "text": cast(JSONDocument, content).get(
                                            "text", canonical(cast(JSONDocument, content))
                                        )
                                        if isinstance(content, dict)
                                        else str(content),
                                        "source": "Legacy preserved",
                                        "citations": [],
                                        "note_ids": [],
                                    }
                                ],
                                "source_result_id": result_id,
                            },
                            expected_revision=approved["revision"] if approved else None,
                        )
            for row in rows("memory_segments"):
                mid = meeting_id(row)
                if not mid:
                    reject("memory_segments", row, "unresolved_meeting")
                    continue
                meeting = w.require_item("meetings", mid)
                result_id = imported_result(
                    mid,
                    "memory",
                    {
                        "text": row.get("summary", ""),
                        "legacy_metadata": {
                            key: decode_json(row.get(key), [])
                            for key in (
                                "key_decisions_json",
                                "action_items_json",
                                "speaker_stats_json",
                            )
                        },
                    },
                )
                save(
                    "memory_segments",
                    row,
                    {
                        "meeting_id": mid,
                        "project_id": meeting.get("project_id"),
                        "work_package_id": meeting.get("work_package_id"),
                        "result_id": result_id,
                        "input_revision": meeting["transcript_revision"],
                        "summary": row.get("summary", ""),
                        "stale": 1,
                    },
                )
            for table in ("briefings", "meeting_briefings"):
                for row in rows(table):
                    mid = meeting_id(row)
                    if not mid:
                        reject(table, row, "unresolved_meeting")
                        continue
                    payload = decode_json(row.get("payload_json"), {"text": row.get("text", "")})
                    result_id = imported_result(mid, "briefings", payload)
                    meeting = w.require_item("meetings", mid)
                    save(
                        "briefings",
                        row,
                        {
                            "meeting_id": mid,
                            "project_id": meeting.get("project_id"),
                            "work_package_id": meeting.get("work_package_id"),
                            "kind": "post"
                            if "post" in str(row.get("briefing_type", row.get("kind", "pre")))
                            else "pre",
                            "scope_hash": fingerprint({"project_id": meeting.get("project_id")}),
                            "input_fingerprint": "legacy:" + source_fingerprint,
                            "result_id": result_id,
                            "stale": 1,
                        },
                    )
            for row in rows("meeting_finalizations"):
                mid = meeting_id(row)
                if not mid:
                    reject("meeting_finalizations", row, "unresolved_meeting")
                    continue
                state = row.get("status")
                meeting = w.require_item("meetings", mid)
                if state not in ("complete", "completed", "succeeded", "done"):
                    w.put(
                        "meetings",
                        mid,
                        {
                            "state": "attention_required"
                            if state in ("failed", "attention_required")
                            else "interrupted",
                            "stop_watermark": row.get("input_watermark"),
                            "legacy_finalization": {
                                "stage": row.get("stage"),
                                "attempts": row.get("attempts", 0),
                                "status": state,
                            },
                        },
                        expected_revision=meeting["revision"],
                    )
                    w.queue_job(
                        "finalize",
                        {
                            "stage": "drain",
                            "input_revision": meeting["transcript_revision"],
                            "legacy_recovery_review_required": True,
                        },
                        meeting_id=mid,
                        idempotency_key="legacy-finalization:" + source_fingerprint + ":" + mid,
                    )
            for table in ("voice_profiles", "speaker_profiles"):
                for row in rows(table):
                    blob = row.get("embedding_blob", row.get("vector_blob"))
                    if not isinstance(blob, bytes) or not 0 < len(blob) <= 65536:
                        reject(table, row, "unsupported_voice_vector")
                        continue
                    person = save(
                        "people",
                        row,
                        {
                            "display_name": row.get(
                                "display_name", row.get("name", "Imported person")
                            ),
                            "notes": "Imported biometric data requires renewed scope consent and a compatible identified model.",
                        },
                    )
                    if not person:
                        continue
                    oid = new_id()
                    relative = "objects/" + oid + ".legacy-vector"
                    target = w.root / relative
                    target.parent.mkdir(exist_ok=True, mode=0o700)
                    target.write_bytes(blob)
                    w.put(
                        "objects",
                        oid,
                        {
                            "kind": "voice_vector_legacy",
                            "relative_path": relative,
                            "sha256": hashlib.sha256(blob).hexdigest(),
                            "bytes": len(blob),
                            "owning_entity_id": person["id"],
                        },
                    )
                    save(
                        "voice_profiles",
                        row,
                        {
                            "person_id": person["id"],
                            "model_fingerprint": row.get("model_fingerprint")
                            or "legacy-unidentified:" + hashlib.sha256(blob).hexdigest()[:16],
                            "vector_object_ref": oid,
                            "consent_at": milliseconds(row.get("consent_at", row.get("created_at")))
                            or now_ms(),
                            "last_seen_at": milliseconds(row.get("last_seen_at")),
                            "matching_scope": {"scope": "unresolved", "requires_reconsent": True},
                            "enabled": 0,
                        },
                    )
            imported_preferences: JSONDocument = {}
            for table in ("settings", "workspace_settings"):
                for row in rows(table):
                    values = decode_json(
                        row.get(
                            "settings_json",
                            row.get("value_json", row.get("nonsecret_settings_json")),
                        ),
                        row,
                    )
                    if isinstance(values, dict):
                        values = cast(JSONDocument, values)
                        imported_preferences.update(
                            {
                                key: values[key]
                                for key in (
                                    "always_on_top",
                                    "tray",
                                    "theme",
                                    "language",
                                    "output_language",
                                )
                                if key in values
                            }
                        )
            if imported_preferences:
                settings = w.require_item("workspace_settings", "workspace")
                existing_preferences = w.settings()
                # Existing destination choices win; missing legacy fields use established defaults.
                merged: JSONDocument = {**imported_preferences, **existing_preferences}
                w.put(
                    "workspace_settings",
                    "workspace",
                    {"nonsecret_settings": merged, "legacy_preferences": imported_preferences},
                    expected_revision=settings["revision"],
                )
            for mid in run_by_meeting:
                w.invalidate(mid)
            warnings += [
                "Legacy device labels and providers require explicit re-selection; no credentials or corporate endpoint was selected.",
                "Opaque legacy biometric vectors are disabled until scope re-consent and model compatibility review.",
                "Search and embeddings are rebuilt rather than copied.",
            ]
            report: JSONDocument = {
                **preview,
                "imported_counts": counts,
                "rejected": rejected,
                "conflicts": [],
                "id_map": mapping,
                "warnings": warnings,
                "unhandled_tables": sorted(
                    (tables & SUPPORTED)
                    - set(counts)
                    - {
                        "sessions",
                        "transcript_segments",
                        "action_items",
                        "summaries",
                        "speaker_profiles",
                        "taxonomy_nodes",
                        "meeting_briefings",
                        "meeting_finalizations",
                        "schema_version",
                        "final_analysis_promotions",
                    }
                ),
            }
            oid = new_id()
            relative = "objects/" + oid + ".json"
            target = w.root / relative
            target.parent.mkdir(exist_ok=True, mode=0o700)
            content = canonical(mapping).encode()
            target.write_bytes(content)
            w.put(
                "objects",
                oid,
                {
                    "kind": "legacy_id_map",
                    "relative_path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                    "owning_entity_id": source_fingerprint,
                },
            )
            item = w.put(
                "legacy_imports",
                new_id(),
                {
                    "source_fingerprint": source_fingerprint,
                    "source_schema": canonical(preview["source_schema"]),
                    "imported_at": now_ms(),
                    "report": report,
                    "id_map_object_ref": oid,
                },
            )
            return {"item": item, "report": report, "source_unchanged": True}
