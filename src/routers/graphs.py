"""Code Graph Operations API router.

Provides endpoints for graph lifecycle management, visualization/querying,
analysis (AI-augmented and runtime), architecture analysis, and internal
data operations.  Ported from the monolith's five sub-routers into a single
flat router using the gateway's service layer.
"""

from __future__ import annotations

import logging
import re as _re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from coding_client.models.responses import (
    internal_error,
    not_found,
)
from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant
from clients.outbound_identity import identity_headers, service_tenant
from config.settings import Settings, get_settings
from routers.proxy import get_proxy_client

logger = logging.getLogger(__name__)

router = APIRouter()

_SAFE_CONTAINER_NAME = _re.compile(r"^[a-zA-Z0-9_\-\.]+$")


def _validate_container_name(name: str) -> str:
    """Validate container_name to prevent path traversal."""
    if not _SAFE_CONTAINER_NAME.fullmatch(name) or ".." in name:
        raise HTTPException(status_code=400, detail="Invalid container name")
    return name


# ---------------------------------------------------------------------------
# Container-service + coding-analysis helpers
# ---------------------------------------------------------------------------


async def _list_active_container_names(
    request: Request,
    tenant_id: str,
    settings: Settings | None = None,
) -> set[str]:
    """Fetch active container names from the containers service for the current tenant."""
    if settings is None:
        settings = get_settings()

    client = await get_proxy_client()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "connection"}
    }
    headers.setdefault("x-tenant-id", tenant_id)

    response = await client.get(
        f"{settings.containers_url}/api/workspaces/",
        headers=headers,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        containers = payload
    elif isinstance(payload, dict):
        containers = payload.get("containers", [])
    else:
        containers = []

    active_names: set[str] = set()
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_name = container.get("container_name")
        if isinstance(container_name, str) and container_name:
            active_names.add(container_name)

    return active_names


# ---------------------------------------------------------------------------
# Lightweight response / request models (gateway-local)
# ---------------------------------------------------------------------------


class InitResponse(BaseModel):
    """Response returned after code graph initialization."""

    message: str = ""
    container_name: str = ""
    graph_db_path: str = ""
    nodes_created: int = 0
    edges_created: int = 0
    status: str = "success"
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class CodebaseInitRequest(BaseModel):
    """Request body for code graph initialization."""

    container_name: str
    codebase_path: str | None = None
    include_tests: bool = False
    exclude_patterns: list[str] | None = None
    include_patterns: list[str] | None = None
    force_rebuild: bool = False


class GraphInfoItem(BaseModel):
    """Summary metadata for a single code graph."""

    container_name: str = ""
    name: str = ""
    description: str = ""
    created_at: str = ""
    updated_at: str = ""
    node_count: int = -1
    edge_count: int = -1
    language: str = "unknown"
    tags: list[str] = Field(default_factory=list)
    status: str = "unknown"
    repository_url: str | None = None


class GraphListResponse(BaseModel):
    """Paginated list of code graphs."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    graphs: list[GraphInfoItem] = Field(default_factory=list)
    total: int = 0


class GraphDetailResponse(BaseModel):
    """Detailed information about a single code graph."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    graph: GraphInfoItem | None = None


class GraphDeletionResponse(BaseModel):
    """Response returned after deleting a code graph."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    container_name: str = ""
    deleted_nodes: int = 0
    deleted_edges: int = 0
    cleanup_performed: bool = False


class GraphNodeItem(BaseModel):
    """A single node in a code graph query result."""

    id: str
    type: str
    name: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] | None = None


class NodesResponse(BaseModel):
    """Paginated response containing graph nodes."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    pagination: dict[str, Any] = Field(default_factory=dict)
    items: list[GraphNodeItem] = Field(default_factory=list)


class GraphEdgeItem(BaseModel):
    """A single edge in a code graph query result."""

    id: str
    source: str
    target: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class EdgesResponse(BaseModel):
    """Paginated response containing graph edges."""

    status: str = "success"
    message: str = ""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    pagination: dict[str, Any] = Field(default_factory=dict)
    items: list[GraphEdgeItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SQLITE_INTEGER_MAX = 9223372036854775807


def _validate_sqlite_pagination(page: int, limit: int) -> None:
    offset = (page - 1) * limit
    if offset > SQLITE_INTEGER_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"Pagination offset too large: page={page}, limit={limit}.",
        )
    if page > 1_000_000:
        raise HTTPException(status_code=400, detail=f"Page number too large: {page}.")


async def _analysis_get(
    path: str,
    tenant_id: str,
    settings: Settings | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    """GET ``path`` on coding-analysis, forwarding the tenant header.

    The reader for every ``/api/graphs/{c}/*`` route now lives in
    coding-analysis (see ``graphs_reader.py`` there) so the writer and
    reader share a pod and no longer need a shared volume. This helper
    keeps the HTTP plumbing in one place — callers just pass the path
    they want under ``/api/analysis/graphs``.
    """
    if settings is None:
        settings = get_settings()
    client = await get_proxy_client()
    url = f"{settings.analysis_url.rstrip('/')}{path}"
    headers = identity_headers(tenant_id)
    return await client.get(url, headers=headers, params=params or None)


async def _get_graph_counts(
    container_name: str,
    settings: Settings | None = None,
    tenant_id: str | None = None,
) -> tuple[int, int]:
    """Return (node_count, edge_count) for *container_name*.

    Asks coding-analysis over HTTP instead of opening sqlite locally. A
    404 from coding-analysis means "no graph yet" and we return
    (-1, -1) so callers can distinguish missing from empty. Any other
    error logs and also returns (-1, -1) — this helper is only used to
    annotate metadata responses, never as the primary error path.
    """
    try:
        response = await _analysis_get(
            f"/api/analysis/graphs/{container_name}",
            tenant_id or service_tenant(),
            settings,
        )
        if response.status_code == 404:
            return -1, -1
        response.raise_for_status()
        body = response.json()
        return int(body.get("node_count", 0)), int(body.get("edge_count", 0))
    except Exception as exc:
        logger.error("Failed to get counts for %s: %s", container_name, exc)
        return -1, -1


async def _analysis_post(
    path: str,
    tenant_id: str,
    settings: Settings | None = None,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    """POST ``path`` on coding-analysis, forwarding the tenant header."""
    if settings is None:
        settings = get_settings()
    client = await get_proxy_client()
    url = f"{settings.analysis_url.rstrip('/')}{path}"
    headers = identity_headers(tenant_id)
    return await client.post(url, headers=headers, json=json or {})


async def _analysis_delete(
    path: str,
    tenant_id: str,
    settings: Settings | None = None,
) -> httpx.Response:
    """DELETE ``path`` on coding-analysis, forwarding the tenant header."""
    if settings is None:
        settings = get_settings()
    client = await get_proxy_client()
    url = f"{settings.analysis_url.rstrip('/')}{path}"
    headers = identity_headers(tenant_id)
    return await client.delete(url, headers=headers)


async def _list_graphs_from_analysis(
    tenant_id: str,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    """Return the graph summaries coding-analysis knows about.

    Each item carries ``container_name``, ``node_count``, ``edge_count`` and a
    representative ``tenant_id`` (see ``graphs_reader.list_graphs`` there).
    """
    response = await _analysis_get("/api/analysis/graphs", tenant_id, settings)
    response.raise_for_status()
    body = response.json()
    graphs = body.get("graphs", []) if isinstance(body, dict) else []
    return [g for g in graphs if isinstance(g, dict)]


# ---------------------------------------------------------------------------
# Graph management endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/initialize",
    response_model=InitResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initialize Code Graph",
    description="Initialize code analysis and graph generation for a codebase.",
    operation_id="initialize_graph",
)
async def initialize_code_graph(
    request: CodebaseInitRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
) -> InitResponse:
    """Initialize comprehensive code graph analysis for a codebase."""
    _validate_container_name(request.container_name)
    try:
        logger.info("Initializing code graph for container: %s", request.container_name)
        # coding-analysis owns the graph store; it creates the minimal
        # repository node and enforces the "already exists" guard.
        payload = {
            "codebase_path": request.codebase_path,
            "language": "python",
            "force_rebuild": request.force_rebuild,
        }
        response = await _analysis_post(
            f"/api/analysis/graphs/{request.container_name}", tenant_id, json=payload
        )
        if response.status_code == 409:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Code graph for container '{request.container_name}' already exists. "
                    "Use force_rebuild=true or delete the existing graph first."
                ),
            )
        response.raise_for_status()
        body = response.json()

        logger.info("Code graph initialized for container: %s", request.container_name)

        return InitResponse(
            message=f"Code graph for container '{request.container_name}' initialized successfully.",
            container_name=request.container_name,
            graph_db_path=str(body.get("repo_node_id", "")),
            nodes_created=1 if body.get("created") else 0,
            edges_created=0,
            status="success",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Failed to initialize code graph for %s: %s", request.container_name, e, exc_info=True
        )
        raise internal_error(f"Failed to initialize code graph: {str(e)}") from e


@router.delete(
    "/{container_name}",
    response_model=GraphDeletionResponse,
    summary="Delete a graph",
    description="Delete a graph and all associated data for a container.",
    operation_id="delete_graph",
)
async def delete_graph(
    container_name: str = Path(..., description="Container name for the graph to delete"),
    force: bool = Query(False, description="Force deletion"),
    tenant_id: str = Depends(require_tenant),
) -> GraphDeletionResponse:
    """Delete a graph and all associated data for a container."""
    _validate_container_name(container_name)
    logger.info("DELETE request for container graph: %s (force=%s)", container_name, force)
    try:
        response = await _analysis_delete(f"/api/analysis/graphs/{container_name}", tenant_id)
        if response.status_code == 404:
            raise not_found("Graph", container_name)
        response.raise_for_status()
        body = response.json()

        return GraphDeletionResponse(
            message=f"Graph '{container_name}' deleted successfully",
            container_name=container_name,
            deleted_nodes=int(body.get("nodes_deleted", 0)),
            deleted_edges=int(body.get("edges_deleted", 0)),
            cleanup_performed=True,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error deleting graph for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to delete graph: {str(e)}") from e


@router.delete(
    "/cleanup/orphaned",
    summary="Clean up orphaned graphs",
    description="Identify and optionally remove graph databases that no longer have containers.",
    include_in_schema=False,
    operation_id="cleanup_orphaned_graphs",
)
async def cleanup_orphaned_graphs(
    request: Request,
    dry_run: bool = Query(True, description="Only scan without deleting"),
    confirm: bool = Query(False, description="Confirm deletion"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Clean up orphaned graph databases."""
    try:
        settings = get_settings()
        active_container_names = await _list_active_container_names(request, tenant_id, settings)

        # Identify graphs (from coding-analysis) with no matching live container.
        # Mirrors the old local-sqlite scan: only this tenant's graphs are in
        # scope, and tenantless graphs are skipped when auth is enforced.
        orphaned_containers: list[str] = []
        for graph in await _list_graphs_from_analysis(tenant_id, settings):
            container_name = graph.get("container_name")
            if not isinstance(container_name, str) or not container_name:
                continue
            graph_tenant_id = graph.get("tenant_id")
            if graph_tenant_id is not None and graph_tenant_id != tenant_id:
                continue
            if graph_tenant_id is None and settings.enable_auth:
                logger.warning("Skipping tenantless graph during orphan scan: %s", container_name)
                continue
            if container_name not in active_container_names:
                orphaned_containers.append(container_name)

        result: dict[str, Any] = {
            "orphaned_containers": orphaned_containers,
            "cleaned_count": 0,
            "message": f"Found {len(orphaned_containers)} orphaned graph(s)",
            "dry_run": dry_run,
            "confirm": confirm,
        }

        if dry_run or not confirm:
            if not dry_run and not confirm:
                result["message"] = (
                    "Deletion not performed. Re-run with dry_run=false and confirm=true."
                )
            return result

        cleaned_count = 0
        deletion_errors: list[dict[str, str]] = []
        for container_name in orphaned_containers:
            try:
                deletion_result = await delete_graph(
                    container_name, force=True, tenant_id=tenant_id
                )
                if deletion_result.cleanup_performed:
                    cleaned_count += 1
                else:
                    deletion_errors.append(
                        {"container_name": container_name, "error": "cleanup_not_performed"}
                    )
            except HTTPException as exc:
                deletion_errors.append({"container_name": container_name, "error": str(exc.detail)})
            except Exception as exc:
                deletion_errors.append({"container_name": container_name, "error": str(exc)})

        result["cleaned_count"] = cleaned_count
        result["deletion_errors"] = deletion_errors
        result["message"] = f"Cleaned {cleaned_count} orphaned graph(s)"
        return result
    except httpx.HTTPError as e:
        logger.error(
            "Unable to verify active containers during orphan cleanup: %s", e, exc_info=True
        )
        raise internal_error("Failed to verify active containers before cleanup") from e
    except Exception as e:
        logger.error("Error during orphaned graph cleanup: %s", e, exc_info=True)
        raise internal_error(f"Failed to perform cleanup: {str(e)}") from e


# ---------------------------------------------------------------------------
# Graph visualization/query endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=GraphListResponse,
    summary="List All Graphs",
    description="Get a list of all available code graphs.",
    operation_id="list_graphs",
)
@router.get(
    "/",
    response_model=GraphListResponse,
    include_in_schema=False,
    operation_id="list_graphs_slash",
)
async def list_graphs(
    include_stats: bool = Query(True, description="Include node/edge counts"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum graphs to return"),
    tenant_id: str = Depends(require_tenant),
) -> GraphListResponse:
    """List all available code graphs with their metadata."""
    try:
        settings = get_settings()
        all_graphs = await _list_graphs_from_analysis(tenant_id, settings)

        response_graphs: list[GraphInfoItem] = []
        for graph in all_graphs[:limit]:
            container_name = str(graph.get("container_name", ""))
            if include_stats:
                node_count = int(graph.get("node_count", 0))
                edge_count = int(graph.get("edge_count", 0))
            else:
                node_count, edge_count = (-1, -1)

            response_graphs.append(
                GraphInfoItem(
                    container_name=container_name,
                    name=container_name,
                    node_count=node_count,
                    edge_count=edge_count,
                )
            )

        return GraphListResponse(
            message="Graphs listed successfully.",
            graphs=response_graphs,
            total=len(all_graphs),
        )
    except Exception as e:
        logger.error("Error listing graphs: %s", e, exc_info=True)
        raise internal_error(f"Failed to list graphs: {str(e)}") from e


@router.get(
    "/initialize-disabled-get",
    include_in_schema=False,
    operation_id="initialize_graph_disabled_get",
)
async def initialize_code_graph_get() -> None:
    """Block GET requests to the initialization endpoint."""
    raise HTTPException(
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        detail={
            "status": "error",
            "message": "Method GET not allowed for graph initialization. Use POST method instead.",
            "error_code": "METHOD_NOT_ALLOWED",
            "details": {},
        },
    )


@router.get(
    "/{container_name}",
    response_model=GraphDetailResponse,
    summary="Get Graph Details",
    description="Get detailed information about a specific code graph.",
    operation_id="get_graph",
)
async def get_graph_details(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> GraphDetailResponse:
    """Get detailed information about a specific code graph.

    Proxies to coding-analysis ``/api/analysis/graphs/{c}`` — that's the
    pod that actually writes the graph SQLite, and reading from there
    closes the "writer pod ≠ reader pod" gap that used to return 404.
    """
    _validate_container_name(container_name)
    try:
        response = await _analysis_get(f"/api/analysis/graphs/{container_name}", tenant_id)
        if response.status_code == 404:
            raise not_found("Graph", container_name)
        response.raise_for_status()
        body = response.json()

        graph_info = GraphInfoItem(
            container_name=container_name,
            name=container_name,
            node_count=int(body.get("node_count", 0)),
            edge_count=int(body.get("edge_count", 0)),
        )

        return GraphDetailResponse(
            message=f"Details for graph '{container_name}' retrieved.",
            graph=graph_info,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting graph details for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to get graph details: {str(e)}") from e


@router.get(
    "/{container_name}/nodes",
    response_model=NodesResponse,
    summary="Get Graph Nodes",
    description="Query nodes in a specific code graph with filtering and pagination.",
    operation_id="list_graph_nodes",
)
async def get_graph_nodes(
    container_name: str = Path(..., description="Container name"),
    node_type: str | None = Query(None, description="Filter by node type"),
    name_pattern: str | None = Query(None, description="Filter by name pattern"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=1000, description="Items per page"),
    tenant_id: str = Depends(require_tenant),
) -> NodesResponse:
    """Query nodes in a code graph with filtering and pagination.

    Forwards to coding-analysis' reader. Converts this router's
    ``page``/``limit`` pagination into coding-analysis' ``limit``/
    ``offset`` form and keeps the public wire shape intact (``items``,
    ``pagination``). ``name_pattern`` is a case-insensitive substring
    match against ``properties.name`` upstream.
    """
    _validate_container_name(container_name)
    try:
        _validate_sqlite_pagination(page, limit)
        offset = (page - 1) * limit
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if node_type:
            params["node_type"] = node_type
        if name_pattern:
            params["name_pattern"] = name_pattern

        response = await _analysis_get(
            f"/api/analysis/graphs/{container_name}/nodes",
            tenant_id,
            params=params,
        )
        if response.status_code == 404:
            raise not_found("Database", container_name)
        response.raise_for_status()
        body = response.json()

        raw_nodes = body.get("nodes", []) if isinstance(body, dict) else []
        total_count = int(body.get("total", len(raw_nodes))) if isinstance(body, dict) else 0
        nodes = [
            GraphNodeItem(
                id=str(n.get("id", "")),
                type=str(n.get("type", "")),
                name=str(n.get("properties", {}).get("name", n.get("id", ""))),
                properties=n.get("properties", {}) or {},
            )
            for n in raw_nodes
        ]

        pagination = {
            "page": page,
            "limit": limit,
            "total": total_count,
            "pages": max(1, (total_count + limit - 1) // limit),
        }

        return NodesResponse(
            message=f"Retrieved {len(nodes)} nodes from container '{container_name}'",
            pagination=pagination,
            items=nodes,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error querying nodes for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to query graph nodes: {str(e)}") from e


@router.get(
    "/{container_name}/edges",
    response_model=EdgesResponse,
    summary="Get Graph Edges",
    description="Query relationships/edges in a specific code graph.",
    operation_id="list_graph_edges",
)
async def get_graph_edges(
    container_name: str = Path(..., description="Container name"),
    edge_type: str | None = Query(None, description="Filter by edge type"),
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(50, ge=1, le=1000, description="Items per page"),
    tenant_id: str = Depends(require_tenant),
) -> EdgesResponse:
    """Query edges/relationships in a code graph with filtering and pagination.

    Forwards to coding-analysis' reader. Converts this router's
    ``page``/``limit`` into coding-analysis' ``limit``/``offset`` and
    preserves the public ``items``/``pagination`` shape.
    """
    _validate_container_name(container_name)
    try:
        _validate_sqlite_pagination(page, limit)
        offset = (page - 1) * limit
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if edge_type:
            params["relationship"] = edge_type

        response = await _analysis_get(
            f"/api/analysis/graphs/{container_name}/edges",
            tenant_id,
            params=params,
        )
        if response.status_code == 404:
            raise not_found("Database", container_name)
        response.raise_for_status()
        body = response.json()

        raw_edges = body.get("edges", []) if isinstance(body, dict) else []
        total_count = int(body.get("total", len(raw_edges))) if isinstance(body, dict) else 0
        edges = [
            GraphEdgeItem(
                id=f"{e.get('source', '')}->{e.get('target', '')}",
                source=str(e.get("source", "")),
                target=str(e.get("target", "")),
                type=str(e.get("relationship", "")),
                properties=e.get("properties", {}) or {},
            )
            for e in raw_edges
        ]

        pagination = {
            "page": page,
            "limit": limit,
            "total": total_count,
            "pages": max(1, (total_count + limit - 1) // limit),
        }

        return EdgesResponse(
            message=f"Retrieved {len(edges)} edges from container '{container_name}'",
            pagination=pagination,
            items=edges,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error querying edges for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to query graph edges: {str(e)}") from e


# ---------------------------------------------------------------------------
# Graph analysis endpoints (AI-augmented, runtime)
# ---------------------------------------------------------------------------


@router.get(
    "/{container_name}/ai-augmented",
    summary="Get AI-Augmented Graph",
    description="Get augmented code graph with AI insights and advanced analysis.",
    operation_id="get_ai_augmented_graph",
)
async def get_augmented_graph(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get augmented code graph with AI insights and advanced analysis."""
    _validate_container_name(container_name)
    try:
        settings = get_settings()
        node_count, edge_count = await _get_graph_counts(container_name, settings, tenant_id)
        if node_count == -1:
            raise not_found("Container", container_name)

        return {
            "status": "success",
            "message": f"Augmented analysis completed for container '{container_name}'",
            "data_available": False,
            "data": {
                "graph": {
                    "container_name": container_name,
                    "name": container_name,
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "status": "augmented",
                    "tags": ["augmented", "ai_insights"],
                },
                "ai_insights": [],
                "similarity_matches": [],
                "security_vulnerabilities": [],
                "security_score": 100.0,
                "recommendations": [],
                "analysis_summary": {
                    "files_analyzed": 0,
                    "functions_analyzed": 0,
                    "total_insights": 0,
                    "total_similarity_matches": 0,
                    "total_vulnerabilities": 0,
                    "security_score": 100.0,
                },
            },
            "request_id": uuid.uuid4().hex,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in augmented analysis for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to generate augmented graph: {str(e)}") from e


@router.get(
    "/{container_name}/runtime-augmented",
    summary="Get Runtime Augmented Graph",
    description="Get runtime-augmented code graph with real-time analysis.",
    operation_id="get_runtime_augmented_graph",
)
async def get_runtime_augmented_graph(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get runtime-augmented code graph with real-time execution analysis."""
    _validate_container_name(container_name)
    try:
        settings = get_settings()
        node_count, edge_count = await _get_graph_counts(container_name, settings, tenant_id)
        if node_count == -1:
            raise not_found("Container", container_name)

        return {
            "status": "success",
            "message": f"Runtime augmented analysis completed for container '{container_name}'",
            "data_available": False,
            "data": {
                "graph": {
                    "container_name": container_name,
                    "name": container_name,
                    "node_count": node_count,
                    "edge_count": edge_count,
                    "status": "runtime_augmented",
                    "tags": ["runtime_augmented", "real_time"],
                },
                "metrics": {
                    "active_paths": 0,
                    "avg_duration": 0.0,
                    "peak_memory": 0.0,
                    "error_rate": 0.0,
                    "total_executions": 0,
                    "successful_executions": 0,
                    "failed_executions": 0,
                },
                "performance": {
                    "hotspots": [],
                    "bottlenecks": [],
                    "memory_intensive_functions": [],
                    "cpu_intensive_functions": [],
                },
                "execution_paths": [],
            },
            "request_id": uuid.uuid4().hex,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error in runtime analysis for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to generate runtime augmented graph: {str(e)}") from e


# ---------------------------------------------------------------------------
# Architecture analysis endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/{container_name}/augmented",
    summary="Get Architecture-Augmented Code Graph",
    description="Get code graph augmented with architecture analysis data.",
    operation_id="get_architecture_graph",
)
async def get_architecture_code_graph(
    container_name: str = Path(..., description="Container name"),
    include_architecture: bool = Query(True, description="Include architecture analysis data"),
    include_dependencies: bool = Query(True, description="Include dependency analysis data"),
    include_quality: bool = Query(True, description="Include quality analysis data"),
    include_runtime: bool = Query(False, description="Include runtime trace data"),
    quality_threshold: float | None = Query(None, description="Minimum quality score"),
    hotspot_threshold: float | None = Query(None, description="Minimum hotspot score"),
    limit: int = Query(1000, ge=1, le=10000, description="Maximum items to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get the code graph augmented with architecture-oriented summary sections.

    Fetches nodes + edges from coding-analysis (the pod that actually owns
    the sqlite file) and applies the quality/hotspot threshold filters
    locally. The ``include_*`` flags gate the placeholder summary
    sections — these exist to keep the public response shape stable;
    backend work to populate them lives in coding-analysis.

    fr-backend's Architecture tab consumes this endpoint via
    ``codeGraph.getArchitectureGraph`` → ``adaptAugmentedGraph``,
    which only requires ``nodes`` + ``edges``; the summaries remain
    for n8n node consumers and future dashboards.
    """
    _validate_container_name(container_name)
    try:
        # Pull nodes + edges in one hop from the pod that writes them.
        response = await _analysis_get(
            f"/api/analysis/graphs/{container_name}/augmented",
            tenant_id,
            params={"node_limit": limit, "edge_limit": limit},
        )
        if response.status_code == 404:
            raise not_found("Database", container_name)
        response.raise_for_status()
        body = response.json()
        raw_nodes = body.get("nodes", []) if isinstance(body, dict) else []
        raw_edges = body.get("edges", []) if isinstance(body, dict) else []

        # Edge slicing to honour ``offset`` — coding-analysis returns from the
        # top; callers who asked for a window further in still get a window.
        raw_nodes = raw_nodes[offset : offset + limit]
        raw_edges = raw_edges[offset : offset + limit]

        processed_nodes: list[dict[str, Any]] = []
        for node in raw_nodes:
            properties = node.get("properties") or {}

            if quality_threshold is not None:
                node_quality = properties.get("quality_score")
                if isinstance(node_quality, (int, float)) and node_quality < quality_threshold:
                    continue

            if hotspot_threshold is not None:
                node_hotspot = properties.get("hotspot_score")
                if isinstance(node_hotspot, (int, float)) and node_hotspot < hotspot_threshold:
                    continue

            processed_nodes.append(
                {
                    "id": node.get("id", ""),
                    "type": node.get("type", ""),
                    "properties": properties,
                }
            )

        processed_edges: list[dict[str, Any]] = []
        for edge in raw_edges:
            processed_edges.append(
                {
                    "source": edge.get("source", ""),
                    "target": edge.get("target", ""),
                    "relationship": edge.get("relationship", ""),
                    "properties": edge.get("properties") or {},
                }
            )

        result: dict[str, Any] = {
            "nodes": processed_nodes,
            "edges": processed_edges,
        }

        if include_architecture:
            result["architecture_metadata"] = {
                "layer_classification": {},
                "component_boundaries": [],
                "pattern_matches": [],
            }
        if include_quality:
            result["quality_summary"] = {
                "overall_score": None,
                "complexity_distribution": {},
                "code_smells": [],
            }
        if include_dependencies:
            result["dependency_summary"] = {
                "direct_dependencies": [],
                "circular_dependencies": [],
                "dependency_depth": 0,
            }
        if include_runtime:
            result["runtime_summary"] = {
                "execution_traces": [],
                "hot_paths": [],
                "coverage_percentage": None,
            }

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting augmented graph for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Failed to get augmented graph: {str(e)}") from e


# ---------------------------------------------------------------------------
# Internal data operations endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/internal/database/counts/{container_name}",
    summary="Internal Database Counts",
    description="Internal endpoint for cross-container database access.",
    include_in_schema=False,
    operation_id="get_internal_database_counts",
)
async def get_internal_database_counts(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get database node/edge counts directly from the container's database."""
    _validate_container_name(container_name)
    try:
        node_count, edge_count = await _get_graph_counts(container_name, tenant_id=tenant_id)
        if node_count == -1:
            return {
                "container_name": container_name,
                "error": f"Database not found for container: {container_name}",
                "status": "error",
            }
        return {
            "container_name": container_name,
            "node_count": node_count,
            "edge_count": edge_count,
            "status": "success",
        }
    except Exception as e:
        logger.error("Error fetching internal counts for %s: %s", container_name, e, exc_info=True)
        raise internal_error(f"Unexpected error fetching internal counts: {str(e)}") from e


@router.get(
    "/debug/database/verify/{container_name}",
    summary="Debug Database Verification",
    description="Debug endpoint to verify database existence and contents.",
    include_in_schema=False,
    operation_id="debug_database_verify",
)
async def debug_database_verification(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Debug endpoint to verify a container's graph existence and contents.

    The graph store is coding-analysis-owned Postgres, so this reports what
    that service knows (existence + counts) rather than inspecting a local
    SQLite file.
    """
    _validate_container_name(container_name)

    node_count, edge_count = await _get_graph_counts(container_name, tenant_id=tenant_id)
    exists = node_count != -1

    debug_info: dict[str, Any] = {
        "container_name": container_name,
        "storage": "postgres (coding-analysis)",
        "file_exists": exists,
        "database_accessible_and_valid_schema": exists,
    }
    if exists:
        debug_info["node_count"] = node_count
        debug_info["edge_count"] = edge_count
    else:
        debug_info["error_details"] = f"No graph found for container '{container_name}'."

    return debug_info


__all__ = ["router"]
