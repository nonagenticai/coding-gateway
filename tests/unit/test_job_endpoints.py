"""Tests for jobs router -- create, get status, delete, list, health.

Sends real HTTP requests via the async test client, mocks only the
KubernetesJobManager (which requires a live K8s cluster). Asserts on
actual response body structure and values.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch


def _mock_job_manager(
    namespace: str = "code-graph-helper",
    active_jobs: list | None = None,
    job_status: dict | None = None,
    job_logs: str = "test output",
    create_result: str = "pytest-job-abc123",
):
    """Return a mock KubernetesJobManager."""
    mgr = AsyncMock()
    mgr.namespace = namespace
    mgr.get_active_jobs = AsyncMock(return_value=active_jobs or [])
    mgr.count_all_active_jobs = AsyncMock(return_value=len(active_jobs or []))
    mgr.get_job_status = AsyncMock(return_value=job_status)
    mgr.get_job_logs = AsyncMock(return_value=job_logs)
    mgr.create_pytest_job = AsyncMock(return_value=create_result)
    # delete_job reports ownership itself: False == missing or another tenant's.
    mgr.delete_job = AsyncMock(return_value=job_status is not None)
    return mgr


def _patch_job_manager(mgr):
    return patch("routers.jobs.get_job_manager", return_value=mgr)


# ---------------------------------------------------------------------------
# Create job
# ---------------------------------------------------------------------------


class TestCreateJob:
    async def test_create_success(self, client):
        """POST /api/jobs creates a new job and returns 201."""
        mgr = _mock_job_manager(create_result="pytest-job-xyz")
        with _patch_job_manager(mgr):
            resp = await client.post(
                "/api/jobs",
                json={"pytest_args": ["-v", "tests/"], "timeout_seconds": 600},
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["job_name"] == "pytest-job-xyz"
        assert data["status"] == "created"
        assert data["namespace"] == "code-graph-helper"

    async def test_create_passes_config_to_manager(self, client):
        """POST /api/jobs forwards the test config to the job manager."""
        mgr = _mock_job_manager()
        with _patch_job_manager(mgr):
            await client.post(
                "/api/jobs",
                json={
                    "pytest_args": ["-x", "--tb=short"],
                    "timeout_seconds": 300,
                    "description": "quick test",
                },
            )

        call_kwargs = mgr.create_pytest_job.call_args
        config = call_kwargs.kwargs["test_config"]
        assert config["pytest_args"] == ["-x", "--tb=short"]
        assert config["timeout_seconds"] == 300
        assert config["description"] == "quick test"

    async def test_create_too_many_jobs_429(self, client):
        """POST /api/jobs returns 429 when max concurrent reached."""
        mgr = _mock_job_manager(active_jobs=["j1", "j2", "j3"])
        with _patch_job_manager(mgr), patch.dict("os.environ", {"MAX_CONCURRENT_JOBS": "3"}):
            resp = await client.post(
                "/api/jobs",
                json={"pytest_args": ["-v"], "timeout_seconds": 120},
            )

        assert resp.status_code == 429
        body = resp.json()
        assert body["detail"]["error_code"] == "TOO_MANY_JOBS"
        assert "3" in body["detail"]["message"]

    async def test_create_manager_failure_500(self, client):
        """POST /api/jobs returns 500 when job manager raises."""
        mgr = _mock_job_manager()
        mgr.create_pytest_job = AsyncMock(side_effect=RuntimeError("k8s error"))
        with _patch_job_manager(mgr):
            resp = await client.post(
                "/api/jobs",
                json={"pytest_args": [], "timeout_seconds": 120},
            )

        assert resp.status_code == 500

    async def test_create_default_config(self, client):
        """POST /api/jobs with empty body uses default config values."""
        mgr = _mock_job_manager()
        with _patch_job_manager(mgr):
            resp = await client.post("/api/jobs", json={})

        assert resp.status_code == 201
        call_kwargs = mgr.create_pytest_job.call_args
        config = call_kwargs.kwargs["test_config"]
        assert config["pytest_args"] == []
        assert config["timeout_seconds"] == 3600

    async def test_create_invalid_timeout_too_low(self, client):
        """POST /api/jobs with timeout < 60 returns 422 validation error."""
        mgr = _mock_job_manager()
        with _patch_job_manager(mgr):
            resp = await client.post(
                "/api/jobs",
                json={"timeout_seconds": 10},
            )

        assert resp.status_code == 422

    async def test_create_invalid_timeout_too_high(self, client):
        """POST /api/jobs with timeout > 3600 returns 422 validation error."""
        mgr = _mock_job_manager()
        with _patch_job_manager(mgr):
            resp = await client.post(
                "/api/jobs",
                json={"timeout_seconds": 9999},
            )

        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Get job status
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    async def test_get_success(self, client):
        """GET /api/jobs/<name> returns full job status with logs."""
        mgr = _mock_job_manager(
            job_status={
                "status": "running",
                "active": 1,
                "succeeded": 0,
                "failed": 0,
                "start_time": "2026-01-01T00:00:00Z",
            },
            job_logs="Running tests...",
        )
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/pytest-job-abc")

        assert resp.status_code == 200
        data = resp.json()
        assert data["job_name"] == "pytest-job-abc"
        assert data["namespace"] == "code-graph-helper"
        assert data["status"] == "running"
        assert data["active"] == 1
        assert data["succeeded"] == 0
        assert data["failed"] == 0
        assert data["start_time"] == "2026-01-01T00:00:00Z"
        assert data["logs"] == "Running tests..."

    async def test_get_not_found(self, client):
        """GET /api/jobs/<missing> returns 404."""
        mgr = _mock_job_manager(job_status=None)
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/nonexistent")

        assert resp.status_code == 404

    async def test_get_logs_failure_graceful(self, client):
        """GET /api/jobs/<name> with log retrieval error still returns status."""
        mgr = _mock_job_manager(
            job_status={"status": "completed", "succeeded": 1},
        )
        mgr.get_job_logs = AsyncMock(side_effect=RuntimeError("log pod gone"))
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/pytest-job-abc")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "completed"
        assert "unavailable" in data["logs"].lower()

    async def test_get_without_logs(self, client):
        """GET /api/jobs/<name>?include_logs=false skips log retrieval."""
        mgr = _mock_job_manager(
            job_status={"status": "running", "active": 1},
        )
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/pytest-job-abc?include_logs=false")

        assert resp.status_code == 200
        mgr.get_job_logs.assert_not_awaited()
        assert resp.json()["logs"] == ""


# ---------------------------------------------------------------------------
# Delete job
# ---------------------------------------------------------------------------


class TestDeleteJob:
    async def test_delete_success(self, client):
        """DELETE /api/jobs/<name> deletes the job and returns confirmation."""
        mgr = _mock_job_manager(
            job_status={"status": "completed"},
        )
        with _patch_job_manager(mgr):
            resp = await client.delete("/api/jobs/pytest-job-abc")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        assert data["job_name"] == "pytest-job-abc"
        assert "pytest-job-abc" in data["message"]
        assert "request_id" in data
        assert "timestamp" in data
        mgr.delete_job.assert_awaited_once_with("pytest-job-abc", tenant_id="n8n-dev")

    async def test_delete_not_found(self, client):
        """DELETE /api/jobs/<missing> returns 404."""
        mgr = _mock_job_manager(job_status=None)
        with _patch_job_manager(mgr):
            resp = await client.delete("/api/jobs/nonexistent")

        assert resp.status_code == 404

    async def test_delete_failure_500(self, client):
        """DELETE /api/jobs/<name> returns 500 when delete raises."""
        mgr = _mock_job_manager(
            job_status={"status": "running"},
        )
        mgr.delete_job = AsyncMock(side_effect=RuntimeError("k8s error"))
        with _patch_job_manager(mgr):
            resp = await client.delete("/api/jobs/pytest-job-abc")

        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# List active jobs
# ---------------------------------------------------------------------------


class TestListJobs:
    async def test_list_empty(self, client):
        """GET /api/jobs returns empty list when no active jobs."""
        mgr = _mock_job_manager(active_jobs=[])
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["active_jobs"] == []
        assert data["namespace"] == "code-graph-helper"
        assert data["max_concurrent"] == 3

    async def test_list_with_jobs(self, client):
        """GET /api/jobs returns active jobs with count."""
        mgr = _mock_job_manager(active_jobs=["job-1", "job-2"])
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2
        assert data["active_jobs"] == ["job-1", "job-2"]


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestJobsHealth:
    async def test_health_success(self, client):
        """GET /api/jobs/health returns healthy status with active count."""
        mgr = _mock_job_manager(active_jobs=["j1"])
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["active_jobs"] == 1
        assert data["namespace"] == "code-graph-helper"
        assert "operational" in data["message"]

    async def test_health_failure_503(self, client):
        """GET /api/jobs/health returns 503 when manager fails."""
        mgr = _mock_job_manager()
        mgr.count_all_active_jobs = AsyncMock(side_effect=RuntimeError("k8s unreachable"))
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/health")

        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestJobErrorPaths:
    """Test error handling in job endpoints."""

    async def test_create_job_too_many_concurrent_returns_429(self, client):
        """POST /api/jobs returns 429 when max concurrent jobs reached."""
        mgr = _mock_job_manager(active_jobs=["j1", "j2", "j3"])
        with _patch_job_manager(mgr):
            resp = await client.post(
                "/api/jobs",
                json={"pytest_args": ["-v"]},
            )
        assert resp.status_code == 429
        data = resp.json()
        assert "TOO_MANY_JOBS" in data["detail"]["error_code"]

    async def test_list_jobs_error_returns_500(self, client):
        """GET /api/jobs returns 500 when manager raises."""
        mgr = _mock_job_manager()
        mgr.get_active_jobs = AsyncMock(side_effect=RuntimeError("k8s down"))
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs")
        assert resp.status_code == 500

    async def test_get_job_status_not_found_returns_404(self, client):
        """GET /api/jobs/{name} returns 404 for nonexistent job."""
        mgr = _mock_job_manager(job_status=None)
        mgr.get_job_status = AsyncMock(return_value=None)
        with _patch_job_manager(mgr):
            resp = await client.get("/api/jobs/nonexistent-job")
        assert resp.status_code == 404

    async def test_delete_job_not_found_returns_404(self, client):
        """DELETE /api/jobs/{name} returns 404 for nonexistent job."""
        mgr = _mock_job_manager(job_status=None)
        mgr.get_job_status = AsyncMock(return_value=None)
        with _patch_job_manager(mgr):
            resp = await client.delete("/api/jobs/nonexistent-job")
        assert resp.status_code == 404
