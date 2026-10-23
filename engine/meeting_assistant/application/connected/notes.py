from __future__ import annotations

import difflib
import json
from importlib.resources import files
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

from .common import all_items, existing, fail, fingerprint, is_json_list, required, save


def template_catalog() -> dict[str, Any]:
    return json.loads(files(__package__).joinpath("templates.json").read_text(encoding="utf-8"))


def validate_sections(
    sections: list[dict[str, Any]], evidence: dict[str, dict[str, Any]], note_ids: set[str]
) -> None:
    identities: set[str] = set()
    for section in sections:
        identity = required(section, "id")
        if identity in identities:
            fail("invalid_minutes", "Generated section IDs must be unique")
        identities.add(identity)
        if section.get("source") not in ("Transcript", "Human note", "Both") or not isinstance(
            section.get("text"), str
        ):
            fail("invalid_minutes", "Every section needs text and explicit source provenance")
        citations = section.get("citations", [])
        notes = section.get("note_ids", [])
        if (
            not is_json_list(citations)
            or not is_json_list(notes)
            or any(note not in note_ids for note in notes)
        ):
            fail("invalid_citation", "Minutes reference unknown human notes")
        if section["source"] in ("Transcript", "Both") and not citations:
            fail("invalid_citation", "Transcript-grounded sections require citations")
        if section["source"] in ("Human note", "Both") and not notes:
            fail("invalid_citation", "Human-note sections require note IDs")
        for citation in citations:
            source = evidence.get(citation.get("source_id"))
            if source is None:
                fail("invalid_citation", "Minutes reference an unknown transcript source")
            if citation.get("revision", source.get("revision")) != source.get("revision"):
                fail("invalid_citation", "Minutes cite a different transcript revision")
            start = (
                citation["start_ms"]
                if citation.get("start_ms") is not None
                else source.get("start_ms", 0)
            )
            end = (
                citation["end_ms"]
                if citation.get("end_ms") is not None
                else source.get("end_ms", 0)
            )
            if (
                not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
                or start > end
                or start < source.get("start_ms", 0)
                or end > source.get("end_ms", end)
            ):
                fail(
                    "invalid_citation",
                    "Minutes citation lies outside the selected transcript source",
                )


class NotesService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def seed_templates(self) -> None:
        for template in template_catalog()["templates"]:
            if self.workspace.get("templates", template["id"]) is not None:
                continue
            version_id = template["id"] + ":v1"

            def commit(
                workspace: Workspace, data: dict[str, Any] = template, version: str = version_id
            ) -> None:
                workspace.put(
                    "templates",
                    data["id"],
                    {
                        **data,
                        "builtin": True,
                        "built_in_key": data["id"],
                        "current_version_id": version,
                    },
                )
                workspace.put(
                    "template_versions",
                    version,
                    {
                        "template_id": data["id"],
                        "version": 1,
                        "section_schema": {"sections": data["sections"]},
                        "extraction_policy": template_catalog()["minutes_policy"],
                        "snapshot": data,
                        "content_hash": fingerprint(data),
                    },
                )

            self.workspace.atomic(commit)

    def get(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = required(payload, "meeting_id")
        existing(self.workspace, "meetings", meeting_id)
        return {
            "item": self.workspace.get("human_notes", meeting_id)
            or {"id": meeting_id, "meeting_id": meeting_id, "revision": 0, "blocks": []}
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = required(payload, "meeting_id")
        existing(self.workspace, "meetings", meeting_id)
        blocks = required(payload, "blocks")
        if not is_json_list(blocks):
            fail("invalid_notes", "Notes must be ordered Markdown blocks")
        ids: set[str] = set()
        for block in blocks:
            identity = required(block, "id")
            if (
                identity in ids
                or not isinstance(block.get("body", ""), str)
                or not isinstance(block.get("heading", ""), str)
            ):
                fail("invalid_notes", "Blocks require unique stable IDs and Markdown text")
            ids.add(identity)
            if len(block.get("body", "")) > 200000:
                fail("note_too_large", "One note block is limited to 200,000 characters")
            anchor = block.get("anchor")
            if anchor:
                source = existing(self.workspace, "utterances", required(anchor, "utterance_id"))
                if source.get("meeting_id") != meeting_id:
                    fail("invalid_anchor", "Note anchor must belong to this meeting")
                if anchor.get("time_ms") is not None and not source.get("start_ms", 0) <= anchor[
                    "time_ms"
                ] <= source.get("end_ms", 0):
                    fail("invalid_anchor", "Note time anchor is outside its utterance")
        item = self.workspace.put(
            "human_notes",
            meeting_id,
            {
                "id": meeting_id,
                "meeting_id": meeting_id,
                "blocks": [{**block, "order": index} for index, block in enumerate(blocks)],
            },
            expected_revision=payload.get("expected_revision"),
        )
        self.workspace.emit(
            "notes.saved",
            {"meeting_id": meeting_id, "revision": item["revision"]},
            meeting_id=meeting_id,
            revision=item["revision"],
        )
        return {"item": item, "save_state": "saved"}

    def templates(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"items": all_items(self.workspace, "templates")}

    def template(self, identity: str) -> dict[str, Any]:
        item = self.workspace.get("templates", identity)
        if item:
            return item
        fail("template_not_found", "The selected template does not exist")

    def template_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity = payload.get("id") or self.workspace.new_id()
        if any(item["id"] == identity for item in template_catalog()["templates"]):
            fail("builtin_readonly", "Clone a built-in template before editing it")
        sections = required(payload, "sections")
        if not is_json_list(sections) or len(
            {required(section, "id") for section in sections}
        ) != len(sections):
            fail("invalid_template", "Template sections require unique IDs")
        for section in sections:
            required(section, "title")
            required(section, "instruction")
        old = self.workspace.get("templates", identity)
        data: dict[str, Any] = {
            **payload,
            "id": identity,
            "name": required(payload, "name"),
            "sections": sections,
            "version": (old or {}).get("version", 0) + 1,
            "builtin": False,
            "built_in_key": None,
        }
        data.pop("expected_revision", None)
        version_id = identity + ":v" + str(data["version"])
        data["current_version_id"] = version_id

        def commit(workspace: Workspace) -> dict[str, Any]:
            item = workspace.put(
                "templates", identity, data, expected_revision=payload.get("expected_revision")
            )
            workspace.put(
                "template_versions",
                version_id,
                {
                    "template_id": identity,
                    "version": data["version"],
                    "section_schema": {"sections": sections},
                    "extraction_policy": {
                        "policy": template_catalog()["minutes_policy"],
                        "custom_fields": data.get("custom_fields", {}),
                    },
                    "snapshot": data,
                    "content_hash": fingerprint(data),
                },
            )
            return item

        return {"item": self.workspace.atomic(commit)}

    def template_clone(self, payload: dict[str, Any]) -> dict[str, Any]:
        template = self.template(required(payload, "id"))
        return self.template_save(
            {
                **template,
                "id": self.workspace.new_id(),
                "name": payload.get("name", template["name"] + " copy"),
                "version": 1,
                "expected_revision": None,
                "cloned_from": {"id": template["id"], "version": template["version"]},
            }
        )

    def template_select(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting = existing(self.workspace, "meetings", required(payload, "meeting_id"))
        template = self.template(required(payload, "template_id"))
        return {
            "item": self.workspace.put(
                "meetings",
                meeting["id"],
                {
                    **meeting,
                    "template_id": template["id"],
                    "template_version_id": template["current_version_id"],
                    "template_snapshot": template,
                },
                expected_revision=required(payload, "expected_revision"),
            )
        }

    def inputs(self, meeting_id: str) -> dict[str, Any]:
        meeting = existing(self.workspace, "meetings", meeting_id)
        notes: dict[str, Any] = self.workspace.get("human_notes", meeting_id) or {
            "id": meeting_id,
            "revision": 0,
            "blocks": [],
        }
        active_runs = {
            run["id"]
            for run in all_items(self.workspace, "transcript_runs", {"meeting_id": meeting_id})
            if run.get("state") == "active"
        }
        transcript = [
            item
            for item in all_items(self.workspace, "utterances", {"meeting_id": meeting_id})
            if item.get("run_id") in active_runs
            and item.get("is_final", True)
            and not item.get("deleted")
        ]
        transcript.sort(key=lambda item: (item.get("start_ms", 0), item["id"]))
        moments = [
            item
            for item in all_items(self.workspace, "moments", {"meeting_id": meeting_id})
            if item.get("include_in_minutes")
        ]
        version = (
            self.workspace.get("template_versions", meeting["template_version_id"])
            if meeting.get("template_version_id")
            else None
        )
        template: dict[str, Any] = (
            meeting.get("template_snapshot")
            or (version or {}).get("snapshot")
            or self.template(meeting.get("template_id") or "general")
        )
        return {
            "meeting_id": meeting_id,
            "meeting_revision": meeting["revision"],
            "note_revision": notes["revision"],
            "notes": notes["blocks"],
            "transcript": transcript,
            "transcript_revision": meeting.get("transcript_revision", 0),
            "template": template,
            "moments": moments,
            "language": meeting.get("config_snapshot", {}).get("output_language", "auto"),
        }

    async def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = required(payload, "meeting_id")
        inputs = self.inputs(meeting_id)
        if payload.get("note_revision") != inputs["note_revision"]:
            fail(
                "revision_conflict",
                "Save and select the exact note revision before generating minutes",
                current_revision=inputs["note_revision"],
            )
        if (
            "transcript_revision" in payload
            and payload["transcript_revision"] != inputs["transcript_revision"]
        ):
            fail("revision_conflict", "The active transcript revision changed")
        outline = inputs["notes"] or [
            {"id": section["id"], "heading": section["title"], "body": ""}
            for section in inputs["template"]["sections"]
        ]
        source_map = {item["id"]: item for item in inputs["transcript"]}
        note_ids = {block["id"] for block in inputs["notes"]}
        sections = [
            {
                "id": block["id"],
                "title": block.get("heading", "Notes"),
                "text": block.get("body", ""),
                "source": "Human note",
                "citations": [],
                "note_ids": [block["id"]],
            }
            for block in inputs["notes"]
            if block.get("body")
        ]
        conflicts: list[dict[str, Any]] = []
        generation = {
            "mode": "deterministic",
            "prompt_version": template_catalog()["minutes_policy"]["version"],
        }
        if payload.get("policy"):
            if self.workspace.inference is None:
                fail(
                    "inference_not_configured",
                    "Choose a provider before requesting generated minutes",
                )
            evidence = [
                {"id": item["id"], "source_id": item["id"], "type": "transcript", **item}
                for item in inputs["transcript"]
            ]
            evidence += [
                {
                    "id": block["id"],
                    "source_id": block["id"],
                    "type": "human_note",
                    "text": block.get("body", ""),
                    "heading": block.get("heading", ""),
                    "revision": inputs["note_revision"],
                }
                for block in inputs["notes"]
            ]
            generated = await self.workspace.inference.generate(
                "minutes",
                evidence,
                payload["policy"],
                language=inputs["language"],
                context={
                    "outline": outline,
                    "template": inputs["template"],
                    "provenance_policy": template_catalog()["minutes_policy"],
                    "note_revision": inputs["note_revision"],
                    "transcript_revision": inputs["transcript_revision"],
                },
            )
            result = generated.get("result", generated)
            sections: list[dict[str, Any]] = []
            for index, section in enumerate(required(result, "sections")):
                references = section.get("source_refs", [])
                transcript_refs = [
                    reference
                    for reference in references
                    if reference.get("source_id") in source_map
                ]
                referenced_notes = [
                    reference["source_id"]
                    for reference in references
                    if reference.get("source_id") in note_ids
                ]
                if any(
                    reference.get("source_id") not in source_map
                    and reference.get("source_id") not in note_ids
                    for reference in references
                ):
                    fail(
                        "invalid_citation", "Generated minutes cite evidence outside this snapshot"
                    )
                sections.append(
                    {
                        "id": section.get("id", f"section-{index}"),
                        "title": section.get("heading", "Minutes"),
                        "text": section.get("body"),
                        "source": section.get("provenance"),
                        "citations": transcript_refs,
                        "note_ids": section.get("note_ids") or referenced_notes,
                    }
                )
            ordering = {block["id"]: index for index, block in enumerate(outline)}
            if any(section["id"] not in ordering for section in sections):
                fail(
                    "outline_mismatch",
                    "Generated minutes must retain the selected outline's stable section IDs",
                )
            sections.sort(key=lambda section: ordering[section["id"]])
            conflicts = result.get("conflicts", [])
            generation = {
                "mode": "inference",
                "prompt_version": generated.get("prompt_version"),
                "input_fingerprint": generated.get("input_fingerprint"),
            }
        validate_sections(sections, source_map, note_ids)
        for conflict in conflicts:
            if (
                not conflict.get("note_ids")
                or not conflict.get("transcript_source_ids")
                or any(identity not in note_ids for identity in conflict["note_ids"])
                or any(identity not in source_map for identity in conflict["transcript_source_ids"])
            ):
                fail(
                    "invalid_conflict",
                    "Conflicts must expose both human-note and transcript sources",
                )
        for moment in inputs["moments"]:
            source = source_map.get(moment.get("utterance_id"))
            citation = (
                {
                    "source_id": source["id"],
                    "revision": source["revision"],
                    "start_ms": source.get("start_ms", 0),
                    "end_ms": source.get("end_ms", 0),
                }
                if source
                else None
            )
            sections.append(
                {
                    "id": "moment-" + moment["id"],
                    "title": "Important moment",
                    "text": moment.get("quote_snapshot")
                    or moment.get("quote")
                    or moment.get("text", ""),
                    "human_note": moment.get("note"),
                    "source": "Both" if moment.get("note") else "Transcript",
                    "citations": [citation] if citation else [],
                    "note_ids": [],
                    "important_moment_id": moment["id"],
                    "verbatim": True,
                    "provenance": {
                        "quote": "transcript_snapshot",
                        "note": "human_note" if moment.get("note") else None,
                        "source_id": moment.get("utterance_id"),
                        "start_ms": moment.get("start_ms"),
                        "end_ms": moment.get("end_ms"),
                        "source_unavailable": source is None,
                    },
                }
            )
        input_hash = fingerprint(inputs)
        previous = self.workspace.get("minutes_approved", meeting_id)
        before = json.dumps(
            (previous or {}).get("sections", []), ensure_ascii=False, indent=2
        ).splitlines()
        after = json.dumps(sections, ensure_ascii=False, indent=2).splitlines()
        identity = self.workspace.new_id()
        stale = fingerprint(self.inputs(meeting_id)) != input_hash
        item = save(
            self.workspace,
            "minutes_candidates",
            identity,
            {
                "id": identity,
                "meeting_id": meeting_id,
                "state": "candidate",
                "stale": stale,
                "input_fingerprint": input_hash,
                "input_revisions": {
                    "notes": inputs["note_revision"],
                    "transcript": inputs["transcript_revision"],
                },
                "template_snapshot": inputs["template"],
                "sections": sections,
                "conflicts": conflicts,
                "generation": generation,
                "language": inputs["language"],
                "approved_base_revision": (previous or {}).get("revision"),
                "diff": "\n".join(
                    difflib.unified_diff(
                        before, after, fromfile="approved", tofile="candidate", lineterm=""
                    )
                ),
            },
        )
        return {"item": item, "human_notes_unchanged": True}

    def approve(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = existing(self.workspace, "minutes_candidates", required(payload, "id"))
        meeting_id = candidate["meeting_id"]
        if fingerprint(self.inputs(meeting_id)) != candidate["input_fingerprint"]:
            fail("stale_candidate", "Minutes evidence changed; regenerate before approving")
        approved = self.workspace.get("minutes_approved", meeting_id)
        expected = payload.get("expected_revision")
        if expected != ((approved or {}).get("revision")):
            fail("revision_conflict", "Approved minutes changed while this draft was reviewed")

        def commit(workspace: Workspace) -> dict[str, Any]:
            item = workspace.put(
                "minutes_approved",
                meeting_id,
                {
                    **candidate,
                    "id": meeting_id,
                    "candidate_id": candidate["id"],
                    "state": "approved",
                },
                expected_revision=expected,
            )
            save(
                workspace,
                "minutes_candidates",
                candidate["id"],
                {**candidate, "state": "approved"},
                candidate,
            )
            return item

        return {"item": self.workspace.atomic(commit)}

    def edit(self, payload: dict[str, Any]) -> dict[str, Any]:
        approved = existing(self.workspace, "minutes_approved", required(payload, "meeting_id"))
        sections = required(payload, "sections")
        known = {section["id"] for section in approved["sections"]}
        if {section.get("id") for section in sections} != known:
            fail(
                "invalid_minutes_edit",
                "Editing approved sections must retain their stable IDs and provenance",
            )
        by_id = {section["id"]: section for section in approved["sections"]}
        edited = [
            {
                **by_id[section["id"]],
                "title": section.get("title", by_id[section["id"]].get("title")),
                "text": str(required(section, "text")),
                "manually_edited": True,
            }
            for section in sections
        ]
        item = self.workspace.put(
            "minutes_approved",
            approved["id"],
            {**approved, "sections": edited, "manually_edited": True},
            expected_revision=required(payload, "expected_revision"),
        )
        return {"item": item}

    def minutes(self, payload: dict[str, Any]) -> dict[str, Any]:
        meeting_id = required(payload, "meeting_id")
        return {
            "item": self.workspace.get("minutes_approved", meeting_id),
            "candidates": all_items(
                self.workspace, "minutes_candidates", {"meeting_id": meeting_id}
            ),
        }

    def invalidate(self, meeting_id: str) -> None:
        for candidate in all_items(
            self.workspace, "minutes_candidates", {"meeting_id": meeting_id}
        ):
            if not candidate.get("stale"):
                save(
                    self.workspace,
                    "minutes_candidates",
                    candidate["id"],
                    {**candidate, "stale": True},
                    candidate,
                )
        approved = self.workspace.get("minutes_approved", meeting_id)
        if approved and not approved.get("stale"):
            save(
                self.workspace,
                "minutes_approved",
                meeting_id,
                {**approved, "stale": True},
                approved,
            )
