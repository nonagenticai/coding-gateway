"""Tests for 3xx redirect rewriting, generic errors, hop-by-hop stripping, and route coverage."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx


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
# 3xx Location header rewriting
# ---------------------------------------------------------------------------


class TestRedirectRewriting:
    """3xx responses must have backend hostnames replaced with the gateway URL."""

    async def test_location_rewritten_to_gateway(self, client):
        from routers.proxy import PROXY_ROUTES

        backend_url = PROXY_ROUTES["/api/analysis"]
        mock = _mock_backend(
            302,
            b"",
            {
                "content-type": "text/html",
                "location": f"{backend_url}/api/analysis/redirected",
            },
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/old")
        assert resp.status_code == 302
        location = resp.headers["location"]
        # Backend hostname must be replaced
        assert backend_url not in location
        # Path must be preserved
        assert "/api/analysis/redirected" in location

    async def test_external_location_not_rewritten(self, client):
        mock = _mock_backend(
            301,
            b"",
            {
                "content-type": "text/html",
                "location": "https://external.example.com/path",
            },
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/ext")
        assert resp.status_code == 301
        assert resp.headers["location"] == "https://external.example.com/path"

    async def test_non_3xx_location_not_rewritten(self, client):
        """A 200 response with a Location header should NOT be rewritten."""
        from routers.proxy import PROXY_ROUTES

        backend_url = PROXY_ROUTES["/api/analysis"]
        mock = _mock_backend(
            200,
            b'{"ok": true}',
            {"location": f"{backend_url}/api/analysis/somewhere"},
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/check")
        assert resp.status_code == 200
        # Location should still contain backend URL (not rewritten for non-3xx)
        location = resp.headers.get("location", "")
        assert backend_url in location


# ---------------------------------------------------------------------------
# Generic exception from backend
# ---------------------------------------------------------------------------


class TestGenericException:
    """Unexpected exceptions during proxying yield 502."""

    async def test_runtime_error_returns_502(self, client):
        mock = AsyncMock()
        mock.request = AsyncMock(side_effect=RuntimeError("unexpected boom"))
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/err")
        assert resp.status_code == 502
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == "HTTP_502"
        assert "proxy error" in data["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Hop-by-hop headers stripped from backend response
# ---------------------------------------------------------------------------


class TestResponseHopByHopStripping:
    """Hop-by-hop headers from the backend response must not be forwarded to the client."""

    async def test_connection_header_stripped(self, client):
        mock = _mock_backend(
            200,
            b'{"ok": true}',
            {
                "connection": "keep-alive",
                "keep-alive": "timeout=5",
                "x-custom-header": "preserved",
            },
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/check")
        assert "connection" not in resp.headers
        assert "keep-alive" not in resp.headers
        assert resp.headers.get("x-custom-header") == "preserved"


# ---------------------------------------------------------------------------
# All PROXY_ROUTES entries resolve correctly (parametrized)
# ---------------------------------------------------------------------------


def _get_proxy_routes() -> list[tuple[str, str]]:
    """Load PROXY_ROUTES lazily (after conftest env setup) for parametrize."""
    from routers.proxy import PROXY_ROUTES

    return list(PROXY_ROUTES.items())


def _get_proxy_route_ids() -> list[str]:
    from routers.proxy import PROXY_ROUTES

    return list(PROXY_ROUTES.keys())


class TestAllProxyRoutesResolve:
    """Every entry in PROXY_ROUTES must resolve to the expected backend."""

    def test_all_routes_resolve_exact(self):
        from routers.proxy import PROXY_ROUTES, _resolve_backend

        for prefix, expected_url in PROXY_ROUTES.items():
            result = _resolve_backend(prefix)
            assert result is not None, f"Route {prefix} did not resolve"
            backend_base, target_url = result
            assert backend_base == expected_url, (
                f"{prefix}: expected {expected_url}, got {backend_base}"
            )
            assert target_url == f"{expected_url}{prefix}"

    def test_all_routes_resolve_with_subpath(self):
        from routers.proxy import PROXY_ROUTES, _resolve_backend

        for prefix, expected_url in PROXY_ROUTES.items():
            path = f"{prefix}/some/sub/path"
            result = _resolve_backend(path)
            assert result is not None, f"Route {path} did not resolve"
            backend_base, target_url = result
            assert backend_base == expected_url, (
                f"{path}: expected {expected_url}, got {backend_base}"
            )
            assert target_url == f"{expected_url}{path}"

    def test_unmatched_path_returns_none(self):
        """Paths that don't match any PROXY_ROUTES prefix return None."""
        from routers.proxy import _resolve_backend

        assert _resolve_backend("/api/nonexistent") is None
        assert _resolve_backend("/unknown") is None
        assert _resolve_backend("/") is None

    def test_longest_prefix_wins(self):
        """More specific route prefixes take priority over general ones."""
        from routers.proxy import PROXY_ROUTES, _resolve_backend

        # /api/validation/multi-line-code-snippet -> containers, not the general validation route
        result = _resolve_backend("/api/validation/multi-line-code-snippet")
        if result is not None:
            backend_base, _ = result
            assert backend_base == PROXY_ROUTES["/api/validation/multi-line-code-snippet"]


# ---------------------------------------------------------------------------
# Auth context forwarding (proxy lines 277-291)
# ---------------------------------------------------------------------------


class TestAuthContextForwarding:
    """Proxy must forward auth context from middleware to backend services."""

    async def test_user_headers_injected_but_resolved_tenant_is_authoritative(self):
        """When request.state.user exists, proxy injects x-user-* identity headers,
        but x-tenant-id is the require_tenant-resolved tenant (the Depends value),
        NEVER re-derived from user.tenant_id.

        Regression: the base AuthMiddleware injects a synthetic trusted-network user
        with tenant_id=DEFAULT_TENANT_ID for in-cluster token-only requests. Letting
        user.tenant_id override the introspection-resolved tenant clobbered the real
        tenant (e.g. 019de842...) with DEFAULT and 404'd the caller's container.
        """
        # Test the auth forwarding logic directly by calling proxy_request
        # with a crafted Request object that has user on state

        from fastapi import Request

        class FakeUser:
            sub = "user-123"
            tenant_id = "tenant-abc"
            preferred_username = "testuser"
            email = "test@example.com"
            organization_id = "org-xyz"
            realm_access = {"roles": ["admin", "user"]}

        mock_backend = _mock_backend(200, b'{"ok": true}')
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/analysis/test",
            "query_string": b"",
            "headers": [(b"host", b"test")],
        }
        request = Request(scope)
        request._body = b""
        request.state.user = FakeUser()

        from routers.proxy import proxy_request

        with patch("routers.proxy.get_proxy_client", return_value=mock_backend):
            await proxy_request(request, "api/analysis/test", tenant_id="test-tenant")

        call_headers = mock_backend.request.call_args.kwargs["headers"]
        assert call_headers["x-user-id"] == "user-123"
        # The resolved tenant (Depends require_tenant) wins; user.tenant_id ("tenant-abc")
        # must NOT override it.
        assert call_headers["x-tenant-id"] == "test-tenant"
        assert call_headers["x-tenant-id"] != "tenant-abc"
        assert call_headers["x-user-name"] == "testuser"
        assert call_headers["x-user-email"] == "test@example.com"
        assert call_headers["x-organization-id"] == "org-xyz"
        assert call_headers["x-user-roles"] == "admin,user"

    async def test_tenant_id_fallback_when_no_user(self):
        """When request.state has tenant_id but no user and no Depends tenant,
        the proxy falls back to request.state.tenant_id."""
        mock_backend = _mock_backend(200, b'{"ok": true}')
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/analysis/test",
            "query_string": b"",
            "headers": [(b"host", b"test")],
        }
        from fastapi import Request

        request = Request(scope)
        request._body = b""
        request.state.tenant_id = "fallback-tenant"

        from routers.proxy import proxy_request

        with patch("routers.proxy.get_proxy_client", return_value=mock_backend):
            # tenant_id=None so the elif falls through to request.state.tenant_id
            await proxy_request(request, "api/analysis/test", tenant_id=None)

        call_headers = mock_backend.request.call_args.kwargs["headers"]
        assert call_headers["x-tenant-id"] == "fallback-tenant"

    async def test_oversized_response_returns_502(self, client):
        """Backend responses exceeding 100MB are rejected with 502."""
        huge_content_length = str(200 * 1024 * 1024)  # 200 MB
        mock = _mock_backend(
            200,
            b'{"ok": true}',
            {"content-length": huge_content_length},
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/huge")
        assert resp.status_code == 502
        assert "too large" in resp.json()["error"]["message"].lower()

    async def test_invalid_content_length_does_not_crash(self, client):
        """Invalid content-length header is logged but doesn't crash."""
        mock = _mock_backend(
            200,
            b'{"ok": true}',
            {"content-length": "not-a-number"},
        )
        with patch("routers.proxy.get_proxy_client", return_value=mock):
            resp = await client.get("/api/analysis/invalid-cl")
        # Should succeed — invalid content-length is skipped
        assert resp.status_code == 200
