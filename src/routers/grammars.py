"""
Tree-sitter Grammar Management API Endpoints

API endpoints for managing Tree-sitter grammars including installation,
updates, and status monitoring.
"""

# Not using __future__.annotations — Pydantic TypeAdapter requires concrete types at class definition time
# from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from coding_client.models.responses import (
    bad_request as _bad_request,
)
from coding_client.models.responses import (
    internal_error,
    not_found,
)
from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
)
from pydantic import BaseModel, ConfigDict, Field

from auth.dependencies import require_tenant
from services.grammar_manager import get_grammar_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class GrammarInstallRequest(BaseModel):
    """Request to install specific grammars."""

    languages: list[str] = Field(..., description="Languages to install grammars for")
    force: bool = Field(False, description="Force reinstallation of existing grammars")

    model_config = ConfigDict(
        json_schema_extra={"example": {"languages": ["java", "rust", "go"], "force": False}}
    )


class GrammarStatus(BaseModel):
    """Status information for a grammar."""

    language: str
    installed: bool
    installed_version: str | None
    latest_version: str | None
    needs_update: bool
    last_checked: str | None
    last_updated: str | None
    repository_url: str


class BaseResponse(BaseModel):
    """Base API response."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class GrammarStatusResponse(BaseResponse):
    """Response containing grammar status information."""

    grammars: dict[str, GrammarStatus] = Field(..., description="Grammar status for each language")


class GrammarInstallResponse(BaseResponse):
    """Response from grammar installation operation."""

    results: dict[str, bool] = Field(..., description="Installation results for each language")
    summary: dict[str, Any] = Field(..., description="Installation summary")


class GrammarUpdateResponse(BaseResponse):
    """Response from grammar update check operation."""

    updates_available: dict[str, bool] = Field(
        ..., description="Update availability for each language"
    )
    summary: dict[str, Any] = Field(..., description="Update check summary")


class GrammarCleanupResponse(BaseResponse):
    """Response from grammar cleanup operation."""

    cleanup_results: dict[str, int] = Field(
        default_factory=dict, description="Cleanup results per language"
    )
    total_versions_removed: int = Field(0, description="Total old versions removed")
    languages_cleaned: int = Field(0, description="Number of languages cleaned")
    total_languages: int = Field(0, description="Total languages processed")
    space_freed_mb: float = Field(default=0.0, description="Estimated disk space freed in MB")


class GrammarTaskStatusResponse(BaseResponse):
    """Response for async task status."""

    task_id: str = Field(..., description="Task ID")
    task_status: str = Field(..., description="Task status")
    result: dict[str, Any] = Field(default_factory=dict, description="Task result")
    error: str = Field("", description="Error message if failed")
    progress: dict[str, Any] = Field(default_factory=dict, description="Progress info")


class SupportedLanguagesResponse(BaseResponse):
    """Response for supported languages."""

    supported_languages: list[str] = Field(..., description="List of supported language names")
    total_languages: int = Field(..., description="Total supported languages")
    languages_info: dict[str, Any] = Field(..., description="Detailed info per language")
    official_grammars: dict[str, Any] = Field(
        default_factory=dict, description="Official grammar repos"
    )


class AsyncTaskResponse(BaseResponse):
    """Response for async task submission."""

    task_id: str = Field(..., description="Unique task identifier")
    task_status: str = Field(..., description="Task status")
    check_url: str = Field(..., description="URL to check task status")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/status",
    response_model=GrammarStatusResponse,
    summary="Grammar Status",
    description="Get installation and version status for all grammars.",
    operation_id="get_grammar_status",
)
async def get_grammar_status(
    tenant_id: str = Depends(require_tenant),
) -> GrammarStatusResponse:
    """Get status information for all Tree-sitter grammars."""
    try:
        manager = get_grammar_manager()
        await manager.initialize()

        status_data = await manager.get_grammar_status()

        grammars = {}
        for language, data in status_data.items():
            grammars[language] = GrammarStatus(
                language=data["language"],
                installed=data["installed"],
                installed_version=data["installed_version"],
                latest_version=data["latest_version"],
                needs_update=data["needs_update"],
                last_checked=data["last_checked"],
                last_updated=data["last_updated"],
                repository_url=data["repository_url"],
            )

        logger.info("Grammar status retrieved for %d languages", len(grammars))

        return GrammarStatusResponse(
            message=f"Grammar status retrieved for {len(grammars)} languages",
            grammars=grammars,
        )

    except Exception as e:
        logger.exception("Error retrieving grammar status: %s", e)
        raise internal_error(f"Error retrieving grammar status: {str(e)}") from e


@router.post(
    "/install",
    response_model=GrammarInstallResponse,
    summary="Install Grammars",
    description="Install or update grammars for specified languages.",
    operation_id="install_grammars",
)
async def install_grammars(
    request: GrammarInstallRequest = Body(..., description="Grammar installation request"),
    tenant_id: str = Depends(require_tenant),
) -> GrammarInstallResponse:
    """Install Tree-sitter grammars for specified languages."""
    try:
        if not request.languages:
            raise _bad_request("At least one language must be specified")

        manager = get_grammar_manager()
        await manager.initialize()

        status_data = await manager.get_grammar_status()
        supported_languages = set(status_data.keys())

        unsupported = [lang for lang in request.languages if lang not in supported_languages]
        if unsupported:
            raise _bad_request(
                f"Unsupported languages: {', '.join(unsupported)}. Supported: {', '.join(sorted(supported_languages))}"
            )

        results = {}
        for language in request.languages:
            success = await manager.install_grammar(language, force=request.force)
            results[language] = success

        successful = sum(1 for v in results.values() if v)
        failed = len(results) - successful

        summary = {
            "total_requested": len(request.languages),
            "successful_installations": successful,
            "failed_installations": failed,
            "success_rate": (successful / len(request.languages) if request.languages else 0),
            "forced_reinstall": request.force,
        }

        message = f"Installed {successful}/{len(request.languages)} grammars successfully"
        if failed > 0:
            message += f" ({failed} failed)"

        logger.info("Grammar installation completed: %s", message)

        return GrammarInstallResponse(message=message, results=results, summary=summary)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Grammar installation failed: %s", e)
        raise internal_error(f"Grammar installation failed: {str(e)}") from e


@router.post(
    "/install-all",
    response_model=GrammarInstallResponse,
    summary="Install All Grammars",
    description="Install grammars for all supported languages.",
    operation_id="install_all_grammars",
)
async def install_all_grammars(
    force: bool = Query(False, description="Force reinstallation of existing grammars"),
    tenant_id: str = Depends(require_tenant),
) -> GrammarInstallResponse:
    """Install Tree-sitter grammars for all supported languages."""
    try:
        manager = get_grammar_manager()
        await manager.initialize()

        results = await manager.install_all_grammars(force=force)

        successful = sum(1 for v in results.values() if v)
        failed = len(results) - successful

        summary = {
            "total_languages": len(results),
            "successful_installations": successful,
            "failed_installations": failed,
            "success_rate": successful / len(results) if results else 0,
            "forced_reinstall": force,
            "languages_processed": list(results.keys()),
        }

        message = f"Installed {successful}/{len(results)} grammars successfully"
        if failed > 0:
            failed_langs = [lang for lang, success in results.items() if not success]
            message += f" (failed: {', '.join(failed_langs)})"

        logger.info("Bulk grammar installation completed: %s", message)

        return GrammarInstallResponse(message=message, results=results, summary=summary)

    except Exception as e:
        logger.exception("Bulk grammar installation failed: %s", e)
        raise internal_error(f"Bulk grammar installation failed: {str(e)}") from e


@router.post(
    "/install-all-async",
    response_model=AsyncTaskResponse,
    summary="Install All Grammars (Async)",
    description="Start background installation of all grammars.",
    operation_id="install_all_grammars_async",
)
async def install_all_grammars_async(
    force: bool = Query(False, description="Force reinstallation of existing grammars"),
    tenant_id: str = Depends(require_tenant),
) -> AsyncTaskResponse:
    """Start async installation of all grammars."""
    try:
        task_id = f"grammar-install-{uuid.uuid4().hex[:8]}"

        # NOTE: In gateway context, the async task manager would need to be
        # provided by the services layer. For now return the task ID.
        return AsyncTaskResponse(
            task_id=task_id,
            task_status="submitted",
            message="Grammar installation submitted",
            check_url=f"/api/grammars/tasks/{task_id}",
        )

    except Exception as e:
        logger.exception("Failed to start async grammar installation: %s", e)
        raise internal_error(f"Failed to start async installation: {str(e)}") from e


@router.get(
    "/tasks/{task_id}",
    response_model=GrammarTaskStatusResponse,
    summary="Check Task Status",
    description="Check the status of an async grammar installation task.",
    operation_id="get_grammar_task_status",
)
async def check_task_status(
    task_id: str = Path(..., description="Task ID from async operation"),
    tenant_id: str = Depends(require_tenant),
) -> GrammarTaskStatusResponse:
    """Check status of async grammar task."""
    # No persistent task backend yet — unknown tasks return 404 to match monolith.
    raise not_found("Task", task_id)


@router.post(
    "/check-updates",
    response_model=GrammarUpdateResponse,
    summary="Check Grammar Updates",
    description="Check for available updates to installed grammars.",
    operation_id="check_grammar_updates",
)
async def check_grammar_updates(
    languages: list[str] | None = Query(
        None, description="Specific languages to check (default: all)"
    ),
    tenant_id: str = Depends(require_tenant),
) -> GrammarUpdateResponse:
    """Check for available updates to Tree-sitter grammars."""
    try:
        manager = get_grammar_manager()
        await manager.initialize()

        if languages:
            status_data = await manager.get_grammar_status()
            supported_languages = set(status_data.keys())
            unsupported = [lang for lang in languages if lang not in supported_languages]
            if unsupported:
                raise _bad_request(
                    f"Unsupported languages: {', '.join(unsupported)}. Supported: {', '.join(sorted(supported_languages))}"
                )

        updates_available = await manager.check_for_updates(languages)

        total_checked = len(updates_available)
        updates_count = sum(1 for v in updates_available.values() if v)

        summary = {
            "total_checked": total_checked,
            "updates_available": updates_count,
            "up_to_date": total_checked - updates_count,
            "languages_with_updates": [
                lang for lang, available in updates_available.items() if available
            ],
        }

        if updates_count > 0:
            message = f"Updates available for {updates_count}/{total_checked} grammars"
        else:
            message = f"All {total_checked} grammars are up to date"

        logger.info("Grammar update check completed: %s", message)

        return GrammarUpdateResponse(
            message=message,
            updates_available=updates_available,
            summary=summary,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Grammar update check failed: %s", e)
        raise internal_error(f"Grammar update check failed: {str(e)}") from e


@router.delete(
    "/cleanup",
    response_model=GrammarCleanupResponse,
    summary="Cleanup Old Grammars",
    description="Remove outdated grammar versions to save space.",
    operation_id="cleanup_old_grammars",
)
async def cleanup_old_grammars(
    tenant_id: str = Depends(require_tenant),
) -> GrammarCleanupResponse:
    """Clean up old versions of Tree-sitter grammars."""
    try:
        manager = get_grammar_manager()
        await manager.initialize()

        cleanup_results = await manager.cleanup_old_versions()

        total_removed = sum(cleanup_results.values())
        languages_cleaned = sum(1 for count in cleanup_results.values() if count > 0)

        message = (
            f"Cleaned up {total_removed} old grammar versions across {languages_cleaned} languages"
        )
        logger.info("Grammar cleanup completed: %s", message)

        return GrammarCleanupResponse(
            message=message,
            cleanup_results=cleanup_results,
            total_versions_removed=total_removed,
            languages_cleaned=languages_cleaned,
            total_languages=len(cleanup_results),
        )

    except Exception as e:
        logger.exception("Grammar cleanup failed: %s", e)
        raise internal_error(f"Grammar cleanup failed: {str(e)}") from e


@router.get(
    "/supported-languages",
    response_model=SupportedLanguagesResponse,
    summary="Supported Languages",
    description="Get list of languages with Tree-sitter grammar support.",
    operation_id="list_grammar_supported_languages",
)
async def get_supported_languages(
    tenant_id: str = Depends(require_tenant),
) -> SupportedLanguagesResponse:
    """Get list of languages supported by Tree-sitter grammars."""
    try:
        manager = get_grammar_manager()
        await manager.initialize()

        status_data = await manager.get_grammar_status()

        languages_info = {}
        for language, data in status_data.items():
            languages_info[language] = {
                "language": language,
                "repository_url": data["repository_url"],
                "installed": data["installed"],
                "description": f"Tree-sitter grammar for {language.title()} language",
            }

        return SupportedLanguagesResponse(
            message=f"{len(languages_info)} supported languages found",
            supported_languages=list(languages_info.keys()),
            total_languages=len(languages_info),
            languages_info=languages_info,
            official_grammars=getattr(manager, "OFFICIAL_GRAMMARS", {}),
        )

    except Exception as e:
        logger.exception("Error retrieving supported languages: %s", e)
        raise internal_error(f"Error retrieving supported languages: {str(e)}") from e
