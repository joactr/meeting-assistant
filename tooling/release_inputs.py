"""Validate protected workflow inputs before downloading release evidence."""

from __future__ import annotations

import os
import re
import tomllib

from publish import GitHub
from release import ROOT


def validate() -> None:
    config = tomllib.loads((ROOT / "release.toml").read_text(encoding="utf-8"))
    repository = config["publisher"]["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise RuntimeError("Populate the real public publisher repository before release")
    if repository != os.environ.get("GITHUB_REPOSITORY"):
        raise RuntimeError("Release workflow must run in the configured publisher repository")
    tag = os.environ.get("RELEASE_TAG", "")
    if tag != "v" + config["version"]:
        raise RuntimeError("Selected existing tag must equal v + release.toml version")
    run_id = os.environ.get("ACCEPTANCE_RUN_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", run_id):
        raise RuntimeError("Select an actual successful acceptance workflow run ID")
    token = os.environ.get("GH_RELEASE_TOKEN")
    if not token:
        raise RuntimeError("Protected workflow authentication is unavailable")
    github = GitHub(repository, token)
    environment = github.json("environments/release-approval")
    if not any(
        rule.get("type") == "required_reviewers" for rule in environment.get("protection_rules", [])
    ):
        raise RuntimeError(
            "release-approval must have real required-reviewer protection configured"
        )
    reference = github.json(f"git/ref/tags/{tag}")["object"]
    for _ in range(8):
        if reference["type"] == "commit":
            break
        if reference["type"] != "tag":
            raise RuntimeError("Release reference does not resolve to a commit")
        reference = github.json(f"git/tags/{reference['sha']}")["object"]
    else:
        raise RuntimeError("Release tag chain exceeds supported bound")
    run = github.json(f"actions/runs/{run_id}")
    if run.get("conclusion") != "success" or run.get("status") != "completed":
        raise RuntimeError("Acceptance run must have completed successfully")
    if run.get("event") not in {"workflow_dispatch", "push"}:
        raise RuntimeError("Fork/PR acceptance runs cannot authorize an official release")
    if (
        run.get("head_sha") != reference["sha"]
        or run.get("repository", {}).get("full_name") != repository
    ):
        raise RuntimeError("Acceptance run does not match the exact release commit/repository")
    print(f"Validated protected {tag} at {reference['sha']}; acceptance run {run_id}")


if __name__ == "__main__":
    try:
        validate()
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        raise SystemExit(f"Release input verification failed: {exc}") from None
