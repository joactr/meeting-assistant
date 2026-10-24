"""Phonon-2.5 (Fermion five-value Parakeet TDT) on the CPU engine; native word timings."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import PCMGrant, serve

PROFILE, BACKEND = "five-value-25", "phonon2-five-value"


def _matches(path: Path, sha256: str) -> bool:
    if not path.is_file():
        return False
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest() == sha256


class PhononRuntime:
    def __init__(self, model_directory: Path, options: dict):
        from fermion._speech import backends, fetch

        pin = json.loads((model_directory / "config.json").read_text(encoding="utf-8"))
        pin = pin["artifact_fsq"]
        # The catalog grants the packed .fsq only; unpack once into a cache keyed by the pinned container hash.
        unpacked = (
            Path(tempfile.gettempdir()) / "meeting-assistant-phonon" / pin["container_sha256"]
        )
        container = unpacked / "model.fermion"
        if not _matches(container, pin["container_sha256"]):
            fetch.unpack_any(model_directory / pin["filename"], unpacked)
            if not _matches(container, pin["container_sha256"]):
                raise ValueError("Unpacked Phonon container does not match its pinned hash")
        os.environ["FERMION_CPU_THREADS"] = str(int(options.get("num_threads", 4)))
        self.speech = backends.load("cpu", unpacked, profile=PROFILE, backend=BACKEND)

    def capabilities(self):
        return {
            "streaming": {"native": False, "interim": False},
            "timestamps": {"token": False, "word": True},
            "vocabulary": {"mode": "none", "update_policy": "unsupported"},
            "confidence": False,
            "diarization": False,
        }

    def run(self, operation, payload, cancelled):
        if operation not in {"transcribe", "transcribe_file"} or payload.get("vocabulary"):
            raise ValueError("Phonon does not accept vocabulary biasing")
        with PCMGrant(payload) as pcm:
            if pcm.rate != 16000:
                raise ValueError("Phonon requires 16 kHz PCM")
            samples = np.frombuffer(pcm.view, dtype="<f4").copy()
            result = self.speech.transcribe_array_detailed(samples)
            if cancelled.is_set():
                raise InterruptedError()
            origin = int(payload.get("origin_ms", 0))
            words = [
                {
                    "text": w["text"],
                    "start_ms": origin + round(w["start"] * 1000),
                    "end_ms": origin + round(w["end"] * 1000),
                    "confidence": None,
                    "speaker": None,
                }
                for w in result.words
            ]
            return {
                "hypotheses": [
                    {
                        "provider_stream_id": payload.get("stream_id", "phonon"),
                        "provider_result_id": payload.get("chunk_id", "file"),
                        "revision": 0,
                        "is_final": True,
                        "start_ms": origin,
                        "end_ms": origin + pcm.frames * 1000 // pcm.rate,
                        "text": result.text,
                        "detected_language": None,
                        "confidence": None,
                        "timing_provenance": "native_word" if words else "chunk_only",
                        "words": words,
                        "speaker_spans": [],
                        "provider_metadata": {},
                    }
                ]
            }

    def close(self):
        self.speech = None


if __name__ == "__main__":
    serve(PhononRuntime, "phonon")
