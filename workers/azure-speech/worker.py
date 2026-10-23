"""Optional Microsoft Speech SDK runtime; credentials never enter protocol replies."""

import json
import threading
import uuid
from pathlib import Path
from queue import Empty, Queue

import numpy as np
from meeting_assistant.infrastructure.workers.runtime import PCMGrant, serve


class AzureSpeechRuntime:
    def __init__(self, model_directory: Path | None, options: dict):
        import azure.cognitiveservices.speech as speechsdk

        self.sdk, self.np = speechsdk, np
        self.options = options
        self.recognizer, self.push = None, None
        self.events = Queue(maxsize=128)
        self.origin, self.revisions, self.stream_id = 0, {}, str(uuid.uuid4())
        self.ack = threading.Event()
        self.started = threading.Event()
        self.start_error = None

    def capabilities(self):
        return {
            "streaming": {"native": True, "interim": True, "revisions": True},
            "timestamps": {"word": True, "segment": True},
            "vocabulary": {
                "mode": "phrase_list",
                "weighted": True,
                "max_terms": 500,
                "update_policy": "next_stream",
            },
            "language_detection": {
                "max_candidates_at_start": 4,
                "max_candidates_continuous": 10,
                "within_sentence": False,
            },
            "diarization": {"available": True, "late_revisions": True},
            "cancellation": {"flush": True, "cooperative": True},
        }

    def _drain(self):
        values = []
        while True:
            try:
                values.append(self.events.get_nowait())
            except Empty:
                return values

    def _result(self, event, final):
        result = event.result
        if result.reason == self.sdk.ResultReason.NoMatch:
            return
        raw = result.properties.get_property(self.sdk.PropertyId.SpeechServiceResponse_JsonResult)
        detail = json.loads(raw) if raw else {}
        offset = int(detail.get("Offset", result.offset))
        duration = int(detail.get("Duration", 0))
        result_id = str(offset)
        revision = self.revisions.get(result_id, 0)
        self.revisions[result_id] = revision + 1
        nbest = (detail.get("NBest") or [{}])[0]
        words = [
            {
                "text": w["Word"],
                "start_ms": self.origin + int(w["Offset"]) // 10000,
                "end_ms": self.origin + (int(w["Offset"]) + int(w["Duration"])) // 10000,
                "confidence": w.get("Confidence"),
                "speaker": str(getattr(result, "speaker_id", None))
                if getattr(result, "speaker_id", None) is not None
                else None,
            }
            for w in nbest.get("Words", [])
        ]
        speaker = getattr(result, "speaker_id", None)
        self.events.put(
            {
                "provider_stream_id": self.stream_id,
                "provider_result_id": result_id,
                "revision": revision,
                "is_final": final,
                "start_ms": self.origin + offset // 10000,
                "end_ms": self.origin + (offset + duration) // 10000,
                "text": result.text,
                "detected_language": detail.get("PrimaryLanguage", {}).get("Language"),
                "confidence": nbest.get("Confidence"),
                "timing_provenance": "native_word" if words else "chunk_only",
                "words": words,
                "speaker_spans": [
                    {
                        "speaker": str(speaker),
                        "start_ms": self.origin + offset // 10000,
                        "end_ms": self.origin + (offset + duration) // 10000,
                        "overlap": False,
                        "confidence": None,
                    }
                ]
                if speaker not in {None, "Unknown"}
                else [],
                "provider_metadata": {"native_ticks": True},
            }
        )

    def _start(self, payload):
        if self.recognizer is not None:
            raise ValueError("Azure stream is already active")
        key = self.options.get("key")
        region = self.options.get("region")
        if not key or not region:
            raise ValueError("Azure Speech credential and region are required")
        config = self.sdk.SpeechConfig(subscription=key, region=region)
        config.output_format = self.sdk.OutputFormat.Detailed
        config.request_word_level_timestamps()
        self.origin = int(payload.get("origin_ms", 0))
        rate = int(payload.get("sample_rate", 16000))
        self.push = self.sdk.audio.PushAudioInputStream(
            stream_format=self.sdk.audio.AudioStreamFormat(
                samples_per_second=rate, bits_per_sample=16, channels=1
            )
        )
        audio = self.sdk.audio.AudioConfig(stream=self.push)
        languages = payload.get("languages", [])
        continuous = bool(payload.get("continuous_language_detection", False))
        if len(languages) > (10 if continuous else 4):
            raise ValueError("Azure language candidate limit exceeded")
        kwargs = {"speech_config": config, "audio_config": audio}
        if len(languages) > 1:
            config.set_property(
                self.sdk.PropertyId.SpeechServiceConnection_LanguageIdMode,
                "Continuous" if continuous else "AtStart",
            )
            kwargs["auto_detect_source_language_config"] = (
                self.sdk.languageconfig.AutoDetectSourceLanguageConfig(languages=languages)
            )
        elif languages:
            config.speech_recognition_language = languages[0]
        if payload.get("diarization"):
            config.set_property(
                self.sdk.PropertyId.SpeechServiceResponse_DiarizeIntermediateResults, "true"
            )
            self.recognizer = self.sdk.transcription.ConversationTranscriber(**kwargs)
            self.recognizer.transcribing.connect(lambda e: self._result(e, False))
            self.recognizer.transcribed.connect(lambda e: self._result(e, True))
        else:
            self.recognizer = self.sdk.SpeechRecognizer(**kwargs)
            self.recognizer.recognizing.connect(lambda e: self._result(e, False))
            self.recognizer.recognized.connect(lambda e: self._result(e, True))
        terms = payload.get("vocabulary", [])
        if len(terms) > 500:
            raise ValueError("Azure phrase list limit exceeded")
        if terms:
            if payload.get("diarization"):
                raise ValueError(
                    "This pinned SDK conversation profile does not advertise phrase lists"
                )
            grammar = self.sdk.PhraseListGrammar.from_recognizer(self.recognizer)
            for term in terms:
                grammar.addPhrase(term)
            grammar.setWeight(float(payload.get("phrase_weight", 1.0)))
        self.recognizer.session_stopped.connect(lambda _: self.ack.set())
        self.started.clear()
        self.start_error = None
        self.recognizer.session_started.connect(lambda _: self.started.set())

        def cancelled_event(event):
            self.start_error = (
                "auth" if "Authentication" in str(getattr(event, "error_code", "")) else "network"
            )
            self.events.put(
                {
                    "error": {
                        "code": self.start_error,
                        "message": "Azure Speech session cancelled",
                        "retryable": False,
                    }
                }
            )
            self.started.set()

        self.recognizer.canceled.connect(cancelled_event)
        if payload.get("diarization"):
            self.recognizer.start_transcribing_async().get()
        else:
            self.recognizer.start_continuous_recognition_async().get()
        if not self.started.wait(20):
            raise TimeoutError("Azure service did not acknowledge stream startup")
        if self.start_error is not None:
            from meeting_assistant.infrastructure.workers.runtime import RuntimeFailure

            raise RuntimeFailure(self.start_error, "Azure Speech service rejected stream startup")
        self.diarization = bool(payload.get("diarization"))
        return {"opened": True, "stream_id": self.stream_id}

    def run(self, operation, payload, cancelled):
        if cancelled.is_set():
            raise InterruptedError()
        if operation == "open_stream":
            return self._start(payload)
        if operation == "poll":
            return {"hypotheses": self._drain()}
        if operation == "push":
            if self.push is None:
                raise ValueError("Azure stream is not open")
            with PCMGrant(payload) as pcm:
                values = self.np.frombuffer(pcm.view, dtype="<f4").copy()
                encoded = (self.np.clip(values, -1, 1) * 32767).astype("<i2").tobytes()
                self.push.write(encoded)
                del values
            return {"hypotheses": self._drain()}
        if operation in {"flush", "close_stream"}:
            self.ack.clear()
            self.push.close()
            if self.diarization:
                self.recognizer.stop_transcribing_async().get()
            else:
                self.recognizer.stop_continuous_recognition_async().get()
            if not self.ack.wait(5):
                raise TimeoutError("Azure boundary stop was not acknowledged")
            self.recognizer, self.push = None, None
            return {"acknowledged": True, "hypotheses": self._drain(), "requires_new_stream": True}
        raise ValueError("Unsupported Azure Speech worker operation")

    def close(self):
        if self.push is not None:
            self.push.close()
        self.recognizer, self.push = None, None
        self.options.clear()


if __name__ == "__main__":
    serve(AzureSpeechRuntime, "azure-speech")
