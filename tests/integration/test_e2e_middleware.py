"""E2E tests for middleware behavior (security headers, CORS, exception handling, input validation).

These are mock/ASGI-only tests: they drive the app through httpx's
ASGITransport with all backend dependencies patched, so they need no running
service, database, or network. They are NOT integration tests and therefore
carry no ``pytest.mark.integration`` marker.
"""

from __future__ import annotations


async def test_cors_headers_on_preflight(client):
    """OPTIONS request receives CORS headers."""
    resp = await client.options(
        "/api/health/ready",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    # FastAPI CORSMiddleware returns 200 for preflight
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers
    assert "access-control-allow-methods" in resp.headers


async def test_cors_headers_on_regular_request(client):
    """Regular GET includes CORS allow-origin header."""
    resp = await client.get(
        "/health",
        headers={"Origin": "http://localhost:3000"},
    )
    assert resp.status_code == 200
    assert "access-control-allow-origin" in resp.headers


async def test_exception_handler_json_envelope(client):
    """Unmatched proxy route returns structured error JSON."""
    resp = await client.get("/api/unknown-service/endpoint")
    assert resp.status_code == 404
    body = resp.json()
    # The proxy catch-all produces a KAG-standard error envelope
    assert "error" in body
    assert body["error"]["code"] == "HTTP_404"
    assert "timestamp" in body["error"]


async def test_exception_handler_422_validation_error(client):
    """Pydantic validation error returns structured error envelope."""
    # Send invalid data to validation endpoint (missing required field)
    resp = await client.post("/api/validation/file-reference", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert "detail" in body
    assert isinstance(body["detail"], list)


async def test_root_endpoint(client):
    """GET / returns service info."""
    resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert "service" in body
    assert "version" in body
    assert "docs" in body
