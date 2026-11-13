"""E2E tests for health endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.integration
async def test_root_health_returns_200(client):
    """GET /health returns 200 with status ok."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "uptime_seconds" in body
    assert "components" in body


@pytest.mark.integration
async def test_health_ready(client):
    """GET /api/health/ready returns readiness probe."""
    resp = await client.get("/api/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["health"]["status"] == "healthy"
    assert "uptime" in body["health"]


@pytest.mark.integration
async def test_health_live(client):
    """GET /api/health/live returns liveness probe."""
    resp = await client.get("/api/health/live")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["health"]["status"] == "healthy"


@pytest.mark.integration
async def test_api_health_detailed(client):
    """GET /api/health returns detailed health with backend checks mocked."""
    mock_response = AsyncMock()
    mock_response.status_code = 200

    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["health"]["status"] == "healthy"


@pytest.mark.integration
async def test_api_health_degraded_when_backends_down(client):
    """GET /api/health returns degraded when backends are unreachable."""
    with patch("routers.health.httpx.AsyncClient") as mock_client_cls:
        mock_http = AsyncMock()
        mock_http.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_http

        resp = await client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["health"]["status"] == "degraded"


@pytest.mark.integration
async def test_api_health_system_metrics(client):
    """GET /api/health/system-metrics returns system metrics."""
    resp = await client.get("/api/health/system-metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    # Either psutil is available and we get real metrics, or we get the fallback
    assert "message" in body
