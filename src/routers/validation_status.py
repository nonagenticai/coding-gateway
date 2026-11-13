"""
Validation Status API Endpoints

Handles validation status updates and tracking for the hybrid PR validation system.
These endpoints are called by GitHub Actions workflows to report status and results.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from coding_client.models.responses import not_found
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process PR Validation Orchestrator
#
# The monolith uses PRValidationOrchestrator from
# app.services.pr_validation.pr_validation_orchestrator which depends on
# GitHubActionsTriggerService and PRGitHubValidationService -- neither of
# which exist in the gateway.  The validation_status endpoints only need
# the in-memory tracking part (get/update), so we provide a minimal
# implementation here.
# ---------------------------------------------------------------------------


class PRValidationOrchestrator:
    """Minimal orchestrator for in-memory validation status tracking.

    Only the read/write parts needed by the status endpoints are
    implemented.  The full two-phase validation (fast + GitHub Actions)
    lives in the monolith.
    """

    MAX_VALIDATIONS = 1000

    def __init__(self) -> None:
        self.validations: dict[str, dict[str, Any]] = {}

    def _prune_expired(self) -> None:
        """Remove validations older than 24 hours."""
        now = time.time()
        expired = [
            vid for vid, v in self.validations.items() if now - v.get("_created_at", 0) > 86400
        ]
        for vid in expired:
            del self.validations[vid]

    def get_validation_status(self, validation_id: str) -> dict[str, Any] | None:
        """Get the current status of a validation."""
        return self.validations.get(validation_id)

    async def update_comprehensive_results(
        self, validation_id: str, results: dict[str, Any]
    ) -> bool:
        """Update validation with comprehensive results from GitHub Actions."""
        if validation_id not in self.validations:
            logger.warning("Validation ID %s not found for results update", validation_id)
            return False

        try:
            self.validations[validation_id]["comprehensive_validation"] = {
                "status": "completed",
                "results": results,
            }
            logger.info("[%s] Comprehensive validation results updated", validation_id)
            return True
        except Exception as e:
            logger.error("[%s] Failed to update comprehensive results: %s", validation_id, e)
            return False

    def register_validation(self, validation_id: str, initial_data: dict[str, Any]) -> None:
        """Register a new validation entry for tracking."""
        self._prune_expired()
        if len(self.validations) >= self.MAX_VALIDATIONS:
            # Remove oldest
            oldest = min(self.validations, key=lambda k: self.validations[k].get("_created_at", 0))
            del self.validations[oldest]
        initial_data["_created_at"] = time.time()
        self.validations[validation_id] = initial_data


_orchestrator: PRValidationOrchestrator | None = None


def get_pr_validation_orchestrator() -> PRValidationOrchestrator:
    """Get or create the PR validation orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = PRValidationOrchestrator()
    return _orchestrator


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ValidationStatusUpdate(BaseModel):
    """Model for validation status updates from GitHub Actions."""

    validation_id: str = Field(..., description="Validation tracking ID")
    status: str = Field(
        ...,
        description="Validation status: in_progress, completed, failed",
    )
    stage: str | None = Field(None, description="Current validation stage")
    message: str | None = Field(None, description="Status message")


class ValidationCompletionRequest(BaseModel):
    """Model for validation completion with results."""

    validation_id: str = Field(..., description="Validation tracking ID")
    status: str = Field(..., description="Final status: completed or failed")
    results: dict[str, Any] | None = Field(None, description="Comprehensive validation results")
    error: str | None = Field(None, description="Error message if failed")


class ValidationStatusResponse(BaseModel):
    """Response model for validation status queries."""

    validation_id: str
    status: str
    fast_validation: dict[str, Any] | None = None
    comprehensive_validation: dict[str, Any] | None = None


class ValidationStatusUpdateResponse(BaseModel):
    """Response from status update."""

    status: str = "success"
    validation_id: str = ""
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ValidationCompletionResponse(BaseModel):
    """Response from completion request."""

    status: str = "success"
    validation_id: str = ""
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/status",
    status_code=status.HTTP_200_OK,
    response_model=ValidationStatusUpdateResponse,
    summary="Update validation status",
    description="Called by GitHub Actions to update validation progress.",
    operation_id="update_validation_status",
)
async def update_validation_status(
    request: ValidationStatusUpdate,
    tenant_id: str = Depends(require_tenant),
) -> ValidationStatusUpdateResponse:
    """Update the status of an ongoing validation.

    This endpoint is called by GitHub Actions workflows to report progress.
    """
    try:
        orchestrator = get_pr_validation_orchestrator()

        validation = orchestrator.get_validation_status(request.validation_id)
        if not validation:
            raise HTTPException(
                status_code=404,
                detail=f"Validation ID '{request.validation_id}' not found",
            )

        comp = validation.get("comprehensive_validation")
        if comp is not None:
            comp["status"] = request.status
            if request.stage:
                comp["stage"] = request.stage
            if request.message:
                comp["message"] = request.message

        logger.info(
            "[%s] Status updated: %s (stage: %s)",
            request.validation_id,
            request.status,
            request.stage,
        )

        return ValidationStatusUpdateResponse(
            status="success",
            validation_id=request.validation_id,
            message="Status updated successfully",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to update validation status: %s", e)
        raise HTTPException(status_code=500, detail="Failed to update status") from e


@router.post(
    "/complete",
    status_code=status.HTTP_200_OK,
    response_model=ValidationCompletionResponse,
    summary="Complete validation",
    description="Called by GitHub Actions when comprehensive validation completes.",
    operation_id="complete_validation",
)
async def complete_validation(
    request: ValidationCompletionRequest,
    tenant_id: str = Depends(require_tenant),
) -> ValidationCompletionResponse:
    """Mark validation as complete and store results.

    This endpoint is called by GitHub Actions workflows when validation finishes.
    """
    try:
        orchestrator = get_pr_validation_orchestrator()

        validation = orchestrator.get_validation_status(request.validation_id)
        if not validation:
            raise HTTPException(
                status_code=404,
                detail=f"Validation ID '{request.validation_id}' not found",
            )

        if request.status == "completed" and request.results:
            success = await orchestrator.update_comprehensive_results(
                request.validation_id, request.results
            )

            if not success:
                raise HTTPException(status_code=500, detail="Failed to store validation results")

            logger.info(
                "[%s] Comprehensive validation completed successfully",
                request.validation_id,
            )

            return ValidationCompletionResponse(
                status="success",
                validation_id=request.validation_id,
                message="Validation completed and results stored",
            )

        elif request.status == "failed":
            comp = validation.get("comprehensive_validation")
            if comp is None:
                validation["comprehensive_validation"] = comp = {}
            comp["status"] = "failed"
            comp["error"] = request.error or "Unknown error"

            logger.warning(
                "[%s] Comprehensive validation failed: %s",
                request.validation_id,
                request.error,
            )

            return ValidationCompletionResponse(
                status="success",
                validation_id=request.validation_id,
                message="Validation failure recorded",
            )

        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid completion request: missing results or error",
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to complete validation: %s", e)
        raise HTTPException(status_code=500, detail="Failed to complete validation") from e


@router.get(
    "/{validation_id}",
    response_model=ValidationStatusResponse,
    summary="Get validation status",
    description="Retrieve the current status of a validation.",
    operation_id="get_validation_status",
)
async def get_validation_status(
    validation_id: str,
    tenant_id: str = Depends(require_tenant),
) -> ValidationStatusResponse:
    """Get the current status and results of a validation.

    Args:
        validation_id: Validation tracking ID

    Returns:
        ValidationStatusResponse with current status and results
    """
    try:
        orchestrator = get_pr_validation_orchestrator()

        validation = orchestrator.get_validation_status(validation_id)
        if not validation:
            raise not_found("Validation", validation_id)

        fast_status = validation.get("fast_validation", {})
        comp_status = validation.get("comprehensive_validation", {})

        if comp_status.get("status") == "completed":
            overall_status = "completed"
        elif comp_status.get("status") == "triggered":
            overall_status = "in_progress"
        elif comp_status.get("status") == "failed":
            overall_status = "failed"
        elif fast_status.get("passed"):
            overall_status = "fast_validation_passed"
        else:
            overall_status = "fast_validation_failed"

        return ValidationStatusResponse(
            validation_id=validation_id,
            status=overall_status,
            fast_validation=validation.get("fast_validation"),
            comprehensive_validation=validation.get("comprehensive_validation"),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to get validation status: %s", e)
        raise HTTPException(status_code=500, detail="Failed to retrieve status") from e
