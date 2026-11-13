"""Tests for services/job_manager.py."""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from services.job_manager import KubernetesJobManager


def _owned_labels() -> dict:
    """Labels of a Job owned by the tenant these tests run as (n8n-dev)."""
    from services.job_manager import (
        APP_LABEL_KEY,
        APP_LABEL_VALUE,
        TENANT_LABEL_KEY,
        tenant_label_value,
    )

    return {APP_LABEL_KEY: APP_LABEL_VALUE, TENANT_LABEL_KEY: tenant_label_value("n8n-dev")}


@pytest.fixture
def manager():
    """KubernetesJobManager with mocked k8s config and API clients."""
    with patch("services.job_manager.config.load_kube_config"):
        with patch("services.job_manager.client.BatchV1Api") as batch_cls:
            with patch("services.job_manager.client.CoreV1Api") as core_cls:
                mgr = KubernetesJobManager(namespace="test-ns", in_cluster=False)
                mgr.batch_v1 = batch_cls.return_value
                mgr.core_v1 = core_cls.return_value
                return mgr


# ── create_pytest_job ─────────────────────────────────────────────────


class TestCreatePytestJob:
    async def test_happy_path(self, manager):
        """Creates a job and returns its name."""
        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[])
        result_mock = MagicMock()
        result_mock.metadata.name = "pytest-job-abc123"
        manager.batch_v1.create_namespaced_job.return_value = result_mock

        name = await manager.create_pytest_job({"pytest_args": ["-x"]}, tenant_id="n8n-dev")
        assert name == "pytest-job-abc123"
        manager.batch_v1.create_namespaced_job.assert_called_once()

    async def test_exceeds_concurrent_limit(self, manager):
        """Raises RuntimeError when at max concurrent jobs."""
        # Return MAX_CONCURRENT_JOBS active jobs
        active_items = []
        for i in range(manager.MAX_CONCURRENT_JOBS):
            job = MagicMock()
            job.metadata.labels = _owned_labels()
            job.metadata.name = f"job-{i}"
            job.status.active = 1
            job.status.completion_time = None
            active_items.append(job)

        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=active_items)

        with pytest.raises(RuntimeError, match="Max .* concurrent jobs"):
            await manager.create_pytest_job({"pytest_args": []}, tenant_id="n8n-dev")

    async def test_api_exception_propagates(self, manager):
        """ApiException from create is re-raised."""
        from kubernetes.client.rest import ApiException

        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[])
        manager.batch_v1.create_namespaced_job.side_effect = ApiException(status=500, reason="ISE")

        with pytest.raises(ApiException):
            await manager.create_pytest_job({"pytest_args": []}, tenant_id="n8n-dev")


# ── get_job_status ────────────────────────────────────────────────────


class TestGetJobStatus:
    def _make_job(self, active=None, succeeded=None, failed=None, start=None, completion=None):
        job = MagicMock()
        job.metadata.labels = _owned_labels()
        job.status.active = active
        job.status.succeeded = succeeded
        job.status.failed = failed
        job.status.start_time = start
        job.status.completion_time = completion
        return job

    async def test_succeeded(self, manager):
        manager.batch_v1.read_namespaced_job.return_value = self._make_job(succeeded=1)
        status = await manager.get_job_status("j1", tenant_id="n8n-dev")
        assert status["status"] == "succeeded"

    async def test_failed(self, manager):
        manager.batch_v1.read_namespaced_job.return_value = self._make_job(failed=1)
        status = await manager.get_job_status("j1", tenant_id="n8n-dev")
        assert status["status"] == "failed"

    async def test_running(self, manager):
        manager.batch_v1.read_namespaced_job.return_value = self._make_job(active=1)
        status = await manager.get_job_status("j1", tenant_id="n8n-dev")
        assert status["status"] == "running"

    async def test_pending(self, manager):
        manager.batch_v1.read_namespaced_job.return_value = self._make_job()
        status = await manager.get_job_status("j1", tenant_id="n8n-dev")
        assert status["status"] == "pending"

    async def test_not_found(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=404, reason="NF")
        result = await manager.get_job_status("missing", tenant_id="n8n-dev")
        assert result is None

    async def test_other_api_error(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=500, reason="ISE")
        with pytest.raises(ApiException):
            await manager.get_job_status("j1", tenant_id="n8n-dev")


# ── get_job_logs ──────────────────────────────────────────────────────


class TestGetJobLogs:
    async def test_returns_logs(self, manager):
        pod = MagicMock()
        pod.metadata.name = "pod-1"
        manager.core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        manager.core_v1.read_namespaced_pod_log.return_value = "test output"

        logs = await manager.get_job_logs("j1", tenant_id="n8n-dev")
        assert "pod-1" in logs
        assert "test output" in logs

    async def test_no_pods(self, manager):
        manager.core_v1.list_namespaced_pod.return_value = MagicMock(items=[])
        logs = await manager.get_job_logs("j1", tenant_id="n8n-dev")
        assert logs == "[No logs available]"

    async def test_pod_log_error(self, manager):
        from kubernetes.client.rest import ApiException

        pod = MagicMock()
        pod.metadata.name = "pod-1"
        manager.core_v1.list_namespaced_pod.return_value = MagicMock(items=[pod])
        manager.core_v1.read_namespaced_pod_log.side_effect = ApiException(
            status=500, reason="fail"
        )

        logs = await manager.get_job_logs("j1", tenant_id="n8n-dev")
        assert "Error reading logs" in logs


# ── delete_job ────────────────────────────────────────────────────────


class TestDeleteJob:
    @pytest.fixture(autouse=True)
    def _owned(self, manager):
        manager.batch_v1.read_namespaced_job.return_value.metadata.labels = _owned_labels()

    async def test_delete_success(self, manager):
        assert await manager.delete_job("j1", tenant_id="n8n-dev") is True
        manager.batch_v1.delete_namespaced_job.assert_called_once()

    async def test_delete_not_found(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.delete_namespaced_job.side_effect = ApiException(status=404, reason="NF")
        await manager.delete_job("j1", tenant_id="n8n-dev")  # should not raise

    async def test_delete_other_error(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.delete_namespaced_job.side_effect = ApiException(status=500, reason="ISE")
        with pytest.raises(ApiException):
            await manager.delete_job("j1", tenant_id="n8n-dev")

    async def test_foreign_job_is_not_deleted(self, manager):
        assert await manager.delete_job("j1", tenant_id="someone-else") is False
        manager.batch_v1.delete_namespaced_job.assert_not_called()

    async def test_missing_job_returns_false(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=404, reason="NF")
        assert await manager.delete_job("j1", tenant_id="n8n-dev") is False
        manager.batch_v1.delete_namespaced_job.assert_not_called()

    async def test_read_error_propagates(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=500, reason="ISE")
        with pytest.raises(ApiException):
            await manager.delete_job("j1", tenant_id="n8n-dev")


# ── get_active_jobs ───────────────────────────────────────────────────


class TestGetActiveJobs:
    async def test_returns_active(self, manager):
        active_job = MagicMock()
        active_job.metadata.labels = _owned_labels()
        active_job.metadata.name = "active-1"
        active_job.status.active = 1
        active_job.status.completion_time = None

        done_job = MagicMock()

        done_job.metadata.labels = _owned_labels()
        done_job.metadata.name = "done-1"
        done_job.status.active = 0
        done_job.status.completion_time = datetime.now(UTC)

        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[active_job, done_job])

        result = await manager.get_active_jobs(tenant_id="n8n-dev")
        assert "active-1" in result
        assert "done-1" not in result


# ── list_all_jobs ─────────────────────────────────────────────────────


class TestListAllJobs:
    async def test_returns_job_list(self, manager):
        job = MagicMock()
        job.metadata.labels = _owned_labels()
        job.metadata.name = "j1"
        job.metadata.creation_timestamp = datetime(2025, 1, 1, tzinfo=UTC)
        job.status.active = 0
        job.status.succeeded = 1
        job.status.failed = 0
        job.status.completion_time = datetime(2025, 1, 1, 0, 5, tzinfo=UTC)

        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[job])

        result = await manager.list_all_jobs()
        assert len(result) == 1
        assert result[0]["name"] == "j1"
        assert result[0]["succeeded"] == 1

    async def test_with_label_selector(self, manager):
        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[])
        await manager.list_all_jobs(label_selector="app=test")
        manager.batch_v1.list_namespaced_job.assert_called_with(
            namespace="test-ns", label_selector="app=test"
        )


# ── cleanup_old_jobs ──────────────────────────────────────────────────


class TestCleanupOldJobs:
    async def test_deletes_old_completed(self, manager):
        old_job = MagicMock()
        old_job.metadata.name = "old-1"
        old_job.status.completion_time = datetime.now(UTC) - timedelta(days=2)

        fresh_job = MagicMock()
        fresh_job.metadata.name = "fresh-1"
        fresh_job.status.completion_time = datetime.now(UTC) - timedelta(hours=1)

        running_job = MagicMock()
        running_job.metadata.name = "running-1"
        running_job.status.completion_time = None

        manager.batch_v1.list_namespaced_job.return_value = MagicMock(
            items=[old_job, fresh_job, running_job]
        )

        deleted = await manager.cleanup_old_jobs(max_age_seconds=86400)
        assert deleted == 1

    async def test_no_old_jobs(self, manager):
        manager.batch_v1.list_namespaced_job.return_value = MagicMock(items=[])
        deleted = await manager.cleanup_old_jobs()
        assert deleted == 0


# ── job_exists ────────────────────────────────────────────────────────


class TestJobExists:
    async def test_exists(self, manager):
        manager.batch_v1.read_namespaced_job.return_value = MagicMock()
        assert await manager.job_exists("j1") is True

    async def test_not_exists(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=404, reason="NF")
        assert await manager.job_exists("j1") is False


# ── get_completion_time ───────────────────────────────────────────────


class TestGetCompletionTime:
    async def test_completed(self, manager):
        dt = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)
        job = MagicMock()
        job.metadata.labels = _owned_labels()
        job.status.completion_time = dt
        manager.batch_v1.read_namespaced_job.return_value = job

        result = await manager.get_completion_time("j1")
        assert result == dt.isoformat()

    async def test_not_completed(self, manager):
        job = MagicMock()
        job.metadata.labels = _owned_labels()
        job.status.completion_time = None
        manager.batch_v1.read_namespaced_job.return_value = job

        result = await manager.get_completion_time("j1")
        assert result is None


# ── _build_job_manifest ───────────────────────────────────────────────


class TestBuildJobManifest:
    def test_manifest_structure(self, manager):
        manifest = manager._build_job_manifest(
            {"pytest_args": ["-x", "--tb=short"]}, tenant_id="n8n-dev"
        )
        assert manifest["apiVersion"] == "batch/v1"
        assert manifest["kind"] == "Job"
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["command"] == ["pytest"]
        assert "-x" in container["args"]

    def test_custom_timeout(self, manager):
        manifest = manager._build_job_manifest(
            {"pytest_args": []}, timeout_seconds=120, tenant_id="n8n-dev"
        )
        assert manifest["spec"]["activeDeadlineSeconds"] == 120


# ── wait_for_completion ───────────────────────────────────────────────


class TestWaitForCompletion:
    async def test_immediate_success(self, manager):
        job = MagicMock()
        job.metadata.labels = _owned_labels()
        job.status.succeeded = 1
        job.status.failed = None
        job.status.active = None
        job.status.start_time = None
        job.status.completion_time = None
        manager.batch_v1.read_namespaced_job.return_value = job

        result = await manager.wait_for_completion("j1", timeout=5, tenant_id="n8n-dev")
        assert result["status"] == "succeeded"

    async def test_not_found_raises(self, manager):
        from kubernetes.client.rest import ApiException

        manager.batch_v1.read_namespaced_job.side_effect = ApiException(status=404, reason="NF")
        with pytest.raises(LookupError, match="Job not found"):
            await manager.wait_for_completion("j1", timeout=1, tenant_id="n8n-dev")
