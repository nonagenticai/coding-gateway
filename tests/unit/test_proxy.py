"""Tests for core proxy behavior: route resolution, header filtering, and HTTP forwarding."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

# ---------------------------------------------------------------------------
# _resolve_backend
# ---------------------------------------------------------------------------


class TestResolveBackend:
    """Route resolution: longest-prefix-first matching."""

    def test_known_prefix_returns_backend_and_url(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/analysis/some/path")
        assert result is not None
        backend_base, target_url = result
        assert "coding-analysis" in backend_base
        assert target_url == f"{backend_base}/api/analysis/some/path"

    def test_no_match_returns_none(self):
        from routers.proxy import _resolve_backend

        assert _resolve_backend("/api/unknown/thing") is None
        assert _resolve_backend("/completely/random") is None

    def test_exact_prefix_matches(self):
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/analysis")
        assert result is not None
        _, target_url = result
        assert target_url.endswith("/api/analysis")

    def test_partial_prefix_without_slash_does_not_match(self):
        """'/api/analysisXYZ' must NOT match '/api/analysis'."""
        from routers.proxy import _resolve_backend

        assert _resolve_backend("/api/analysisXYZ") is None

    def test_longer_prefix_wins_over_shorter(self):
        """/api/validation/multi-line-code-snippet is more specific than /api/validation."""
        from routers.proxy import _resolve_backend

        result = _resolve_backend("/api/validation/multi-line-code-snippet/run")
        assert result is not None
        backend_base, _ = result
        assert "coding-containers" in backend_base

    @pytest.mark.parametrize(
        "path, expected_host",
        [
            ("/api/analysis/x", "coding-analysis"),
            ("/api/bug-hunter/x", "coding-analysis"),
            ("/api/quality/x", "coding-analysis"),
            ("/api/tests/x", "coding-testing"),
            ("/api/ai/x", "coding-ai"),
            ("/api/workspaces/x", "coding-containers"),
            ("/api/containers/x", "coding-containers"),
            ("/api/repositories/x", "coding-github"),
            ("/api/webhooks/pr-actions/x", "coding-github"),
            ("/api/security/x", "coding-security"),
            ("/api/dependencies/x", "coding-security"),
            ("/api/ws-health/x", "coding-realtime"),
            ("/api/realtime/x", "coding-realtime"),
            ("/api/broadcast/x", "coding-realtime"),
        ],
    )
    def test_all_route_prefixes(self, path: str, expected_host: str):
        from routers.proxy import _resolve_backend

        result = _resolve_backend(path)
        assert result is not None, f"Expected match for {path}"
        assert expected_host in result[0]


# ---------------------------------------------------------------------------
# _filter_headers
# ---------------------------------------------------------------------------


class TestFilterHeaders:
    """Hop-by-hop header stripping."""

    def test_removes_all_hop_by_hop_headers(self):
        from routers.proxy import _filter_headers

        headers = {
            "host": "gateway:8687",
            "connection": "keep-alive",
            "keep-alive": "timeout=5",
            "proxy-authenticate": "Basic",
            "proxy-authorization": "Basic xyz",
            "te": "trailers",
            "trailers": "x",
            "transfer-encoding": "chunked",
            "upgrade": "websocket",
            "content-length": "42",
            "content-encoding": "gzip",
            # Non-hop headers that must survive
            "content-type": "application/json",
            "authorization": "Bearer tok",
            "x-custom": "value",
        }
        filtered = _filter_headers(headers)
        for hop in (
            "host",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "content-length",
            "content-encoding",
        ):
            assert hop not in filtered, f"{hop} should have been stripped"
        assert filtered["content-type"] == "application/json"
        assert filtered["authorization"] == "Bearer tok"
        assert filtered["x-custom"] == "value"

    def test_empty_headers(self):
        from routers.proxy import _filter_headers

        assert _filter_headers({}) == {}

    def test_preserves_non_hop_headers_unchanged(self):
        from routers.proxy import _filter_headers

        headers = {"accept": "text/html", "authorization": "Bearer abc"}
        assert _filter_headers(headers) == headers


# ---------------------------------------------------------------------------
# _strip_client_identity
# ---------------------------------------------------------------------------


class TestStripClientIdentity:
    """Client-supplied identity headers must never be forwarded as-is."""

    def test_drops_all_spoofable_identity_headers(self):
        from routers.proxy import _strip_client_identity

        headers = {
            "x-user-id": "spoofed",
            "x-user-roles": "admin",
            "x-user-name": "evil",
            "x-user-email": "evil@example.com",
            "x-tenant-id": "victim-tenant",
            "x-organization-id": "victim-org",
            # Unrelated headers must survive.
            "content-type": "application/json",
            "authorization": "Bearer tok",
        }
        _strip_client_identity(headers)
        for spoofed in (
            "x-user-id",
            "x-user-roles",
            "x-user-name",
            "x-user-email",
            "x-tenant-id",
            "x-organization-id",
        ):
            assert spoofed not in headers, f"{spoofed} should have been stripped"
        assert headers["content-type"] == "application/json"
        assert headers["authorization"] == "Bearer tok"

    def test_roles_only_spoof_is_dropped(self):
        """The case AuthMiddleware ignores (roles without x-user-id) must not leak."""
        from routers.proxy import _strip_client_identity

        headers = {"x-user-roles": "admin"}
        _strip_client_identity(headers)
        assert "x-user-roles" not in headers

    def test_noop_when_no_identity_headers(self):
        from routers.proxy import _strip_client_identity

        headers = {"accept": "text/html"}
        _strip_client_identity(headers)
        assert headers == {"accept": "text/html"}


# ---------------------------------------------------------------------------
# WebSocket route registration
# ---------------------------------------------------------------------------


class TestWebSocketRoutes:
    """Confirm WS forwarder endpoints are wired with the expected paths."""

    def test_ws_analysis_url_derived_from_analysis_url(self):
        from routers import proxy

        assert proxy._WS_ANALYSIS_URL.startswith("ws://")
        assert proxy._WS_ANALYSIS_URL.replace("ws://", "http://") == proxy._ANALYSIS_URL

    def test_quality_ws_route_registered(self):
        from routers.proxy import router

        paths = {getattr(r, "path", None) for r in router.routes}
        assert "/api/quality/ws/{container_name}" in paths


# ---------------------------------------------------------------------------
# proxy_request via the ASGI client
# ---------------------------------------------------------------------------


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


class TestProxyRequest:
    """Integration tests hitting the catch-all endpoint via the test client."""

    async def test_successful_get_forward(self, client):
        mock = _mock_backend(200, b'{"result": "ok"}')
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/foo")
        assert resp.status_code == 200
        assert resp.json() == {"result": "ok"}
        call_kw = mock.request.call_args.kwargs
        assert call_kw["method"] == "GET"
        assert "/api/analysis/foo" in call_kw["url"]

    async def test_unknown_path_returns_404_with_error_envelope(self, client):
        resp = await client.get("/api/nonexistent/path")
        assert resp.status_code == 404
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == "HTTP_404"
        assert "/api/nonexistent/path" in data["error"]["message"]

    async def test_connect_error_returns_502(self, client):
        mock = AsyncMock()
        mock.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/down")
        assert resp.status_code == 502
        data = resp.json()
        assert data["error"]["code"] == "HTTP_502"
        assert "unreachable" in data["error"]["message"].lower()

    async def test_timeout_returns_504(self, client):
        mock = AsyncMock()
        mock.request = AsyncMock(side_effect=httpx.TimeoutException("read timeout"))
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/slow")
        assert resp.status_code == 504
        data = resp.json()
        assert data["error"]["code"] == "HTTP_504"
        assert "timeout" in data["error"]["message"].lower()

    async def test_query_string_forwarded(self, client):
        mock = _mock_backend()
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/search?lang=python&limit=10")
        assert resp.status_code == 200
        url = mock.request.call_args.kwargs["url"]
        assert "lang=python" in url
        assert "limit=10" in url

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
    async def test_http_methods_forwarded(self, client, method: str):
        mock = _mock_backend(200)
        kwargs: dict = {}
        if method in ("POST", "PUT", "PATCH"):
            kwargs["content"] = b'{"key":"val"}'
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await getattr(client, method.lower())("/api/analysis/item/1", **kwargs)
        assert resp.status_code == 200
        assert mock.request.call_args.kwargs["method"] == method

    async def test_backend_500_passed_through(self, client):
        mock = _mock_backend(500, b'{"error": "internal failure"}')
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/broken")
        assert resp.status_code == 500
        assert resp.json()["error"] == "internal failure"

    async def test_non_json_body_forwarded(self, client):
        mock = _mock_backend(200, b"<html>hello</html>", {"content-type": "text/html"})
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/report")
        assert resp.status_code == 200
        assert b"<html>hello</html>" in resp.content

    async def test_resolved_tenant_forwarded_for_token_only_request(self, app, client):
        """A token-only request (no request.state.user) must still forward the
        require_tenant-resolved tenant as x-tenant-id, so the backend does not fall
        through to its trusted-network bypass / DEFAULT_TENANT_ID and 404 on a
        tenant-scoped resource — exactly the in-cluster n8n case. Override
        require_tenant with a realistic UUIDv7 license tenant (the production shape)
        and assert that exact value reaches the backend.
        """
        from auth.dependencies import require_tenant

        resolved_tenant = "0195f48b-d000-7000-8000-000000000001"
        app.dependency_overrides[require_tenant] = lambda: resolved_tenant
        try:
            mock = _mock_backend(200)
            with patch("routers.proxy.get_proxy_client", return_value=mock):
                resp = await client.get("/api/containers/foo")
            assert resp.status_code == 200
            headers = mock.request.call_args.kwargs["headers"]
            assert headers.get("x-tenant-id") == resolved_tenant
        finally:
            app.dependency_overrides.pop(require_tenant, None)

    async def test_client_supplied_tenant_replaced_by_resolved(self, app, client):
        """An inbound client-supplied x-tenant-id must never be forwarded verbatim;
        the gateway-resolved tenant (a UUIDv7 license tenant) wins (BOLA / cross-tenant
        isolation guard)."""
        from auth.dependencies import require_tenant

        resolved_tenant = "0195f48b-d000-7000-8000-000000000003"
        app.dependency_overrides[require_tenant] = lambda: resolved_tenant
        try:
            mock = _mock_backend(200)
            with patch("routers.proxy.get_proxy_client", return_value=mock):
                resp = await client.get(
                    "/api/containers/foo",
                    headers={"x-tenant-id": "0195f48b-d000-7000-8000-0000000000ff"},
                )
            assert resp.status_code == 200
            headers = mock.request.call_args.kwargs["headers"]
            assert headers.get("x-tenant-id") == resolved_tenant
            assert headers.get("x-tenant-id") != "0195f48b-d000-7000-8000-0000000000ff"
        finally:
            app.dependency_overrides.pop(require_tenant, None)
