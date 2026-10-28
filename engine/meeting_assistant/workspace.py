from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from meeting_assistant.domain.errors import AppError, require
from meeting_assistant.domain.model import canonical, fingerprint, new_id, now_ms
from meeting_assistant.domain.ports import (
    CommandRegistry,
    HandlerResult,
    NativeGateway,
    NativeRequest,
)
from meeting_assistant.infrastructure.sqlite.store import Store

if TYPE_CHECKING:
    from meeting_assistant.application.capture import AudioPipeline
    from meeting_assistant.application.connected.registration import ConnectedWorkflows
    from meeting_assistant.application.inference.service import InferenceService
    from meeting_assistant.application.speakers import SpeakerEvidence


class Workspace(Store):
    now_ms = staticmethod(now_ms)
    new_id = staticmethod(new_id)
    connected: ConnectedWorkflows
    speaker_evidence: SpeakerEvidence

    def __init__(
        self,
        database_path: str | Path,
        *,
        build_id: str = "1.0.0-beta.1",
        native_gateway: NativeGateway | NativeRequest | None = None,
        inference: InferenceService | None = None,
    ) -> None:
        super().__init__(database_path, build_id)
        self.build_id = build_id
        self.native_gateway = native_gateway
        self.inference = inference
        self.registry: CommandRegistry = {}
        self.run_id = new_id()
        self.shutdown_reservation: dict[str, Any] | None = None
        self._tasks: list[asyncio.Task[None]] = []
        self._stopping = False
        self.shutdown_requested = asyncio.Event()
        self._live_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.interim_ids: dict[tuple[str, str, str], str] = {}
        self.audio_pipeline: AudioPipeline | None = None
        self._active_job_id: str | None = None
        self._active_job_task: asyncio.Task[None] | None = None
        self._register_core()
        try:
            for module in (
                "meeting_assistant.application.inference.registration",
                "meeting_assistant.application.connected.registration",
            ):
                registration = importlib.import_module(module)
                registration.register_workflows(self, self.registry)
        except BaseException:
            self.close()
            raise
        from meeting_assistant.application.inference.scheduler import LiveAnalysisScheduler

        self.analysis_scheduler = LiveAnalysisScheduler(self._schedule_analysis)
        for temporary in (self.root / "staging").glob("*"):
            if temporary.is_file() and temporary.suffix in (".f32", ".backup"):
                temporary.unlink(missing_ok=True)
        with self.transaction():
            self.connection.execute(
                "UPDATE process_runs SET ended_at=? WHERE ended_at IS NULL", (now_ms(),)
            )
            self.connection.execute(
                "INSERT INTO process_runs VALUES(?,?,?,?,NULL)",
                (self.run_id, os.getpid(), now_ms(), now_ms()),
            )
            for meeting in self.connection.execute(
                "SELECT id FROM meetings WHERE state IN('starting','recording','paused','stopping')"
            ).fetchall():
                current = self.require_item("meetings", meeting["id"])
                self.put(
                    "meetings",
                    current["id"],
                    {
                        "state": "interrupted",
                        "health": "degraded",
                        "interrupted_reason": "Engine ended before capture preservation. Unacknowledged audio is unavailable when recording was disabled.",
                    },
                    expected_revision=current["revision"],
                )
            # A finalize job that finished while the engine was down, or a processing meeting whose
            # chain already ended, must not sit in `processing` forever.
            for meeting in self.connection.execute(
                "SELECT id FROM meetings WHERE state IN('processing','attention_required')"
            ).fetchall():
                self._refresh_finalization(meeting["id"])
            self.connection.execute(
                "UPDATE jobs SET status='queued',lease_owner=NULL,lease_until=NULL WHERE status='running' AND lease_owner IN(SELECT id FROM process_runs WHERE ended_at IS NOT NULL)"
            )

    @property
    def stopping(self) -> bool:
        return self._stopping or self.shutdown_requested.is_set()

    def cancel_active_job(self, job_id: str) -> None:
        if self._active_job_id == job_id and self._active_job_task is not None:
            self._active_job_task.cancel()

    def require_item(self, collection: str, entity_id: str) -> dict[str, Any]:
        item = self.get(collection, entity_id)
        require(item is not None, "not_found", f"{collection} resource does not exist.", status=404)
        assert item is not None
        return item

    def subscribe_live(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._live_subscribers.add(queue)
        return queue

    def unsubscribe_live(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._live_subscribers.discard(queue)

    def emit_live(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        meeting_id: str,
        entity_id: str | None = None,
    ) -> None:
        event = {
            "protocol_version": 1,
            "seq": max(1, self.events()["watermark"]),
            "event_id": new_id(),
            "type": event_type,
            "meeting_id": meeting_id,
            "entity_id": entity_id,
            "revision": None,
            "occurred_at_ms": now_ms(),
            "data": data,
        }
        for queue in self._live_subscribers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    def put(
        self,
        collection: str,
        entity_id: str,
        data: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self.transaction():
            item = super().put(collection, entity_id, data, expected_revision=expected_revision)
            if (
                collection in ("actions", "artifacts", "questions", "important_moments")
                and item.get("meeting_id")
                and getattr(self, "connected", None)
            ):
                self.connected.invalidate(item["meeting_id"])
            return item

    def delete(self, collection: str, entity_id: str, *, expected_revision: int) -> dict[str, Any]:
        with self.transaction():
            before = self.get(collection, entity_id)
            result = super().delete(collection, entity_id, expected_revision=expected_revision)
            if (
                collection in ("actions", "artifacts", "questions", "important_moments")
                and before
                and before.get("meeting_id")
                and getattr(self, "connected", None)
            ):
                self.connected.invalidate(before["meeting_id"])
            return result

    def dispatch(
        self, operation_id: str, payload: dict[str, Any]
    ) -> HandlerResult | Awaitable[HandlerResult]:
        handler = self.registry.get(operation_id)
        require(
            handler is not None, "unknown_operation", "Operation is not allowlisted.", status=404
        )
        assert handler is not None
        return handler(payload)

    async def adispatch(self, operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        from meeting_assistant.infrastructure.providers.contracts import ProviderError

        try:
            result = self.dispatch(operation_id, payload)
            return cast(dict[str, Any], await result if inspect.isawaitable(result) else result)
        except ProviderError as exc:
            raise AppError(
                exc.code,
                exc.message,
                status=403 if exc.code in ("consent_required", "auth", "privacy_mode") else 503,
                retryable=exc.retryable,
                details=exc.details,
            ) from exc

    def _register_core(self) -> None:
        from meeting_assistant.application.capture import register_capture
        from meeting_assistant.application.core import register_core
        from meeting_assistant.application.embeddings import register_embeddings
        from meeting_assistant.application.import_export import register_import_export
        from meeting_assistant.application.knowledge import register_knowledge
        from meeting_assistant.application.media import register_media
        from meeting_assistant.application.operations import register_operations
        from meeting_assistant.application.retrieval import register_retrieval
        from meeting_assistant.application.speakers import register_speakers
        from meeting_assistant.application.transcripts import register_transcripts
        from meeting_assistant.contracts import attach_core_models

        register_core(self, self.registry)
        register_transcripts(self, self.registry)
        register_retrieval(self, self.registry)
        register_import_export(self, self.registry)
        register_capture(self, self.registry)
        register_media(self, self.registry)
        register_speakers(self, self.registry)
        register_operations(self, self.registry)
        register_embeddings(self, self.registry)
        register_knowledge(self, self.registry)
        attach_core_models(self.registry)

    def settings(self) -> dict[str, Any]:
        return self.require_item("workspace_settings", "workspace").get("nonsecret_settings", {})

    def offline(self) -> bool:
        """Settings → Privacy policy → Offline privacy mode."""
        privacy = cast(dict[str, Any], self.settings().get("privacy") or {})
        return bool(privacy.get("network_disabled"))

    def purge_objects(self, object_ids: list[str], *owner_ids: str) -> None:
        """Delete stored objects, their files, and history of them and their owners."""
        objects = [o for o in (self.get("objects", i) for i in object_ids if i) if o]
        for obj in objects:
            self.delete("objects", obj["id"], expected_revision=obj["revision"])
        ids = [o["id"] for o in objects] + list(owner_ids)
        if ids:
            self.connection.execute(
                f"DELETE FROM document_history WHERE entity_id IN({','.join('?' * len(ids))})", ids
            )
        if objects:
            self.queue_job("purge_media", {"paths": [o["relative_path"] for o in objects]})

    async def native(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        require(
            self.native_gateway is not None,
            "native_unavailable",
            "This operation requires the native host. Engine-only mode supports text and file workflows.",
            status=503,
            details={"operation": operation},
        )
        gateway = self.native_gateway
        assert gateway is not None
        result = (
            gateway(operation, payload)
            if callable(gateway)
            else gateway.request(operation, payload)
        )
        return await result if inspect.isawaitable(result) else result

    async def resolve_secret(self, ref: str) -> str:
        local = self._local_secrets().get(ref)
        if local is not None:
            return local
        reply = await self.native("vault.resolve", {"secret_ref": ref})
        return str(reply.get("value", reply.get("secret", "")))

    def _local_secrets(self) -> dict[str, str]:
        # ponytail: plain JSON in the user-only workspace dir; the OS vault caused too many failures.
        path = self.root / "secrets.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def store_secret(self, value: str) -> dict[str, str]:
        require(bool(value.strip()), "invalid_input", "Enter a value.", status=422)
        ref = f"credential:{uuid.uuid4()}"
        secrets = {**self._local_secrets(), ref: value.strip()}
        path = self.root / "secrets.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(secrets), encoding="utf-8")
        os.replace(temporary, path)
        return {"secret_ref": ref}

    def check_consent(
        self,
        destination: str | dict[str, Any] | None,
        data_classes: Any = None,
        scope: Any = None,
        **kwargs: Any,
    ) -> bool:
        if isinstance(destination, dict):
            request: dict[str, Any] = destination
            destination = request.get("destination_fingerprint", request.get("destination"))
            data_classes = request.get("data_classes", data_classes)
            scope = request.get("scope", scope)
        if isinstance(scope, (dict, list)):
            scope = canonical(scope)
        if self.offline() and not kwargs.get("confirmed_local", False):
            raise AppError(
                "privacy_mode", "External processing is blocked by privacy mode.", status=403
            )
        for consent in self.list("consents"):
            if (
                consent["destination_fingerprint"] == destination
                and consent.get("revoked_at") is None
                and consent["scope"] == scope
                and set(cast(list[str], data_classes or [])) <= set(consent["data_classes"])
            ):
                return True
        raise AppError(
            "consent_required",
            "Explicit destination, data-class and scope consent is required.",
            status=403,
            details={
                "destination_fingerprint": destination,
                "data_classes": data_classes,
                "scope": scope,
            },
        )

    def queue_job(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        meeting_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        key = idempotency_key or new_id()
        with self.transaction():
            previous = self.connection.execute(
                "SELECT id,payload_json FROM jobs WHERE idempotency_key=?", (key,)
            ).fetchone()
            if previous:
                require(
                    json.loads(previous["payload_json"]) == payload,
                    "idempotency_conflict",
                    "Job key was reused for different input.",
                )
                return self.require_item("jobs", previous["id"])
            return self.put(
                "jobs",
                new_id(),
                {
                    "kind": kind,
                    "meeting_id": meeting_id,
                    "idempotency_key": key,
                    "stage": payload.get("stage", kind),
                    "payload": payload,
                    "status": "queued",
                    "available_at": now_ms(),
                },
            )

    def idempotent(
        self, operation: str, payload: dict[str, Any], callback: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        key = payload.get("idempotency_key")
        require(
            isinstance(key, str) and 1 <= len(key) <= 128,
            "idempotency_required",
            "Supply a durable idempotency_key.",
            status=422,
        )
        digest = fingerprint(payload)
        with self.transaction():
            row = self.connection.execute(
                "SELECT * FROM idempotency_records WHERE key=? AND operation=?", (key, operation)
            ).fetchone()
            if row:
                require(
                    row["request_hash"] == digest,
                    "idempotency_conflict",
                    "Idempotency key was used with different input.",
                )
                return json.loads(row["response_json"])
            response = callback()
            self.connection.execute(
                "INSERT INTO idempotency_records VALUES(?,?,?,?,?)",
                (key, operation, digest, canonical(response), now_ms() + 30 * 86400000),
            )
            return response

    def invalidate(self, meeting_id: str, *, notes: bool = False) -> dict[str, Any]:
        with self.transaction():
            meeting = self.require_item("meetings", meeting_id)
            field = "note_revision" if notes else "transcript_revision"
            meeting = self.put(
                "meetings",
                meeting_id,
                {field: meeting[field] + 1},
                expected_revision=meeting["revision"],
            )
            self.connection.execute("DELETE FROM search_chunks WHERE meeting_id=?", (meeting_id,))
            self.connection.execute(
                "UPDATE memory_segments SET stale=1 WHERE meeting_id=?", (meeting_id,)
            )
            self.connection.execute(
                "UPDATE briefings SET stale=1 WHERE meeting_id=? OR project_id IS ?",
                (meeting_id, meeting.get("project_id")),
            )
            self.connection.execute(
                "UPDATE citations SET stale=1 WHERE meeting_id=?", (meeting_id,)
            )
            self.connection.execute(
                "UPDATE analysis_results SET state='superseded' WHERE state='candidate' AND run_id IN(SELECT id FROM analysis_runs WHERE meeting_id=?)",
                (meeting_id,),
            )
            self.queue_job(
                "index",
                {"input_revision": meeting["transcript_revision"]},
                meeting_id=meeting_id,
                idempotency_key=f"index:{meeting_id}:{meeting['transcript_revision']}",
            )
            if getattr(self, "connected", None):
                self.connected.invalidate(meeting_id)
            return meeting

    async def _schedule_analysis(
        self, meeting_id: str, task: str, watermark: int, window_start_ms: int
    ) -> None:
        from meeting_assistant.application.routing import task_policy

        meeting = self.get("meetings", meeting_id)
        if meeting is None or meeting["state"] not in ("recording", "paused"):
            return
        policy = task_policy(self, task, meeting)
        if not policy:
            return
        with self.transaction():
            self.connection.execute(
                "UPDATE jobs SET status='cancelled',cancellation_requested=1 WHERE meeting_id=? AND kind='analysis' AND status='queued' AND json_extract(payload_json,'$.task')=?",
                (meeting_id, task),
            )
            self.queue_job(
                "analysis",
                {
                    "task": task,
                    "input_revision": watermark,
                    "note_revision": meeting["note_revision"],
                    "policy_hash": meeting["policy_hash"],
                    "policy": policy,
                    "window_start_ms": window_start_ms,
                },
                meeting_id=meeting_id,
                idempotency_key=f"live:{meeting_id}:{task}:{watermark}:{meeting['policy_hash']}",
            )

    async def start(self) -> None:
        self.analysis_scheduler.start()
        if getattr(self, "connected", None):
            await self.connected.start()
        if self.native_gateway is not None and any((self.root / "media").glob("*.commit")):
            self.queue_job("reconcile_media", {}, idempotency_key=f"media-recovery:{self.run_id}")
        self._tasks = [
            asyncio.create_task(self._job_loop()),
            asyncio.create_task(self._heartbeat()),
            asyncio.create_task(self._warm_models()),
        ]

    async def _warm_models(self) -> None:
        # Start the local models while the app is idle. A cold transcription worker made
        # meetings.start outlast the native host's request timeout, and the speaker model took
        # ~100s under capture load. Workers stay up for later meetings; a failure here
        # resurfaces on first use. Transcription first: Start waits on it.
        from meeting_assistant.application.routing import task_policy

        for task in ("asr", "diarization"):
            policy = task_policy(self, task)
            if self.inference is not None and policy:
                with contextlib.suppress(Exception):
                    await self.inference.prepare_runtime(policy)

    async def stop(self) -> None:
        self._stopping = True
        if getattr(self, "connected", None):
            await self.connected.stop()
        await self.analysis_scheduler.close()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.inference is not None and hasattr(self.inference, "close"):
            await self.inference.close()
        with self.transaction():
            self.connection.execute(
                "UPDATE process_runs SET ended_at=? WHERE id=?", (now_ms(), self.run_id)
            )
        self.close()

    async def _heartbeat(self) -> None:
        while not self._stopping:
            with self.transaction():
                self.connection.execute(
                    "UPDATE process_runs SET heartbeat_at=? WHERE id=?", (now_ms(), self.run_id)
                )
                self.connection.execute(
                    "DELETE FROM outbox_events WHERE created_at<? OR seq<(SELECT COALESCE(MAX(seq),0)-99999 FROM outbox_events)",
                    (now_ms() - 7 * 86400000,),
                )
                self.connection.execute("DELETE FROM undo_commands WHERE expires_at<?", (now_ms(),))
                self.connection.execute(
                    "DELETE FROM idempotency_records WHERE expires_at<?", (now_ms(),)
                )
                self.connection.execute(
                    "UPDATE jobs SET status='queued',lease_owner=NULL,lease_until=NULL WHERE status='running' AND lease_until<? AND lease_owner IN(SELECT id FROM process_runs WHERE ended_at IS NOT NULL)",
                    (now_ms(),),
                )
            await asyncio.sleep(2)

    async def _job_loop(self) -> None:
        while not self._stopping:
            with self.transaction():
                row = self.connection.execute(
                    "SELECT j.id FROM jobs j WHERE j.status='queued' AND j.available_at<=? AND NOT EXISTS(SELECT 1 FROM json_each(json_extract(j.payload_json,'$.depends_on')) d JOIN jobs dependency ON dependency.id=d.value WHERE dependency.status NOT IN('succeeded','skipped')) ORDER BY j.created_at,j.id LIMIT 1",
                    (now_ms(),),
                ).fetchone()
                job = self.require_item("jobs", row["id"]) if row else None
                if job:
                    job = self.put(
                        "jobs",
                        job["id"],
                        {
                            "status": "running",
                            "attempts": job["attempts"] + 1,
                            "lease_owner": self.run_id,
                            "lease_until": now_ms() + 30000,
                            "heartbeat_at": now_ms(),
                        },
                        expected_revision=job["revision"],
                    )
            if job is None:
                await asyncio.sleep(0.1)
                continue
            heartbeat = asyncio.create_task(self._lease_heartbeat(job["id"]))
            try:
                if job["cancellation_requested"]:
                    raise AppError("cancelled", "Job cancelled.")
                self._active_job_id = job["id"]
                self._active_job_task = asyncio.create_task(self._execute_job(job))
                await self._active_job_task
                completed = self.require_item("jobs", job["id"])
                state, error = (
                    (
                        "skipped"
                        if completed.get("stage_output", {}).get("state") == "skipped"
                        else "succeeded"
                    ),
                    None,
                )
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                state, error = "cancelled", AppError("cancelled", "Job cancelled.").as_dict()
            except Exception as exc:
                state = (
                    "cancelled"
                    if isinstance(exc, AppError) and exc.code == "cancelled"
                    else "failed"
                )
                from meeting_assistant.infrastructure.providers.contracts import ProviderError

                error = (
                    exc.as_dict()
                    if isinstance(exc, (AppError, ProviderError))
                    else AppError(
                        "stage_failed",
                        "The durable stage failed; retry is available.",
                        retryable=True,
                        status=503,
                        details={"exception_type": type(exc).__name__},
                    ).as_dict()
                )
            finally:
                self._active_job_id = None
                self._active_job_task = None
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            with self.transaction():
                latest = self.get("jobs", job["id"])
                if latest:
                    self.put(
                        "jobs",
                        job["id"],
                        {"status": state, "error": error, "lease_owner": None, "lease_until": None},
                        expected_revision=latest["revision"],
                    )
                    if job.get("meeting_id"):
                        self._refresh_finalization(job["meeting_id"])

    async def _lease_heartbeat(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(5)
            with self.transaction():
                job = self.get("jobs", job_id)
                if not job or job["status"] != "running":
                    return
                self.put(
                    "jobs",
                    job_id,
                    {"heartbeat_at": now_ms(), "lease_until": now_ms() + 30000},
                    expected_revision=job["revision"],
                )

    async def _execute_job(self, job: dict[str, Any]) -> None:
        from meeting_assistant.application.core import execute_analysis
        from meeting_assistant.application.retrieval import rebuild_index

        meeting_id = job.get("meeting_id")
        if job["kind"] == "reconcile_media":
            recovered = await self.native("native.media.reconcile", {})
            for commit in recovered.get("items", []):
                if (
                    self.get("meetings", commit["meeting_id"]) is not None
                    and self.get("media_segments", commit["media_id"]) is None
                ):
                    await self.adispatch("media.commit", commit)
        elif job["kind"] == "index":
            rebuild_index(self, str(meeting_id))
        elif job["kind"] == "analysis":
            await execute_analysis(self, job)
        elif job["kind"] == "finalize":
            stage = job["stage"]
            meeting = self.require_item("meetings", str(meeting_id))
            if stage == "drain":
                require(
                    meeting.get("stop_watermark") is not None,
                    "capture_not_preserved",
                    "Native capture has not supplied a durable drain watermark.",
                    status=503,
                    retryable=True,
                )
            elif stage == "index":
                rebuild_index(self, str(meeting_id))
            elif stage in ("final_analysis", "minutes", "memory"):
                from meeting_assistant.application.routing import task_policy
                from meeting_assistant.infrastructure.providers.contracts import ProviderError

                tasks = (
                    ("summary", "actions", "topics", "artifacts")
                    if stage == "final_analysis"
                    else (stage,)
                )
                configured = False
                failed: dict[str, dict[str, Any]] = {}
                for task in tasks:
                    policy = task_policy(self, task, meeting)
                    if not policy and task != "minutes":
                        continue
                    configured = True
                    try:
                        if task == "minutes":
                            await self.connected.notes.prepare(
                                {
                                    "meeting_id": meeting_id,
                                    "note_revision": meeting["note_revision"],
                                    "transcript_revision": meeting["transcript_revision"],
                                    **({"policy": policy} if policy else {}),
                                }
                            )
                        else:
                            await execute_analysis(
                                self,
                                {
                                    **job,
                                    "payload": {**job["payload"], "task": task, "policy": policy},
                                },
                            )
                    except Exception as exc:
                        if getattr(exc, "code", None) == "cancelled":
                            raise
                        # One failed AI task must not block the others or the meeting: it is
                        # recorded here, shown by analysis.status and retried with analysis.generate.
                        failed[task] = (
                            exc.as_dict()
                            if isinstance(exc, (AppError, ProviderError))
                            else {
                                "code": "stage_failed",
                                "message": f"{task} failed unexpectedly ({type(exc).__name__}).",
                            }
                        )
                current = self.require_item("jobs", job["id"])
                self.put(
                    "jobs",
                    job["id"],
                    {
                        "stage_output": {"failed": failed}
                        if configured
                        else {"state": "skipped", "reason": "AI not configured"}
                    },
                    expected_revision=current["revision"],
                )
            elif stage == "final_diarization":
                if meeting["config_snapshot"].get("diarization_policy"):
                    require(
                        self.inference is not None and hasattr(self.inference, "diarize"),
                        "diarization_unavailable",
                        "Selected final diarization requires available inference and retained or bounded-window audio.",
                        status=503,
                        retryable=True,
                    )
                    if self.list(
                        "media_segments", {"meeting_id": meeting_id, "state": "committed"}
                    ):
                        await self.speaker_evidence.diarize_retained(str(meeting_id), job["id"])
                    else:
                        self.speaker_evidence.finalize(str(meeting_id))
                else:
                    current = self.require_item("jobs", job["id"])
                    self.put(
                        "jobs",
                        job["id"],
                        {"stage_output": {"state": "skipped", "reason": "Diarization disabled"}},
                        expected_revision=current["revision"],
                    )
        elif job["kind"] == "purge_media":
            for relative in job["payload"]["paths"]:
                target = (self.root / relative).resolve()
                require(
                    target.is_relative_to(self.root),
                    "unsafe_path",
                    "Media path escapes workspace.",
                    status=422,
                )
                target.unlink(missing_ok=True)
        else:
            handler = self.registry.get("job." + job["kind"])
            require(
                handler is not None,
                "unknown_job",
                "No executable handler for durable job.",
                status=503,
            )
            assert handler is not None
            result = handler(job)
            if inspect.isawaitable(result):
                await result

    def _refresh_finalization(self, meeting_id: str) -> None:
        meeting = self.get("meetings", meeting_id)
        if not meeting or meeting["state"] not in ("processing", "attention_required"):
            return
        latest: dict[str, dict[str, Any]] = {}
        for job in self.list("jobs", {"meeting_id": meeting_id, "kind": "finalize"}):
            if (
                job["stage"] not in latest
                or job["created_at"] >= latest[job["stage"]]["created_at"]
            ):
                latest[job["stage"]] = job
        jobs = list(latest.values())
        if not jobs:
            return
        state = (
            "attention_required"
            if any(j["status"] == "failed" for j in jobs)
            else "complete"
            if all(j["status"] in ("succeeded", "skipped") for j in jobs)
            else "processing"
        )
        changes: dict[str, Any] = {} if state == meeting["state"] else {"state": state}
        if (
            state == "complete"
            and meeting["health"] != "degraded"
            and any(j.get("stage_output", {}).get("failed") for j in jobs)
        ):
            changes["health"] = "degraded"  # finished, but some AI task needs a retry
        if changes:
            self.put("meetings", meeting_id, changes, expected_revision=meeting["revision"])
