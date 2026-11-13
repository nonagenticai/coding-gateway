"""E2E tests for the reverse proxy router.

These are mock/ASGI-only tests: every test that reaches a backend patches
``routers.proxy.get_proxy_client``, and the one that does not
(``test_unmatched_route_returns_404``) never leaves the router. Verified to make
zero outbound connections, so they need no running service, database, or
network. They are NOT integration tests and therefore carry no
``pytest.mark.integration`` marker.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx


async def test_unmatched_route_returns_404(client):
    """A path that matches no proxy prefix returns 404 JSON."""
    resp = await client.get("/api/nonexistent-route/something")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == "HTTP_404"


async def test_proxy_backend_unreachable_returns_502(client):
    """When the backend service is unreachable, return 502."""
    with patch("routers.proxy.get_proxy_client") as mock_get_client:
        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.request = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_get_client.return_value = mock_http_client

        resp = await client.get("/api/analysis/some-endpoint")
        assert resp.status_code == 502
        body = resp.json()
        assert body["error"]["code"] == "HTTP_502"


async def test_proxy_backend_timeout_returns_504(client):
    """When the backend times out, return 504."""
    with patch("routers.proxy.get_proxy_client") as mock_get_client:
        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.request = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        mock_get_client.return_value = mock_http_client

        resp = await client.get("/api/analysis/some-endpoint")
        assert resp.status_code == 504
        body = resp.json()
        assert body["error"]["code"] == "HTTP_504"


async def test_proxy_forwards_method(client):
    """Proxy forwards the HTTP method to the backend."""
    mock_backend_response = AsyncMock(spec=httpx.Response)
    mock_backend_response.status_code = 200
    mock_backend_response.content = b'{"ok": true}'
    mock_backend_response.headers = {"content-type": "application/json"}

    with patch("routers.proxy.get_proxy_client") as mock_get_client:
        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.request = AsyncMock(return_value=mock_backend_response)
        mock_get_client.return_value = mock_http_client

        resp = await client.post("/api/analysis/some-endpoint", json={"data": "test"})
        assert resp.status_code == 200

        call_args = mock_http_client.request.call_args
        assert call_args.kwargs["method"] == "POST"


async def test_proxy_forwards_to_correct_backend(client):
    """Proxy resolves the correct backend URL based on prefix."""
    mock_backend_response = AsyncMock(spec=httpx.Response)
    mock_backend_response.status_code = 200
    mock_backend_response.content = b'{"result": "ok"}'
    mock_backend_response.headers = {"content-type": "application/json"}

    with patch("routers.proxy.get_proxy_client") as mock_get_client:
        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.request = AsyncMock(return_value=mock_backend_response)
        mock_get_client.return_value = mock_http_client

        await client.get("/api/tests/run")

        call_args = mock_http_client.request.call_args
        target_url = call_args.kwargs["url"]
        # testing routes go to coding-testing
        assert "coding-testing" in target_url or "8689" in target_url


async def test_proxy_preserves_query_params(client):
    """Proxy forwards query parameters to the backend."""
    mock_backend_response = AsyncMock(spec=httpx.Response)
    mock_backend_response.status_code = 200
    mock_backend_response.content = b'{"ok": true}'
    mock_backend_response.headers = {"content-type": "application/json"}

    with patch("routers.proxy.get_proxy_client") as mock_get_client:
        mock_http_client = AsyncMock(spec=httpx.AsyncClient)
        mock_http_client.request = AsyncMock(return_value=mock_backend_response)
        mock_get_client.return_value = mock_http_client

        await client.get("/api/analysis/endpoint?foo=bar&baz=1")

        call_args = mock_http_client.request.call_args
        target_url = call_args.kwargs["url"]
        assert "foo=bar" in target_url
        assert "baz=1" in target_url
