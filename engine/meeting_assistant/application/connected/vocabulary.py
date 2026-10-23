from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace

from .common import all_items, existing, fail, fingerprint, is_json_list, required

MODES = {"prompt_hint", "decoder_bias", "keyterms", "phrase_list", "custom_model", "none"}


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def compile_terms(
    terms: list[dict[str, Any]],
    project_id: str | None,
    capabilities: dict[str, Any],
    *,
    acknowledgement: str | None = None,
) -> dict[str, Any]:
    mode = capabilities.get("vocabulary_mode", "none")
    if mode not in MODES:
        fail("invalid_capability", "Unknown vocabulary compilation mode")
    selected: dict[str, dict[str, Any]] = {}
    for item in sorted(terms, key=lambda term: (term.get("project_id") is not None, term["id"])):
        if item.get("project_id") not in (None, project_id):
            continue
        selected[normalize(item["spelling"])] = item
    ordered = sorted(
        (item for item in selected.values() if item.get("enabled", True)),
        key=lambda item: (-int(item.get("priority", 0)), item["id"]),
    )
    limit = int(capabilities.get("vocabulary_max_terms", 0))
    char_limit = int(capabilities.get("vocabulary_max_chars", 0))
    byte_limit = int(capabilities.get("vocabulary_max_bytes", 0))
    term_chars = int(capabilities.get("vocabulary_max_term_chars", 0))
    if min(limit, char_limit, byte_limit, term_chars) < 0:
        fail("invalid_capability", "Vocabulary limits cannot be negative")
    included: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    length = byte_length = effective_count = 0
    for term in ordered:
        phrases = list(dict.fromkeys([term["spelling"], *term.get("aliases", [])]))
        hint = term["spelling"]
        if mode == "prompt_hint":
            if term.get("aliases"):
                hint += " (" + ", ".join(term["aliases"]) + ")"
            if term.get("pronunciation"):
                hint += " [" + term["pronunciation"] + "]"
        else:
            hint = ", ".join(phrases)
        slots = len(phrases) if mode in ("keyterms", "phrase_list", "decoder_bias") else 1
        additional = len(hint) + (2 if included else 0)
        additional_bytes = len(hint.encode("utf-8")) + (2 if included else 0)
        reason = None
        if mode == "none":
            reason = "unsupported_backend"
        elif limit and effective_count + slots > limit:
            reason = "term_limit"
        elif term_chars and len(hint) > term_chars:
            reason = "term_length"
        elif char_limit and length + additional > char_limit:
            reason = "character_limit"
        elif byte_limit and byte_length + additional_bytes > byte_limit:
            reason = "byte_limit"
        if reason:
            omitted.append({"id": term["id"], "spelling": term["spelling"], "reason": reason})
        else:
            included.append(
                {
                    "id": term["id"],
                    "spelling": term["spelling"],
                    "aliases": term.get("aliases", []),
                    "pronunciation": term.get("pronunciation") if mode == "prompt_hint" else None,
                    "language": term.get("language"),
                    "priority": term.get("priority", 0),
                    "hint": hint,
                    "hints": phrases,
                }
            )
            effective_count += slots
            length += additional
            byte_length += additional_bytes
    revision = fingerprint(
        [
            {key: value for key, value in item.items() if key not in ("updated_at", "created_at")}
            for item in ordered
        ]
    )
    effective_hash = fingerprint(included)
    acknowledgement_key = fingerprint(
        {
            "revision": revision,
            "effective_hash": effective_hash,
            "omitted": omitted,
            "capabilities": capabilities,
        }
    )
    return {
        "mode": mode,
        "terms": included,
        "prompt_hint": ", ".join(item["hint"] for item in included)
        if mode == "prompt_hint"
        else None,
        "omitted": omitted,
        "acknowledgement_required": bool(omitted) and acknowledgement != acknowledgement_key,
        "acknowledgement_key": acknowledgement_key,
        "vocabulary_revision": revision,
        "effective_hash": effective_hash,
        "hot_update": bool(capabilities.get("vocabulary_hot_update", False)),
        "applies_at": "now"
        if capabilities.get("vocabulary_hot_update")
        else capabilities.get("vocabulary_boundary", "next_meeting"),
        "limitation": "Hints do not guarantee spelling; aliases never replace transcript text.",
    }


class VocabularyService:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    def list(self, payload: dict[str, Any]) -> dict[str, Any]:
        terms = all_items(self.workspace, "vocabulary")
        return {
            "items": [
                term
                for term in terms
                if term.get("project_id") in (None, payload.get("project_id"))
            ]
        }

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        spelling = str(required(payload, "spelling")).strip()
        if not spelling or len(spelling) > 512:
            fail("invalid_input", "Vocabulary spelling must contain 1–512 characters")
        aliases = payload.get("aliases", [])
        if not is_json_list(aliases) or any(
            not isinstance(alias, str) or len(alias) > 512 for alias in aliases
        ):
            fail("invalid_input", "Aliases must be a list of text hints")
        identity = payload.get("id") or self.workspace.new_id()
        data: dict[str, Any] = {
            "id": identity,
            "spelling": spelling,
            "aliases": aliases,
            "pronunciation": payload.get("pronunciation"),
            "language": payload.get("language"),
            "priority": int(payload.get("priority", 0)),
            "enabled": bool(payload.get("enabled", True)),
            "project_id": payload.get("project_id"),
        }
        item = self.workspace.put(
            "vocabulary", identity, data, expected_revision=payload.get("expected_revision")
        )
        self.workspace.emit(
            "vocabulary.changed", {"id": identity}, entity_id=identity, revision=item["revision"]
        )
        return {"item": item}

    def delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": self.workspace.delete(
                "vocabulary",
                required(payload, "id"),
                expected_revision=required(payload, "expected_revision"),
            )
        }

    async def compile(self, payload: dict[str, Any]) -> dict[str, Any]:
        profile: dict[str, Any] | None = None
        if payload.get("profile_id"):
            profile = existing(self.workspace, "model_profiles", payload["profile_id"])
            capabilities = dict(profile.get("resolved_capabilities", {}))
            connection = (
                existing(self.workspace, "provider_connections", profile["connection_id"])
                if profile.get("connection_id")
                else {}
            )
            backend = connection.get("adapter_id") or profile.get("adapter_id")
        else:
            capabilities = dict(required(payload, "capabilities"))
            backend = payload.get("backend")
            if payload.get("apply"):
                fail(
                    "profile_required",
                    "Applying vocabulary requires a configured ASR profile, not caller-supplied capability claims",
                )
        if isinstance(capabilities.get("vocabulary"), dict):
            declared = capabilities["vocabulary"]
            capabilities = {
                **capabilities,
                **{"vocabulary_" + key: value for key, value in declared.items()},
            }
            update_policy = declared.get("update_policy", "next_meeting")
            capabilities["vocabulary_hot_update"] = update_policy in ("immediate", "hot_update")
            capabilities["vocabulary_boundary"] = update_policy
        if backend in ("parakeet", "sherpa_parakeet_greedy", "sherpa_onnx"):
            capabilities["vocabulary_mode"] = "none"
            capabilities["vocabulary_hot_update"] = False
        result = compile_terms(
            all_items(self.workspace, "vocabulary"),
            payload.get("project_id"),
            capabilities,
            acknowledgement=payload.get("acknowledgement"),
        )
        token_limit = int(capabilities.get("vocabulary_max_tokens", 0))
        if token_limit < 0:
            fail("invalid_capability", "Vocabulary token limits cannot be negative")
        if token_limit and result["terms"]:
            candidates = result["terms"]
            exact_tokenizer = (
                result["mode"] == "prompt_hint"
                and profile is not None
                and self.workspace.inference is not None
            )
            if not exact_tokenizer and backend not in ("deepgram", "deepgram_streaming"):
                fail(
                    "vocabulary_tokenizer_required",
                    "The selected profile requires its actual tokenizer to enforce the prompt limit",
                )

            async def count(prefix: int) -> int:
                text = ", ".join(item["hint"] for item in candidates[:prefix])
                if not exact_tokenizer:
                    return len(text.encode("utf-8"))
                assert self.workspace.inference is not None and profile is not None
                counts = cast(
                    list[object],
                    await self.workspace.inference.count_vocabulary_tokens(profile, [text]),
                )
                if len(counts) != 1 or not isinstance(counts[0], int) or counts[0] < 0:
                    fail("tokenizer_protocol", "The tokenizer returned an invalid token count")
                return counts[0]

            accepted = len(candidates)
            used_tokens = await count(accepted)
            if used_tokens > token_limit:
                low, high = 0, accepted - 1
                accepted, used_tokens = 0, 0
                while low <= high:
                    middle = (low + high) // 2
                    tokens = await count(middle)
                    if tokens <= token_limit:
                        accepted, used_tokens = middle, tokens
                        low = middle + 1
                    else:
                        high = middle - 1
                result["omitted"].extend(
                    {"id": item["id"], "spelling": item["spelling"], "reason": "token_limit"}
                    for item in candidates[accepted:]
                )
                result["terms"] = candidates[:accepted]
                result["prompt_hint"] = (
                    ", ".join(item["hint"] for item in result["terms"])
                    if result["mode"] == "prompt_hint"
                    else None
                )
                result["effective_hash"] = fingerprint(result["terms"])
            result["token_budget"] = {
                "limit": token_limit,
                "used": used_tokens,
                "counting": "exact_selected_tokenizer"
                if exact_tokenizer
                else "conservative_utf8_byte_upper_bound",
            }
            result["acknowledgement_key"] = fingerprint(
                {
                    "revision": result["vocabulary_revision"],
                    "effective_hash": result["effective_hash"],
                    "omitted": result["omitted"],
                    "capabilities": capabilities,
                    "token_budget": result["token_budget"],
                }
            )
            result["acknowledgement_required"] = (
                bool(result["omitted"])
                and payload.get("acknowledgement") != result["acknowledgement_key"]
            )
        if payload.get("meeting_id") and payload.get("apply"):
            if result["acknowledgement_required"]:
                fail(
                    "vocabulary_acknowledgement_required",
                    "Review omitted vocabulary before applying",
                    compilation=result,
                )
            meeting = existing(self.workspace, "meetings", payload["meeting_id"])
            if meeting.get("state") in ("recording", "paused") and not result["hot_update"]:
                return {
                    **result,
                    "applied": False,
                    "applies_at": capabilities.get("vocabulary_boundary", "next_meeting"),
                }
            config = {**meeting.get("config_snapshot", {}), "vocabulary": result}
            self.workspace.put(
                "meetings",
                meeting["id"],
                {"config_snapshot": config},
                expected_revision=meeting["revision"],
            )
        return result
