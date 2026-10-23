from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from urllib.parse import quote, urlsplit

import httpx

from .common import (
    all_items,
    authorized_connection,
    existing,
    fail,
    fingerprint,
    is_json_list,
    iso,
    native,
    privacy_guard,
    required,
    safe_https,
    save,
    vault_reference,
)
from .network import ProviderHTTP

REPOSITORY = re.compile(r"^(?!\.{1,2}/)[A-Za-z0-9_.-]+/(?!\.{1,2}$)[A-Za-z0-9_.-]+$")


def marker(operation_id: str) -> str:
    return f"<!-- meeting-assistant-operation:{operation_id} -->"


class IssueService:
    def __init__(self, workspace: Workspace, http: ProviderHTTP) -> None:
        self.workspace = workspace
        self.http = http

    async def auth_begin(self, payload: dict[str, Any]) -> dict[str, Any]:
        privacy_guard(self.workspace)
        return await native(
            self.workspace,
            "oauth.device",
            {"provider": "github", "client_id": required(payload, "client_id"), "scopes": []},
        )

    async def auth_poll(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await native(self.workspace, "oauth.poll", {"flow_id": required(payload, "flow_id")})

    async def auth_cancel(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await native(
            self.workspace, "oauth.cancel", {"flow_id": required(payload, "flow_id")}
        )

    def connection_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        repositories = payload.get("repositories", [])
        if not is_json_list(repositories) or any(
            not REPOSITORY.fullmatch(repo) for repo in repositories
        ):
            fail("invalid_repository", "Repositories must be explicit owner/name identifiers")
        api_origin = safe_https(payload.get("api_origin", "https://api.github.com"))
        if urlsplit(api_origin).path not in ("", "/", "/api/v3"):
            fail("invalid_github_origin", "Enterprise API origin must use the /api/v3 path")
        identity = payload.get("id") or self.workspace.new_id()
        credential_kind = payload.get("credential_kind", "device")
        if credential_kind not in ("device", "pat"):
            fail(
                "unsupported_credential",
                "Choose GitHub App device authentication or an explicit fine-grained PAT",
            )
        if credential_kind == "device":
            required(payload, "client_id")
        item = self.workspace.put(
            "github_connections",
            identity,
            {
                "id": identity,
                "provider": "github",
                "client_id": payload.get("client_id"),
                "secret_ref": vault_reference(required(payload, "secret_ref")),
                "api_origin": api_origin.rstrip("/"),
                "repositories": sorted(set(repositories)),
                "enabled": True,
                "credential_kind": credential_kind,
            },
            expected_revision=payload.get("expected_revision"),
        )
        return {"item": {key: value for key, value in item.items() if key != "secret_ref"}}

    def connections(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [
                {key: value for key, value in item.items() if key != "secret_ref"}
                for item in all_items(self.workspace, "github_connections")
            ]
        }

    async def disconnect(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection = existing(self.workspace, "github_connections", required(payload, "id"))
        item = self.workspace.put(
            "github_connections",
            connection["id"],
            {"enabled": False},
            expected_revision=required(payload, "expected_revision"),
        )
        revocation = await native(
            self.workspace,
            "oauth.revoke",
            {"secret_ref": connection["secret_ref"], "provider": "github"},
        )
        return {
            "item": {key: value for key, value in item.items() if key != "secret_ref"},
            "revocation": revocation,
        }

    async def repositories(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection = existing(
            self.workspace, "github_connections", required(payload, "connection_id")
        )
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["api_origin"],
            ["github_metadata"],
            {"connection_id": connection["id"]},
        )
        path = (
            self._path(connection, "/user/installations")
            if connection.get("credential_kind") != "pat"
            else self._path(connection, "/user/repos")
        )
        items: list[dict[str, Any]] = []
        params: dict[str, Any] | None = {"per_page": 100}
        seen: set[str] = set()
        while path:
            if path in seen:
                fail("github_pagination", "Provider repeated a repository pagination cursor")
            seen.add(path)
            response = await self.http.request(connection, "GET", path, params=params)
            data = response["json"]
            if is_json_list(data):
                items.extend(
                    {
                        "id": item["id"],
                        "full_name": item["full_name"],
                        "permissions": item.get("permissions", {}),
                    }
                    for item in data
                )
            else:
                for installation in data.get("installations", []):
                    repo_path = self._path(
                        connection, f"/user/installations/{installation['id']}/repositories"
                    )
                    repo_params: dict[str, Any] | None = {"per_page": 100}
                    repo_seen: set[str] = set()
                    while repo_path:
                        if repo_path in repo_seen:
                            fail(
                                "github_pagination",
                                "Provider repeated an installation repository cursor",
                            )
                        repo_seen.add(repo_path)
                        repo_response = await self.http.request(
                            connection, "GET", repo_path, params=repo_params
                        )
                        items.extend(
                            {
                                "id": item["id"],
                                "full_name": item["full_name"],
                                "permissions": item.get("permissions", {}),
                            }
                            for item in repo_response["json"].get("repositories", [])
                        )
                        repo_path = self._next(repo_response["headers"])
                        repo_params = None
            path = self._next(response["headers"])
            params = None
        return {"items": sorted(items, key=lambda item: item["full_name"].casefold())}

    @staticmethod
    def _path(connection: dict[str, Any], suffix: str) -> str:
        api_origin: str = connection["api_origin"]
        prefix = urlsplit(api_origin).path.rstrip("/")
        return prefix + suffix

    @staticmethod
    def _next(headers: dict[str, Any]) -> str | None:
        for link in headers.get("link", "").split(","):
            match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="([^"]+)"\s*', link)
            if match and match[2] == "next":
                return match[1]
        return None

    def prepare(self, payload: dict[str, Any]) -> dict[str, Any]:
        connection = existing(
            self.workspace, "github_connections", required(payload, "connection_id")
        )
        repository = required(payload, "repository")
        if repository not in connection.get("repositories", []):
            fail(
                "repository_not_selected", "Select the reviewed repository on this connection first"
            )
        action_id = required(payload, "action_id")
        previous = [
            item
            for item in all_items(self.workspace, "github_operations", {"action_id": action_id})
            if item.get("state") in ("prepared", "sending", "created", "unknown")
        ]
        if previous and not payload.get("create_distinct"):
            return {
                "item": sorted(previous, key=lambda item: item.get("prepared_at", ""))[-1],
                "existing": True,
            }
        action = existing(self.workspace, "actions", action_id)
        meeting_id = action.get("meeting_id") or payload.get("meeting_id")
        meeting = existing(self.workspace, "meetings", meeting_id) if meeting_id else {}
        operation_id = str(uuid.uuid4())
        title = str(
            payload.get("title")
            or action.get("description")
            or action.get("text")
            or action.get("title")
            or ""
        ).strip()
        if not title or len(title) > 256 or "\n" in title:
            fail("invalid_title", "Review a single-line issue title of at most 256 characters")
        body = str(payload.get("body", action.get("description") or action.get("text", "")))
        sources = all_items(self.workspace, "entity_sources", {"action_id": action_id})
        saved_quotes = [
            source["quote_snapshot"] for source in sources if source.get("quote_snapshot")
        ]
        quote_text = payload.get("source_quote") or "\n".join(saved_quotes)
        if payload.get("source_quote"):
            available = saved_quotes + [
                item.get("text", "")
                for item in all_items(self.workspace, "utterances", {"meeting_id": meeting_id})
            ]
            if not any(payload["source_quote"] in text for text in available):
                fail(
                    "invalid_source_quote",
                    "The reviewed source quote is not present in saved meeting evidence",
                )
        if quote_text:
            body += "\n\nSource quote (saved evidence snapshot):\n" + "\n".join(
                "> " + line for line in str(quote_text).splitlines()
            )
        meeting_date = meeting.get("started_at") or meeting.get("created_at")
        if isinstance(meeting_date, (int, float)):
            meeting_date = iso(datetime.fromtimestamp(meeting_date / 1000, UTC))
        body += f"\n\nMeeting: {meeting.get('title', 'Local meeting')} ({meeting_date or 'date unavailable'})"
        if meeting_id:
            body += f"\nLocal reference (usable only on this installation): meeting-assistant://meetings/{quote(meeting_id, safe='')}"
        due_date_text = (
            payload.get("due_date_text") or action.get("deadline_text") or action.get("due_date")
        )
        if due_date_text:
            body += "\nDue date (reviewed wording): " + str(due_date_text)
        body += "\n\n" + marker(operation_id)
        request = {
            "title": title,
            "body": body,
            "labels": payload.get("labels", []),
            "assignees": payload.get("assignees", []),
        }
        if any(
            not is_json_list(value) or any(not isinstance(item, str) for item in value)
            for value in (request["labels"], request["assignees"])
        ):
            fail("invalid_input", "Labels and assignees must be reviewed string lists")
        item = save(
            self.workspace,
            "github_operations",
            operation_id,
            {
                "id": operation_id,
                "operation_id": operation_id,
                "connection_id": connection["id"],
                "repository": repository,
                "action_id": action_id,
                "meeting_id": meeting_id,
                "request": request,
                "request_hash": fingerprint(request),
                "sources": sources,
                "state": "prepared",
                "prepared_at": iso(datetime.fromtimestamp(self.workspace.now_ms() / 1000, UTC)),
            },
        )
        return {"item": item, "requires_review": True}

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = existing(self.workspace, "github_operations", required(payload, "id"))
        if operation["state"] == "created":
            return {"item": operation, "existing": True}
        if operation["state"] in ("unknown", "sending") and not payload.get(
            "authorize_duplicate_risk"
        ):
            fail(
                "reconciliation_required",
                "Reconcile uncertain creation before retrying or explicitly authorize duplicate risk",
            )
        if not payload.get("reviewed") or payload.get("request_hash") != operation["request_hash"]:
            fail(
                "review_required",
                "Approve the exact destination, title, body, labels and assignees before sending",
            )
        connection = existing(self.workspace, "github_connections", operation["connection_id"])
        if operation["repository"] not in connection.get("repositories", []):
            fail("repository_not_selected", "The repository is no longer selected")
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["api_origin"],
            ["reviewed_issue", "meeting_metadata"],
            {"connection_id": connection["id"], "repository": operation["repository"]},
        )
        operation = save(
            self.workspace,
            "github_operations",
            operation["id"],
            {**operation, "state": "sending", "sending_at_ms": self.workspace.now_ms()},
            operation,
        )
        try:
            response = await self.http.request(
                connection,
                "POST",
                self._path(connection, f"/repos/{operation['repository']}/issues"),
                body=operation["request"],
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            result = save(
                self.workspace,
                "github_operations",
                operation["id"],
                {
                    **operation,
                    "state": "unknown",
                    "uncertainty": "The provider may have created this issue; do not blindly resend.",
                },
                operation,
            )
            return {"item": result}
        except Exception as error:
            details = getattr(error, "details", {}) or {}
            uncertain = details.get("http_status", 0) >= 500
            save(
                self.workspace,
                "github_operations",
                operation["id"],
                {
                    **operation,
                    "state": "unknown" if uncertain else "failed",
                    "error_code": getattr(error, "code", "request_failed"),
                },
                operation,
            )
            raise
        return {"item": self._created(operation, response["json"])}

    def _created(self, operation: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(remote.get("number"), int) or not remote.get("html_url"):
            return save(
                self.workspace,
                "github_operations",
                operation["id"],
                {
                    **operation,
                    "state": "unknown",
                    "uncertainty": "Provider acknowledgement was missing the issue identity.",
                },
                operation,
            )
        connection = existing(self.workspace, "github_connections", operation["connection_id"])
        api_origin: str = connection["api_origin"]
        hostname = urlsplit(api_origin).hostname or ""
        safe_https(
            remote["html_url"], hosts={"github.com" if hostname == "api.github.com" else hostname}
        )
        labels = [
            cast(dict[str, Any], label)["name"] if isinstance(label, dict) else label
            for label in remote.get("labels", [])
        ]
        assignees = [assignee["login"] for assignee in remote.get("assignees", [])]
        omitted = {
            "labels": [label for label in operation["request"]["labels"] if label not in labels],
            "assignees": [
                assignee
                for assignee in operation["request"]["assignees"]
                if assignee not in assignees
            ],
        }
        result = save(
            self.workspace,
            "github_operations",
            operation["id"],
            {
                **operation,
                "state": "created",
                "remote_url": remote["html_url"],
                "remote_number": remote["number"],
                "remote_state": remote.get("state"),
                "actual_labels": labels,
                "actual_assignees": assignees,
                "omitted": omitted,
            },
            operation,
        )
        self.workspace.emit(
            "github.issue.created",
            {"id": operation["id"], "url": remote["html_url"]},
            entity_id=operation["id"],
        )
        return result

    async def reconcile(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = existing(self.workspace, "github_operations", required(payload, "id"))
        if operation["state"] == "created":
            return {"item": operation}
        connection = existing(self.workspace, "github_connections", operation["connection_id"])
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["api_origin"],
            ["github_metadata"],
            {"connection_id": connection["id"], "repository": operation["repository"]},
        )
        since = datetime.fromisoformat(operation["prepared_at"].replace("Z", "+00:00")) - timedelta(
            minutes=5
        )
        path = self._path(connection, f"/repos/{operation['repository']}/issues")
        params: dict[str, Any] | None = {
            "state": "all",
            "sort": "created",
            "direction": "desc",
            "since": iso(since),
            "per_page": 100,
        }
        matches: list[dict[str, Any]] = []
        seen: set[str] = set()
        while path:
            if path in seen:
                fail("github_pagination", "Provider repeated a pagination cursor")
            seen.add(path)
            response = await self.http.request(connection, "GET", path, params=params)
            matches.extend(
                issue
                for issue in response["json"]
                if "pull_request" not in issue
                and marker(operation["id"]) in (issue.get("body") or "")
            )
            path, params = self._next(response["headers"]), None
        if len(matches) == 1:
            return {"item": self._created(operation, matches[0])}
        result = save(
            self.workspace,
            "github_operations",
            operation["id"],
            {
                **operation,
                "state": "unknown",
                "reconciled_at_ms": self.workspace.now_ms(),
                "matching_issues": [
                    {"number": issue["number"], "url": issue["html_url"]} for issue in matches
                ],
                "uncertainty": "Multiple issues carry this marker; review them."
                if matches
                else "No marker found yet. Provider propagation/indexing may be delayed; absence is not proof of non-creation.",
            },
            operation,
        )
        return {"item": result}

    async def refresh(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = existing(self.workspace, "github_operations", required(payload, "id"))
        if operation.get("state") != "created":
            fail("issue_not_created", "Reconcile the issue before refreshing its status")
        connection = existing(self.workspace, "github_connections", operation["connection_id"])
        connection = authorized_connection(
            self.workspace,
            connection,
            connection["api_origin"],
            ["github_metadata"],
            {"connection_id": connection["id"], "repository": operation["repository"]},
        )
        response = await self.http.request(
            connection,
            "GET",
            self._path(
                connection, f"/repos/{operation['repository']}/issues/{operation['remote_number']}"
            ),
        )
        remote = response["json"]
        return {
            "item": self._created(operation, remote),
            "suggested_local_update": {
                "status": "done" if remote.get("state") == "closed" else "open"
            },
            "local_action_unchanged": True,
        }
