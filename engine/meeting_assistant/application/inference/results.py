"""Strict completed-result schemas and source validation."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, cast, get_args, get_origin

from pydantic import Field, model_validator

from ...infrastructure.providers.contracts import ProviderError, Value


class Result(Value):
    # A model that leaves out a nullable key means null and one that leaves out a list means
    # empty; unknown keys stay forbidden.
    @model_validator(mode="before")
    @classmethod
    def _missing_optional_is_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = cast(dict[str, Any], data)
            for name, field in cls.model_fields.items():
                if name not in data:
                    if type(None) in get_args(field.annotation):
                        data = {**data, name: None}
                    elif get_origin(field.annotation) is list:
                        data = {**data, name: []}
        return data


class SourceRef(Result):
    source_id: str = Field(min_length=1, max_length=200)
    revision: int | None = Field(ge=0)
    start_ms: int | None = Field(ge=0)
    end_ms: int | None = Field(ge=0)
    quote: str | None = Field(max_length=4000)


class Section(Result):
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=20000)
    source_refs: list[SourceRef] = Field(max_length=100)
    provenance: Literal["Transcript", "Human note", "Both"]


class SummaryResult(Result):
    sections: list[Section] = Field(max_length=40)
    citations: list[SourceRef] = Field(max_length=200)


class ActionCandidate(Result):
    description: str = Field(min_length=1, max_length=2000)
    assignee: str | None = Field(max_length=200)
    deadline_text: str | None = Field(max_length=300)
    due_date: date | None
    priority: Literal["low", "normal", "high", "urgent"]
    source_refs: list[SourceRef] = Field(min_length=1, max_length=20)


class ActionsResult(Result):
    candidates: list[ActionCandidate] = Field(max_length=100)


class Topic(Result):
    title: str = Field(min_length=1, max_length=200)
    parent_key: str | None = Field(max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    sentiment: Literal["positive", "neutral", "negative", "mixed"] | None
    source_refs: list[SourceRef] = Field(min_length=1, max_length=50)


class TopicsResult(Result):
    topics: list[Topic] = Field(max_length=100)


class Artifact(Result):
    title: str = Field(min_length=1, max_length=2000)
    status: Literal["proposed", "confirmed", "open", "resolved", "superseded"]
    owner: str | None = Field(max_length=200)
    date: date | None
    source_refs: list[SourceRef] = Field(min_length=1, max_length=30)


class ArtifactsResult(Result):
    decisions: list[Artifact] = Field(max_length=100)
    risks: list[Artifact] = Field(max_length=100)
    milestones: list[Artifact] = Field(max_length=100)
    deliverables: list[Artifact] = Field(max_length=100)
    follow_ups: list[Artifact] = Field(max_length=100)


class Cue(Result):
    kind: Literal["question", "clarification", "risk", "reminder", "answer"]
    text: str = Field(min_length=1, max_length=1000)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=20)


class CuesResult(Result):
    cues: list[Cue] = Field(max_length=20)


class DocumentResult(Result):
    sections: list[Section] = Field(max_length=100)
    warnings: list[str] = Field(max_length=50)


class MinutesSection(Section):
    id: str = Field(min_length=1, max_length=100)
    note_ids: list[str] = Field(max_length=100)


class Conflict(Result):
    note_ids: list[str] = Field(min_length=1, max_length=20)
    transcript_source_ids: list[str] = Field(min_length=1, max_length=20)
    message: str = Field(min_length=1, max_length=2000)


class MinutesResult(Result):
    sections: list[MinutesSection] = Field(max_length=100)
    warnings: list[str] = Field(max_length=50)
    conflicts: list[Conflict] = Field(max_length=50)


class AskResult(Result):
    answer: str = Field(max_length=20000)
    citations: list[SourceRef] = Field(max_length=100)
    insufficient_evidence: bool


RESULT_MODELS: dict[str, type[Result]] = {
    "summary": SummaryResult,
    "actions": ActionsResult,
    "topics": TopicsResult,
    "artifacts": ArtifactsResult,
    "cues": CuesResult,
    "briefings": DocumentResult,
    "minutes": MinutesResult,
    "digest": DocumentResult,
    "memory": DocumentResult,
    "ask": AskResult,
}


def _source_map(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("source_id", item.get("id"))): item for item in evidence}


def _citation_error(ref: dict[str, Any], sources: dict[str, dict[str, Any]]) -> str | None:
    source = sources.get(ref["source_id"])
    if source is None:
        return "Generated citation references unselected evidence"
    expected_revision = source.get("revision", source.get("text_revision"))
    if ref.get("revision") != expected_revision:
        return "Generated citation references a different source revision"
    start, end = ref.get("start_ms"), ref.get("end_ms")
    if (
        source.get("start_ms") is not None
        and source.get("end_ms") is not None
        and (start is None or end is None)
    ):
        return "Transcript citations require exact source time coordinates"
    if (start is None) != (end is None) or (
        start is not None
        and end is not None
        and (start > end or start < source.get("start_ms", 0) or end > source.get("end_ms", end))
    ):
        return "Generated citation is outside its source time range"
    quote = ref.get("quote")
    if quote and quote not in source.get("text", source.get("body_markdown", "")):
        return "Generated citation quote is not verbatim evidence"
    return None


def _provenance(refs: list[dict[str, Any]], sources: dict[str, dict[str, Any]]) -> str:
    kinds = {
        sources.get(ref["source_id"], {}).get(
            "kind", sources.get(ref["source_id"], {}).get("type", "transcript")
        )
        for ref in refs
    }
    has_note = bool(kinds & {"human_note", "note"})
    has_transcript = bool(kinds - {"human_note", "note"})
    return "Both" if has_note and has_transcript else "Human note" if has_note else "Transcript"


def _note_ids(sources: dict[str, dict[str, Any]]) -> set[str]:
    return {
        key
        for key, item in sources.items()
        if item.get("kind", item.get("type")) in {"human_note", "note"}
    }


def prune_sources(result: Any, evidence: list[dict[str, Any]]) -> None:
    """Drop what the model cited wrongly instead of rejecting the whole result.

    Removes citations that fail `validate_sources`, then list items left without evidence or with
    a reversed time range, and corrects provenance and note ids from the evidence. Nothing is
    added or reworded.
    """
    sources = _source_map(evidence)
    note_ids = _note_ids(sources)

    def visit(value: Any) -> Any:
        if isinstance(value, dict):
            value = cast(dict[str, Any], value)
            for key in ("source_refs", "citations"):
                refs = value.get(key)
                if isinstance(refs, list):
                    value[key] = [
                        ref
                        for ref in cast(list[Any], refs)
                        if not (
                            isinstance(ref, dict)
                            and "source_id" in ref
                            and _citation_error(cast(dict[str, Any], ref), sources)
                        )
                    ]
            refs = value.get("source_refs")
            if value.get("body") and refs:
                value["provenance"] = _provenance(cast(list[dict[str, Any]], refs), sources)
            for key, child in value.items():
                value[key] = visit(child)
        elif isinstance(value, list):
            items = [visit(child) for child in cast(list[Any], value)]
            value = [item for item in items if not _unsupported(item)]
        return value

    def _unsupported(item: Any) -> bool:
        if not isinstance(item, dict):
            return False
        item = cast(dict[str, Any], item)
        start, end = item.get("start_ms"), item.get("end_ms")
        reversed_range = start is not None and end is not None and start > end
        no_evidence = (
            "source_refs" in item
            and not item["source_refs"]
            and ("body" not in item or bool(item["body"]))
        )
        return reversed_range or no_evidence

    if isinstance(result, dict):
        result = cast(dict[str, Any], result)
        visit(result)  # also filters top-level `citations`
        for section in result.get("sections", []):
            if isinstance(section, dict):
                entry = cast(dict[str, Any], section)
                if isinstance(entry.get("note_ids"), list):
                    entry["note_ids"] = [
                        n for n in cast(list[Any], entry["note_ids"]) if n in note_ids
                    ]
        if isinstance(result.get("conflicts"), list):
            conflicts = [
                cast(dict[str, Any], c) for c in result["conflicts"] if isinstance(c, dict)
            ]
            result["conflicts"] = [
                conflict
                for conflict in conflicts
                if all(key in note_ids for key in conflict.get("note_ids", []))
                and all(
                    key in sources and key not in note_ids
                    for key in conflict.get("transcript_source_ids", [])
                )
            ]


def validate_sources(result: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    sources = _source_map(evidence)

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value = cast(dict[str, Any], value)
            if "source_id" in value:
                error = _citation_error(value, sources)
                if error:
                    raise ProviderError("invalid_output", error)
            refs = value.get("source_refs")
            if "body" in value and value.get("body") and refs is not None:
                if not refs:
                    raise ProviderError(
                        "invalid_output", "Generated factual section has no evidence"
                    )
                if value.get("provenance") != _provenance(refs, sources):
                    raise ProviderError(
                        "invalid_output", "Generated section provenance misrepresents its evidence"
                    )
            if (
                "start_ms" in value
                and "end_ms" in value
                and value["start_ms"] is not None
                and value["end_ms"] is not None
                and value["start_ms"] > value["end_ms"]
            ):
                raise ProviderError("invalid_output", "Generated time range is reversed")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            value = cast(list[Any], value)
            for child in value:
                visit(child)

    visit(result)
    if (
        result.get("answer")
        and not result.get("insufficient_evidence")
        and not result.get("citations")
    ):
        raise ProviderError("invalid_output", "Grounded Ask answer requires citations")
    note_ids = _note_ids(sources)
    for section in result.get("sections", []):
        if "note_ids" in section and any(note not in note_ids for note in section["note_ids"]):
            raise ProviderError("invalid_output", "Minutes reference unknown human notes")
    for conflict in result.get("conflicts", []):
        if any(key not in note_ids for key in conflict["note_ids"]) or any(
            key not in sources or key in note_ids for key in conflict["transcript_source_ids"]
        ):
            raise ProviderError("invalid_output", "Minutes conflict references unknown sources")
