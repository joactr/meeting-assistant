from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
import threading
from collections.abc import Callable, Generator
from datetime import datetime
from pathlib import Path
from typing import Protocol, Self, TypeVar, cast, runtime_checkable

from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, fingerprint, new_id, now_ms
from meeting_assistant.domain.ports import JSONDocument

T = TypeVar("T")
CURRENT_SCHEMA_VERSION = 5
ALIASES = {
    "vocabulary": "vocabulary_terms",
    "moments": "important_moments",
    "speakers": "meeting_speakers",
    "settings": "workspace_settings",
}
DOCUMENTS = frozenset(
    {
        "calendar_sync",
        "calendar_reminders",
        "github_connections",
        "github_operations",
        "human_notes",
        "minutes_candidates",
        "minutes_approved",
        "digest_inbox",
        "delivery_settings",
        "delivery_operations",
        "delivery_previews",
    }
)
SECRET_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "password",
        "client_secret",
        "authorization",
        "smtp_password",
    }
)


@runtime_checkable
class _InvalidationTarget(Protocol):
    def invalidate(self, meeting_id: str, *, notes: bool = False) -> JSONDocument: ...


def reject_secrets(value: object) -> None:
    if isinstance(value, dict):
        for key, child in cast(JSONDocument, value).items():
            require(
                key.casefold() not in SECRET_KEYS,
                "secret_in_document",
                "Credentials belong in the native vault, not workspace state.",
                status=422,
            )
            if key == "secret_ref" and child is not None:
                require(
                    isinstance(child, str)
                    and re.fullmatch(
                        r"(?:credential|oauth|backup):[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                        child,
                    )
                    is not None,
                    "invalid_secret_ref",
                    "Use an opaque native vault reference, never credential text.",
                    status=422,
                )
            reject_secrets(child)
    elif isinstance(value, list):
        for child in cast(list[object], value):
            reject_secrets(child)


class Store:
    def __init__(self, database_path: str | Path, build_id: str) -> None:
        self.path = Path(database_path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = self.path.parent
        self._lock_file = (self.root.parent / ("." + self.root.name + ".workspace.lock")).open(
            "a+b"
        )
        try:
            if os.name == "nt":
                import msvcrt

                self._lock_file.seek(0)
                if self._lock_file.read(1) == b"":
                    self._lock_file.write(b"0")
                    self._lock_file.flush()
                self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._lock_file.close()
            raise AppError(
                "workspace_locked", "Another engine owns this workspace.", status=409
            ) from exc
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        # WAL + NORMAL cannot corrupt the file; a power cut may lose the last commits only.
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._mutex = threading.RLock()
        self._depth = 0
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        require(
            version <= CURRENT_SCHEMA_VERSION,
            "newer_workspace_schema",
            "This workspace was created by a newer engine. Downgrading it is not supported.",
            status=409,
        )
        migrations = (
            Path(cast(str, vars(sys)["_MEIPASS"])) / "meeting_assistant" / "migrations"
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[3] / "migrations"
        )
        for migration in sorted(migrations.glob("*.sql")):
            if int(migration.name.split("_")[0]) > version:
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + migration.read_text(encoding="utf-8") + "\nCOMMIT;"
                )
        self.columns = {
            row[0]: {col[1] for col in self.connection.execute(f'PRAGMA table_info("{row[0]}")')}
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        self.tables = frozenset(
            name
            for name, columns in self.columns.items()
            if {"id", "revision", "created_at", "updated_at", "extensions_json"} <= columns
        )
        if self.get("workspace", "workspace") is None:
            self.put(
                "workspace",
                "workspace",
                {
                    "schema_version": self.connection.execute("PRAGMA user_version").fetchone()[0],
                    "build_id": build_id,
                    "display_name": "My workspace",
                    "default_timezone": "UTC",
                },
            )
            self.put(
                "workspace_settings",
                "workspace",
                {
                    "workspace_id": "workspace",
                    "nonsecret_settings": {
                        "recording": False,
                        "retention_days": 7,
                        "privacy": {"network_disabled": False},
                        "output_language": "auto",
                        "tray": False,
                        "always_on_top": True,
                    },
                },
            )
        else:
            metadata = self.get("workspace", "workspace")
            if metadata and (
                metadata["schema_version"]
                != self.connection.execute("PRAGMA user_version").fetchone()[0]
                or metadata["build_id"] != build_id
            ):
                self.put(
                    "workspace",
                    "workspace",
                    {
                        "schema_version": self.connection.execute("PRAGMA user_version").fetchone()[
                            0
                        ],
                        "build_id": build_id,
                    },
                    expected_revision=metadata["revision"],
                )
        with contextlib.suppress(OSError):
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def transaction(self) -> Generator[None]:
        with self._mutex:
            outer = self._depth == 0
            savepoint = f"sp_{self._depth}"
            self.connection.execute("BEGIN IMMEDIATE" if outer else f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK" if outer else f"ROLLBACK TO {savepoint}")
                if not outer:
                    self.connection.execute(f"RELEASE {savepoint}")
                raise
            else:
                self.connection.execute("COMMIT" if outer else f"RELEASE {savepoint}")
            finally:
                self._depth -= 1

    def atomic(self, callback: Callable[[Self], T]) -> T:
        with self.transaction():
            return callback(self)

    def backup_to(self, destination: sqlite3.Connection) -> None:
        with self._mutex:
            self.connection.backup(destination)

    def _table(self, collection: str) -> str:
        table = ALIASES.get(collection, collection)
        require(
            table in self.tables or table in DOCUMENTS,
            "invalid_collection",
            "Collection is not an application resource.",
            status=422,
        )
        return table

    def decode(self, row: sqlite3.Row | None) -> JSONDocument | None:
        if row is None:
            return None
        item: JSONDocument = dict(row)
        extras: JSONDocument = json.loads(item.pop("extensions_json", "{}"))
        for key in list(item):
            if key.endswith("_json"):
                value = item.pop(key)
                item[key[:-5]] = json.loads(value) if value is not None else None
        extras.update(item)
        return extras

    def get(self, collection: str, entity_id: str) -> JSONDocument | None:
        table = self._table(collection)
        with self._mutex:
            if table in DOCUMENTS:
                row = self.connection.execute(
                    "SELECT * FROM workflow_documents WHERE collection=? AND id=?",
                    (table, entity_id),
                ).fetchone()
                if row is None:
                    return None
                result: JSONDocument = json.loads(row["payload_json"])
                result.update(
                    {key: row[key] for key in ("id", "revision", "created_at", "updated_at")}
                )
                return result
            return self.decode(
                self.connection.execute(
                    f'SELECT * FROM "{table}" WHERE id=?', (entity_id,)
                ).fetchone()
            )

    def list(
        self,
        collection: str,
        filters: JSONDocument | None = None,
        *,
        limit: int = 200,
        offset: int = 0,
    ) -> list[JSONDocument]:
        table = self._table(collection)
        require(
            1 <= limit <= 200 and 0 <= offset <= 100000,
            "invalid_page",
            "Page limit must be 1..200 and offset bounded.",
            status=422,
        )
        filters = filters or {}
        with self._mutex:
            if table in DOCUMENTS:
                rows = self.connection.execute(
                    "SELECT id FROM workflow_documents WHERE collection=? ORDER BY created_at,id",
                    (table,),
                ).fetchall()
                items = [self.get(collection, row["id"]) for row in rows]
                return [
                    item
                    for item in items
                    if item is not None and all(item.get(k) == v for k, v in filters.items())
                ][offset : offset + limit]
            clauses: list[str] = []
            params: list[object] = []
            extra_filters: JSONDocument = {}
            for key, value in filters.items():
                column = key if key in self.columns[table] else key + "_json"
                if column not in self.columns[table]:
                    extra_filters[key] = value
                    continue
                clauses.append(f'"{column}" IS ?')
                params.append(canonical(value) if column.endswith("_json") else value)
            sql = (
                f'SELECT * FROM "{table}"'
                + (" WHERE " + " AND ".join(clauses) if clauses else "")
                + " ORDER BY created_at,id"
            )
            if not extra_filters:
                sql += " LIMIT ? OFFSET ?"
                params += [limit, offset]
            items = [self.decode(row) for row in self.connection.execute(sql, params).fetchall()]
            if extra_filters:
                items = [
                    item
                    for item in items
                    if item and all(item.get(k) == v for k, v in extra_filters.items())
                ][offset : offset + limit]
            return [item for item in items if item is not None]

    def snapshot_at(self, collection: str, at_ms: int | str) -> list[JSONDocument]:
        self._table(collection)
        if isinstance(at_ms, str):
            at_ms = int(datetime.fromisoformat(at_ms.replace("Z", "+00:00")).timestamp() * 1000)
        rows = self.connection.execute(
            "SELECT h.* FROM document_history h WHERE h.collection=? AND h.changed_at<=? AND h.seq=(SELECT MAX(x.seq) FROM document_history x WHERE x.collection=h.collection AND x.entity_id=h.entity_id AND x.changed_at<=?) AND h.tombstone=0 ORDER BY h.entity_id",
            (collection, at_ms, at_ms),
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def _normalize(self, table: str, item: JSONDocument) -> JSONDocument:
        def milliseconds(value: object) -> int:
            if isinstance(value, (int, float)):
                return int(value)
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)

        if table == "vocabulary_terms":
            item["term"] = item.get("spelling", item.get("term", ""))
            item["normalized_term"] = item["term"].casefold().strip()
            item["locale"] = item.get("language", item.get("locale"))
            item["pronunciation_hint"] = item.get("pronunciation", item.get("pronunciation_hint"))
        elif table == "calendar_events":
            item["external_id"] = item.get("provider_event_id", item.get("external_id", item["id"]))
            item["occurrence_key"] = item.get(
                "occurrence_id", item.get("occurrence_key", item["external_id"])
            )
            item["start_utc"] = milliseconds(item.get("start", item.get("start_utc")))
            item["end_utc"] = milliseconds(item.get("end", item.get("end_utc")))
            item["canceled"] = int(item.get("cancelled", item.get("canceled", False)))
            item["join_url"] = item.get("meeting_url", item.get("join_url"))
            item["content_hash"] = fingerprint(
                {
                    k: v
                    for k, v in item.items()
                    if k not in ("revision", "created_at", "updated_at", "content_hash")
                }
            )
        elif table == "digest_schedules":
            item["local_time"] = item.get("time", item.get("local_time", "16:00"))
            if item.get("next_boundary"):
                item["next_run_at"] = milliseconds(item["next_boundary"])
            if item.get("last_success_boundary"):
                item["last_period_end"] = milliseconds(item["last_success_boundary"])
        elif table == "digest_runs":
            if isinstance(item.get("period_start"), str):
                item["period_start_iso"] = item["period_start"]
            if isinstance(item.get("period_end"), str):
                item["period_end_iso"] = item["period_end"]
            item["period_start"] = milliseconds(item["period_start"])
            item["period_end"] = milliseconds(item["period_end"])
            item["input_fingerprint"] = item.get(
                "snapshot_hash", item.get("input_fingerprint", fingerprint(item))
            )
            item["status"] = item.get("state", item.get("status", "generating"))
        elif table == "consents" and isinstance(item.get("scope"), (dict, list)):
            item["scope"] = canonical(item["scope"])
        return item

    def _project_aggregate(self, collection: str, item: JSONDocument) -> None:
        if collection == "human_notes":
            mid = item.get("meeting_id", item["id"])
            meeting = self.get("meetings", mid)
            require(meeting is not None, "not_found", "Notes meeting does not exist.", status=404)
            assert meeting is not None
            note_revision = meeting["note_revision"] + 1
            keep: set[str] = set()
            for ordinal, block in enumerate(item.get("blocks", [])):
                bid = block["id"]
                keep.add(bid)
                anchor: JSONDocument = block.get("anchor") or {}
                existing = self.get("notes", bid)
                note = self.put(
                    "notes",
                    bid,
                    {
                        "meeting_id": mid,
                        "heading": block.get("heading", ""),
                        "body_markdown": block.get("body", ""),
                        "ordinal": ordinal,
                        "anchor_utterance_id": anchor.get("utterance_id"),
                        "anchor_ms": anchor.get("time_ms"),
                        "provenance": "human",
                    },
                    expected_revision=existing["revision"] if existing else None,
                )
                self.put(
                    "note_revisions",
                    new_id(),
                    {
                        "note_id": bid,
                        "meeting_note_revision": note_revision,
                        "heading": note["heading"],
                        "body_markdown": note["body_markdown"],
                        "ordinal": ordinal,
                        "anchor_utterance_id": note.get("anchor_utterance_id"),
                        "anchor_ms": note.get("anchor_ms"),
                        "actor": "user",
                    },
                )
            for previous in self.list("notes", {"meeting_id": mid}):
                if previous["id"] not in keep:
                    self.delete("notes", previous["id"], expected_revision=previous["revision"])
            if isinstance(self, _InvalidationTarget):
                self.invalidate(mid, notes=True)

    def put(
        self,
        collection: str,
        entity_id: str,
        data: JSONDocument,
        *,
        expected_revision: int | None = None,
    ) -> JSONDocument:
        reject_secrets(data)
        table = self._table(collection)
        require(
            len(canonical(data).encode("utf-8")) <= 1_048_576,
            "document_too_large",
            "Application resource exceeds 1 MiB.",
            status=413,
        )
        with self.transaction():
            before = self.get(collection, entity_id)
            if before is not None:
                require(
                    expected_revision is not None and expected_revision == before["revision"],
                    "revision_conflict",
                    "Resource changed since it was read.",
                    details={"latest": before},
                )
                require(
                    table not in {"note_revisions", "utterance_revisions"},
                    "immutable_resource",
                    "Historical revisions are immutable.",
                )
            else:
                require(
                    expected_revision in (None, 0),
                    "revision_conflict",
                    "Resource no longer exists.",
                    details={"latest": None},
                )
            item = self._normalize(
                table,
                {
                    **(before or {}),
                    **data,
                    "id": entity_id,
                    "revision": (before["revision"] + 1 if before else 1),
                    "created_at": (before["created_at"] if before else now_ms()),
                    "updated_at": now_ms(),
                },
            )
            if table in DOCUMENTS:
                self.connection.execute(
                    "INSERT INTO workflow_documents(collection,id,meeting_id,project_id,state,status,payload_json,created_at,updated_at,revision) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(collection,id) DO UPDATE SET meeting_id=excluded.meeting_id,project_id=excluded.project_id,state=excluded.state,status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at,revision=excluded.revision",
                    (
                        table,
                        entity_id,
                        item.get("meeting_id"),
                        item.get("project_id"),
                        item.get("state"),
                        item.get("status"),
                        canonical(item),
                        item["created_at"],
                        item["updated_at"],
                        item["revision"],
                    ),
                )
            else:
                columns = self.columns[table]
                stored: JSONDocument = {}
                extra: JSONDocument = {}
                for key, value in item.items():
                    column = key if key in columns else key + "_json"
                    if column in columns and column != "extensions_json":
                        stored[column] = (
                            canonical(value)
                            if column.endswith("_json") and value is not None
                            else value
                        )
                    else:
                        extra[key] = value
                stored["extensions_json"] = canonical(extra)
                names = list(stored)
                try:
                    if before:
                        self.connection.execute(
                            f'UPDATE "{table}" SET '
                            + ",".join(f'"{name}"=?' for name in names)
                            + " WHERE id=?",
                            [stored[name] for name in names] + [entity_id],
                        )
                    else:
                        self.connection.execute(
                            f'INSERT INTO "{table}" ('
                            + ",".join(f'"{name}"' for name in names)
                            + ") VALUES("
                            + ",".join("?" for _ in names)
                            + ")",
                            [stored[name] for name in names],
                        )
                except sqlite3.IntegrityError as exc:
                    raise AppError(
                        "constraint_conflict",
                        "Resource violates an ownership, uniqueness or value constraint.",
                        status=409,
                        details={"constraint": str(exc)},
                    ) from exc
            self._project_aggregate(collection, item)
            self.connection.execute(
                "INSERT INTO document_history(collection,entity_id,revision,changed_at,tombstone,meeting_id,project_id,payload_json) VALUES(?,?,?,?,0,?,?,?)",
                (
                    collection,
                    entity_id,
                    item["revision"],
                    item["updated_at"],
                    item.get("meeting_id"),
                    item.get("project_id"),
                    canonical(item),
                ),
            )
            self.emit(
                collection + ".updated" if before else collection + ".created",
                {"changed_fields": sorted(data)},
                entity_id=entity_id,
                meeting_id=self._event_meeting(collection, entity_id, item),
                revision=item["revision"],
            )
            return self.get(collection, entity_id) or item

    def delete(self, collection: str, entity_id: str, *, expected_revision: int) -> JSONDocument:
        table = self._table(collection)
        with self.transaction():
            before = self.get(collection, entity_id)
            require(before is not None, "not_found", "Resource does not exist.", status=404)
            assert before is not None
            require(
                before["revision"] == expected_revision,
                "revision_conflict",
                "Resource changed since it was read.",
                details={"latest": before},
            )
            if table in DOCUMENTS:
                self.connection.execute(
                    "DELETE FROM workflow_documents WHERE collection=? AND id=?", (table, entity_id)
                )
            else:
                self.connection.execute(f'DELETE FROM "{table}" WHERE id=?', (entity_id,))
            if table == "meetings":
                self.connection.execute(
                    "DELETE FROM document_history WHERE meeting_id=? OR (collection='meetings' AND entity_id=?)",
                    (entity_id, entity_id),
                )
            self.connection.execute(
                "INSERT INTO document_history(collection,entity_id,revision,changed_at,tombstone,meeting_id,project_id,payload_json) VALUES(?,?,?,?,1,?,?,?)",
                (
                    collection,
                    entity_id,
                    expected_revision + 1,
                    now_ms(),
                    before.get("meeting_id"),
                    before.get("project_id"),
                    "{}",
                ),
            )
            self.emit(
                collection + ".deleted",
                {},
                entity_id=entity_id,
                # A deleted meeting can't own its own event (outbox FK); every view drops it anyway.
                meeting_id=None
                if table == "meetings"
                else self._event_meeting(collection, entity_id, before),
                revision=expected_revision + 1,
            )
            return {"id": entity_id, "deleted": True}

    def _event_meeting(self, collection: str, entity_id: str, item: JSONDocument) -> str | None:
        """The meeting an entity event belongs to, so clients refresh only that meeting's views."""
        if item.get("meeting_id"):
            return str(item["meeting_id"])
        if collection == "meetings":
            return entity_id
        if item.get("utterance_id"):
            sql, key = "SELECT meeting_id FROM utterances WHERE id=?", item["utterance_id"]
        elif item.get("run_id") and collection == "analysis_results":
            sql, key = "SELECT meeting_id FROM analysis_runs WHERE id=?", item["run_id"]
        elif item.get("owning_entity_id"):
            sql, key = "SELECT id FROM meetings WHERE id=?", item["owning_entity_id"]
        else:
            return None
        row = self.connection.execute(sql, (key,)).fetchone()
        return row[0] if row else None

    def emit(
        self,
        event_type: str,
        data: JSONDocument,
        *,
        entity_id: str | None = None,
        meeting_id: str | None = None,
        revision: int | None = None,
    ) -> None:
        if self._depth == 0:
            with self.transaction():
                self.emit(
                    event_type, data, entity_id=entity_id, meeting_id=meeting_id, revision=revision
                )
            return
        self.connection.execute(
            "INSERT INTO outbox_events(event_id,event_type,entity_id,meeting_id,revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id(), event_type, entity_id, meeting_id, revision, canonical(data), now_ms()),
        )

    def events(
        self, after_seq: int = 0, meeting_id: str | None = None, limit: int = 200
    ) -> JSONDocument:
        with self._mutex:
            bounds = self.connection.execute(
                "SELECT COALESCE(MIN(seq),0),COALESCE(MAX(seq),0) FROM outbox_events"
            ).fetchone()
            if after_seq and after_seq < bounds[0] - 1:
                return {"resync_required": True, "watermark": bounds[1], "items": []}
            rows = self.connection.execute(
                "SELECT * FROM outbox_events WHERE seq>?"
                + (" AND (meeting_id=? OR meeting_id IS NULL)" if meeting_id else "")
                + " ORDER BY seq LIMIT ?",
                ([after_seq, meeting_id, limit] if meeting_id else [after_seq, limit]),
            ).fetchall()
            return {
                "resync_required": False,
                "watermark": bounds[1],
                "items": [
                    {
                        "protocol_version": 1,
                        "seq": r["seq"],
                        "event_id": r["event_id"],
                        "type": r["event_type"],
                        "meeting_id": r["meeting_id"],
                        "entity_id": r["entity_id"],
                        "revision": r["revision"],
                        "occurred_at_ms": r["created_at"],
                        "data": json.loads(r["payload_json"]),
                    }
                    for r in rows
                ],
            }

    def activate_restored(self, staged: Path) -> Path:
        require(
            self._depth == 0,
            "transaction_active",
            "Restore cannot switch inside a write transaction.",
            status=409,
        )
        rollback = self.root.with_name(self.root.name + ".rollback-" + new_id())
        with self._mutex:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            switched = False
            try:
                self.root.rename(rollback)
                staged.rename(self.root)
                switched = True
                self.connection = sqlite3.connect(
                    self.path, isolation_level=None, check_same_thread=False
                )
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA foreign_keys=ON")
                self.connection.execute("PRAGMA journal_mode=WAL")
                self.connection.execute("PRAGMA synchronous=NORMAL")
                version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                migrations = (
                    Path(cast(str, vars(sys)["_MEIPASS"])) / "meeting_assistant" / "migrations"
                    if getattr(sys, "frozen", False)
                    else Path(__file__).resolve().parents[3] / "migrations"
                )
                for migration in sorted(migrations.glob("*.sql")):
                    if int(migration.name.split("_")[0]) > version:
                        self.connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + migration.read_text(encoding="utf-8")
                            + "\nCOMMIT;"
                        )
                require(
                    self.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                    "restore_reopen_failed",
                    "Restored workspace did not reopen cleanly.",
                    status=503,
                )
                require(
                    not self.connection.execute("PRAGMA foreign_key_check").fetchall(),
                    "restore_ownership_failed",
                    "Restored ownership checks failed.",
                    status=503,
                )
                self.columns = {
                    row[0]: {
                        col[1] for col in self.connection.execute(f'PRAGMA table_info("{row[0]}")')
                    }
                    for row in self.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                    ).fetchall()
                }
                self.tables = frozenset(
                    name
                    for name, columns in self.columns.items()
                    if {"id", "revision", "created_at", "updated_at", "extensions_json"} <= columns
                )
            except BaseException:
                with contextlib.suppress(Exception):
                    self.connection.close()
                if switched:
                    self.root.rename(staged)
                if rollback.exists():
                    rollback.rename(self.root)
                self.connection = sqlite3.connect(
                    self.path, isolation_level=None, check_same_thread=False
                )
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA foreign_keys=ON")
                raise
        return rollback

    def close(self) -> None:
        with self._mutex:
            self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connection.close()
            self._lock_file.close()
