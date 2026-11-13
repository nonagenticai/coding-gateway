"""The gateway must never report an incomplete OpenAPI aggregation as complete.

PR #44 made partial aggregation visible via the ``x-coding-aggregation``
extension. Two surfaces bypassed it and read as full success:

  * ``GET /openapi.json`` — when ``get_aggregated_spec`` itself raised, the bare
    ``gateway_spec`` was served with NO ``x-coding-aggregation`` key at all, so a
    consumer gating on ``complete`` saw a missing marker rather than ``false``.
  * ``POST /api/openapi/refresh`` — returned ``{"status": "ok"}`` whether one
    backend was down or all of them were.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import openapi_aggregator
from openapi_aggregator import AGGREGATION_EXT


@pytest.fixture(autouse=True)
def _clear_cache():
    openapi_aggregator.invalidate_cache()
    yield
    openapi_aggregator.invalidate_cache()


# ---------------------------------------------------------------------------
# GET /openapi.json — exception fallback
# ---------------------------------------------------------------------------


class TestAggregationExceptionFallbackIsHonest:
    async def test_fallback_spec_carries_incomplete_marker(self, client):
        """Aggregation blowing up must still yield an explicit complete=false."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(side_effect=RuntimeError("aggregator exploded")),
        ):
            resp = await client.get("/openapi.json")

        assert resp.status_code == 200
        spec = resp.json()
        assert AGGREGATION_EXT in spec, "fallback spec dropped the aggregation marker"
        ext = spec[AGGREGATION_EXT]
        assert ext["complete"] is False
        assert ext["succeeded"] == []
        # Nothing was merged, so EVERY configured backend is reported failed.
        assert set(ext["failed"]) == set(openapi_aggregator._backend_urls())
        assert all(b["ok"] is False for b in ext["backends"])
        assert "aggregator exploded" in ext["error"]

    async def test_fallback_still_serves_the_gateway_paths(self, client):
        """Honesty must not cost availability — the gateway-only spec still ships."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            resp = await client.get("/openapi.json")

        spec = resp.json()
        assert spec["info"]["title"] == "Code Graph Helper API"
        assert len(spec.get("paths", {})) > 0

    async def test_bare_exception_reports_the_class_name_not_empty_string(self, client):
        """`str(SomeError())` is empty; an ``"error": ""`` field reads as no error."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(side_effect=ValueError()),
        ):
            resp = await client.get("/openapi.json")

        assert resp.json()[AGGREGATION_EXT]["error"] == "ValueError"

    async def test_successful_aggregation_is_not_marked_failed(self, client):
        """Negative control: the marker must reflect reality, not always say false."""
        resp = await client.get("/openapi.json")
        ext = resp.json().get(AGGREGATION_EXT, {})
        # All backends are unreachable in unit tests, so this scrape is genuinely
        # partial — but it must come from the real aggregator, not the fallback,
        # so it carries no synthetic "error" field.
        assert "error" not in ext

    def test_spec_with_failure_marker_does_not_mutate_the_input(self):
        """`app.openapi()` hands back FastAPI's OWN cached spec object."""
        original = {"paths": {"/health": {}}}
        stamped = openapi_aggregator.spec_with_failure_marker(original, "boom")

        assert AGGREGATION_EXT not in original
        assert stamped[AGGREGATION_EXT]["complete"] is False
        assert stamped["paths"] == {"/health": {}}


# ---------------------------------------------------------------------------
# POST /api/openapi/refresh
# ---------------------------------------------------------------------------


def _merged(complete: bool, succeeded: list[str], failed: list[str]) -> dict:
    return {
        "paths": {"/gateway": {}, "/analysis": {}},
        AGGREGATION_EXT: {
            "complete": complete,
            "generated_at": 0.0,
            "backends": [{"name": n, "ok": True} for n in succeeded]
            + [{"name": n, "ok": False} for n in failed],
            "succeeded": succeeded,
            "failed": failed,
        },
    }


class TestRefreshReportsRealAggregationState:
    async def test_partial_refresh_reports_partial(self, client):
        """A refresh that could not reach every backend must NOT read as ok."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(return_value=_merged(False, ["analysis"], ["ai", "realtime"])),
        ):
            resp = await client.post("/api/openapi/refresh")

        assert resp.status_code == 207, "a partial refresh must be distinguishable from 200"
        body = resp.json()
        assert body["status"] == "partial"
        assert body["status"] != "ok"
        assert body["complete"] is False
        assert body["succeeded"] == ["analysis"]
        assert body["failed"] == ["ai", "realtime"]
        # The operator-facing message names what is missing.
        assert "ai" in body["message"] and "realtime" in body["message"]

    async def test_complete_refresh_reports_ok(self, client):
        """Negative control: a genuinely complete refresh still reads as ok/200."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(return_value=_merged(True, ["analysis", "ai"], [])),
        ):
            resp = await client.post("/api/openapi/refresh")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["complete"] is True
        assert body["failed"] == []
        assert body["total_paths"] == 2

    async def test_refresh_exception_reports_every_backend_failed(self, client):
        """The 500 path also carries the honest per-backend breakdown."""
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(side_effect=RuntimeError("refresh exploded")),
        ):
            resp = await client.post("/api/openapi/refresh")

        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "error"
        assert body["complete"] is False
        assert body["succeeded"] == []
        assert set(body["failed"]) == set(openapi_aggregator._backend_urls())
        assert "refresh exploded" in body["error"]

    async def test_refresh_invalidates_the_cache(self, client):
        """Regression guard: the refresh endpoint must still bust the cache."""
        openapi_aggregator._cache = ({"paths": {}}, 1e12)
        with patch(
            "openapi_aggregator.get_aggregated_spec",
            AsyncMock(return_value=_merged(True, ["analysis"], [])),
        ):
            await client.post("/api/openapi/refresh")
        # get_aggregated_spec was mocked, so nothing repopulated the cache.
        assert openapi_aggregator._cache == (None, 0.0)
