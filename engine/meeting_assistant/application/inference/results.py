"""Strict completed-result schemas and source validation."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal, cast

from pydantic import Field

from ...infrastructure.providers.contracts import ProviderError, Value


class SourceRef(Value):
    source_id: str = Field(min_length=1, max_length=200)
    revision: int | None = Field(ge=0)
    start_ms: int | None = Field(ge=0)
    end_ms: int | None = Field(ge=0)
    quote: str | None = Field(max_length=4000)


class Section(Value):
    heading: str = Field(min_length=1, max_length=200)
    body: str = Field(max_length=20000)
    source_refs: list[SourceRef] = Field(max_length=100)
    provenance: Literal["Transcript", "Human note", "Both"]


class SummaryResult(Value):
    sections: list[Section] = Field(max_length=40)
    citations: list[SourceRef] = Field(max_length=200)


class ActionCandidate(Value):
    description: str = Field(min_length=1, max_length=2000)
    assignee: str | None = Field(max_length=200)
    deadline_text: str | None = Field(max_length=300)
    due_date: date | None
    priority: Literal["low", "normal", "high", "urgent"]
    source_refs: list[SourceRef] = Field(min_length=1, max_length=20)


class ActionsResult(Value):
    candidates: list[ActionCandidate] = Field(max_length=100)


class Topic(Value):
    title: str = Field(min_length=1, max_length=200)
    parent_key: str | None = Field(max_length=200)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    sentiment: Literal["positive", "neutral", "negative", "mixed"] | None
    source_refs: list[SourceRef] = Field(min_length=1, max_length=50)


class TopicsResult(Value):
    topics: list[Topic] = Field(max_length=100)


class Artifact(Value):
    title: str = Field(min_length=1, max_length=2000)
    status: Literal["proposed", "confirmed", "open", "resolved", "superseded"]
    owner: str | None = Field(max_length=200)
    date: date | None
    source_refs: list[SourceRef] = Field(min_length=1, max_length=30)


class ArtifactsResult(Value):
    decisions: list[Artifact] = Field(max_length=100)
    risks: list[Artifact] = Field(max_length=100)
    milestones: list[Artifact] = Field(max_length=100)
    deliverables: list[Artifact] = Field(max_length=100)
    follow_ups: list[Artifact] = Field(max_length=100)


class Cue(Value):
    kind: Literal["question", "clarification", "risk", "reminder", "answer"]
    text: str = Field(min_length=1, max_length=1000)
    source_refs: list[SourceRef] = Field(min_length=1, max_length=20)


class CuesResult(Value):
    cues: list[Cue] = Field(max_length=20)


class DocumentResult(Value):
    sections: list[Section] = Field(max_length=100)
    warnings: list[str] = Field(max_length=50)


class MinutesSection(Section):
    id: str = Field(min_length=1, max_length=100)
    note_ids: list[str] = Field(max_length=100)


class Conflict(Value):
    note_ids: list[str] = Field(min_length=1, max_length=20)
    transcript_source_ids: list[str] = Field(min_length=1, max_length=20)
    message: str = Field(min_length=1, max_length=2000)


class MinutesResult(Value):
    sections: list[MinutesSection] = Field(max_length=100)
    warnings: list[str] = Field(max_length=50)
    conflicts: list[Conflict] = Field(max_length=50)


class AskResult(Value):
    answer: str = Field(max_length=20000)
    citations: list[SourceRef] = Field(max_length=100)
    insufficient_evidence: bool


RESULT_MODELS: dict[str, type[Value]] = {
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


def validate_sources(result: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    sources = {str(item.get("source_id", item.get("id"))): item for item in evidence}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            value = cast(dict[str, Any], value)
            if "source_id" in value:
                source = sources.get(value["source_id"])
                if source is None:
                    raise ProviderError(
                        "invalid_output", "Generated citation references unselected evidence"
                    )
                expected_revision = source.get("revision", source.get("text_revision"))
                if value.get("revision") != expected_revision:
                    raise ProviderError(
                        "invalid_output",
                        "Generated citation references a different source revision",
                    )
                start, end = value.get("start_ms"), value.get("end_ms")
                if (
                    source.get("start_ms") is not None
                    and source.get("end_ms") is not None
                    and (start is None or end is None)
                ):
                    raise ProviderError(
                        "invalid_output",
                        "Transcript citations require exact source time coordinates",
                    )
                if (start is None) != (end is None) or (
                    start is not None
                    and end is not None
                    and (
                        start > end
                        or start < source.get("start_ms", 0)
                        or end > source.get("end_ms", end)
                    )
                ):
                    raise ProviderError(
                        "invalid_output", "Generated citation is outside its source time range"
                    )
                quote = value.get("quote")
                if quote and quote not in source.get("text", source.get("body_markdown", "")):
                    raise ProviderError(
                        "invalid_output", "Generated citation quote is not verbatim evidence"
                    )
            refs = value.get("source_refs")
            if "body" in value and value.get("body") and refs is not None:
                provenance = value.get("provenance")
                if not refs:
                    raise ProviderError(
                        "invalid_output", "Generated factual section has no evidence"
                    )
                kinds = {
                    sources.get(ref["source_id"], {}).get(
                        "kind", sources.get(ref["source_id"], {}).get("type", "transcript")
                    )
                    for ref in refs
                }
                has_note = bool(kinds & {"human_note", "note"})
                has_transcript = bool(kinds - {"human_note", "note"})
                expected = (
                    "Both"
                    if has_note and has_transcript
                    else "Human note"
                    if has_note
                    else "Transcript"
                )
                if provenance != expected:
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
    note_ids = {
        key
        for key, item in sources.items()
        if item.get("kind", item.get("type")) in {"human_note", "note"}
    }
    for section in result.get("sections", []):
        if "note_ids" in section and any(note not in note_ids for note in section["note_ids"]):
            raise ProviderError("invalid_output", "Minutes reference unknown human notes")
    for conflict in result.get("conflicts", []):
        if any(key not in note_ids for key in conflict["note_ids"]) or any(
            key not in sources or key in note_ids for key in conflict["transcript_source_ids"]
        ):
            raise ProviderError("invalid_output", "Minutes conflict references unknown sources")
