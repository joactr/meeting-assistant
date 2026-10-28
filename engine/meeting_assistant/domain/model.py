from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from typing import Any


def now_ms() -> int:
    return time.time_ns() // 1_000_000


_last_id = [0, 0]  # (milliseconds, 12-bit sequence) of the previous id


def new_id() -> str:
    # UUIDv7 with a per-millisecond counter (RFC 9562 method 1): ids made in the same
    # millisecond still sort in creation order, which every ORDER BY created_at,id relies on.
    ms = now_ms()
    if ms <= _last_id[0]:
        ms, sequence = _last_id[0], _last_id[1] + 1
        if sequence > 0xFFF:
            ms, sequence = ms + 1, 0
    else:
        sequence = secrets.randbits(11)
    _last_id[:] = [ms, sequence]
    value = (ms << 80) | (7 << 76) | (sequence << 64) | (2 << 62) | secrets.randbits(62)
    return str(uuid.UUID(int=value))


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def is_question(text: str, language: str | None = None) -> bool:
    normalized = text.strip().casefold()
    if not normalized.endswith("?"):
        return False
    # A comma-separated confirmation suffix qualifies a statement, not a request.
    if re.search(
        r",\s*(?:right|isn[’\']?t it|don[’\']?t we|no|¿no|verdad|vale|ok|okay)\s*\?$", normalized
    ):
        prefix = normalized.rsplit(",", 1)[0].lstrip("¿")
        interrogative = r"^(?:who|what|when|where|why|how|is|are|do|does|did|can|could|will|would|should|quién|qué|cuándo|dónde|por qué|cómo|cuál|podemos|puede)\b"
        return re.search(interrogative, prefix) is not None
    return language is None or language.split("-")[0] in ("en", "es") or normalized.startswith("¿")


def meaningful_transcript(text: str, language: str | None) -> bool:
    tokens = re.findall(r"\w+", text.casefold())
    if not tokens:
        return False
    fillers = {"en": {"um", "uh", "erm", "hmm"}, "es": {"eh", "em", "mmm"}}
    return not (
        len(tokens) <= 2
        and all(token in fillers.get((language or "").split("-")[0], set()) for token in tokens)
    )


def union_duration(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end = -1
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total
