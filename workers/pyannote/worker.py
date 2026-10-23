"""Community-1 exclusive/overlap turns plus its own speaker embeddings."""

import os

os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
from pathlib import Path

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import PCMGrant, serve


class CommunityRuntime:
    def __init__(self, model_directory: Path, options: dict):
        import torch
        import yaml
        from pyannote.audio.pipelines import SpeakerDiarization

        self.np, self.torch = np, torch
        config = yaml.safe_load((model_directory / "config.yaml").read_text(encoding="utf-8"))
        parameters = dict(config["pipeline"].get("params", {}))
        parameters.update(
            segmentation=str(model_directory / "segmentation/pytorch_model.bin"),
            embedding=str(model_directory / "embedding/pytorch_model.bin"),
            plda={"checkpoint": str(model_directory), "subfolder": "plda"},
        )
        self.pipeline = SpeakerDiarization(**parameters)
        self.pipeline.instantiate(config.get("params", self.pipeline.default_parameters()))
        if self.pipeline is None:
            raise FileNotFoundError("Community-1 model unavailable")
        self.pipeline.to(torch.device(options.get("device", "cpu")))
        self.model_fingerprint = options.get("model_fingerprint")

    def capabilities(self):
        return {
            "diarization": {
                "available": True,
                "live": True,
                "late_revisions": True,
                "overlap": True,
            },
            "speaker_embeddings": True,
            "embedding_source": "pyannote.audio4.0.7 Community-1 pipeline speaker_embeddings",
            "asr": False,
        }

    def run(self, operation, payload, cancelled):
        if operation not in {"diarize", "diarize_final", "voice_embedding"}:
            raise ValueError("Unsupported Community-1 operation")
        with PCMGrant(payload) as pcm:
            values = self.np.frombuffer(pcm.view, dtype="<f4").copy()
            waveform = self.torch.from_numpy(values).unsqueeze(0)
            result = self.pipeline({"waveform": waveform, "sample_rate": pcm.rate})
            if cancelled.is_set():
                raise InterruptedError()
            origin = int(payload.get("origin_ms", 0))
            labels = result.speaker_diarization.labels()
            if result.speaker_embeddings is None or len(result.speaker_embeddings) != len(labels):
                raise ValueError("Pinned pipeline speaker embedding conformance failed")
            embeddings = {
                str(label): result.speaker_embeddings[i].tolist() for i, label in enumerate(labels)
            }

            def turns(annotation, overlap):
                return [
                    {
                        "provider_session_id": payload.get("session_id", "community-1"),
                        "local_speaker_id": str(label),
                        "start_ms": origin + round(turn.start * 1000),
                        "end_ms": origin + round(turn.end * 1000),
                        "overlap": overlap,
                        "confidence": None,
                        "embedding_ref": str(label),
                    }
                    for turn, _, label in annotation.itertracks(yield_label=True)
                ]

            exclusive = turns(result.exclusive_speaker_diarization, False)
            all_turns = turns(result.speaker_diarization, False)
            for turn in all_turns:
                turn["overlap"] = any(
                    other["local_speaker_id"] != turn["local_speaker_id"]
                    and max(other["start_ms"], turn["start_ms"])
                    < min(other["end_ms"], turn["end_ms"])
                    for other in all_turns
                )
            del values
            return {
                "exclusive_turns": exclusive,
                "turns": all_turns,
                "speaker_embeddings": embeddings,
                "model_fingerprint": payload["model_fingerprint"],
                "coverage": {"start_ms": origin, "end_ms": origin + pcm.frames * 1000 // pcm.rate},
                "final": operation == "diarize_final",
            }

    def close(self):
        self.pipeline = None


if __name__ == "__main__":
    serve(CommunityRuntime, "pyannote")
