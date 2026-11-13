"""Tests for the validation_status router and PRValidationOrchestrator.

Covers GET /{validation_id}, POST /status, POST /complete endpoints
and the in-memory orchestrator logic (register, evict, update).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_orchestrator():
    """Create a fresh orchestrator with one pre-registered validation."""
    from routers.validation_status import PRValidationOrchestrator

    orch = PRValidationOrchestrator()
    orch.register_validation(
        "val-001",
        {
            "fast_validation": {"passed": True, "checks": 5},
            "comprehensive_validation": {"status": "triggered"},
        },
    )
    return orch


def _patch_orchestrator(orch):
    return patch(
        "routers.validation_status.get_pr_validation_orchestrator",
        return_value=orch,
    )


# ---------------------------------------------------------------------------
# GET /api/validation/{validation_id}
# ---------------------------------------------------------------------------


class TestGetValidationStatus:
    async def test_existing_validation_in_progress(self, client):
        """Status is 'in_progress' when comprehensive_validation.status == 'triggered'."""
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.get("/api/validation/val-001")
        assert resp.status_code == 200
        body = resp.json()
        assert body["validation_id"] == "val-001"
        assert body["status"] == "in_progress"
        assert body["fast_validation"]["passed"] is True
        assert body["fast_validation"]["checks"] == 5

    async def test_missing_validation_returns_404(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.get("/api/validation/nonexistent")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        "comp_status,expected_overall",
        [
            ("completed", "completed"),
            ("failed", "failed"),
        ],
    )
    async def test_comprehensive_status_propagates(self, client, comp_status, expected_overall):
        orch = _fresh_orchestrator()
        orch.validations["val-001"]["comprehensive_validation"]["status"] = comp_status
        with _patch_orchestrator(orch):
            resp = await client.get("/api/validation/val-001")
        assert resp.status_code == 200
        assert resp.json()["status"] == expected_overall

    async def test_fast_passed_no_comprehensive(self, client):
        """When comprehensive_validation is empty and fast passed, status is
        'fast_validation_passed'."""
        orch = _fresh_orchestrator()
        orch.validations["val-001"]["comprehensive_validation"] = {}
        with _patch_orchestrator(orch):
            resp = await client.get("/api/validation/val-001")
        assert resp.status_code == 200
        assert resp.json()["status"] == "fast_validation_passed"

    async def test_fast_failed_no_comprehensive(self, client):
        orch = _fresh_orchestrator()
        orch.validations["val-001"]["comprehensive_validation"] = {}
        orch.validations["val-001"]["fast_validation"]["passed"] = False
        with _patch_orchestrator(orch):
            resp = await client.get("/api/validation/val-001")
        assert resp.status_code == 200
        assert resp.json()["status"] == "fast_validation_failed"


# ---------------------------------------------------------------------------
# POST /api/validation/status
# ---------------------------------------------------------------------------


class TestUpdateValidationStatus:
    async def test_update_existing_validation(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/status",
                json={
                    "validation_id": "val-001",
                    "status": "in_progress",
                    "stage": "lint",
                    "message": "Running lint checks",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["validation_id"] == "val-001"
        # Verify the stage was stored on the comprehensive_validation dict
        comp = orch.validations["val-001"]["comprehensive_validation"]
        assert comp["stage"] == "lint"
        assert comp["message"] == "Running lint checks"

    async def test_update_missing_returns_404(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/status",
                json={
                    "validation_id": "nonexistent",
                    "status": "in_progress",
                },
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/validation/complete
# ---------------------------------------------------------------------------


class TestCompleteValidation:
    async def test_completed_with_results(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/complete",
                json={
                    "validation_id": "val-001",
                    "status": "completed",
                    "results": {"score": 95, "issues": []},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "completed" in body["message"].lower() or "stored" in body["message"].lower()
        # Verify results actually stored
        comp = orch.validations["val-001"]["comprehensive_validation"]
        assert comp["status"] == "completed"
        assert comp["results"]["score"] == 95

    async def test_failed_records_error(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/complete",
                json={
                    "validation_id": "val-001",
                    "status": "failed",
                    "error": "Tests did not pass",
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "failure" in body["message"].lower() or "recorded" in body["message"].lower()
        comp = orch.validations["val-001"]["comprehensive_validation"]
        assert comp["status"] == "failed"
        assert comp["error"] == "Tests did not pass"

    async def test_missing_validation_returns_404(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/complete",
                json={
                    "validation_id": "nonexistent",
                    "status": "completed",
                    "results": {"score": 0},
                },
            )
        assert resp.status_code == 404

    async def test_invalid_status_without_results_returns_400(self, client):
        orch = _fresh_orchestrator()
        with _patch_orchestrator(orch):
            resp = await client.post(
                "/api/validation/complete",
                json={
                    "validation_id": "val-001",
                    "status": "pending",
                },
            )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PRValidationOrchestrator unit tests
# ---------------------------------------------------------------------------


class TestOrchestratorUnit:
    def test_register_and_retrieve(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        orch.register_validation("v1", {"data": "hello"})
        result = orch.get_validation_status("v1")
        assert result is not None
        assert result["data"] == "hello"
        assert "_created_at" in result  # timestamp is injected

    def test_get_missing_returns_none(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        assert orch.get_validation_status("missing") is None

    async def test_update_comprehensive_results_success(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        orch.register_validation("v1", {"data": "x"})
        success = await orch.update_comprehensive_results("v1", {"score": 100})
        assert success is True
        comp = orch.validations["v1"]["comprehensive_validation"]
        assert comp["status"] == "completed"
        assert comp["results"]["score"] == 100

    async def test_update_comprehensive_missing_returns_false(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        success = await orch.update_comprehensive_results("missing", {})
        assert success is False

    def test_eviction_when_max_exceeded(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        orch.MAX_VALIDATIONS = 3
        for i in range(4):
            orch.register_validation(f"v{i}", {"idx": i})
        assert len(orch.validations) == 3
        assert "v0" not in orch.validations
        assert "v3" in orch.validations

    def test_register_overwrites_existing(self):
        from routers.validation_status import PRValidationOrchestrator

        orch = PRValidationOrchestrator()
        orch.register_validation("v1", {"version": 1})
        orch.register_validation("v1", {"version": 2})
        assert orch.validations["v1"]["version"] == 2
