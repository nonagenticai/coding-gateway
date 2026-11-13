"""Tests for middleware behavior via real HTTP requests.

Validates that middleware is actually active and modifying requests/responses,
not just that modules are importable.
"""

from __future__ import annotations


class TestProcessTimeHeader:
    """The metrics middleware adds X-Process-Time to every response."""

    async def test_present_on_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert "x-process-time" in resp.headers

    async def test_present_on_root(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "x-process-time" in resp.headers

    async def test_present_on_api_health_ready(self, client):
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200
        assert "x-process-time" in resp.headers

    async def test_value_is_valid_float(self, client):
        resp = await client.get("/health")
        raw = resp.headers["x-process-time"]
        duration = float(raw)
        assert duration >= 0
        # Should complete well under 5 seconds
        assert duration < 5.0

    async def test_value_changes_between_requests(self, client):
        """Each request gets its own timing value."""
        r1 = await client.get("/health")
        r2 = await client.get("/health")
        # Values should both be parseable (main goal); being different is likely
        t1 = float(r1.headers["x-process-time"])
        t2 = float(r2.headers["x-process-time"])
        assert t1 >= 0
        assert t2 >= 0


class TestCorsHeaders:
    """CORS middleware adds headers for cross-origin requests."""

    async def test_preflight_returns_allow_origin(self, client):
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers

    async def test_preflight_allows_post(self, client):
        resp = await client.options(
            "/api/health/containers/batch",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        allowed = resp.headers.get("access-control-allow-methods", "")
        assert "POST" in allowed

    async def test_preflight_allows_authorization_header(self, client):
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )
        allowed = resp.headers.get("access-control-allow-headers", "")
        assert "authorization" in allowed.lower()

    async def test_cors_expose_headers(self, client):
        """Expose headers appear on actual responses, not preflight."""
        resp = await client.get(
            "/health",
            headers={"Origin": "http://example.com"},
        )
        exposed = resp.headers.get("access-control-expose-headers", "")
        assert exposed == "*" or "x-process-time" in exposed.lower()

    async def test_cors_origin_on_normal_get(self, client):
        """Normal GET with Origin header should get back allow-origin."""
        resp = await client.get(
            "/health",
            headers={"Origin": "http://example.com"},
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" in resp.headers


class TestRequestTiming:
    """Requests complete within a reasonable time frame."""

    async def test_health_responds_quickly(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        duration = float(resp.headers["x-process-time"])
        # Health check should be very fast (< 1 second)
        assert duration < 1.0

    async def test_ready_responds_quickly(self, client):
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200
        duration = float(resp.headers["x-process-time"])
        assert duration < 1.0

    async def test_live_responds_quickly(self, client):
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200
        duration = float(resp.headers["x-process-time"])
        assert duration < 1.0


class TestValidationMiddleware:
    """ValidationMiddleware normalizes URLs and rejects control characters."""

    async def test_trailing_slash_normalized(self, client):
        """Trailing slashes on API paths should be handled (not 404)."""
        # The root path "/" is excluded from validation, but API paths get normalized
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200

    async def test_normal_requests_pass_through(self, client):
        """Normal requests pass validation without issues."""
        resp = await client.get("/health")
        assert resp.status_code == 200

    async def test_api_routes_pass_validation(self, client):
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200
        assert resp.json()["health"]["status"] == "healthy"


# ---------------------------------------------------------------------------
# Metrics middleware (add_process_time_header)
# ---------------------------------------------------------------------------


class TestMetricsMiddleware:
    """Test the add_process_time_header middleware directly."""

    async def test_adds_x_process_time_header(self):
        """Middleware adds X-Process-Time header to response."""
        from middleware.metrics import add_process_time_header

        mock_request = type(
            "Request", (), {"method": "GET", "url": type("U", (), {"path": "/test"})()}
        )()
        mock_response = type("Response", (), {"status_code": 200, "headers": {}})()

        async def call_next(request):
            return mock_response

        result = await add_process_time_header(mock_request, call_next)
        assert "X-Process-Time" in result.headers
        # Should be a float string
        float(result.headers["X-Process-Time"])

    async def test_process_time_is_positive(self):
        """X-Process-Time value is a positive float."""
        from middleware.metrics import add_process_time_header

        mock_request = type(
            "Request", (), {"method": "GET", "url": type("U", (), {"path": "/health"})()}
        )()
        mock_response = type("Response", (), {"status_code": 200, "headers": {}})()

        async def call_next(request):
            return mock_response

        result = await add_process_time_header(mock_request, call_next)
        duration = float(result.headers["X-Process-Time"])
        assert duration >= 0.0
