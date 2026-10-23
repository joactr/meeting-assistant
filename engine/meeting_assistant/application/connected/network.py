from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meeting_assistant.workspace import Workspace
from urllib.parse import urljoin, urlsplit

import httpx

from .common import consent, fail, native, privacy_guard, safe_https

ORIGINS = {
    "google": "https://www.googleapis.com",
    "microsoft": "https://graph.microsoft.com",
    "github": "https://api.github.com",
}


class ProviderHTTP:
    def __init__(self, workspace: Workspace, client: httpx.AsyncClient | None = None) -> None:
        self.workspace = workspace
        self.client = client

    async def request(
        self,
        connection: dict[str, Any],
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        privacy_guard(self.workspace)
        if not connection.get("enabled", True):
            fail("connection_disabled", "The connection is disabled")
        provider = connection["provider"]
        origin = ORIGINS.get(provider)
        if provider == "github" and connection.get("api_origin"):
            origin = safe_https(connection["api_origin"]).rstrip("/")
        if not origin:
            fail("unknown_provider", "Unsupported connected provider")
        url = urljoin(origin + "/", path)
        parsed, base = urlsplit(url), urlsplit(origin)
        if (
            (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
            or parsed.username
            or parsed.password
        ):
            fail("unsafe_pagination", "Provider pagination attempted to change origin")
        token = await self._token(connection)
        privacy_guard(self.workspace)
        if connection.get("id"):
            collection = "github_connections" if provider == "github" else "calendar_connections"
            latest = self.workspace.get(collection, connection["id"])
            if (
                latest is None
                or not latest.get("enabled")
                or latest["revision"] != connection.get("revision")
            ):
                fail(
                    "connection_changed",
                    "The connection changed or was revoked before the request was sent",
                )
        authorization = connection.get("_request_consent")
        if authorization:
            consent(
                self.workspace,
                authorization["destination"],
                authorization["data_classes"],
                authorization["scope"],
            )
        request_headers = {
            "Accept": "application/json",
            **(headers or {}),
            "Authorization": "Bearer " + token,
        }
        if provider == "github":
            request_headers.update(
                {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
            )
        if provider == "microsoft":
            request_headers["Prefer"] = 'outlook.timezone="UTC"'
        if self.client is not None:
            response = await self.client.request(
                method,
                url,
                params=params,
                json=body,
                headers=request_headers,
                follow_redirects=False,
                timeout=httpx.Timeout(30, connect=10),
            )
        else:
            async with httpx.AsyncClient(
                follow_redirects=False, timeout=httpx.Timeout(30, connect=10)
            ) as client:
                response = await client.request(
                    method, url, params=params, json=body, headers=request_headers
                )
        if response.status_code == 304:
            return {"status": 304, "headers": dict(response.headers), "json": None}
        if response.status_code >= 400:
            retry_after = response.headers.get("retry-after")
            code = (
                "connection_revoked"
                if response.status_code == 401
                else "permission_denied"
                if response.status_code == 403
                else "rate_limited"
                if response.status_code == 429
                else "provider_error"
            )
            from meeting_assistant.domain.errors import AppError

            raise AppError(
                code,
                "The connected provider rejected the request",
                retryable=response.status_code in (429, 500, 502, 503, 504),
                details={"http_status": response.status_code, "retry_after": retry_after},
            )
        if response.is_redirect:
            fail("provider_redirect_rejected", "Credentialed requests cannot follow redirects")
        try:
            data = response.json() if response.content else None
        except ValueError:
            fail("provider_protocol", "The provider returned invalid JSON")
        return {"status": response.status_code, "headers": dict(response.headers), "json": data}

    async def _token(self, connection: dict[str, Any]) -> str:
        secret_ref = connection.get("secret_ref")
        if not secret_ref:
            fail(
                "authentication_required",
                "Connect an account through the native OAuth or vault prompt",
            )
        resolved = await native(self.workspace, "vault.resolve", {"secret_ref": secret_ref})
        value = resolved.get("value")
        if not isinstance(value, str):
            fail("vault_unavailable", "The native vault did not return a credential")
        try:
            credential = json.loads(value)
        except ValueError:
            credential = {"access_token": value}
        expires = credential.get("expires_at")
        if expires and float(expires) < time.time() + 60:
            await native(
                self.workspace,
                "oauth.refresh",
                {
                    "secret_ref": secret_ref,
                    "provider": connection["provider"],
                    "client_id": connection.get("client_id"),
                },
            )
            credential = json.loads(
                (await native(self.workspace, "vault.resolve", {"secret_ref": secret_ref}))["value"]
            )
        token = credential.get("access_token")
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            fail("invalid_credential", "The credential contains no valid access token")
        return token
