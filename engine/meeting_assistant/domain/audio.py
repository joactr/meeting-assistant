from __future__ import annotations

import math
import sys
from array import array

from meeting_assistant.domain.errors import require

# A fixed anti-alias FIR for the canonical 48k -> 16k branch. Versioned with the
# processing profile; no external DSP module is imported into the engine.
_TAPS = 63
_RAW: tuple[float, ...] = tuple(
    (2 / 6 if n == 0 else math.sin(2 * math.pi * n / 6) / (math.pi * n))
    * (0.54 + 0.46 * math.cos(2 * math.pi * n / (_TAPS - 1)))
    for n in range(-(_TAPS // 2), _TAPS // 2 + 1)
)
_FIR: tuple[float, ...] = tuple(value / sum(_RAW) for value in _RAW)


def pcm16k(pcm: bytes, sample_rate: int) -> bytes:
    require(
        sample_rate in (16000, 48000) and len(pcm) % 4 == 0,
        "invalid_pcm",
        "Only canonical mono float32 PCM at 16/48 kHz is accepted.",
        status=422,
    )
    if sample_rate == 16000:
        return pcm
    samples: array[float] = array("f")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    # A finite float32 sum is always finite in double precision: one C pass instead of per-sample.
    require(
        math.isfinite(sum(samples)),
        "invalid_pcm",
        "Audio contains a non-finite sample.",
        status=422,
    )
    # Zero-padded edges; one C-level dot product per output. A per-sample Python loop held the
    # GIL for ~50 ms per second of audio and stalled the engine loop during live meetings.
    pad = array("f", bytes(4 * (_TAPS // 2)))
    padded = pad + samples + pad
    output = array(
        "f", [math.sumprod(padded[c : c + _TAPS], _FIR) for c in range(0, len(samples), 3)]
    )
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()
