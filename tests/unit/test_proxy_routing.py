"""Tests for response size limits, auth header injection, and body forwarding."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _mock_backend(
    status: int = 200,
    body: bytes = b'{"ok": true}',
    headers: dict[str, str] | None = None,
) -> AsyncMock:
    """Return an AsyncMock httpx client whose .request() returns a canned response."""
    resp_headers = {"content-type": "application/json"}
    if headers:
        resp_headers.update(headers)
    mock_response = httpx.Response(status, content=body, headers=resp_headers)
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(return_value=mock_response)
    return mock_client


# ---------------------------------------------------------------------------
# Response size limits (100 MB)
# ---------------------------------------------------------------------------


class TestResponseSizeLimits:
    """Backend responses exceeding 100 MB must be rejected with 502."""

    async def test_content_length_header_exceeding_limit_returns_502(self, client):
        mock = _mock_backend(
            200,
            b"small body",
            {"content-length": str(200 * 1024 * 1024)},
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/huge")
        assert resp.status_code == 502
        data = resp.json()
        assert data["error"]["code"] == "HTTP_502"
        assert "too large" in data["error"]["message"].lower()

    async def test_actual_body_exceeding_limit_returns_502(self, client):
        big = b"x" * (101 * 1024 * 1024)
        mock = _mock_backend(200, big)
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/huge-body")
        assert resp.status_code == 502
        data = resp.json()
        assert "too large" in data["error"]["message"].lower()

    async def test_invalid_content_length_header_skipped_gracefully(self, client):
        mock = _mock_backend(
            200,
            b'{"ok": true}',
            {"content-length": "not-a-number"},
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/bad-cl")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_content_length_at_limit_passes(self, client):
        """Exactly 100 MB content-length should NOT be rejected."""
        exactly_100mb = str(100 * 1024 * 1024)
        mock = _mock_backend(200, b'{"ok": true}', {"content-length": exactly_100mb})
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/at-limit")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Auth header injection from request.state.user
# ---------------------------------------------------------------------------


class TestAuthHeaderInjection:
    """The proxy must never forward client-supplied identity headers.

    In production the real AuthMiddleware sets request.state.user from the
    Istio-injected headers and the proxy re-injects the validated values. When
    there is no authenticated user on request.state (as in this test harness,
    which stubs auth out), the proxy strips client-supplied identity headers
    rather than passing them through — closing the trusted-network spoof where
    an unauthenticated caller sends e.g. ``x-user-roles: admin``.
    """

    async def _call_with_istio_headers(self, client, istio_headers: dict) -> dict[str, str]:
        """Make a proxied request with client-supplied identity headers and no
        authenticated user on request.state. Returns the headers forwarded to
        the mock backend.
        """
        mock = _mock_backend()

        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/test", headers=istio_headers)

        assert resp.status_code == 200
        return mock.request.call_args.kwargs["headers"]

    async def test_identity_headers_stripped_when_unauthenticated(self, app, client):
        from auth.dependencies import require_tenant

        resolved_tenant = "0195f48b-d000-7000-8000-000000000001"
        app.dependency_overrides[require_tenant] = lambda: resolved_tenant
        try:
            headers = await self._call_with_istio_headers(
                client,
                {
                    "x-user-id": "user-42",
                    "x-tenant-id": "0195f48b-d000-7000-8000-0000000000ff",
                    "x-user-name": "bob",
                },
            )
        finally:
            app.dependency_overrides.pop(require_tenant, None)
        # No request.state.user → client-supplied user identity is dropped, not forwarded.
        assert "x-user-id" not in headers
        assert "x-user-name" not in headers
        # x-tenant-id is replaced by the gateway-resolved UUIDv7 tenant, never the
        # client-supplied value — so the spoof is closed AND a token-only request
        # still carries the correct tenant downstream.
        assert headers.get("x-tenant-id") == resolved_tenant
        assert headers.get("x-tenant-id") != "0195f48b-d000-7000-8000-0000000000ff"

    async def test_email_not_forwarded(self, client):
        headers = await self._call_with_istio_headers(
            client,
            {"x-user-id": "uid-1", "x-user-email": "alice@example.com"},
        )
        assert "x-user-email" not in headers

    async def test_organization_id_not_forwarded(self, client):
        headers = await self._call_with_istio_headers(
            client,
            {"x-user-id": "uid-1", "x-organization-id": "org-99"},
        )
        assert "x-organization-id" not in headers

    async def test_roles_spoof_not_forwarded(self, client):
        # The headline spoof: an unauthenticated caller must not have a
        # client-supplied roles header reach the backend.
        headers = await self._call_with_istio_headers(client, {"x-user-roles": "admin,editor"})
        assert "x-user-roles" not in headers


# ---------------------------------------------------------------------------
# X-Forwarded-* header injection
# ---------------------------------------------------------------------------


class TestForwardedHeaders:
    """Standard proxy headers added to forwarded request."""

    async def test_x_forwarded_proto_and_host(self, client):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/test")
        assert resp.status_code == 200
        sent_headers = mock.request.call_args.kwargs["headers"]
        assert "x-forwarded-proto" in sent_headers
        assert "x-forwarded-host" in sent_headers

    async def test_x_forwarded_for_set(self, client):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/test")
        assert resp.status_code == 200
        sent_headers = mock.request.call_args.kwargs["headers"]
        assert "x-forwarded-for" in sent_headers


# ---------------------------------------------------------------------------
# POST / PUT body forwarding
# ---------------------------------------------------------------------------


class TestBodyForwarding:
    """Request bodies are forwarded to the backend."""

    @pytest.mark.parametrize("method", ["post", "put"])
    async def test_json_body_forwarded(self, client, method: str):
        mock = _mock_backend(201, b'{"created": true}')
        payload = {"name": "test-item", "value": 42}
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await getattr(client, method)("/api/analysis/create", json=payload)
        assert resp.status_code == 201
        forwarded_body = mock.request.call_args.kwargs.get("content")
        assert forwarded_body is not None
        assert b"test-item" in forwarded_body

    async def test_raw_body_forwarded(self, client):
        mock = _mock_backend(200)
        raw = b"raw binary content here"
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.post("/api/analysis/upload", content=raw)
        assert resp.status_code == 200
        forwarded_body = mock.request.call_args.kwargs.get("content")
        assert forwarded_body == raw


@pytest.mark.parametrize(
    "path",
    [
        "/api/repositories/get-pull-request",
        "/api/repositories/list-pull-requests",
        "/api/repositories/get-branch",
        "/api/repositories/compare",
    ],
)
def test_pr_read_routes_proxy_to_coding_github(path: str):
    """coding-github #104's PR reads and #126's get-branch/compare ride the /api/repositories prefix."""
    from routers.proxy import PROXY_ROUTES, _resolve_backend

    resolved = _resolve_backend(path)
    assert resolved is not None
    assert resolved == (
        PROXY_ROUTES["/api/repositories"],
        f"{PROXY_ROUTES['/api/repositories']}{path}",
    )


# ---------------------------------------------------------------------------
# git fetch (fetch_workspace_ref) is proxied and authorised exactly like git push
#
# The gateway has NO per-operation role gate for push_workspace_to_fork: its
# authorisation is the catch-all proxy's `Depends(require_tenant)` plus the
# identity re-injection below, and the repo/credential gating (tenant allowlist,
# platform_repo_forbidden) is enforced by coding-containers' resolve_repo_credential,
# which git_fetch.py shares with git_push.py. These tests pin that fetch takes
# the same path through the gateway as push, so the two cannot drift apart.
# ---------------------------------------------------------------------------

_GIT_CREDENTIAL_ROUTES = [
    "/api/workspaces/aa-ws/git/push",
    "/api/workspaces/aa-ws/git/fetch",
]


@pytest.mark.parametrize("path", _GIT_CREDENTIAL_ROUTES)
def test_git_credential_routes_proxy_to_coding_containers(path: str):
    from routers.proxy import PROXY_ROUTES, _resolve_backend

    containers = PROXY_ROUTES["/api/workspaces"]
    assert _resolve_backend(path) == (containers, f"{containers}{path}")


@pytest.mark.parametrize("path", _GIT_CREDENTIAL_ROUTES)
async def test_git_credential_routes_forward_only_the_gateway_tenant(app, client, path):
    from auth.dependencies import require_tenant

    resolved_tenant = "0195f48b-d000-7000-8000-000000000001"
    app.dependency_overrides[require_tenant] = lambda: resolved_tenant
    mock = _mock_backend()
    try:
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.post(
                path,
                json={"refspec": "pull/1/head:refs/remotes/pr/1"},
                headers={
                    "x-tenant-id": "0195f48b-d000-7000-8000-0000000000ff",
                    "x-user-roles": "admin",
                },
            )
    finally:
        app.dependency_overrides.pop(require_tenant, None)
    assert resp.status_code == 200, resp.text
    kwargs = mock.request.call_args.kwargs
    assert kwargs["method"] == "POST"
    assert kwargs["url"].endswith(path)
    headers = kwargs["headers"]
    assert headers.get("x-tenant-id") == resolved_tenant
    assert "x-user-roles" not in headers


@pytest.mark.parametrize("path", _GIT_CREDENTIAL_ROUTES)
async def test_git_credential_routes_refused_without_a_tenant(app, client, path):
    """No tenant context => refused at the gateway; the backend is never called."""
    from fastapi import HTTPException

    from auth.dependencies import require_tenant

    def _no_tenant() -> str:
        raise HTTPException(status_code=401, detail="tenant required")

    app.dependency_overrides[require_tenant] = _no_tenant
    mock = _mock_backend()
    try:
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.post(path, json={"refspec": "main"})
    finally:
        app.dependency_overrides.pop(require_tenant, None)
    assert resp.status_code == 401, resp.text
    mock.request.assert_not_called()
