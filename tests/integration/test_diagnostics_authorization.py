from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from meeting_assistant.transport.http import Authorization, create_app
from meeting_assistant.workspace import Workspace


@pytest.mark.parametrize(
    ("operation", "scope", "payload"),
    [
        ("diagnostics.get", "read", {}),
        ("diagnostics.export", "write", {"confirm": True}),
    ],
)
def test_diagnostics_commands_require_diagnostics_authority(
    tmp_path: Path, operation: str, scope: str, payload: dict[str, object]
) -> None:
    launch_token = "ab" * 32
    restricted_token = "cd" * 32
    authorization = Authorization(launch_token)
    authorization.grant(restricted_token, {scope})
    workspace = Workspace(tmp_path / "workspace.sqlite3")
    try:
        app = create_app(
            workspace, launch_token, authorization=authorization, manage_lifespan=False
        )
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/commands/" + operation,
                json=payload,
                headers={"Authorization": "Bearer " + restricted_token},
            )
        assert response.status_code == 403
    finally:
        workspace.close()
