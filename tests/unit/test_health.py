"""Tests for core health endpoints — GET /health, GET /, readiness, and liveness."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestRootHealth:
    """GET /health — lightweight gateway health check."""

    async def test_returns_200_with_status_ok(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_contains_api_version(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert "api_version" in data
        assert isinstance(data["api_version"], str)

    async def test_contains_uptime_seconds(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert "uptime_seconds" in data
        assert isinstance(data["uptime_seconds"], (int, float))
        assert data["uptime_seconds"] >= 0

    async def test_contains_components_list(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert "components" in data
        assert isinstance(data["components"], list)
        assert len(data["components"]) >= 1
        gw = data["components"][0]
        assert gw["name"] == "gateway"
        assert gw["status"] == "ok"

    async def test_contains_timestamp(self, client):
        resp = await client.get("/health")
        data = resp.json()
        assert "timestamp" in data
        assert isinstance(data["timestamp"], str)
        # ISO format contains 'T' separator
        assert "T" in data["timestamp"]


class TestRootEndpoint:
    """GET / — service discovery root."""

    async def test_returns_200(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200

    async def test_service_name(self, client):
        resp = await client.get("/")
        data = resp.json()
        assert data["service"] == "Code Graph Helper API"

    async def test_contains_version(self, client):
        resp = await client.get("/")
        data = resp.json()
        assert "version" in data
        assert isinstance(data["version"], str)

    async def test_contains_docs_link(self, client):
        resp = await client.get("/")
        data = resp.json()
        assert data["docs"] == "/docs"

    async def test_contains_health_link(self, client):
        resp = await client.get("/")
        data = resp.json()
        assert data["health"] == "/health"


class TestHealthReady:
    """GET /api/health/ready — Kubernetes readiness probe."""

    async def test_returns_200(self, client):
        resp = await client.get("/api/health/ready")
        assert resp.status_code == 200

    async def test_response_status_success(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert data["status"] == "success"

    async def test_health_status_healthy(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert data["health"]["status"] == "healthy"

    async def test_message_indicates_ready(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert "ready" in data["message"].lower()

    async def test_contains_uptime(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert "uptime" in data["health"]
        assert isinstance(data["health"]["uptime"], (int, float))

    async def test_contains_version_and_environment(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert data["health"]["version"] == "1.0.0"
        assert data["health"]["environment"] == "test"

    async def test_contains_timestamp_and_request_id(self, client):
        resp = await client.get("/api/health/ready")
        data = resp.json()
        assert "timestamp" in data
        assert isinstance(data["request_id"], str) and len(data["request_id"]) > 0


class TestHealthLive:
    """GET /api/health/live — Kubernetes liveness probe."""

    async def test_returns_200(self, client):
        resp = await client.get("/api/health/live")
        assert resp.status_code == 200

    async def test_response_status_success(self, client):
        resp = await client.get("/api/health/live")
        data = resp.json()
        assert data["status"] == "success"

    async def test_health_status_healthy(self, client):
        resp = await client.get("/api/health/live")
        data = resp.json()
        assert data["health"]["status"] == "healthy"

    async def test_message_indicates_alive(self, client):
        resp = await client.get("/api/health/live")
        data = resp.json()
        assert "alive" in data["message"].lower()

    async def test_contains_uptime(self, client):
        resp = await client.get("/api/health/live")
        data = resp.json()
        assert "uptime" in data["health"]
        assert isinstance(data["health"]["uptime"], (int, float))


class TestHealthCheckMain:
    """GET /api/health — main health check that probes backend services."""

    async def test_healthy_when_all_backends_respond_200(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["health"]["status"] == "healthy"
            assert data["message"] == "System health check completed"

    async def test_degraded_when_backend_returns_500(self, client):
        call_count = 0

        async def fake_get(url):
            nonlocal call_count
            call_count += 1
            return MagicMock(status_code=500 if call_count == 1 else 200)

        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["health"]["status"] == "degraded"

    async def test_degraded_when_backend_unreachable(self, client):
        import httpx

        async def fake_get(url):
            raise httpx.ConnectError("connection refused")

        mock_client = AsyncMock()
        mock_client.get = fake_get
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["health"]["status"] == "degraded"

    async def test_response_contains_all_expected_fields(self, client):
        mock_response = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("routers.health._health_http_client", mock_client):
            resp = await client.get("/api/health")
            data = resp.json()
            assert "status" in data
            assert "message" in data
            assert "timestamp" in data
            assert "request_id" in data
            assert "health" in data
            health = data["health"]
            assert "status" in health
            assert "uptime" in health
            assert "version" in health
            assert "environment" in health
            assert "timestamp" in health
