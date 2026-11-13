"""
Kubernetes Job Manager Service

Provides asynchronous management of Kubernetes batch jobs for pytest execution
with resource limits, concurrent job management, and automatic cleanup.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from config.settings import get_settings

logger = logging.getLogger(__name__)

#: Label every Job this service creates carries; a Job without it is not ours.
APP_LABEL_KEY = "app"
APP_LABEL_VALUE = "code-graph-helper"

#: Label that records which tenant owns a Job. The VALUE is a digest of the
#: tenant id, not the id itself: a label value is capped at 63 characters of
#: ``[A-Za-z0-9._-]`` and a tenant id is not guaranteed to fit that alphabet.
#: 32 hex chars (128 bits) is collision-safe for any realistic tenant count.
TENANT_LABEL_KEY = "coding.nonagentic.ai/tenant"

#: Annotation carrying the raw tenant id, for humans reading ``kubectl``.
#: Never used for authorisation (annotations cannot be selected on).
TENANT_ANNOTATION_KEY = "coding.nonagentic.ai/tenant-id"


def tenant_label_value(tenant_id: str) -> str:
    """Return the label value that marks a Job as owned by ``tenant_id``.

    Refuses an empty tenant: an empty id would hash to a real, shared value and
    every caller that failed to resolve a tenant would land in the same bucket.
    """
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant_id is required to scope a job")
    return hashlib.sha256(tenant_id.strip().encode("utf-8")).hexdigest()[:32]


def tenant_selector(tenant_id: str) -> str:
    """Label selector matching only this service's Jobs owned by ``tenant_id``."""
    return f"{APP_LABEL_KEY}={APP_LABEL_VALUE},{TENANT_LABEL_KEY}={tenant_label_value(tenant_id)}"


def job_owned_by(job: Any, tenant_id: str) -> bool:
    """True when ``job`` was created by this service for ``tenant_id``.

    A Job with no tenant label (created before tenant scoping, or by anything
    else in the namespace) is owned by nobody and is invisible to every tenant.
    """
    labels = getattr(getattr(job, "metadata", None), "labels", None) or {}
    if not isinstance(labels, dict):
        return False
    if labels.get(APP_LABEL_KEY) != APP_LABEL_VALUE:
        return False
    return labels.get(TENANT_LABEL_KEY) == tenant_label_value(tenant_id)


class KubernetesJobManager:
    """
    Manages Kubernetes batch jobs for pytest execution.

    Features:
    - Create jobs with specified pytest arguments
    - Monitor job status and logs
    - Enforce concurrent job limits
    - Automatic cleanup via TTL
    """

    def __init__(self, namespace: str = "code-graph-helper", in_cluster: bool | None = None):
        """
        Initialize the Kubernetes Job Manager.

        Args:
            namespace: Kubernetes namespace for job operations
            in_cluster: Whether running inside a cluster (auto-detect if None)
        """
        self.namespace = namespace

        # Load settings from centralized config
        settings = get_settings()
        self.MAX_CONCURRENT_JOBS = settings.max_concurrent_jobs
        self.DEFAULT_TTL_SECONDS = settings.job_ttl_seconds
        self.DEFAULT_BACKOFF_LIMIT = settings.job_backoff_limit
        self.DEFAULT_TIMEOUT_SECONDS = settings.job_timeout_seconds

        # Determine if running in-cluster
        if in_cluster is None:
            in_cluster = os.environ.get("KUBERNETES_IN_CLUSTER", "false").lower() == "true"

        try:
            if in_cluster:
                logger.info("Loading in-cluster Kubernetes configuration")
                config.load_incluster_config()
            else:
                logger.info("Loading local Kubernetes configuration")
                config.load_kube_config()
        except Exception as e:
            logger.error(f"Failed to load Kubernetes config: {e}")
            raise

        # Workaround for kubernetes>=33: InClusterConfigLoader._set_config writes
        # the SA token to api_key['authorization'] but Configuration.auth_settings()
        # only reads api_key['BearerToken']. Without this bridge every request
        # goes anonymously and the apiserver returns 401. Remove when the lib pin
        # bumps past the upstream fix. Mirrors coding-containers' fix (2026-05-27).
        _cfg = client.Configuration.get_default_copy()
        if "authorization" in _cfg.api_key and "BearerToken" not in _cfg.api_key:
            _cfg.api_key["BearerToken"] = _cfg.api_key["authorization"]
            client.Configuration.set_default(_cfg)

        # Initialize API clients
        self.batch_v1 = client.BatchV1Api()
        self.core_v1 = client.CoreV1Api()

        logger.info(
            f"KubernetesJobManager initialized for namespace: {namespace}, in_cluster: {in_cluster}"
        )

    async def create_pytest_job(
        self,
        test_config: dict[str, Any],
        timeout_seconds: int | None = None,
        *,
        tenant_id: str,
    ) -> str:
        """
        Create a Kubernetes job for pytest execution, owned by ``tenant_id``.

        Args:
            test_config: Configuration containing pytest_args
            timeout_seconds: Maximum execution time (optional)
            tenant_id: The VERIFIED tenant the job belongs to

        Returns:
            The generated job name

        Raises:
            Exception: If concurrent limit exceeded or creation fails
        """
        # Check concurrent limit (namespace-wide: it protects the cluster, not a tenant)
        active_count = await self.count_all_active_jobs()
        if active_count >= self.MAX_CONCURRENT_JOBS:
            raise RuntimeError(f"Max {self.MAX_CONCURRENT_JOBS} concurrent jobs allowed.")

        # Build job manifest
        job = self._build_job_manifest(test_config, timeout_seconds, tenant_id=tenant_id)

        try:
            # Create the job
            result = self.batch_v1.create_namespaced_job(namespace=self.namespace, body=job)
            job_name = result.metadata.name
            logger.info(f"Created pytest job: {job_name}")
            return job_name

        except ApiException as e:
            logger.error(f"Failed to create job: {e}")
            raise

    async def get_job_status(self, job_name: str, *, tenant_id: str) -> dict[str, Any] | None:
        """
        Get the status of a job owned by ``tenant_id``.

        Args:
            job_name: Name of the job
            tenant_id: The VERIFIED tenant of the caller

        Returns:
            Job status dictionary, or None if the job does not exist OR belongs
            to another tenant. The two are deliberately indistinguishable, so a
            caller cannot probe for other tenants' job names.
        """
        try:
            job = self.batch_v1.read_namespaced_job(name=job_name, namespace=self.namespace)
            if not job_owned_by(job, tenant_id):
                logger.info(f"Job not found for tenant: {job_name}")
                return None

            # Determine status
            if job.status.succeeded and job.status.succeeded > 0:
                status = "succeeded"
            elif job.status.failed and job.status.failed > 0:
                status = "failed"
            elif job.status.active and job.status.active > 0:
                status = "running"
            else:
                status = "pending"

            return {
                "status": status,
                "active": job.status.active or 0,
                "succeeded": job.status.succeeded or 0,
                "failed": job.status.failed or 0,
                "start_time": (
                    job.status.start_time.isoformat() if job.status.start_time else None
                ),
                "completion_time": (
                    job.status.completion_time.isoformat() if job.status.completion_time else None
                ),
            }

        except ApiException as e:
            if e.status == 404:
                logger.info(f"Job not found: {job_name}")
                return None
            logger.error(f"Failed to get job status: {e}")
            raise

    async def get_job_logs(self, job_name: str, *, tenant_id: str) -> str:
        """
        Get logs from the pods of a job owned by ``tenant_id``.

        Args:
            job_name: Name of the job
            tenant_id: The VERIFIED tenant of the caller

        Returns:
            Combined logs from all job pods
        """
        try:
            # Pods carry the tenant label too, so a foreign job's pods never match.
            pods = self.core_v1.list_namespaced_pod(
                namespace=self.namespace,
                label_selector=f"job-name={job_name},{tenant_selector(tenant_id)}",
            )

            logs = []
            for pod in pods.items:
                try:
                    pod_log = self.core_v1.read_namespaced_pod_log(
                        name=pod.metadata.name, namespace=self.namespace
                    )
                    logs.append(f"--- Pod: {pod.metadata.name} ---\n{pod_log}\n")
                except ApiException as e:
                    logs.append(f"[Error reading logs from {pod.metadata.name}: {e}]\n")

            return "".join(logs) or "[No logs available]"

        except ApiException as e:
            logger.error(f"Failed to get logs for job {job_name}: {e}")
            raise

    async def delete_job(self, job_name: str, *, tenant_id: str) -> bool:
        """
        Delete a job owned by ``tenant_id`` and its pods.

        Ownership is re-checked here rather than trusted from the caller, so no
        route can delete a job it has not proven the tenant owns.

        Args:
            job_name: Name of the job to delete
            tenant_id: The VERIFIED tenant of the caller

        Returns:
            True if deleted (or already gone after the ownership check), False
            if the job does not exist or belongs to another tenant.
        """
        try:
            job = self.batch_v1.read_namespaced_job(name=job_name, namespace=self.namespace)
        except ApiException as e:
            if e.status == 404:
                return False
            logger.error(f"Failed to read job before delete: {e}")
            raise
        if not job_owned_by(job, tenant_id):
            logger.info(f"Refusing delete of job not owned by tenant: {job_name}")
            return False

        await self._delete_by_name(job_name)
        return True

    async def _delete_by_name(self, job_name: str) -> None:
        """Delete a job and its pods with NO ownership check.

        Private on purpose: only ``delete_job`` (after its ownership check) and
        the operator-side ``cleanup_old_jobs`` may call it. Never a route.
        """
        try:
            # Delete with propagation policy to ensure pods are cleaned up
            self.batch_v1.delete_namespaced_job(
                name=job_name,
                namespace=self.namespace,
                body=client.V1DeleteOptions(propagation_policy="Background"),
            )
            logger.info(f"Deleted job: {job_name}")

        except ApiException as e:
            if e.status == 404:
                logger.info(f"Job not found (already deleted?): {job_name}")
                return
            logger.error(f"Failed to delete job: {e}")
            raise

    async def get_active_jobs(self, *, tenant_id: str) -> list[str]:
        """
        Get the active (running/pending) jobs owned by ``tenant_id``.

        Args:
            tenant_id: The VERIFIED tenant of the caller

        Returns:
            List of active job names
        """
        try:
            jobs = self.batch_v1.list_namespaced_job(
                namespace=self.namespace, label_selector=tenant_selector(tenant_id)
            )

            active_jobs = []
            for job in jobs.items:
                # Belt and braces: the selector already scoped the list, but a
                # list returned by anything other than a real apiserver (or a
                # future edit that drops the selector) must still not leak.
                if not job_owned_by(job, tenant_id):
                    continue
                # A job is active if it has active pods or hasn't completed
                if job.status.active or (not job.status.completion_time):
                    active_jobs.append(job.metadata.name)

            return active_jobs

        except ApiException as e:
            logger.error(f"Failed to list active jobs: {e}")
            raise

    async def count_all_active_jobs(self) -> int:
        """
        Count active jobs of this service across ALL tenants.

        For the namespace-wide concurrency cap and the health check only. It
        returns a number, never names, so it cannot leak another tenant's jobs.
        """
        try:
            jobs = self.batch_v1.list_namespaced_job(
                namespace=self.namespace, label_selector=f"{APP_LABEL_KEY}={APP_LABEL_VALUE}"
            )
            return sum(
                1 for job in jobs.items if job.status.active or (not job.status.completion_time)
            )
        except ApiException as e:
            logger.error(f"Failed to count active jobs: {e}")
            raise

    async def wait_for_completion(
        self, job_name: str, timeout: int = 60, *, tenant_id: str
    ) -> dict[str, Any]:
        """
        Wait for a job to complete with a timeout.

        Args:
            job_name: Name of the job
            timeout: Maximum time to wait in seconds

        Returns:
            Final job status

        Raises:
            TimeoutError: If job doesn't complete within timeout
        """
        start_time = datetime.now(UTC)
        max_wait = timeout

        while True:
            status = await self.get_job_status(job_name, tenant_id=tenant_id)

            if not status:
                raise LookupError(f"Job not found: {job_name}")

            if status["status"] in ("succeeded", "failed"):
                return status

            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            if elapsed > max_wait:
                raise TimeoutError(f"Job {job_name} did not complete within {timeout} seconds")

            # Wait before next check
            await asyncio.sleep(2)

    def _build_job_manifest(
        self,
        test_config: dict[str, Any],
        timeout_seconds: int | None = None,
        *,
        tenant_id: str,
    ) -> dict[str, Any]:
        """
        Build a Kubernetes Job manifest for pytest execution.

        Args:
            test_config: Test configuration with pytest_args
            timeout_seconds: Optional timeout override

        Returns:
            Kubernetes Job manifest dictionary
        """
        if timeout_seconds is None:
            timeout_seconds = self.DEFAULT_TIMEOUT_SECONDS

        settings = get_settings()
        ttl_seconds = settings.job_ttl_seconds
        pytest_image = settings.pytest_image

        # Extract pytest arguments from config
        pytest_args = test_config.get("pytest_args", [])

        labels = {
            APP_LABEL_KEY: APP_LABEL_VALUE,
            "component": "test-runner",
            TENANT_LABEL_KEY: tenant_label_value(tenant_id),
        }

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "generateName": "pytest-job-",
                "namespace": self.namespace,
                "labels": dict(labels),
                "annotations": {TENANT_ANNOTATION_KEY: tenant_id.strip()},
            },
            "spec": {
                "ttlSecondsAfterFinished": ttl_seconds,
                "backoffLimit": self.DEFAULT_BACKOFF_LIMIT,
                "activeDeadlineSeconds": timeout_seconds,
                "template": {
                    "metadata": {"labels": dict(labels)},
                    "spec": {
                        "serviceAccountName": "code-graph-helper-sa",
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "pytest",
                                "image": pytest_image,
                                "imagePullPolicy": "Always",
                                "command": ["pytest"],
                                "args": pytest_args,
                                "resources": {
                                    "requests": {"memory": "512Mi", "cpu": "500m"},
                                    "limits": {"memory": "1Gi", "cpu": "1000m"},
                                },
                                "env": [{"name": "PYTHONUNBUFFERED", "value": "1"}],
                            }
                        ],
                    },
                },
            },
        }

    async def job_exists(self, job_name: str) -> bool:
        """
        Check if a job exists in the namespace.

        Args:
            job_name: Name of the job

        Returns:
            True if job exists, False otherwise
        """
        try:
            self.batch_v1.read_namespaced_job(name=job_name, namespace=self.namespace)
            return True
        except ApiException as e:
            if e.status == 404:
                return False
            logger.error(f"Error checking job existence: {e}")
            raise

    async def get_completion_time(self, job_name: str) -> str | None:
        """
        Get the completion time of a job.

        Args:
            job_name: Name of the job

        Returns:
            Completion time ISO string or None if not completed
        """
        try:
            job = self.batch_v1.read_namespaced_job(name=job_name, namespace=self.namespace)
            if job.status.completion_time:
                return job.status.completion_time.isoformat()
            return None
        except ApiException as e:
            logger.error(f"Failed to get completion time: {e}")
            raise

    async def list_all_jobs(self, label_selector: str | None = None) -> list[dict[str, Any]]:
        """
        List all jobs in the namespace with optional label filtering.

        Args:
            label_selector: Optional Kubernetes label selector string

        Returns:
            List of job summary dictionaries
        """
        try:
            jobs = self.batch_v1.list_namespaced_job(
                namespace=self.namespace, label_selector=label_selector
            )

            job_list = []
            for job in jobs.items:
                job_list.append(
                    {
                        "name": job.metadata.name,
                        "created": (
                            job.metadata.creation_timestamp.isoformat()
                            if job.metadata.creation_timestamp
                            else None
                        ),
                        "active": job.status.active or 0,
                        "succeeded": job.status.succeeded or 0,
                        "failed": job.status.failed or 0,
                        "completion_time": (
                            job.status.completion_time.isoformat()
                            if job.status.completion_time
                            else None
                        ),
                    }
                )

            logger.info(f"Listed {len(job_list)} jobs in namespace {self.namespace}")
            return job_list

        except ApiException as e:
            logger.error(f"Failed to list jobs: {e}")
            raise

    async def cleanup_old_jobs(self, max_age_seconds: int = 86400) -> int:
        """
        Clean up completed jobs older than max_age_seconds.

        Note: TTL controller should handle this automatically,
        but this provides a manual cleanup option.

        Args:
            max_age_seconds: Delete jobs completed more than this many seconds ago

        Returns:
            Number of jobs deleted
        """
        try:
            jobs = self.batch_v1.list_namespaced_job(namespace=self.namespace)

            deleted_count = 0
            current_time = datetime.now(UTC)

            for job in jobs.items:
                # Check if job is completed
                if job.status.completion_time:
                    completion_time = job.status.completion_time
                    age_seconds = (current_time - completion_time).total_seconds()

                    if age_seconds > max_age_seconds:
                        logger.info(
                            f"Cleaning up old job {job.metadata.name} (age: {age_seconds}s)"
                        )
                        await self._delete_by_name(job.metadata.name)
                        deleted_count += 1

            logger.info(f"Cleaned up {deleted_count} old jobs")
            return deleted_count

        except ApiException as e:
            logger.error(f"Failed to cleanup old jobs: {e}")
            raise


# Singleton instance
_job_manager_instance: KubernetesJobManager | None = None


def get_job_manager(namespace: str = "code-graph-helper") -> KubernetesJobManager:
    """
    Get or create a singleton KubernetesJobManager instance.

    Args:
        namespace: Kubernetes namespace for job execution

    Returns:
        KubernetesJobManager instance
    """
    global _job_manager_instance
    if _job_manager_instance is None:
        _job_manager_instance = KubernetesJobManager(namespace=namespace)
    return _job_manager_instance
