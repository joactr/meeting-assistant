from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, cast

from meeting_assistant.application.legacy import import_legacy, legacy_preview
from meeting_assistant.application.transcripts import active_run, all_items
from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import canonical, new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry, JSONDocument

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def snapshot(w: Workspace, meeting_id: str) -> JSONDocument:
    with w.transaction():
        meeting = w.require_item("meetings", meeting_id)
        run = active_run(w, meeting_id)
        utterances = all_items(w, "utterances", {"run_id": run["id"]})
        utterances.sort(key=lambda u: (u["start_ms"], u["id"]))
        return {
            "schema_version": 1,
            "meeting": meeting,
            "transcript": utterances,
            "speakers": all_items(w, "meeting_speakers", {"meeting_id": meeting_id}),
            "actions": all_items(w, "actions", {"meeting_id": meeting_id}),
            "important_moments": all_items(w, "important_moments", {"meeting_id": meeting_id}),
            "notes": all_items(w, "notes", {"meeting_id": meeting_id}),
            "artifacts": all_items(w, "artifacts", {"meeting_id": meeting_id}),
            "approved_minutes": w.get("minutes_approved", meeting_id),
            "results": [
                result
                for run in all_items(w, "analysis_runs", {"meeting_id": meeting_id})
                for result in all_items(
                    w, "analysis_results", {"run_id": run["id"], "state": "promoted"}
                )
            ],
        }


def markdown(
    data: JSONDocument, include_transcript: bool = False, *, email_only: bool = False
) -> str:
    m = data["meeting"]
    start = datetime.fromtimestamp((m.get("started_at") or m["created_at"]) / 1000, UTC).isoformat()
    lines: list[str] = [
        f"# {m['title']}",
        "",
        f"Date: {start}",
        f"Duration: {m['elapsed_ms'] // 1000} seconds",
        f"Transcript revision: {m['transcript_revision']}; human-note revision: {m['note_revision']}",
        "",
    ]
    if data["speakers"]:
        lines += [
            "Attendees / speaker labels: " + ", ".join(s["display_name"] for s in data["speakers"]),
            "",
        ]
    approved = data.get("approved_minutes")
    if approved:
        for section in approved.get("sections", []):
            lines += ["## " + section.get("title", "Section"), "", section.get("text", ""), ""]
            if section.get("human_note"):
                lines += ["Human note: " + section["human_note"], ""]
            if section.get("source"):
                lines += ["Source: " + str(section["source"]), ""]
    else:
        for result in data["results"]:
            if result["kind"] in (("summary",) if email_only else ("summary", "minutes")):
                payload: object = result["payload"]
                lines += [
                    "## " + result["kind"].title(),
                    "",
                    cast(JSONDocument, payload).get(
                        "text",
                        cast(JSONDocument, payload).get(
                            "summary", canonical(cast(JSONDocument, payload))
                        ),
                    )
                    if isinstance(payload, dict)
                    else str(payload),
                    "",
                ]
    if not email_only:
        lines += ["## Decisions and outcomes", ""]
        for item in data["artifacts"]:
            lines += [
                "- ["
                + item["kind"]
                + "] "
                + item["title"]
                + (": " + item["description"] if item.get("description") else "")
            ]
    lines += ["", "## Actions", ""]
    for action in data["actions"]:
        if action["status"] == "dismissed":
            continue
        lines += [
            "- ["
            + ("x" if action["status"] == "done" else " ")
            + "] "
            + action["description"]
            + (" — " + action["assignee_text"] if action.get("assignee_text") else "")
            + (" (due " + action["due_date"] + ")" if action.get("due_date") else "")
        ]
    lines += ["", "## Important moments", ""]
    for moment in data["important_moments"]:
        if moment["include_in_minutes"]:
            lines += [
                f"- [{moment['start_ms'] // 1000}s] {moment['label']}: “{moment['quote_snapshot']}”",
                ("  Human note: " + moment["note"]) if moment["note"] else "",
            ]
    if not email_only and data["notes"]:
        lines += ["", "## Human notes (not independently verified transcript claims)", ""]
        for note in sorted(data["notes"], key=lambda n: (n["ordinal"], n["id"])):
            lines += ["### " + note["heading"], "", note["body_markdown"], ""]
    if include_transcript and not email_only:
        lines += ["", "## Transcript (explicitly included)", ""]
        for utterance in data["transcript"]:
            lines += [f"[{utterance['start_ms'] // 1000}s] " + utterance["text"], ""]
    return "\n".join(lines).rstrip() + "\n"


def srt(data: JSONDocument) -> str:
    def stamp(ms: int) -> str:
        seconds, millis = divmod(ms, 1000)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"

    chunks: list[str] = []
    for index, u in enumerate(data["transcript"], 1):
        text = u["text"].replace("\x00", "").replace("-->", "—›").replace("\r", "")
        chunks.append(f"{index}\n{stamp(u['start_ms'])} --> {stamp(u['end_ms'])}\n{text}\n")
    return "\n".join(chunks)


def export_meeting(w: Workspace, p: JSONDocument) -> JSONDocument:
    data = snapshot(w, p["meeting_id"])
    fmt = p.get("format", "markdown")
    name = (
        "".join(c if c.isalnum() or c in "-_ " else "_" for c in data["meeting"]["title"]).strip()[
            :100
        ]
        or "meeting"
    )
    require(
        fmt in ("markdown", "email", "pdf", "srt", "json"),
        "invalid_format",
        "Supported exports: markdown, email text, PDF, SRT and JSON.",
        status=422,
    )
    if fmt in ("markdown", "email"):
        body = markdown(
            data, bool(p.get("include_transcript", False)), email_only=fmt == "email"
        ).encode()
        mime = "text/markdown"
        suffix = "md"
    elif fmt == "srt":
        body = srt(data).encode()
        mime = "application/x-subrip"
        suffix = "srt"
    elif fmt == "json":
        if not p.get("include_transcript", False):
            data.pop("transcript", None)
        body = (canonical(data) + "\n").encode()
        mime = "application/json"
        suffix = "json"
    else:
        import reportlab
        from reportlab.lib import utils
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas

        font = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
        require(
            font.is_file(),
            "unicode_font_missing",
            "The bundled Unicode PDF font is missing.",
            status=503,
        )
        pdfmetrics.registerFont(TTFont("MeetingUnicode", str(font)))
        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(595.28, 841.89), pageCompression=1)
        pdf.setTitle(data["meeting"]["title"])
        y = 795
        for line in markdown(data, bool(p.get("include_transcript", False))).splitlines():
            for wrapped in utils.simpleSplit(line, "MeetingUnicode", 10, 495):
                if y < 50:
                    pdf.showPage()
                    y = 795
                pdf.setFont("MeetingUnicode", 10)
                pdf.drawString(50, y, wrapped)
                y -= 14
            if not line:
                y -= 8
        pdf.save()
        body = buffer.getvalue()
        mime = "application/pdf"
        suffix = "pdf"
    return {
        "filename": name + "." + suffix,
        "media_type": mime,
        "encoding": "base64",
        "content": base64.b64encode(body).decode(),
        "sha256": hashlib.sha256(body).hexdigest(),
        "snapshot": {
            "meeting_revision": data["meeting"]["revision"],
            "transcript_revision": data["meeting"]["transcript_revision"],
            "note_revision": data["meeting"]["note_revision"],
        },
    }


def backup(w: Workspace, p: JSONDocument) -> JSONDocument:
    require(
        not p.get("passphrase_secret_ref")
        or bool(p.get("_native_encryption") and p.get("file_grant")),
        "file_grant_required",
        "Passphrase backups require a native save destination and encryption broker.",
        status=422,
    )
    require(
        bool(
            p.get("plain_warning_accepted") is True
            or p.get("recipient")
            or p.get("_passphrase")
            or p.get("_native_encryption")
        ),
        "backup_encryption_required",
        "Choose passphrase encryption or explicitly acknowledge a plaintext backup.",
        status=422,
    )
    include_media = bool(p.get("include_media", False))
    with tempfile.TemporaryDirectory(prefix="ma-backup-", dir=w.root) as temp:
        root = Path(temp)
        db = root / "workspace.sqlite3"
        target = sqlite3.connect(db)
        w.backup_to(target)
        target.close()
        manifest: JSONDocument = {
            "format": "meeting-assistant-backup",
            "version": 1,
            "schema_version": w.connection.execute("PRAGMA user_version").fetchone()[0],
            "build_id": w.build_id,
            "created_at": now_ms(),
            "files": [],
            "excludes": [
                "vault secrets",
                "models",
                "runtimes",
                "cache",
                "raw logs",
                "temporary audio",
            ],
        }
        files: list[tuple[str, Path]] = [("workspace.sqlite3", db)]
        for row in w.connection.execute("SELECT relative_path,sha256 FROM objects").fetchall():
            path = (w.root / row["relative_path"]).resolve()
            require(
                path.is_relative_to(w.root),
                "unsafe_path",
                "Object path escapes workspace.",
                status=422,
            )
            if path.is_file():
                files.append((row["relative_path"], path))
        if include_media:
            for media in all_items(w, "media_segments", {"state": "committed"}):
                path = (w.root / media["relative_path"]).resolve()
                require(
                    path.is_relative_to(w.root),
                    "unsafe_path",
                    "Media path escapes workspace.",
                    status=422,
                )
                require(
                    path.is_file(), "media_hash_mismatch", "Retained media is missing.", status=409
                )
                with path.open("rb") as stream:
                    require(
                        hashlib.file_digest(stream, "sha256").hexdigest() == media["sha256"],
                        "media_hash_mismatch",
                        "Retained media changed.",
                        status=409,
                    )
                files.append((media["relative_path"], path))
        archive = root / "backup.zip"
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as output:
            for relative, path in files:
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                manifest["files"].append(
                    {"path": relative, "bytes": path.stat().st_size, "sha256": digest}
                )
                output.write(path, relative)
            output.writestr("manifest.json", canonical(manifest))
        if p.get("_output_path"):
            output_path = Path(p["_output_path"])
            output_path.parent.mkdir(exist_ok=True, mode=0o700)
            shutil.copyfile(archive, output_path)
            return {
                "filename": "meeting-assistant-" + str(now_ms()) + ".zip",
                "manifest": manifest,
                "staged": True,
            }
        require(
            archive.stat().st_size <= 256 * 1024 * 1024,
            "backup_memory_limit",
            "Use native streaming backup for archives larger than 256 MiB.",
            status=413,
        )
        body = archive.read_bytes()
        if p.get("_passphrase"):
            from pyrage import passphrase

            body = passphrase.encrypt(body, p["_passphrase"])
        elif p.get("recipient"):
            import pyrage

            body = pyrage.encrypt(body, [pyrage.x25519.Recipient.from_str(p["recipient"])])
        return {
            "filename": "meeting-assistant-"
            + str(now_ms())
            + "."
            + ("zip.age" if p.get("recipient") or p.get("_passphrase") else "zip"),
            "encoding": "base64",
            "content": base64.b64encode(body).decode(),
            "manifest": manifest,
            "sha256": hashlib.sha256(body).hexdigest(),
        }


def validate_restore(archive: Path, destination: Path) -> JSONDocument:
    require(
        not destination.exists(),
        "restore_destination_exists",
        "Restore validates into a new directory and never overwrites an existing workspace.",
        status=409,
    )
    require(
        archive.stat().st_size <= 10 * 1024**3,
        "backup_too_large",
        "Backup exceeds 10 GiB validation limit.",
        status=413,
    )
    destination.mkdir(parents=True, mode=0o700)
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            require(
                len(entries) <= 100000,
                "backup_too_many_files",
                "Backup contains too many entries.",
                status=413,
            )
            require(
                sum(e.file_size for e in entries) <= 20 * 1024**3,
                "backup_expansion_limit",
                "Expanded backup exceeds 20 GiB.",
                status=413,
            )
            manifest: JSONDocument = json.loads(source.read("manifest.json"))
            require(
                manifest.get("format") == "meeting-assistant-backup"
                and manifest.get("version") == 1,
                "unsupported_backup",
                "Unsupported backup format.",
                status=422,
            )
            expected: dict[str, JSONDocument] = {
                entry["path"]: entry for entry in manifest["files"]
            }
            require(
                len(expected) == len(manifest["files"]),
                "duplicate_backup_entry",
                "Backup manifest contains duplicate paths.",
                status=422,
            )
            require(
                "workspace.sqlite3" in expected,
                "missing_backup_database",
                "Backup must contain its indexed workspace database.",
                status=422,
            )
            seen: set[str] = set()
            for entry in entries:
                parts = PurePosixPath(entry.filename.replace("\\", "/"))
                require(
                    not parts.is_absolute()
                    and ".." not in parts.parts
                    and ":" not in entry.filename
                    and entry.filename not in seen,
                    "unsafe_backup_path",
                    "Backup path is unsafe or duplicated.",
                    status=422,
                )
                seen.add(entry.filename)
                require(
                    (entry.external_attr >> 16) & 0o170000 != 0o120000,
                    "backup_symlink",
                    "Backup symlinks are forbidden.",
                    status=422,
                )
                if entry.filename == "manifest.json":
                    continue
                require(
                    entry.filename in expected,
                    "unexpected_backup_entry",
                    "Backup contains an unindexed file.",
                    status=422,
                )
                indexed = expected[entry.filename]
                target = destination.joinpath(*parts.parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                digest = hashlib.sha256()
                size = 0
                with source.open(entry) as input_stream, target.open("xb") as output_stream:
                    while chunk := input_stream.read(262144):
                        size += len(chunk)
                        require(
                            size <= indexed["bytes"],
                            "backup_size_mismatch",
                            "Backup entry exceeds its declared size.",
                            status=422,
                        )
                        digest.update(chunk)
                        output_stream.write(chunk)
                require(
                    size == indexed["bytes"] and digest.hexdigest() == indexed["sha256"],
                    "backup_hash_mismatch",
                    "Backup integrity validation failed.",
                    status=422,
                )
                target.chmod(0o600)
            require(
                set(expected) <= seen,
                "missing_backup_entry",
                "Backup manifest lists a missing file.",
                status=422,
            )
        db = sqlite3.connect(destination / "workspace.sqlite3")
        try:
            from meeting_assistant.infrastructure.sqlite.store import CURRENT_SCHEMA_VERSION

            version = db.execute("PRAGMA user_version").fetchone()[0]
            require(
                version == manifest.get("schema_version")
                and 1 <= version <= CURRENT_SCHEMA_VERSION,
                "unsupported_backup_schema",
                "Backup schema is mismatched or requires a newer engine.",
                status=422,
            )
            require(
                db.execute("PRAGMA integrity_check").fetchone()[0] == "ok",
                "backup_database_corrupt",
                "Restored database failed integrity check.",
                status=422,
            )
            require(
                not db.execute("PRAGMA foreign_key_check").fetchall(),
                "backup_foreign_keys",
                "Restored database failed ownership checks.",
                status=422,
            )
        finally:
            db.close()
        return {
            "validated": True,
            "workspace": str(destination),
            "manifest": manifest,
            "activation_required": True,
            "rollback_workspace_retained": True,
        }
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def register_import_export(w: Workspace, r: CommandRegistry) -> None:
    async def export_command(p: JSONDocument) -> JSONDocument:
        result = export_meeting(w, p)
        if p.get("format", "markdown") in ("markdown", "email"):
            result["text"] = base64.b64decode(result["content"]).decode("utf-8")
        if p.get("file_grant"):
            relative = "staging/" + new_id() + ".export"
            staged = w.root / relative
            staged.parent.mkdir(exist_ok=True, mode=0o700)
            try:
                staged.write_bytes(base64.b64decode(result["content"]))
                staged.chmod(0o600)
                saved = await w.native(
                    "file.write",
                    {
                        "grant_id": p["file_grant"],
                        "source_relative_path": relative,
                        "filename": result["filename"],
                        "media_type": result["media_type"],
                    },
                )
                result = {
                    key: value
                    for key, value in result.items()
                    if key not in ("content", "encoding", "text")
                }
                result["saved"] = True
                result["save_result"] = saved
            finally:
                staged.unlink(missing_ok=True)
        return result

    async def backup_command(p: JSONDocument) -> JSONDocument:
        if p.get("file_grant") and not p.get("passphrase_secret_ref") and not p.get("recipient"):
            relative = "staging/" + new_id() + ".backup"
            staged = w.root / relative
            try:
                result = backup(w, {**p, "_output_path": str(staged)})
                saved = await w.native(
                    "file.write",
                    {
                        "grant_id": p["file_grant"],
                        "source_relative_path": relative,
                        "filename": result["filename"],
                        "media_type": "application/zip",
                    },
                )
                return {**result, "saved": True, "encrypted": False, "save_result": saved}
            finally:
                staged.unlink(missing_ok=True)
        if p.get("passphrase_secret_ref") and p.get("file_grant"):
            relative = "staging/" + new_id() + ".backup"
            staged = w.root / relative
            try:
                result = backup(w, {**p, "_native_encryption": True, "_output_path": str(staged)})
                encrypted = await w.native(
                    "backup.encrypt",
                    {
                        "input_relative_path": relative,
                        "output_grant": p["file_grant"],
                        "passphrase_secret_ref": p["passphrase_secret_ref"],
                    },
                )
                return {
                    **result,
                    "filename": result["filename"] + ".age",
                    "saved": True,
                    "encrypted": True,
                    **encrypted,
                }
            finally:
                staged.unlink(missing_ok=True)
        result = backup(w, p)
        if p.get("file_grant"):
            relative = "staging/" + new_id() + ".backup"
            staged = w.root / relative
            staged.parent.mkdir(exist_ok=True, mode=0o700)
            try:
                staged.write_bytes(base64.b64decode(result["content"]))
                staged.chmod(0o600)
                saved = await w.native(
                    "file.write",
                    {
                        "grant_id": p["file_grant"],
                        "source_relative_path": relative,
                        "filename": result["filename"],
                        "media_type": "application/octet-stream",
                    },
                )
                result = {
                    key: value
                    for key, value in result.items()
                    if key not in ("content", "encoding")
                }
                result["saved"] = True
                result["save_result"] = saved
            finally:
                staged.unlink(missing_ok=True)
        return result

    r["export.create"] = export_command
    r["backup.create"] = backup_command

    async def resolve(p: JSONDocument, purpose: str) -> Path:
        if p.get("_trusted_path"):
            return Path(p["_trusted_path"]).resolve()
        grant = p.get("grant_id", p.get("file_grant"))
        require(
            isinstance(grant, str),
            "file_grant_required",
            "Select a file through the native file dialog.",
            status=422,
        )
        assert isinstance(grant, str)
        response = await w.native("file.resolve", {"grant_id": grant, "purpose": purpose})
        require(
            isinstance(response.get("path"), str),
            "file_grant_invalid",
            "Native file grant did not resolve.",
            status=403,
        )
        return Path(response["path"]).resolve()

    async def preview(p: JSONDocument) -> JSONDocument:
        return legacy_preview(await resolve(p, "legacy_import"))

    async def legacy(p: JSONDocument) -> JSONDocument:
        return import_legacy(w, await resolve(p, "legacy_import"), p)

    async def restore(p: JSONDocument) -> JSONDocument:
        require(
            not any(
                m["state"] in ("starting", "recording", "paused", "stopping")
                for m in w.list("meetings")
            ),
            "restore_capture_active",
            "Stop and preserve capture before restoring.",
        )
        require(
            not w.list("jobs", {"status": "running"}),
            "restore_jobs_active",
            "Wait for running jobs before switching workspace.",
        )
        staged_archive = None
        if p.get("passphrase_secret_ref"):
            relative = "staging/" + new_id() + ".backup"
            staged_archive = w.root / relative
            staged_archive.parent.mkdir(exist_ok=True, mode=0o700)
            await w.native(
                "backup.decrypt",
                {
                    "input_grant": p.get("grant_id", p.get("file_grant")),
                    "output_relative_path": relative,
                    "passphrase_secret_ref": p["passphrase_secret_ref"],
                },
            )
            path = staged_archive
        else:
            path = await resolve(p, "restore")
        destination = w.root.parent / ("restored-" + new_id())
        try:
            if p.get("_passphrase"):
                from pyrage import passphrase

                require(
                    path.stat().st_size <= 256 * 1024 * 1024,
                    "restore_memory_limit",
                    "Use native streaming restore for encrypted archives larger than 256 MiB.",
                    status=413,
                )
                decrypted = passphrase.decrypt(path.read_bytes(), p["_passphrase"])
                staged_archive = w.root / "staging" / (new_id() + ".backup")
                staged_archive.parent.mkdir(exist_ok=True, mode=0o700)
                staged_archive.write_bytes(decrypted)
                path = staged_archive
            result = validate_restore(path, destination)
            if p.get("validate_only"):
                return result
            rollback = w.activate_restored(destination)
            with w.transaction():
                w.run_id = new_id()
                w.connection.execute(
                    "UPDATE process_runs SET ended_at=? WHERE ended_at IS NULL", (now_ms(),)
                )
                w.connection.execute(
                    "INSERT INTO process_runs VALUES(?,?,?,?,NULL)",
                    (w.run_id, os.getpid(), now_ms(), now_ms()),
                )
                w.connection.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
                w.connection.execute("UPDATE issue_links SET state='unknown' WHERE state='sending'")
                w.connection.execute(
                    "UPDATE jobs SET status='queued',lease_owner=NULL,lease_until=NULL WHERE status='running'"
                )
                w.emit("workspace.restored", {"rollback_retained": True})
            return {
                **result,
                "activated": True,
                "activation_required": False,
                "rollback_workspace": str(rollback),
            }
        finally:
            if staged_archive:
                staged_archive.unlink(missing_ok=True)

    r["legacy.preview"] = preview
    r["legacy.import"] = legacy
    r["backup.restore"] = restore

    async def media_delete(p: JSONDocument) -> JSONDocument:
        with w.transaction():
            media = w.require_item("media_segments", p["id"])
            w.connection.execute(
                "UPDATE jobs SET cancellation_requested=1 WHERE meeting_id=? AND kind IN('retranscribe','media_import')",
                (media["meeting_id"],),
            )
            result = w.put(
                "media_segments",
                media["id"],
                {"state": "purge_pending"},
                expected_revision=p.get("expected_revision"),
            )
            job = w.queue_job(
                "purge_media", {"paths": [media["relative_path"]]}, meeting_id=media["meeting_id"]
            )
            return {"item": result, "job": job}

    r["media.delete"] = media_delete
