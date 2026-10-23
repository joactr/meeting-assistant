"""Explicit credentialed/model smoke runner; no fake provider result or download.

python -m meeting_assistant.application.inference.smoke --mode analysis \
  --config smoke-policy.json --credential-env MY_PROVIDER_KEY \
  --consent-destination EXACT_DESTINATION --evidence selected-evidence.json

Local workers additionally use --runtime-manifest and --model-directory. The
manifest is an explicit developer file grant; installed application routing only
uses reviewed catalog-installed paths. Cloud runs may incur provider charges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import wave
from array import array
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...infrastructure.providers.contracts import EmbeddingBatch, ProviderError
from ...infrastructure.providers.registry import ProviderRegistry, resolve_capabilities
from ...infrastructure.workers.client import WorkerClient
from .service import InferenceService, TaskPolicy


class SmokeArguments(argparse.Namespace):
    mode: str
    config: Path
    consent_destination: str
    credential_env: str | None
    runtime_manifest: Path | None
    model_directory: Path | None
    evidence: Path | None
    audio: Path | None
    task: str
    query: str
    meeting_local_date: str | None
    meeting_timezone: str | None


async def execute(args: SmokeArguments) -> dict[str, Any]:
    policy: dict[str, Any] = json.loads(args.config.read_text(encoding="utf-8"))
    connection = policy["connection"]
    destination = connection["destination"]
    if args.consent_destination != destination:
        raise ProviderError("permission", "Smoke destination was not explicitly consented")
    secret = os.environ.get(args.credential_env) if args.credential_env else None
    if connection.get("auth_scheme", "bearer") != "none" and not secret:
        raise ProviderError("auth", "Explicit credential environment variable is absent")
    if secret:
        connection["secret_ref"] = "developer-smoke-env"
    policy["scope"] = "developer-smoke"
    policy["data_classes"] = [
        "connection_test",
        "meeting_text",
        "historical_excerpts",
        "audio",
        "embedding_text",
    ]
    registry = ProviderRegistry()

    async def secret_resolver(reference: str) -> str:
        if reference != "developer-smoke-env" or not secret:
            raise ProviderError("auth", "Smoke credential reference is unavailable")
        return secret

    def consent_checker(actual: str, classes: list[str], scope: str) -> bool:
        return actual == args.consent_destination and scope in {
            "developer-smoke",
            "connection_test",
        }

    service = InferenceService(registry, secret_resolver, consent_checker)
    try:
        parsed = TaskPolicy.model_validate(policy)
    except ValidationError as exc:
        raise ProviderError(
            "invalid_input",
            "Task policy is invalid",
            details={
                "fields": [".".join(str(part) for part in item["loc"]) for item in exc.errors()]
            },
        ) from None
    if args.runtime_manifest:
        manifest = json.loads(args.runtime_manifest.read_text(encoding="utf-8"))
        executable = args.runtime_manifest.parent / manifest["executable"]
        worker = WorkerClient(executable, manifest["executable_sha256"], manifest["family"])
        options: dict[str, Any] = {
            **parsed.profile.options,
            "model_fingerprint": resolve_capabilities(
                parsed.connection, parsed.profile
            ).fingerprint,
        }
        if parsed.connection.adapter == "azure-speech":
            options.update(key=secret, region=parsed.connection.region)
        await worker.start(args.model_directory, options["model_fingerprint"], options)
        registry.worker_runtimes[parsed.profile.id] = worker
    try:
        if args.mode == "connection":
            return await service.test_connection(
                connection, policy["profile"], scope="developer-smoke"
            )
        if args.mode == "analysis":
            if not args.evidence:
                raise ProviderError(
                    "invalid_input",
                    "Analysis smoke needs an explicitly selected evidence JSON file",
                )
            evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
            return await service.generate(
                args.task,
                evidence,
                policy,
                context={
                    "question": args.query,
                    "meeting_local_date": args.meeting_local_date,
                    "meeting_timezone": args.meeting_timezone,
                },
            )
        if args.mode == "embeddings":
            batch = EmbeddingBatch(
                purpose="query",
                texts=[args.query],
                model_fingerprint=resolve_capabilities(
                    parsed.connection, parsed.profile
                ).fingerprint,
            )
            return await service.embed(batch, policy)
        if not args.audio:
            raise ProviderError(
                "invalid_input", "Speech smoke needs an explicitly selected mono PCM16 WAV"
            )
        with wave.open(str(args.audio), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise ProviderError("invalid_input", "Smoke audio must be mono signed PCM16 WAV")
            rate = source.getframerate()
            samples = array("h")
            samples.frombytes(source.readframes(source.getnframes()))
        if sys.byteorder != "little":
            samples.byteswap()
        pcm = array("f", (sample / 32768 for sample in samples))
        if sys.byteorder != "little":
            pcm.byteswap()
        if args.mode == "diarization":
            return await service.diarize(
                pcm_float32=pcm.tobytes(), sample_rate=rate, policy=policy, final=True
            )
        return {
            "hypotheses": await service.transcribe(pcm.tobytes(), sample_rate=rate, policy=policy)
        }
    finally:
        await service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["connection", "analysis", "embeddings", "asr", "diarization"],
        required=True,
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--consent-destination", required=True)
    parser.add_argument("--credential-env")
    parser.add_argument("--runtime-manifest", type=Path)
    parser.add_argument("--model-directory", type=Path)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--task", default="summary")
    parser.add_argument("--query", default="Explicit user inference connection test")
    parser.add_argument("--meeting-local-date")
    parser.add_argument("--meeting-timezone")
    args = parser.parse_args(namespace=SmokeArguments())
    try:
        result = asyncio.run(execute(args))
        print(json.dumps({"actual_provider_output": result, "mode": args.mode}, ensure_ascii=False))
    except ProviderError as exc:
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
