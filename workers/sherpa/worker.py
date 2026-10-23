"""Pinned greedy Parakeet TDT profile; BPE token times stay token times."""

from pathlib import Path

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import PCMGrant, serve


class SherpaRuntime:
    def __init__(self, model_directory: Path, options: dict):
        import sherpa_onnx

        self.np = np
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(model_directory / "encoder.int8.onnx"),
            decoder=str(model_directory / "decoder.int8.onnx"),
            joiner=str(model_directory / "joiner.int8.onnx"),
            tokens=str(model_directory / "tokens.txt"),
            num_threads=int(options.get("num_threads", 4)),
            sample_rate=16000,
            feature_dim=80,
            decoding_method="greedy_search",
            model_type="nemo_transducer",
            provider="cpu",
        )

    def capabilities(self):
        return {
            "streaming": {"native": False, "interim": False},
            "timestamps": {"token": True, "word": False},
            "vocabulary": {"mode": "none", "update_policy": "unsupported"},
            "confidence": False,
            "diarization": False,
        }

    def run(self, operation, payload, cancelled):
        if operation not in {"transcribe", "transcribe_file"} or payload.get("vocabulary"):
            raise ValueError("Greedy Parakeet does not accept vocabulary biasing")
        with PCMGrant(payload) as pcm:
            samples = self.np.frombuffer(pcm.view, dtype="<f4").copy()
            stream = self.recognizer.create_stream()
            stream.accept_waveform(pcm.rate, samples)
            self.recognizer.decode_stream(stream)
            if cancelled.is_set():
                raise InterruptedError()
            result = stream.result
            origin = int(payload.get("origin_ms", 0))
            tokens = [
                {"token": token, "start_ms": origin + round(start * 1000)}
                for token, start in zip(result.tokens, result.timestamps, strict=True)
            ]
            del samples
            return {
                "hypotheses": [
                    {
                        "provider_stream_id": payload.get("stream_id", "sherpa"),
                        "provider_result_id": payload.get("chunk_id", "file"),
                        "revision": 0,
                        "is_final": True,
                        "start_ms": origin,
                        "end_ms": origin + pcm.frames * 1000 // pcm.rate,
                        "text": result.text,
                        "detected_language": None,
                        "confidence": None,
                        "timing_provenance": "native_token",
                        "words": [],
                        "speaker_spans": [],
                        "provider_metadata": {"tokens": tokens},
                    }
                ]
            }

    def close(self):
        self.recognizer = None


if __name__ == "__main__":
    serve(SherpaRuntime, "sherpa")
