from __future__ import annotations

import hashlib
import json
import math
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.application.transcripts import active_run, all_items
from meeting_assistant.domain.errors import require
from meeting_assistant.domain.model import canonical, new_id, now_ms
from meeting_assistant.domain.ports import CommandRegistry

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def cosine(left: list[float], right: list[float]) -> float:
    require(
        len(left) == len(right) and bool(left),
        "voice_space_mismatch",
        "Speaker vectors have incompatible dimensions.",
        status=422,
    )
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return numerator / denominator if denominator else -1


class SpeakerEvidence:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def vector(self, object_id: str) -> list[float]:
        obj = self.workspace.require_item("objects", object_id)
        path = (self.workspace.root / obj["relative_path"]).resolve()
        require(
            path.is_relative_to(self.workspace.root),
            "unsafe_object",
            "Speaker vector path escapes workspace.",
            status=422,
        )
        content = path.read_bytes()
        require(
            hashlib.sha256(content).hexdigest() == obj["sha256"],
            "voice_integrity",
            "Stored voice evidence failed integrity validation.",
            status=409,
        )
        value: object = json.loads(content)
        require(
            isinstance(value, list)
            and 0 < len(cast(list[object], value)) <= 4096
            and all(
                isinstance(x, (float, int)) and math.isfinite(x) for x in cast(list[object], value)
            ),
            "invalid_voice_vector",
            "Speaker vector is invalid.",
            status=422,
        )
        return cast(list[float], value)

    def store_vector(self, vector: list[float], owner: str) -> str:
        require(
            0 < len(vector) <= 4096 and all(math.isfinite(x) for x in vector),
            "invalid_voice_vector",
            "Speaker evidence must be finite and bounded.",
            status=422,
        )
        identity = new_id()
        relative = "objects/" + identity + ".json"
        path = self.workspace.root / relative
        path.parent.mkdir(exist_ok=True, mode=0o700)
        content = canonical(vector).encode()
        path.write_bytes(content)
        path.chmod(0o600)
        self.workspace.put(
            "objects",
            identity,
            {
                "kind": "voice_vector",
                "relative_path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "owning_entity_id": owner,
            },
        )
        return identity

    def apply(self, meeting_id: str, source_kind: str, result: dict[str, Any]) -> dict[str, Any]:
        w = self.workspace
        fingerprint = result["model_fingerprint"]
        vectors: dict[str, list[float]] = result.get("speaker_embeddings", {})
        mapping: dict[str, str] = {}
        suggestions: list[dict[str, Any]] = []
        with w.transaction():
            existing = all_items(
                w,
                "speaker_clusters",
                {
                    "meeting_id": meeting_id,
                    "model_fingerprint": fingerprint,
                    "source_kind": source_kind,
                },
            )
            for label, vector in vectors.items():
                # Best cluster per speaker: every window stores another cluster for the matched
                # speaker, so a per-cluster margin would split one voice into new speakers.
                # ponytail: scan is linear in stored clusters; cap clusters per speaker if long
                # meetings lag.
                best_by_speaker: dict[str, tuple[float, dict[str, Any]]] = {}
                for cluster in existing:
                    if cluster.get("embedding_object_ref"):
                        similarity = cosine(vector, self.vector(cluster["embedding_object_ref"]))
                        prior = best_by_speaker.get(cluster["mapped_speaker_id"])
                        if prior is None or similarity > prior[0]:
                            best_by_speaker[cluster["mapped_speaker_id"]] = (similarity, cluster)
                ranked = sorted(
                    best_by_speaker.values(), key=lambda value: (-value[0], value[1]["id"])
                )
                best = ranked[0] if ranked else None
                confident = (
                    best is not None
                    and best[0] >= 0.72
                    and (len(ranked) == 1 or best[0] - ranked[1][0] >= 0.08)
                )
                if confident:
                    assert best is not None
                    speaker_id = best[1]["mapped_speaker_id"]
                else:
                    # Number speakers across the whole meeting, not per window or source.
                    number = (
                        len(
                            {
                                c["mapped_speaker_id"]
                                for c in all_items(
                                    w, "speaker_clusters", {"meeting_id": meeting_id}
                                )
                            }
                        )
                        + 1
                    )
                    speaker = w.put(
                        "meeting_speakers",
                        new_id(),
                        {
                            "meeting_id": meeting_id,
                            "source_kind": source_kind,
                            "display_name": f"Speaker {number}",
                            "color_token": f"speaker-{number}",
                        },
                    )
                    speaker_id = speaker["id"]
                object_id = self.store_vector(vector, meeting_id)
                w.put(
                    "speaker_clusters",
                    new_id(),
                    {
                        "meeting_id": meeting_id,
                        "source_kind": source_kind,
                        "provider_session_id": result.get("provider_session_id", meeting_id),
                        "provider_speaker_id": str(label),
                        "mapped_speaker_id": speaker_id,
                        "model_fingerprint": fingerprint,
                        "embedding_object_ref": object_id,
                    },
                )
                mapping[str(label)] = speaker_id
                meeting = w.require_item("meetings", meeting_id)
                for profile in w.list(
                    "voice_profiles", {"model_fingerprint": fingerprint, "enabled": 1}
                ):
                    scope = profile["matching_scope"]
                    allowed = (
                        scope.get("scope") == "workspace"
                        or scope.get("project_id") == meeting.get("project_id")
                        and meeting.get("project_id") is not None
                    )
                    if not allowed:
                        continue
                    similarity = cosine(vector, self.vector(profile["vector_object_ref"]))
                    if similarity >= 0.72:
                        suggestion = {
                            "speaker_id": speaker_id,
                            "person_id": profile["person_id"],
                            "profile_id": profile["id"],
                            "similarity": similarity,
                            "requires_confirmation": not profile.get("trusted_auto_match", False),
                        }
                        suggestions.append(suggestion)
                        if profile.get("trusted_auto_match"):
                            person = w.require_item("people", profile["person_id"])
                            speaker = w.require_item("meeting_speakers", speaker_id)
                            if not speaker["manual_name_lock"]:
                                w.put(
                                    "meeting_speakers",
                                    speaker_id,
                                    {
                                        "person_id": person["id"],
                                        "display_name": person["display_name"],
                                    },
                                    expected_revision=speaker["revision"],
                                )
            run = active_run(w, meeting_id)
            utterances = all_items(w, "utterances", {"run_id": run["id"]})
            turns = result.get("exclusive_turns", [])
            overlaps = [turn for turn in result.get("turns", []) if turn.get("overlap")]
            for utterance in utterances:
                if (
                    utterance.get("source_id")
                    and w.require_item("capture_sources", utterance["source_id"])["source_kind"]
                    != source_kind
                ):
                    continue
                if utterance["manual_speaker_lock"]:
                    continue
                relevant = [
                    turn
                    for turn in turns
                    if max(turn["start_ms"], utterance["start_ms"])
                    < min(turn["end_ms"], utterance["end_ms"])
                    and str(turn["local_speaker_id"]) in mapping
                ]
                if not relevant:
                    continue
                for old in w.list("speaker_assignments", {"utterance_id": utterance["id"]}):
                    if not old["manual_lock"]:
                        w.delete(
                            "speaker_assignments", old["id"], expected_revision=old["revision"]
                        )
                for turn in relevant + overlaps:
                    if str(turn["local_speaker_id"]) not in mapping:
                        continue
                    start = max(turn["start_ms"], utterance["start_ms"])
                    end = min(turn["end_ms"], utterance["end_ms"])
                    if start >= end:
                        continue
                    w.put(
                        "speaker_assignments",
                        new_id(),
                        {
                            "utterance_id": utterance["id"],
                            "speaker_id": mapping[str(turn["local_speaker_id"])],
                            "start_ms": start,
                            "end_ms": end,
                            "origin": "diarization",
                            "overlap": int(turn.get("overlap", False)),
                            "confidence": turn.get("confidence"),
                        },
                    )
                latest = w.require_item("utterances", utterance["id"])
                w.put(
                    "utterances",
                    latest["id"],
                    {"speaker_revision": latest["speaker_revision"] + 1},
                    expected_revision=latest["revision"],
                )
                w.emit(
                    "transcript.utterance_updated",
                    {"text_changed": False, "speaker_changed": True},
                    meeting_id=meeting_id,
                    entity_id=latest["id"],
                    revision=latest["revision"] + 1,
                )
            for suggestion in suggestions:
                w.emit(
                    "speaker.profile_suggestion",
                    suggestion,
                    meeting_id=meeting_id,
                    entity_id=suggestion["speaker_id"],
                )
            return {
                "mapping": mapping,
                "suggestions": suggestions,
                "coverage": result.get("coverage"),
                "model_fingerprint": fingerprint,
            }

    def finalize(self, meeting_id: str) -> dict[str, Any]:
        w = self.workspace
        clusters = all_items(w, "speaker_clusters", {"meeting_id": meeting_id})
        representatives: list[tuple[dict[str, Any], list[float]]] = []
        mapping: dict[str, str] = {}
        if not clusters:
            return {"state": "skipped", "reason": "No speaker evidence"}
        with w.transaction():
            for cluster in clusters:
                speaker = w.get("meeting_speakers", cluster["mapped_speaker_id"])
                if not speaker or not cluster.get("embedding_object_ref"):
                    continue
                vector = self.vector(cluster["embedding_object_ref"])
                ranked: list[tuple[float, dict[str, Any]]] = []
                if not speaker["manual_name_lock"]:
                    for previous, previous_vector in representatives:
                        if previous["model_fingerprint"] == cluster[
                            "model_fingerprint"
                        ] and previous.get("source_kind") == cluster.get("source_kind"):
                            ranked.append((cosine(vector, previous_vector), previous))
                ranked.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
                if (
                    ranked
                    and ranked[0][0] >= 0.78
                    and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08)
                ):
                    target = ranked[0][1]["mapped_speaker_id"]
                    mapping[speaker["id"]] = target
                    w.put(
                        "speaker_clusters",
                        cluster["id"],
                        {"mapped_speaker_id": target, "finalized": True},
                        expected_revision=cluster["revision"],
                    )
                else:
                    representatives.append((cluster, vector))
                    mapping.setdefault(speaker["id"], speaker["id"])
                    w.put(
                        "speaker_clusters",
                        cluster["id"],
                        {"finalized": True},
                        expected_revision=cluster["revision"],
                    )
            for assignment in all_items(w, "speaker_assignments"):
                target = mapping.get(assignment["speaker_id"])
                if not target or target == assignment["speaker_id"] or assignment["manual_lock"]:
                    continue
                utterance = w.require_item("utterances", assignment["utterance_id"])
                if utterance["meeting_id"] != meeting_id or utterance["manual_speaker_lock"]:
                    continue
                w.put(
                    "speaker_assignments",
                    assignment["id"],
                    {"speaker_id": target, "origin": "final_clustering"},
                    expected_revision=assignment["revision"],
                )
            w.emit(
                "speakers.finalized",
                {
                    "mapping": mapping,
                    "method": "accumulated_real_embeddings_v1",
                    "full_retained_audio": False,
                },
                meeting_id=meeting_id,
            )
        return {"mapping": mapping, "method": "accumulated_real_embeddings_v1"}

    async def diarize_retained(self, meeting_id: str, job_id: str) -> dict[str, Any]:
        w = self.workspace
        meeting = w.require_item("meetings", meeting_id)
        policy = meeting["config_snapshot"].get("diarization_policy")
        require(
            bool(policy),
            "diarization_policy_required",
            "Select a ready diarization policy before final processing.",
            status=503,
        )
        assert w.inference is not None
        for media in all_items(
            w, "media_segments", {"meeting_id": meeting_id, "state": "committed"}
        ):
            relative = "staging/" + new_id() + ".f32"
            path = w.root / relative
            try:
                asset = await w.native(
                    "media.decode_existing",
                    {
                        "media_id": media["id"],
                        "relative_path": media["relative_path"],
                        "meeting_id": meeting_id,
                        "job_id": job_id,
                        "temporary_decode_consent": True,
                        "output_relative_path": relative,
                        "timeline_start_ms": media["start_ms"],
                    },
                )
                require(
                    path.is_file()
                    and asset["decoded_relative_path"] == relative
                    and asset["sample_rate"] == 16000
                    and asset["channels"] == 1
                    and path.stat().st_size == asset["frames"] * 4,
                    "decoder_protocol",
                    "Retained-media decoder returned invalid canonical audio.",
                    status=422,
                )
                source = (
                    w.get("capture_sources", media["source_id"]) if media.get("source_id") else None
                )
                if (
                    source
                    and source["source_kind"] == "mic"
                    and meeting["config_snapshot"].get("source") == "both"
                ):
                    continue
                with path.open("rb") as stream:
                    offset = 0
                    while pcm := stream.read(12000 * 16 * 4):
                        require(
                            not w.require_item("jobs", job_id)["cancellation_requested"],
                            "cancelled",
                            "Final diarization cancelled.",
                        )
                        result = await w.inference.diarize(
                            pcm_float32=pcm,
                            sample_rate=16000,
                            policy=policy,
                            origin_ms=media["start_ms"] + offset,
                            final=True,
                            meeting=meeting,
                        )
                        self.apply(meeting_id, source["source_kind"] if source else "file", result)
                        offset += len(pcm) // 64
            finally:
                path.unlink(missing_ok=True)
        return {**self.finalize(meeting_id), "full_retained_audio": True}


def register_speakers(w: Workspace, r: CommandRegistry) -> None:
    service = SpeakerEvidence(w)
    w.speaker_evidence = service

    def create(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("consent") is True,
            "biometric_consent_required",
            "Voice profile enrollment is explicit opt-in; no calendar identity inference.",
            status=403,
        )
        person = w.require_item("people", p["person_id"])
        speaker = w.require_item("meeting_speakers", p["meeting_speaker_id"])
        meeting = w.require_item("meetings", speaker["meeting_id"])
        scope = p.get("scope", "workspace")
        require(
            scope in ("workspace", "project"),
            "invalid_scope",
            "Voice matching scope must be project or workspace.",
            status=422,
        )
        if scope == "project":
            require(
                p.get("project_id") == meeting.get("project_id")
                and p.get("project_id") is not None,
                "scope_mismatch",
                "Select the speaker meeting project explicitly.",
                status=422,
            )
        clusters = w.list("speaker_clusters", {"mapped_speaker_id": speaker["id"]})
        require(
            bool(clusters),
            "voice_evidence_unavailable",
            "Enable diarization and select sufficient speaker audio first. Enrollment cannot invent a voice vector.",
            status=503,
        )
        cluster = clusters[-1]
        profile_id = new_id()
        with w.transaction():
            object_id = service.store_vector(
                service.vector(cluster["embedding_object_ref"]), profile_id
            )
            profile = w.put(
                "voice_profiles",
                profile_id,
                {
                    "person_id": person["id"],
                    "model_fingerprint": cluster["model_fingerprint"],
                    "vector_object_ref": object_id,
                    "consent_at": now_ms(),
                    "matching_scope": {"scope": scope, "project_id": p.get("project_id")},
                    "enabled": 1,
                    "trusted_auto_match": bool(p.get("trusted_auto_match", False)),
                },
            )
            return {
                "item": profile,
                "sensitivity_notice": "Voice vectors are biometric data stored only in this workspace. Delete independently from transcript.",
            }

    r["voice_profiles.create"] = create

    def delete(p: dict[str, Any]) -> dict[str, Any]:
        with w.transaction():
            profile = w.require_item("voice_profiles", p["id"])
            obj = w.require_item("objects", profile["vector_object_ref"])
            result = w.delete(
                "voice_profiles", p["id"], expected_revision=cast(int, p.get("expected_revision"))
            )
            w.purge_objects([obj["id"]], profile["id"])
            return result

    r["voice_profiles.delete"] = delete

    def adopt(p: dict[str, Any]) -> dict[str, Any]:
        require(
            p.get("confirm") is True,
            "confirmation_required",
            "Confirm adopting a recurring person identity.",
            status=422,
        )
        person = w.require_item("people", p["person_id"])
        speaker = w.require_item("meeting_speakers", p["speaker_id"])
        return {
            "item": w.put(
                "meeting_speakers",
                speaker["id"],
                {
                    "person_id": person["id"],
                    "display_name": person["display_name"],
                    "manual_name_lock": 1,
                },
                expected_revision=p.get("expected_revision"),
            )
        }

    r["voice_profiles.adopt"] = adopt
