"""Integration test fixtures for coding-gateway.

Inherits stub installation from the root conftest (auto-runs via _install_test_stubs()).
Provides app and async HTTP client fixtures for E2E testing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _integration_env(monkeypatch):
    """Set environment variables required by the gateway under test."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("ENABLE_AUTH", "false")
    monkeypatch.setenv("ENABLE_DOCS", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SERVICE_NAME", "Code Graph Helper API")
    # Clear settings cache so env vars take effect
    from config.settings import get_settings

    get_settings.cache_clear()


@pytest.fixture
def mock_cache_manager():
    """Provide a mock cache manager with common methods stubbed."""
    cm = AsyncMock()
    cm.get = AsyncMock(return_value=None)
    cm.set = AsyncMock(return_value=True)
    cm.close = AsyncMock()
    cm.get_stats = MagicMock(return_value={"hits": 0, "misses": 0, "size": 0})
    cm.get_redis = AsyncMock(return_value=MagicMock())
    return cm


@pytest.fixture
async def app(mock_cache_manager):
    """Create a test FastAPI application with cache manager mocked."""
    with patch("services.cache_manager.get_cache_manager", return_value=mock_cache_manager):
        from app_factory import create_app

        application = create_app()
        yield application


@pytest.fixture
async def client(app):
    """Async HTTP test client backed by ASGI transport."""
    import httpx
    from httpx._transports.asgi import ASGITransport

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
