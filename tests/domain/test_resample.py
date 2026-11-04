import math
from array import array

import pytest
from meeting_assistant.domain.audio import pcm16k
from meeting_assistant.domain.errors import AppError


def tone(hz: float, seconds: float = 0.5) -> bytes:
    return array(
        "f", (math.sin(2 * math.pi * hz * n / 48000) for n in range(int(48000 * seconds)))
    ).tobytes()


def rms(pcm: bytes) -> float:
    samples = array("f")
    samples.frombytes(pcm)
    middle = samples[len(samples) // 4 : -len(samples) // 4]  # skip zero-padded edges
    return math.sqrt(sum(x * x for x in middle) / len(middle))


def test_speech_band_passes_and_aliases_are_rejected():
    speech = pcm16k(tone(1000), 48000)
    assert len(speech) == 4 * 8000
    assert rms(speech) == pytest.approx(1 / math.sqrt(2), rel=0.02)
    # 14 kHz would fold to 2 kHz at 16 kHz without the anti-alias filter.
    assert rms(pcm16k(tone(14000), 48000)) < 0.02


def test_non_finite_audio_is_rejected():
    with pytest.raises(AppError):
        pcm16k(array("f", [0.0, math.nan, 0.0]).tobytes(), 48000)
