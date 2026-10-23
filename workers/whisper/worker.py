"""Whisper worker: CPU INT8 by default, optional explicit CUDA float16.

Chunks emit finals, never fabricated interims. The device is an explicit profile
option; a missing or failing GPU raises an actionable error and never silently
falls back to the CPU.
"""

import os
import sys
from pathlib import Path

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import PCMGrant, RuntimeFailure, serve


def _expose_bundled_cuda_libraries() -> None:
    """Make NVIDIA libraries packaged with the CUDA variant loadable on Windows."""
    base = Path(getattr(sys, "_MEIPASS", "")) / "nvidia"
    if os.name != "nt" or not base.is_dir():
        return
    for folder in sorted(base.glob("*/bin")):
        os.add_dll_directory(str(folder))
        os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


class WhisperRuntime:
    def __init__(self, model_directory: Path, options: dict):
        from faster_whisper import WhisperModel

        self.np = np
        device = options.get("device", "cpu")
        if device not in {"cpu", "cuda"}:
            raise RuntimeFailure(
                "invalid_input",
                "Whisper device must be cpu or cuda; automatic device selection is disabled",
            )
        compute_type = options.get("compute_type", "float16" if device == "cuda" else "int8")
        if device == "cuda":
            _expose_bundled_cuda_libraries()
            import ctranslate2

            if ctranslate2.get_cuda_device_count() < 1:
                raise RuntimeFailure(
                    "unsupported",
                    "No usable NVIDIA CUDA device or driver was found; update the NVIDIA "
                    "driver or explicitly select the CPU Whisper runtime",
                )
        try:
            self.model = WhisperModel(
                str(model_directory),
                device=device,
                device_index=int(options.get("device_index", 0)),
                compute_type=compute_type,
                local_files_only=True,
                cpu_threads=int(options.get("cpu_threads", 4)),
            )
            if device == "cuda":
                # CTranslate2 loads cuBLAS/cuDNN lazily; force it now so preflight is honest.
                segments, _ = self.model.transcribe(
                    np.zeros(8000, dtype="<f4"), language="en", vad_filter=False
                )
                list(segments)
        except Exception:
            if device != "cuda":
                raise
            self.model = None
            raise RuntimeFailure(
                "unsupported",
                "The CUDA Whisper runtime could not start on this GPU (missing CUDA libraries, "
                "unsupported compute type, or insufficient GPU memory); install the Whisper CUDA "
                "runtime pack, update the NVIDIA driver, or explicitly select the CPU runtime",
            ) from None

    def capabilities(self):
        return {
            "streaming": {"native": False, "interim": False},
            "timestamps": {"word": True, "segment": True},
            "vocabulary": {"mode": "prompt_hint", "update_policy": "next_chunk"},
            "diarization": False,
            "confidence": False,
        }

    def run(self, operation, payload, cancelled):
        if operation == "tokenize":
            return {
                "counts": [
                    len(self.model.hf_tokenizer.encode(text, add_special_tokens=False).ids)
                    for text in payload["texts"]
                ],
                "tokenizer": "whisper-multilingual-pinned",
            }
        if operation not in {"transcribe", "transcribe_file"}:
            raise ValueError("Unsupported Whisper operation")
        hints, prefix = payload.get("vocabulary", []), payload.get("prefix")
        if hints and prefix:
            raise ValueError("Whisper prefix conflicts with hotword hints")
        with PCMGrant(payload) as pcm:
            if pcm.rate != 16000:
                raise ValueError("Whisper requires explicitly resampled 16 kHz PCM")
            audio = self.np.frombuffer(pcm.view, dtype="<f4").copy()
            segments, info = self.model.transcribe(
                audio,
                language=payload.get("language"),
                word_timestamps=bool(payload.get("word_timestamps", True)),
                hotwords=", ".join(hints) if hints else None,
                prefix=prefix,
                vad_filter=False,
                condition_on_previous_text=False,
            )
            origin = int(payload.get("origin_ms", 0))
            output = []
            for segment in segments:
                if cancelled.is_set():
                    raise InterruptedError()
                output.append(
                    {
                        "provider_stream_id": payload.get("stream_id", "whisper"),
                        "provider_result_id": f"{payload.get('chunk_id', 'file')}:{segment.id}",
                        "revision": 0,
                        "is_final": True,
                        "start_ms": origin + round(segment.start * 1000),
                        "end_ms": origin + round(segment.end * 1000),
                        "text": segment.text.strip(),
                        "detected_language": info.language,
                        "confidence": None,
                        "timing_provenance": "native_word" if segment.words else "chunk_only",
                        "words": [
                            {
                                "text": w.word,
                                "start_ms": origin + round(w.start * 1000),
                                "end_ms": origin + round(w.end * 1000),
                                "confidence": w.probability,
                                "speaker": None,
                            }
                            for w in segment.words or []
                        ],
                        "speaker_spans": [],
                        "provider_metadata": {
                            "language_probability": info.language_probability,
                            "avg_logprob": segment.avg_logprob,
                        },
                    }
                )
            del audio
            return {"hypotheses": output}

    def close(self):
        self.model = None


if __name__ == "__main__":
    serve(WhisperRuntime, "whisper")
