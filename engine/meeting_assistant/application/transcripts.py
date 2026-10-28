from __future__ import annotations

import base64
import difflib
import json
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import (
    is_question,
    meaningful_transcript,
    new_id,
    now_ms,
    union_duration,
)
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def active_run(w: Workspace, meeting_id: str) -> dict[str, Any]:
    runs = w.list("transcript_runs", {"meeting_id": meeting_id, "state": "active"})
    require(bool(runs), "transcript_missing", "Meeting has no active transcript.", status=404)
    return runs[0]


def all_items(
    w: Workspace, collection: str, filters: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = w.list(collection, filters, limit=200, offset=offset)
        result.extend(page)
        if len(page) < 200:
            return result
        offset += 200


def transcript_source_watermark(
    w: Workspace, meeting_id: str, media_ids: list[str]
) -> dict[str, Any]:
    meeting = w.require_item("meetings", meeting_id)
    media = [w.get("media_segments", identity) or {} for identity in sorted(media_ids)]
    return {
        "capture_epoch": meeting.get("capture_epoch"),
        "stop_watermark": meeting.get("stop_watermark"),
        "sources": sorted(
            [item["id"], item["revision"]]
            for item in all_items(w, "capture_sources", {"meeting_id": meeting_id})
        ),
        "originals": sorted(
            [item["id"], item["hash"]]
            for item in all_items(w, "import_assets", {"meeting_id": meeting_id})
        ),
        "media": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "meeting_id",
                    "source_id",
                    "state",
                    "sha256",
                    "start_ms",
                    "duration_ms",
                )
            }
            for item in media
        ],
    }


def _subtract_spans(start: int, end: int, removed: list[tuple[int, int]]) -> list[tuple[int, int]]:
    spans = [(start, end)]
    for left, right in sorted(removed):
        spans = [
            part
            for low, high in spans
            for part in ((low, min(high, left)), (max(low, right), high))
            if part[0] < part[1]
        ]
    return spans


def _run_rows(w: Workspace, run_id: str) -> list[dict[str, Any]]:
    rows = all_items(w, "utterances", {"run_id": run_id})
    for row in rows:
        row["words"] = [
            dict(word)
            for word in w.connection.execute(
                "SELECT * FROM words WHERE utterance_id=? ORDER BY ordinal", (row["id"],)
            )
        ]
        row["assignments"] = all_items(w, "speaker_assignments", {"utterance_id": row["id"]})
        row["origin_id"] = row["id"]
    return sorted(rows, key=lambda row: (row["start_ms"], row["id"]))


def _word_fragments(
    row: dict[str, Any], spans: list[tuple[int, int]], *, preserve: bool
) -> list[dict[str, Any]]:
    """Split only with real word timing; never invent a substring/timestamp alignment."""
    words = row["words"]
    if not words or any(word["start_ms"] is None or word["end_ms"] is None for word in words):
        return [row] if preserve else []
    groups: list[list[dict[str, Any]]] = []
    previous_span: int | None = None
    for word in words:
        selected_span = next(
            (
                index
                for index, (start, end) in enumerate(spans)
                if (word["start_ms"] < end and start < word["end_ms"])
                if preserve or (start <= word["start_ms"] and word["end_ms"] <= end)
            ),
            None,
        )
        if selected_span is not None:
            if selected_span != previous_span:
                groups.append([])
            groups[-1].append(word)
        previous_span = selected_span
    return [
        {
            **row,
            "id": f"{row['origin_id']}:{group[0]['ordinal']}:{group[-1]['ordinal']}",
            "start_ms": group[0]["start_ms"],
            "end_ms": group[-1]["end_ms"],
            "text": " ".join(word["text"] for word in group),
            "words": group,
        }
        for group in groups
    ]


def _candidate_plan(
    w: Workspace, active: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    before = _run_rows(w, active["id"])
    generated = _run_rows(w, candidate["id"])
    coverage = candidate.get("coverage", [])
    require(bool(coverage), "candidate_incomplete", "Candidate has no completed audio coverage.")
    preserved: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    boundaries: list[str] = []
    for row in before:
        covered = [
            (span["start_ms"], span["end_ms"])
            for span in coverage
            if span.get("source_id") == row["source_id"]
        ]
        outside = _subtract_spans(row["start_ms"], row["end_ms"], covered)
        touched = outside != [(row["start_ms"], row["end_ms"])]
        locked_assignments = any(item["manual_lock"] for item in row["assignments"])
        if touched and (
            row["manual_text_lock"] or row["manual_speaker_lock"] or locked_assignments
        ):
            conflicts.append(
                {
                    "utterance_id": row["id"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "text": row["text"],
                    "proposed_text": [
                        item["text"]
                        for item in generated
                        if item["source_id"] == row["source_id"]
                        and item["start_ms"] < row["end_ms"]
                        and row["start_ms"] < item["end_ms"]
                    ],
                    "manual_text_lock": bool(row["manual_text_lock"]),
                    "manual_speaker_lock": bool(row["manual_speaker_lock"] or locked_assignments),
                    "resolution": "preserve_manual_corrections",
                }
            )
        generated_spans = [
            (item["start_ms"], item["end_ms"])
            for item in generated
            if item["source_id"] == row["source_id"]
        ]
        locked_spans = [
            (item["start_ms"], item["end_ms"])
            for item in row["assignments"]
            if item["manual_lock"] or row["manual_speaker_lock"]
        ]
        if row["manual_speaker_lock"] and not locked_spans:
            locked_spans = [(row["start_ms"], row["end_ms"])]
        unmapped_identity = any(
            _subtract_spans(start, end, generated_spans) for start, end in locked_spans
        )
        if row["manual_text_lock"] or not touched or unmapped_identity:
            preserved.append(row)
        elif outside:
            fragments = _word_fragments(row, outside, preserve=True)
            preserved.extend(fragments)
            if fragments == [row]:
                boundaries.append(row["id"])
    # A segment-only hypothesis cannot safely be cut around a manual correction.
    # Preserve its neighboring original rows too, rather than losing their text.
    whole_preserved = {row["id"] for row in preserved if row["id"] == row["origin_id"]}
    changed = True
    while changed:
        changed = False
        for row in generated:
            if row["words"] and all(
                word["start_ms"] is not None and word["end_ms"] is not None for word in row["words"]
            ):
                continue
            if not any(
                old["source_id"] == row["source_id"]
                and old["start_ms"] < row["end_ms"]
                and row["start_ms"] < old["end_ms"]
                for old in preserved
            ):
                continue
            for old in before:
                if (
                    old["id"] not in whole_preserved
                    and old["source_id"] == row["source_id"]
                    and old["start_ms"] < row["end_ms"]
                    and row["start_ms"] < old["end_ms"]
                ):
                    preserved = [item for item in preserved if item["origin_id"] != old["id"]]
                    preserved.append(old)
                    whole_preserved.add(old["id"])
                    boundaries.append(old["id"])
                    changed = True
    after = list(preserved)
    suppressed: list[str] = []
    for row in generated:
        covered = [
            (max(row["start_ms"], span["start_ms"]), min(row["end_ms"], span["end_ms"]))
            for span in coverage
            if span.get("source_id") == row["source_id"]
            and span["start_ms"] < row["end_ms"]
            and row["start_ms"] < span["end_ms"]
        ]
        protected = [
            (old["start_ms"], old["end_ms"])
            for old in preserved
            if old["source_id"] == row["source_id"]
        ]
        allowed = [
            part for start, end in covered for part in _subtract_spans(start, end, protected)
        ]
        if allowed == [(row["start_ms"], row["end_ms"])]:
            fragments = [row]
        else:
            fragments = _word_fragments(row, allowed, preserve=False) if allowed else []
            if not fragments:
                suppressed.append(row["id"])
        for fragment in fragments:
            assignments: list[dict[str, Any]] = []
            for old in before:
                if (
                    old["source_id"] != fragment["source_id"]
                    or old["start_ms"] >= fragment["end_ms"]
                    or fragment["start_ms"] >= old["end_ms"]
                ):
                    continue
                assignments.extend(
                    {**item, "manual_lock": int(item["manual_lock"] or old["manual_speaker_lock"])}
                    for item in old["assignments"]
                )
            manual_spans = [
                (item["start_ms"], item["end_ms"]) for item in assignments if item["manual_lock"]
            ]
            mapped: list[dict[str, Any]] = []
            # Old effective identities map by source/time, never provider numeric labels.
            for assignment in assignments or fragment["assignments"]:
                low = max(fragment["start_ms"], assignment["start_ms"])
                high = min(fragment["end_ms"], assignment["end_ms"])
                spans = [(low, high)] if low < high else []
                if not assignment["manual_lock"]:
                    spans = [
                        part
                        for left, right in spans
                        for part in _subtract_spans(left, right, manual_spans)
                    ]
                mapped.extend(
                    {**assignment, "start_ms": left, "end_ms": right} for left, right in spans
                )
            fragment = {
                **fragment,
                "assignments": mapped,
                "manual_speaker_lock": int(any(item["manual_lock"] for item in mapped)),
            }
            after.append(fragment)
    after.sort(key=lambda row: (row["start_ms"], row["id"]))
    mappings: list[dict[str, Any]] = []
    for old in before:
        matches = [
            row
            for row in after
            if row["source_id"] == old["source_id"]
            and row["start_ms"] < old["end_ms"]
            and old["start_ms"] < row["end_ms"]
        ]
        mappings.append(
            {
                "utterance_id": old["id"],
                "replacement_ids": [row["id"] for row in matches],
                "preserved": any(row["origin_id"] == old["id"] for row in matches),
            }
        )
    return {
        "before": before,
        "after": after,
        "manual_conflicts": conflicts,
        "preserved_boundary_ids": boundaries,
        "suppressed_candidate_ids": suppressed,
        "source_mappings": mappings,
    }


def _remap_transcript_sources(
    w: Workspace, meeting_id: str, before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    originals = {row["id"]: row for row in before}
    mappings: list[dict[str, Any]] = []
    for collection, field in (
        ("citations", "source_utterance_id"),
        ("entity_sources", "utterance_id"),
        ("important_moments", "utterance_id"),
        ("questions", "utterance_id"),
    ):
        for reference in all_items(w, collection, {"meeting_id": meeting_id}):
            original_id = reference.get(field)
            if not original_id:
                continue
            original = originals.get(original_id)
            quote = reference.get("quote_snapshot") or (
                reference.get("text") if collection == "questions" else ""
            )
            start = reference.get("start_ms")
            end = reference.get("end_ms")
            matches = [
                row
                for row in after
                if original
                and row["source_id"] == original["source_id"]
                and (row["start_ms"] < original["end_ms"] and original["start_ms"] < row["end_ms"])
                and (start is None or row["start_ms"] <= start < row["end_ms"])
                and (end is None or end <= row["end_ms"])
            ]
            quoted = [row for row in matches if quote and quote in row["text"]]
            timed = [
                row
                for row in matches
                if not quote
                or end is not None
                or (
                    original
                    and row["start_ms"] <= original["start_ms"]
                    and original["end_ms"] <= row["end_ms"]
                )
            ]
            selected = quoted[0] if len(quoted) == 1 else timed[0] if len(timed) == 1 else None
            if (
                collection == "questions"
                and selected
                and any(
                    item["id"] != reference["id"]
                    and item["detection_version"] == reference["detection_version"]
                    for item in all_items(w, "questions", {"utterance_id": selected["id"]})
                )
            ):
                selected = None
            exact = bool(
                selected
                and (
                    (quote and quote in selected["text"])
                    or (not quote and original and selected["text"] == original["text"])
                )
            )
            stale = bool(reference.get("stale")) or not exact
            # A mapped timestamp is not permission to rewrite historical evidence.
            # Unmapped references stay attached to the archived source with snapshots.
            changes: dict[str, Any] = {
                "stale": stale,
                "mapping_status": "mapped" if selected else "unmapped",
                "original_utterance_id": reference.get("original_utterance_id", original_id),
            }
            if selected:
                changes[field] = selected["id"]
                if "source_revision" in reference and exact:
                    changes["source_revision"] = selected["text_revision"]
            updated = w.put(
                collection, reference["id"], changes, expected_revision=reference["revision"]
            )
            mappings.append(
                {
                    "collection": collection,
                    "id": reference["id"],
                    "from_utterance_id": original_id,
                    "to_utterance_id": selected["id"] if selected else None,
                    "stale": updated.get("stale", stale),
                }
            )
    return mappings


def register_transcripts(w: Workspace, r: CommandRegistry) -> None:
    def transcript_list(p: dict[str, Any]) -> dict[str, Any]:
        from meeting_assistant.application.retrieval import paginate

        mid = p["meeting_id"]
        run_id = p.get("run_id") or active_run(w, mid)["id"]
        require(
            w.require_item("transcript_runs", run_id)["meeting_id"] == mid,
            "source_scope",
            "Run does not belong to meeting.",
            status=422,
        )
        # Validate the shared cursor contract before using its key in SQL. Fetch
        # one lookahead row; words and speaker data are only read for this page.
        paginate([], p, sort_key=lambda u: (u["start_ms"], u["id"]))
        clauses = ["meeting_id=?", "run_id=?"]
        parameters: list[Any] = [mid, run_id]
        cursor = p.get("cursor")
        if cursor:
            raw_last: object = json.loads(
                base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            )["last"]
            last = cast(list[object], raw_last)
            require(
                isinstance(raw_last, list)
                and len(last) == 2
                and isinstance(last[0], int)
                and isinstance(last[1], str),
                "invalid_cursor",
                "Transcript cursor must contain a timestamp and utterance ID.",
                status=422,
            )
            clauses.append("(start_ms>? OR (start_ms=? AND id>?))")
            parameters.extend((last[0], last[0], last[1]))
        parameters.append(int(p.get("limit", 50)) + 1)
        rows = [
            item
            for item in map(
                w.decode,
                w.connection.execute(
                    "SELECT * FROM utterances WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY start_ms,id LIMIT ?",
                    parameters,
                ).fetchall(),
            )
            if item is not None
        ]
        # Words and speaker assignments for the whole page in two queries, not two per row.
        by_id = {item["id"]: item for item in rows}
        for item in rows:
            item["words"], item["speaker_assignments"] = [], []
        marks = ",".join("?" * len(by_id))
        for row in w.connection.execute(
            f"SELECT * FROM words WHERE utterance_id IN ({marks}) ORDER BY utterance_id,ordinal",
            list(by_id),
        ).fetchall():
            by_id[row["utterance_id"]]["words"].append(dict(row))
        for row in w.connection.execute(
            f"SELECT * FROM speaker_assignments WHERE utterance_id IN ({marks})"
            " ORDER BY created_at,id",
            list(by_id),
        ).fetchall():
            assignment = w.decode(row)
            assert assignment is not None
            by_id[row["utterance_id"]]["speaker_assignments"].append(assignment)
        return paginate(rows, p, sort_key=lambda u: (u["start_ms"], u["id"]))

    r["transcript.list"] = transcript_list

    def ingest(p: dict[str, Any]) -> dict[str, Any]:
        mid = p["meeting_id"]
        meeting = w.require_item("meetings", mid)
        run_id = p.get("run_id") or active_run(w, mid)["id"]
        run = w.require_item("transcript_runs", run_id)
        require(
            run["meeting_id"] == mid and run["state"] in ("active", "candidate"),
            "source_scope",
            "Invalid transcript destination.",
            status=422,
        )
        require(
            run["state"] != "candidate" or run.get("completed_at") is None,
            "candidate_complete",
            "Completed transcript candidates are immutable.",
        )
        text = str(p.get("text", ""))
        language = p.get("language", p.get("detected_language"))
        require(len(text) <= 100000, "text_too_large", "Utterance is too large.", status=413)
        start = int(p["start_ms"])
        end = int(p["end_ms"])
        require(0 <= start <= end, "invalid_span", "Transcript timestamps are invalid.", status=422)
        source_id = p.get("source_id")
        if source_id:
            require(
                w.require_item("capture_sources", source_id)["meeting_id"] == mid,
                "source_scope",
                "Capture source belongs to another meeting.",
                status=422,
            )
        if p.get("capture_epoch") is not None:
            require(
                p["capture_epoch"] == meeting["capture_epoch"],
                "capture_epoch_conflict",
                "Result belongs to an obsolete capture epoch.",
            )
        key = (
            str(p.get("source_id", "")),
            str(p.get("provider_stream_id", "text-import")),
            str(p.get("provider_result_id", p.get("id", ""))),
        )
        if not p.get("is_final", True):
            if len(w.interim_ids) >= 200 and key not in w.interim_ids:
                w.interim_ids.pop(next(iter(w.interim_ids)))
            identity = w.interim_ids.setdefault(key, p.get("id") or new_id())
            item = {**p, "id": identity, "is_final": False}
            w.emit_live("transcript.interim", item, meeting_id=mid, entity_id=identity)
            return {"ephemeral": True, "item": item}
        pending_id = w.interim_ids.pop(key, None)
        if not meaningful_transcript(text, language):
            if pending_id:
                w.emit_live(
                    "transcript.utterance_final",
                    {
                        "id": pending_id,
                        "source_id": p.get("source_id"),
                        "provider_stream_id": p.get("provider_stream_id"),
                        "provider_result_id": p.get("provider_result_id"),
                        "filtered": True,
                    },
                    meeting_id=mid,
                    entity_id=pending_id,
                )
            return {"filtered": True, "rule_version": "en-es-noise-v1"}
        stream = str(p.get("provider_stream_id", "text-import"))
        result = str(p.get("provider_result_id", p.get("id") or new_id()))
        row = w.connection.execute(
            "SELECT id FROM utterances WHERE run_id=? AND source_id IS ? AND provider_stream_id=? AND provider_result_id=?",
            (run_id, source_id, stream, result),
        ).fetchone()
        current = w.get("utterances", row["id"]) if row else None
        raw_words: object = p.get("words", [])
        require(
            isinstance(raw_words, list) and len(cast(list[object], raw_words)) <= 10000,
            "invalid_words",
            "Words must be a bounded list.",
            status=422,
        )
        words = cast(list[dict[str, Any]], raw_words)
        for word in words:
            if word.get("start_ms") is not None:
                require(
                    start - 20 <= word["start_ms"] <= word["end_ms"] <= end + 20,
                    "invalid_word_span",
                    "Provider word spans exceed the utterance; rejected, not clamped.",
                    status=422,
                )
        with w.transaction():
            if current and current["manual_text_lock"]:
                item = current
            else:
                data = {
                    "meeting_id": mid,
                    "run_id": run_id,
                    "source_id": source_id,
                    "provider_stream_id": stream,
                    "provider_result_id": result,
                    "start_ms": start,
                    "end_ms": end,
                    "text": text,
                    "language": language,
                    "confidence": p.get("confidence"),
                    "is_final": 1,
                    "timing_provenance": p.get("timing_provenance", "segment"),
                    "text_revision": current["text_revision"] + 1 if current else 1,
                }
                item = w.put(
                    "utterances",
                    current["id"] if current else p.get("id") or pending_id or new_id(),
                    data,
                    expected_revision=current["revision"] if current else None,
                )
                w.put(
                    "utterance_revisions",
                    new_id(),
                    {
                        "utterance_id": item["id"],
                        "actor": "import" if stream == "text-import" else "asr",
                        "before": current or {},
                        "after": item,
                        "revision": item["text_revision"],
                    },
                )
                w.connection.execute("DELETE FROM words WHERE utterance_id=?", (item["id"],))
                for ordinal, word in enumerate(words):
                    w.connection.execute(
                        "INSERT INTO words VALUES(?,?,?,?,?,?,?)",
                        (
                            item["id"],
                            ordinal,
                            word["text"],
                            word.get("start_ms"),
                            word.get("end_ms"),
                            word.get("confidence"),
                            word.get("speaker_id"),
                        ),
                    )
                if run["state"] == "active":
                    revised = w.invalidate(mid)
                    if hasattr(w, "analysis_scheduler"):
                        w.analysis_scheduler.observe_final(
                            mid, start, end, revised["transcript_revision"]
                        )
                if (
                    run["state"] == "active"
                    and is_question(text, language)
                    and not w.list("questions", {"utterance_id": item["id"]})
                ):
                    w.put(
                        "questions",
                        new_id(),
                        {
                            "meeting_id": mid,
                            "utterance_id": item["id"],
                            "text": text,
                            "detection_version": "en-es-v1",
                        },
                    )
            speaker_id = p.get("speaker_id")
            if not speaker_id and source_id:
                speaker_id = w.require_item("capture_sources", source_id).get("speaker_id")
            if speaker_id and not item["manual_speaker_lock"]:
                assign_speaker(w, item, speaker_id, manual=False)
            w.emit(
                "transcript.utterance_final",
                {
                    "id": item["id"],
                    "source_id": source_id,
                    "provider_stream_id": stream,
                    "provider_result_id": result,
                },
                meeting_id=mid,
                entity_id=item["id"],
                revision=item["revision"],
            )
            return {"item": item}

    r["transcript.ingest"] = ingest

    def edit(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            item = w.require_item("utterances", cast(str, p.get("id", p.get("utterance_id"))))
            text = str(p.get("text", p.get("changes", {}).get("text", "")))
            require(
                0 < len(text) <= 100000,
                "invalid_text",
                "Text cannot be empty or exceed 100000 characters.",
                status=422,
            )
            updated = w.put(
                "utterances",
                item["id"],
                {"text": text, "manual_text_lock": 1, "text_revision": item["text_revision"] + 1},
                expected_revision=p.get("expected_revision"),
            )
            w.put(
                "utterance_revisions",
                new_id(),
                {
                    "utterance_id": item["id"],
                    "actor": "user",
                    "before": item,
                    "after": updated,
                    "revision": updated["text_revision"],
                },
            )
            # Word-level ASR text no longer reliably describes an arbitrary manual edit.
            w.connection.execute("DELETE FROM words WHERE utterance_id=?", (item["id"],))
            w.invalidate(item["meeting_id"])
            return {"item": updated}

    r["transcript.edit"] = edit

    def split(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            item = w.require_item("utterances", cast(str, p.get("id", p.get("utterance_id"))))
            timestamp = int(p.get("timestamp_ms", p.get("split_ms", -1)))
            require(
                item["start_ms"] < timestamp < item["end_ms"],
                "invalid_split",
                "Choose a real timestamp inside the utterance.",
                status=422,
            )
            words = [
                dict(row)
                for row in w.connection.execute(
                    "SELECT * FROM words WHERE utterance_id=? ORDER BY ordinal", (item["id"],)
                ).fetchall()
            ]
            timed = [word for word in words if word["start_ms"] is not None]
            if timed:
                require(
                    any(abs(word["start_ms"] - timestamp) <= 20 for word in timed[1:]),
                    "word_boundary_required",
                    "Timestamp must coincide with a timed word boundary.",
                    status=422,
                )
                left = [word for word in words if word.get("start_ms", timestamp) < timestamp]
                right = [word for word in words if word not in left]
                left_text = " ".join(word["text"] for word in left)
                right_text = " ".join(word["text"] for word in right)
                provenance = item["timing_provenance"]
            else:
                offset = p.get("text_offset")
                require(
                    isinstance(offset, int) and 0 < offset < len(item["text"]),
                    "text_boundary_required",
                    "Without word timing, select both text offset and timestamp.",
                    status=422,
                )
                left_text = item["text"][:offset].rstrip()
                right_text = item["text"][offset:].lstrip()
                left = []
                right = []
                provenance = "user_estimated"
            require(
                bool(left_text) and bool(right_text),
                "empty_split",
                "Both split utterances need text.",
                status=422,
            )
            first = w.put(
                "utterances",
                item["id"],
                {
                    "text": left_text,
                    "end_ms": timestamp,
                    "manual_text_lock": 1,
                    "timing_provenance": provenance,
                    "text_revision": item["text_revision"] + 1,
                },
                expected_revision=p.get("expected_revision"),
            )
            second = w.put(
                "utterances",
                new_id(),
                {
                    k: v
                    for k, v in {
                        **item,
                        "text": right_text,
                        "start_ms": timestamp,
                        "manual_text_lock": 1,
                        "timing_provenance": provenance,
                        "provider_result_id": item["provider_result_id"] + ":split:" + new_id(),
                    }.items()
                    if k not in ("id", "revision", "created_at", "updated_at")
                },
            )
            w.connection.execute("DELETE FROM words WHERE utterance_id=?", (item["id"],))
            for destination, group in ((first, left), (second, right)):
                for ordinal, word in enumerate(group):
                    w.connection.execute(
                        "INSERT INTO words VALUES(?,?,?,?,?,?,?)",
                        (
                            destination["id"],
                            ordinal,
                            word["text"],
                            word["start_ms"],
                            word["end_ms"],
                            word["confidence"],
                            word["speaker_id"],
                        ),
                    )
            for assignment in w.list("speaker_assignments", {"utterance_id": item["id"]}):
                if assignment["end_ms"] > timestamp:
                    w.put(
                        "speaker_assignments",
                        new_id(),
                        {
                            "utterance_id": second["id"],
                            "speaker_id": assignment["speaker_id"],
                            "start_ms": max(timestamp, assignment["start_ms"]),
                            "end_ms": assignment["end_ms"],
                            "origin": "user_split",
                            "manual_lock": 1,
                        },
                    )
                    if assignment["start_ms"] >= timestamp:
                        w.delete(
                            "speaker_assignments",
                            assignment["id"],
                            expected_revision=assignment["revision"],
                        )
                    else:
                        w.put(
                            "speaker_assignments",
                            assignment["id"],
                            {"end_ms": timestamp, "manual_lock": 1},
                            expected_revision=assignment["revision"],
                        )
            w.put(
                "utterance_revisions",
                new_id(),
                {
                    "utterance_id": item["id"],
                    "actor": "user",
                    "before": item,
                    "after": {"left": first, "right": second},
                    "revision": first["text_revision"],
                },
            )
            w.invalidate(item["meeting_id"])
            return {"items": [first, second]}

    r["transcript.split"] = split

    def reassign(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            item = w.require_item("utterances", cast(str, p.get("id", p.get("utterance_id"))))
            require(
                item["revision"] == p.get("expected_revision"),
                "revision_conflict",
                "Utterance changed.",
                details={"latest": item},
            )
            assign_speaker(w, item, p["speaker_id"], manual=True)
            w.invalidate(item["meeting_id"])
            return {"item": w.require_item("utterances", item["id"])}

    r["transcript.reassign"] = reassign

    def comparison(p: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        candidate = w.require_item("transcript_runs", p["run_id"])
        mid = candidate["meeting_id"]
        require(
            not p.get("meeting_id") or p["meeting_id"] == mid,
            "source_scope",
            "Candidate belongs to another meeting.",
            status=422,
        )
        active = active_run(w, mid)
        require(
            candidate["state"] == "candidate" and candidate.get("completed_at") is not None,
            "candidate_incomplete",
            "Only complete candidate runs can be reviewed or promoted.",
        )
        return active, candidate, _candidate_plan(w, active, candidate)

    def diff(p: dict[str, Any]) -> dict[str, Any]:
        active, candidate, plan = comparison(p)
        meeting = w.require_item("meetings", candidate["meeting_id"])
        return {
            "active_run_id": active["id"],
            "candidate_run_id": candidate["id"],
            "input_revision": candidate.get("input_revision"),
            "current_revision": meeting["transcript_revision"],
            "boundary_policy": (
                "Crossing utterances are split only with real word timing. Unaligned originals and "
                "neighbors of unsplittable manual corrections remain unchanged; conflicting ASR "
                "segments are omitted. Use transcript split with a chosen timestamp and text "
                "boundary before re-transcribing an untimed partial utterance."
            ),
            "stale": candidate.get("input_revision") != meeting["transcript_revision"]
            or candidate.get("parent_run_id") != active["id"]
            or candidate.get("source_watermark")
            != transcript_source_watermark(w, meeting["id"], candidate.get("media_ids", [])),
            "diff": list(
                difflib.unified_diff(
                    [row["text"] for row in plan["before"]],
                    [row["text"] for row in plan["after"]],
                    lineterm="",
                )
            ),
            "resolved_transcript": [
                {
                    key: row[key]
                    for key in (
                        "id",
                        "source_id",
                        "start_ms",
                        "end_ms",
                        "text",
                        "manual_text_lock",
                        "manual_speaker_lock",
                        "assignments",
                    )
                }
                for row in plan["after"]
            ],
            **{
                key: plan[key]
                for key in (
                    "manual_conflicts",
                    "preserved_boundary_ids",
                    "suppressed_candidate_ids",
                    "source_mappings",
                )
            },
        }

    r["transcript.diff"] = diff

    def promote(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            active, candidate, plan = comparison(p)
            mid = candidate["meeting_id"]
            meeting = w.require_item("meetings", mid)
            require(
                candidate["revision"] == p.get("expected_revision"),
                "revision_conflict",
                "Candidate changed since it was reviewed.",
                details={"latest": candidate},
            )
            require(
                meeting["state"] not in ("starting", "recording", "paused", "stopping")
                and candidate.get("parent_run_id") == active["id"]
                and candidate.get("input_revision") == meeting["transcript_revision"]
                and candidate.get("source_watermark")
                == transcript_source_watermark(w, mid, candidate.get("media_ids", [])),
                "stale_candidate",
                "Transcript or source watermark changed; create and review a new candidate.",
            )
            require(
                not plan["manual_conflicts"] or p.get("acknowledge_manual_conflicts") is True,
                "manual_conflicts",
                "Review the resolved transcript preserving manual corrections and speaker locks.",
                details={"conflicts": plan["manual_conflicts"]},
            )
            require(
                bool(plan["after"]),
                "empty_transcript",
                "Resolved candidate contains no transcript.",
            )
            # Keep both source runs immutable. The resolved run includes untouched ranges,
            # manual text and source/time-mapped speaker assignments, not just ASR chunks.
            promoted = w.put(
                "transcript_runs",
                new_id(),
                {
                    "meeting_id": mid,
                    "source": "retranscribe",
                    "state": "candidate",
                    "parent_run_id": active["id"],
                    "reconciled_candidate_id": candidate["id"],
                    "provider_fingerprint": candidate["provider_fingerprint"],
                    "model_fingerprint": candidate["model_fingerprint"],
                    "language_policy": candidate["language_policy"],
                    "completed_at": now_ms(),
                    "input_revision": meeting["transcript_revision"],
                },
            )
            resolved: list[dict[str, Any]] = []
            for row in plan["after"]:
                item = w.put(
                    "utterances",
                    new_id(),
                    {
                        **{
                            key: value
                            for key, value in row.items()
                            if key
                            not in (
                                "id",
                                "revision",
                                "created_at",
                                "updated_at",
                                "run_id",
                                "words",
                                "assignments",
                                "origin_id",
                            )
                        },
                        "run_id": promoted["id"],
                        "provider_result_id": row["provider_result_id"]
                        + ":reconciled:"
                        + row["id"],
                        "reconciled_from_utterance_id": row["origin_id"],
                    },
                )
                for assignment in row["assignments"]:
                    low = max(item["start_ms"], assignment["start_ms"])
                    high = min(item["end_ms"], assignment["end_ms"])
                    if low < high:
                        w.put(
                            "speaker_assignments",
                            new_id(),
                            {
                                **{
                                    key: value
                                    for key, value in assignment.items()
                                    if key
                                    not in (
                                        "id",
                                        "revision",
                                        "created_at",
                                        "updated_at",
                                        "utterance_id",
                                    )
                                },
                                "utterance_id": item["id"],
                                "start_ms": low,
                                "end_ms": high,
                                "reconciled_from_assignment_id": assignment["id"],
                            },
                        )
                for ordinal, word in enumerate(row["words"]):
                    locked = {
                        assignment["speaker_id"]
                        for assignment in row["assignments"]
                        if assignment["manual_lock"]
                        and word["start_ms"] is not None
                        and assignment["start_ms"] < word["end_ms"]
                        and word["start_ms"] < assignment["end_ms"]
                    }
                    speaker_id = (
                        next(iter(locked))
                        if len(locked) == 1
                        else None
                        if locked
                        else word["speaker_id"]
                    )
                    w.connection.execute(
                        "INSERT INTO words VALUES(?,?,?,?,?,?,?)",
                        (
                            item["id"],
                            ordinal,
                            word["text"],
                            word["start_ms"],
                            word["end_ms"],
                            word["confidence"],
                            speaker_id,
                        ),
                    )
                for revision in all_items(
                    w, "utterance_revisions", {"utterance_id": row["origin_id"]}
                ):
                    w.put(
                        "utterance_revisions",
                        new_id(),
                        {
                            "utterance_id": item["id"],
                            "actor": revision["actor"],
                            "before": revision["before"],
                            "after": revision["after"],
                            "source_revision_id": revision["id"],
                        },
                    )
                resolved.append({**item, "origin_id": row["origin_id"], "projection_id": row["id"]})
            for run in (active, candidate):
                w.put(
                    "transcript_runs",
                    run["id"],
                    {"state": "archived"},
                    expected_revision=run["revision"],
                )
            updated = w.put(
                "transcript_runs",
                promoted["id"],
                {"state": "active"},
                expected_revision=promoted["revision"],
            )
            w.invalidate(mid)
            mapping = _remap_transcript_sources(w, mid, plan["before"], resolved)
            for row in resolved:
                if is_question(row["text"], row.get("language")) and not w.list(
                    "questions", {"utterance_id": row["id"]}
                ):
                    w.put(
                        "questions",
                        new_id(),
                        {
                            "meeting_id": mid,
                            "utterance_id": row["id"],
                            "text": row["text"],
                            "detection_version": "en-es-v1",
                        },
                    )
            return {
                "item": updated,
                "candidate_run_id": candidate["id"],
                "preserved_archived_run_id": active["id"],
                "source_mappings": mapping,
                "manual_corrections_preserved": True,
            }

    r["transcript.promote"] = promote
    r["speakers.list"] = lambda p: {
        "items": w.list("meeting_speakers", {"meeting_id": p["meeting_id"]})
    }

    def rename(p: dict[str, Any]) -> dict[str, Any]:
        name = str(p.get("display_name", p.get("name", ""))).strip()
        require(
            0 < len(name) <= 100,
            "invalid_name",
            "Speaker name is required and bounded.",
            status=422,
        )
        with w.transaction():
            item = w.put(
                "meeting_speakers",
                cast(str, p.get("id", p.get("speaker_id"))),
                {"display_name": name, "manual_name_lock": 1},
                expected_revision=p.get("expected_revision"),
            )
            w.invalidate(item["meeting_id"])
            return {"item": item}

    r["speakers.rename"] = rename

    def merge(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            source = w.require_item(
                "meeting_speakers", cast(str, p.get("source_id", p.get("speaker_id")))
            )
            target = w.require_item("meeting_speakers", p["target_id"])
            require(
                source["meeting_id"] == target["meeting_id"] and source["id"] != target["id"],
                "speaker_scope",
                "Merge needs two different speakers in one meeting.",
                status=422,
            )
            require(
                source["revision"] == p.get("expected_revision"),
                "revision_conflict",
                "Speaker changed.",
                details={"latest": source},
            )
            for assignment in all_items(w, "speaker_assignments", {"speaker_id": source["id"]}):
                w.put(
                    "speaker_assignments",
                    assignment["id"],
                    {"speaker_id": target["id"], "manual_lock": 1, "origin": "user_merge"},
                    expected_revision=assignment["revision"],
                )
                u = w.require_item("utterances", assignment["utterance_id"])
                w.put(
                    "utterances",
                    u["id"],
                    {"manual_speaker_lock": 1, "speaker_revision": u["speaker_revision"] + 1},
                    expected_revision=u["revision"],
                )
            w.connection.execute(
                "UPDATE words SET speaker_id=? WHERE speaker_id=?", (target["id"], source["id"])
            )
            w.connection.execute(
                "UPDATE speaker_clusters SET mapped_speaker_id=? WHERE mapped_speaker_id=?",
                (target["id"], source["id"]),
            )
            w.delete("meeting_speakers", source["id"], expected_revision=source["revision"])
            updated = w.put(
                "meeting_speakers",
                target["id"],
                {"manual_name_lock": 1},
                expected_revision=target["revision"],
            )
            w.invalidate(target["meeting_id"])
            return {"item": updated}

    r["speakers.merge"] = merge

    def speaker_split(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            old = w.require_item("meeting_speakers", cast(str, p.get("id", p.get("speaker_id"))))
            require(
                old["revision"] == p.get("expected_revision"),
                "revision_conflict",
                "Speaker changed.",
                details={"latest": old},
            )
            ids = p.get("utterance_ids", [])
            require(
                bool(ids), "selection_required", "Select utterances for speaker split.", status=422
            )
            new = w.put(
                "meeting_speakers",
                new_id(),
                {
                    "meeting_id": old["meeting_id"],
                    "source_kind": old["source_kind"],
                    "display_name": p.get("display_name", "Unknown"),
                    "manual_name_lock": 1,
                },
            )
            for uid in ids:
                u = w.require_item("utterances", uid)
                require(
                    u["meeting_id"] == old["meeting_id"],
                    "source_scope",
                    "Selected utterance belongs to another meeting.",
                    status=422,
                )
                assign_speaker(w, u, new["id"], manual=True)
            w.invalidate(old["meeting_id"])
            return {"item": new}

    r["speakers.split"] = speaker_split

    def analytics(p: dict[str, Any]) -> dict[str, Any]:
        run = active_run(w, p["meeting_id"])
        utterances = all_items(w, "utterances", {"run_id": run["id"]})
        ids = {u["id"] for u in utterances}
        utterance_map = {u["id"]: u for u in utterances}
        total = union_duration([(u["start_ms"], u["end_ms"]) for u in utterances])
        results: list[dict[str, Any]] = []
        for speaker in w.list("meeting_speakers", {"meeting_id": p["meeting_id"]}):
            turns = [
                a
                for a in all_items(w, "speaker_assignments", {"speaker_id": speaker["id"]})
                if a["utterance_id"] in ids
            ]
            duration = union_duration([(a["start_ms"], a["end_ms"]) for a in turns])
            text = " ".join(utterance_map[a["utterance_id"]]["text"] for a in turns)
            word_count = len(text.split())
            results.append(
                {
                    "speaker_id": speaker["id"],
                    "display_name": speaker["display_name"],
                    "talk_time_ms": duration,
                    "turns": len(turns),
                    "participation": duration / total if total else 0,
                    "words_per_minute": word_count * 60000 / duration if duration else None,
                    "filler_count": sum(
                        token.casefold().strip(".,") in ("um", "uh", "erm", "eh", "mmm")
                        for token in text.split()
                    ),
                    "overlap_ms": union_duration(
                        [(a["start_ms"], a["end_ms"]) for a in turns if a["overlap"]]
                    ),
                    "interruptions": None,
                }
            )
        return {
            "items": results,
            "formula_version": "interval-union-v1",
            "limitations": [
                "Unknown speakers and overlapping turns limit balance estimates.",
                "A speaker transition alone is not evidence of interruption.",
            ],
        }

    r["speakers.analytics"] = analytics


def assign_speaker(
    w: Workspace, utterance: dict[str, Any], speaker_id: str, *, manual: bool
) -> None:
    speaker = w.require_item("meeting_speakers", speaker_id)
    require(
        speaker["meeting_id"] == utterance["meeting_id"],
        "speaker_scope",
        "Speaker belongs to a different meeting.",
        status=422,
    )
    if not manual and utterance["manual_speaker_lock"]:
        return
    for assignment in w.list("speaker_assignments", {"utterance_id": utterance["id"]}):
        if manual or not assignment["manual_lock"]:
            w.delete(
                "speaker_assignments", assignment["id"], expected_revision=assignment["revision"]
            )
    w.put(
        "speaker_assignments",
        new_id(),
        {
            "utterance_id": utterance["id"],
            "speaker_id": speaker_id,
            "start_ms": utterance["start_ms"],
            "end_ms": utterance["end_ms"],
            "origin": "user" if manual else "inference",
            "manual_lock": int(manual),
        },
    )
    latest = w.require_item("utterances", utterance["id"])
    w.put(
        "utterances",
        utterance["id"],
        {"manual_speaker_lock": int(manual), "speaker_revision": latest["speaker_revision"] + 1},
        expected_revision=latest["revision"],
    )
