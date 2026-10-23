from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace


def task_policy(
    workspace: Workspace, task: str, meeting: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    meeting = meeting or {}
    project_id = meeting.get("project_id")
    policies = workspace.list("task_policies")
    exact = [p for p in policies if p["task"] == task and p.get("project_id") == project_id]
    global_ = [p for p in policies if p["task"] == task and p.get("project_id") is None]
    general = (
        [
            p
            for p in policies
            if p["task"] in ("general", "default", "llm")
            and p.get("project_id") in (None, project_id)
        ]
        if task not in ("asr", "embeddings", "diarization")
        else []
    )
    candidates = exact or global_ or general
    if candidates:
        chosen = sorted(candidates, key=lambda p: (p.get("project_id") != project_id, p["id"]))[0]
        if chosen.get("model_profile_id") or chosen.get("profile_id"):
            return {
                "policy_id": chosen["id"],
                "scope": chosen.get("scope", "workspace"),
                "data_classes": ["live_audio"]
                if task in ("asr", "diarization")
                else ["historical_text"]
                if task == "ask"
                else ["meeting_text", "human_notes"],
            }
    config = meeting.get("config_snapshot", {})
    if task == "asr":
        return config.get("asr_policy") or workspace.settings().get("asr_policy")
    if task == "diarization":
        return config.get("diarization_policy") or workspace.settings().get("diarization_policy")
    return (
        config.get("ai_policy")
        or workspace.settings().get(task + "_policy")
        or workspace.settings().get("ai_policy")
    )
