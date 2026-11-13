"""
Kubernetes Job Management API Endpoints

Provides endpoints for creating, monitoring, and managing pytest jobs
in the Kubernetes cluster.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from coding_client.models.responses import internal_error, not_found
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant

logger = logging.getLogger(__name__)

# Initialize router
router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


class TestConfig(BaseModel):
    """Configuration for pytest job execution."""

    pytest_args: list[str] = Field(
        default_factory=lambda: [], description="Arguments to pass to pytest command"
    )
    timeout_seconds: int = Field(
        default=3600, description="Maximum execution time in seconds", ge=60, le=3600
    )
    description: str = Field(default="", description="Human-readable description of the test job")


class JobResponse(BaseModel):
    """Response model for job creation."""

    job_name: str = Field(..., description="Name of the created Kubernetes job")
    namespace: str = Field(..., description="Kubernetes namespace")
    status: str = Field(default="created", description="Initial job status")


class JobStatusResponse(BaseModel):
    """Response model for job status."""

    job_name: str = Field(..., description="Name of the job")
    namespace: str = Field(..., description="Kubernetes namespace")
    status: str = Field(..., description="Current job status")
    active: int = Field(default=0, description="Number of active pods")
    succeeded: int = Field(default=0, description="Number of succeeded pods")
    failed: int = Field(default=0, description="Number of failed pods")
    completion_time: str | None = Field(
        default=None, description="Job completion time (ISO format)"
    )
    start_time: str | None = Field(default=None, description="Job start time (ISO format)")
    logs: str = Field(default="", description="Job pod logs")


class JobDeleteResponse(BaseModel):
    """Response model for job deletion."""

    job_name: str = Field(..., description="Name of the deleted job")
    status: str = Field(default="deleted", description="Deletion status")
    message: str = Field(default="", description="Deletion details")
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Global Job Manager Instance
# ---------------------------------------------------------------------------

_job_manager: Any | None = None


def get_job_manager() -> Any:
    """Get or initialize the Kubernetes job manager."""
    global _job_manager
    if _job_manager is None:
        try:
            from services.job_manager import KubernetesJobManager

            namespace = os.environ.get("KUBERNETES_NAMESPACE", "code-graph-helper")
            _job_manager = KubernetesJobManager(namespace=namespace)
            logger.info("Initialized KubernetesJobManager for namespace: %s", namespace)
        except Exception as e:
            logger.error("Failed to initialize job manager: %s", e)
            raise

    return _job_manager


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=JobResponse,
    summary="Create a new pytest job",
    description="Create a new Kubernetes job for pytest execution with specified arguments.",
    status_code=201,
    operation_id="create_job",
)
@router.post("/", include_in_schema=False, status_code=201, operation_id="create_job_slash")
async def create_job(
    test_config: TestConfig,
    tenant_id: str = Depends(require_tenant),
) -> JobResponse:
    """Create a new pytest job in the Kubernetes cluster.

    The job will run with the specified pytest arguments and be automatically
    cleaned up after ttlSecondsAfterFinished (default 1800 seconds / 30 minutes).
    """
    try:
        job_manager = get_job_manager()

        # Namespace-wide cap: it protects the cluster, so it counts every
        # tenant's jobs. The message deliberately omits the count, which would
        # tell one tenant how busy the others are.
        active_count = await job_manager.count_all_active_jobs()
        max_concurrent = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))

        if active_count >= max_concurrent:
            raise HTTPException(
                status_code=429,
                detail={
                    "status": "error",
                    "message": f"Maximum concurrent jobs limit ({max_concurrent}) reached.",
                    "error_code": "TOO_MANY_JOBS",
                    "details": {},
                },
            )

        job_name = await job_manager.create_pytest_job(
            test_config=test_config.model_dump(),
            timeout_seconds=test_config.timeout_seconds,
            tenant_id=tenant_id,
        )

        logger.info("Created pytest job: %s with args: %s", job_name, test_config.pytest_args)

        return JobResponse(job_name=job_name, namespace=job_manager.namespace, status="created")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to create job: %s", e)
        raise internal_error(f"Failed to create job: {str(e)}") from e


@router.get(
    "/health",
    operation_id="jobs_health_check",
    summary="Health check for job service",
    description="Check if the job management service is operational.",
)
async def health_check() -> dict[str, Any]:
    """Health check for the job management service.

    Verifies that the service can connect to the Kubernetes API.
    """
    try:
        job_manager = get_job_manager()
        active_count = await job_manager.count_all_active_jobs()

        return {
            "status": "healthy",
            "namespace": job_manager.namespace,
            "message": "Job management service is operational",
            "active_jobs": active_count,
        }

    except Exception as e:
        logger.error("Health check failed: %s", e)
        raise HTTPException(
            status_code=503,
            detail="Job management service is unavailable",
        ) from e


@router.get(
    "/{job_name}",
    response_model=JobStatusResponse,
    summary="Get job status and details",
    description="Retrieve the status, logs, and metadata for a specific job.",
    operation_id="get_job_status",
)
async def get_job_status(
    job_name: str = Path(..., description="Name of the job to query"),
    include_logs: bool = Query(default=True, description="Include job pod logs in response"),
    tenant_id: str = Depends(require_tenant),
) -> JobStatusResponse:
    """Get the status and details of a specific job."""
    try:
        job_manager = get_job_manager()

        # A job owned by another tenant is reported exactly like a missing one
        # (404, same body): a 403 would confirm the name exists.
        status = await job_manager.get_job_status(job_name, tenant_id=tenant_id)

        if not status:
            raise not_found("Job", job_name)

        logs = ""
        if include_logs:
            try:
                logs = await job_manager.get_job_logs(job_name, tenant_id=tenant_id)
            except Exception as log_error:
                logger.warning("Failed to fetch logs for %s: %s", job_name, log_error)
                logs = f"[Logs unavailable: {log_error!s}]"

        logger.info("Retrieved status for job: %s", job_name)

        return JobStatusResponse(
            job_name=job_name,
            namespace=job_manager.namespace,
            status=status.get("status", "unknown"),
            active=status.get("active", 0),
            succeeded=status.get("succeeded", 0),
            failed=status.get("failed", 0),
            completion_time=status.get("completion_time"),
            start_time=status.get("start_time"),
            logs=logs,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get job status for %s: %s", job_name, e)
        raise internal_error("Failed to retrieve job status") from e


@router.delete(
    "/{job_name}",
    response_model=JobDeleteResponse,
    summary="Delete a job",
    description="Manually delete a job and its associated pods.",
    operation_id="delete_job",
)
async def delete_job(
    job_name: str = Path(..., description="Name of the job to delete"),
    tenant_id: str = Depends(require_tenant),
) -> JobDeleteResponse:
    """Manually delete a job from the Kubernetes cluster."""
    try:
        job_manager = get_job_manager()

        # 404, never 403, for another tenant's job: no existence leak.
        # delete_job re-checks ownership itself; this route does not rely on
        # having looked first.
        deleted = await job_manager.delete_job(job_name, tenant_id=tenant_id)
        if not deleted:
            raise not_found("Job", job_name)

        logger.info("Deleted job: %s", job_name)

        return JobDeleteResponse(
            job_name=job_name,
            status="deleted",
            message=f"Job '{job_name}' successfully deleted from namespace '{job_manager.namespace}'",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete job %s: %s", job_name, e)
        raise internal_error("Failed to delete job") from e


@router.get(
    "",
    summary="List active jobs",
    description="Get a list of all active jobs in the namespace.",
    operation_id="list_jobs",
)
@router.get("/", include_in_schema=False, operation_id="list_jobs_slash")
async def list_active_jobs(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """List the calling tenant's currently active jobs."""
    try:
        job_manager = get_job_manager()

        active_jobs = await job_manager.get_active_jobs(tenant_id=tenant_id)
        max_concurrent = int(os.environ.get("MAX_CONCURRENT_JOBS", "3"))

        logger.info("Listed active jobs: %d jobs", len(active_jobs))

        return {
            "active_jobs": active_jobs,
            "count": len(active_jobs),
            "namespace": job_manager.namespace,
            "max_concurrent": max_concurrent,
        }

    except Exception as e:
        logger.error("Failed to list active jobs: %s", e)
        raise internal_error(f"Failed to list jobs: {str(e)}") from e
