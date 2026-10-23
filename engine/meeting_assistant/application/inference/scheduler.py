"""Final-only scheduler; coalesces watermarks instead of queuing token work."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class MeetingClock:
    watermark: int = 0
    end_ms: int = 0
    finalized_ms: int = 0
    policy_revision: int = 0
    last_run: dict[str, float] = field(default_factory=dict[str, float])
    consumed_ms: dict[str, int] = field(default_factory=dict[str, int])
    pending: dict[str, asyncio.Task[None]] = field(default_factory=dict[str, asyncio.Task[None]])


class LiveAnalysisScheduler:
    def __init__(
        self,
        schedule: Callable[[str, str, int, int], Awaitable[None]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.schedule, self.clock = schedule, clock
        self.meetings: dict[str, MeetingClock] = {}
        self.runner: asyncio.Task[None] | None = None
        self.closed = False

    def observe_final(self, meeting_id: str, start_ms: int, end_ms: int, revision: int) -> None:
        state = self.meetings.setdefault(meeting_id, MeetingClock())
        if revision <= state.watermark:
            return
        state.watermark = revision
        # Only newly covered finalized meeting time counts toward the 30s threshold.
        state.finalized_ms += max(0, end_ms - max(start_ms, state.end_ms))
        state.end_ms = max(state.end_ms, end_ms)
        now = self.clock()
        for task in ("summary", "actions", "topics", "artifacts", "memory"):
            state.last_run.setdefault(task, now)

    def policy_changed(self, meeting_id: str) -> None:
        state = self.meetings.get(meeting_id)
        if state:
            state.policy_revision += 1
            for task in state.pending.values():
                task.cancel()
            state.pending.clear()

    def start(self) -> None:
        if self.runner is None:
            self.runner = asyncio.create_task(self._run())

    async def tick(self) -> None:
        now = self.clock()
        for meeting_id, state in list(self.meetings.items()):
            if state.watermark <= 0:
                continue
            for task, interval, minimum in (
                ("cues", 5, 0),
                ("summary", 60, 30000),
                ("actions", 60, 30000),
                ("topics", 300, 0),
                ("artifacts", 300, 0),
                ("memory", 300, 0),
            ):
                if (
                    now - state.last_run.get(task, 0) < interval
                    or state.finalized_ms - state.consumed_ms.get(task, 0) < minimum
                ):
                    continue
                if state.consumed_ms.get(task, -1) == state.finalized_ms or task in state.pending:
                    continue
                state.last_run[task] = now
                watermark, consumed = state.watermark, state.finalized_ms
                window = max(0, state.end_ms - 30000) if task == "cues" else 0

                async def submit(
                    meeting: str = meeting_id,
                    kind: str = task,
                    mark: int = watermark,
                    start: int = window,
                    covered: int = consumed,
                    current: MeetingClock = state,
                ) -> None:
                    try:
                        await self.schedule(meeting, kind, mark, start)
                        current.consumed_ms[kind] = covered
                    finally:
                        current.pending.pop(kind, None)

                state.pending[task] = asyncio.create_task(submit())

    async def _run(self) -> None:
        while not self.closed:
            await self.tick()
            await asyncio.sleep(1)

    async def stop_meeting(self, meeting_id: str) -> None:
        state = self.meetings.pop(meeting_id, None)
        if state:
            for job in state.pending.values():
                job.cancel()
            if state.pending:
                await asyncio.gather(*state.pending.values(), return_exceptions=True)

    async def close(self) -> None:
        self.closed = True
        if self.runner:
            self.runner.cancel()
            await asyncio.gather(self.runner, return_exceptions=True)
        for meeting_id in list(self.meetings):
            await self.stop_meeting(meeting_id)
