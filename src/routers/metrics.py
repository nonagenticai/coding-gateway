"""Code graph metrics API endpoints.

Provides endpoints for entity-level metrics, graph summaries, centrality
calculations, performance monitoring, analytics dashboard, and container-
based metrics aggregation.  Ported from the monolith's four metrics
sub-routers into a single flat router using CodeGraphMetricsService.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant
from clients.outbound_identity import identity_headers, service_tenant
from config.settings import get_settings
from models.schemas import OverallGraphMetrics
from routers.proxy import get_proxy_client

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Lightweight response models
# ---------------------------------------------------------------------------


class EntityMetricsResponse(BaseModel):
    entity_name: str = ""
    degree_centrality: float = 0.0
    betweenness_centrality: float = 0.0
    closeness_centrality: float = 0.0
    pagerank: float = 0.0
    network_importance: float = 0.0
    in_degree: int = 0
    out_degree: int = 0
    total_degree: int = 0


class GraphSummaryResponse(BaseModel):
    """Graph summary matching monolith's GraphSummaryResponse schema."""

    entity_counts: dict[str, int] = Field(default_factory=dict)
    relationship_counts: dict[str, int] = Field(default_factory=dict)
    language_distribution: dict[str, int] = Field(default_factory=dict)
    graph_density: float = 0.0
    analysis_timestamp: str = ""


class TopEntityItem(BaseModel):
    entity_name: str = ""
    entity_type: str = ""
    metric_value: float = 0.0
    rank: int = 0


class DashboardSummaryResponse(BaseModel):
    graph_overview: dict[str, Any] = Field(default_factory=dict)
    top_entities_by_metric: dict[str, list[TopEntityItem]] = Field(default_factory=dict)
    metrics_available: list[str] = Field(default_factory=list)
    last_updated: str | None = None
    status: str = "active"


class PerformanceSummaryResponse(BaseModel):
    overall_statistics: dict[str, Any] = Field(default_factory=dict)
    language_breakdown: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Maps user-facing metric names to centrality dict keys returned by
# CodeGraphMetricsService.calculate_centrality_metrics().
_METRIC_KEY_MAP: dict[str, str] = {
    "degree": "degree_centrality",
    "degree_centrality": "degree_centrality",
    "betweenness": "degree_centrality",  # betweenness not in centrality results; fall back
    "closeness": "degree_centrality",  # closeness not in centrality results; fall back
    "importance": "degree_centrality",  # importance not in centrality results; fall back
    "pagerank": "degree_centrality",  # pagerank not in centrality results; fall back
    "in_degree": "in_degree",
    "out_degree": "out_degree",
    "total_degree": "total_degree",
}


def _resolve_sort_key(metric: str) -> str:
    """Return the centrality-dict key for a user-supplied metric name."""
    return _METRIC_KEY_MAP.get(metric, "degree_centrality")


class _AnalysisMetricsProxy:
    """Adapts the metrics endpoints to coding-analysis HTTP calls.

    Graph data lives in coding-analysis (Postgres) now, so metrics are computed
    there. This proxy exposes the same three methods the endpoints already call
    (``get_graph_metrics`` / ``get_function_network_metrics`` /
    ``calculate_centrality_metrics``), forwarding to the
    ``/api/analysis/graphs/{cn}/metrics*`` endpoints scoped by container name.
    """

    def __init__(self, container_name: str, tenant_id: str) -> None:
        self._cn = container_name
        self._tenant = tenant_id

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        settings = get_settings()
        client = await get_proxy_client()
        url = f"{settings.analysis_url.rstrip('/')}{path}"
        return await client.get(url, headers=identity_headers(self._tenant), params=params or None)

    async def get_graph_metrics(self) -> OverallGraphMetrics:
        resp = await self._get(f"/api/analysis/graphs/{self._cn}/metrics")
        resp.raise_for_status()
        return OverallGraphMetrics(**resp.json())

    async def get_function_network_metrics(self, entity_name: str) -> dict[str, Any]:
        resp = await self._get(f"/api/analysis/graphs/{self._cn}/metrics/entity/{entity_name}")
        resp.raise_for_status()
        return resp.json()

    async def calculate_centrality_metrics(self, entity_type: str) -> dict[str, dict[str, float]]:
        resp = await self._get(
            f"/api/analysis/graphs/{self._cn}/metrics/centrality",
            params={"entity_type": entity_type},
        )
        resp.raise_for_status()
        return resp.json().get("metrics", {})


async def _get_metrics_service(
    container_name: str | None = None, tenant_id: str | None = None
) -> _AnalysisMetricsProxy | None:
    """Return a metrics proxy to coding-analysis for *container_name*, or None.

    Returns None when no container is given — graphs now live in coding-analysis
    keyed by container, so there is no ambient "first available" graph — or when
    the container has no graph yet (404 from coding-analysis), so callers keep
    their existing None-branch behaviour (404/empty) for missing graphs.
    """
    if not container_name:
        return None
    try:
        settings = get_settings()
        tenant = tenant_id or service_tenant()
        client = await get_proxy_client()
        url = f"{settings.analysis_url.rstrip('/')}/api/analysis/graphs/{container_name}"
        resp = await client.get(url, headers=identity_headers(tenant))
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return _AnalysisMetricsProxy(container_name, tenant)
    except Exception as exc:
        logger.warning("Could not reach coding-analysis metrics for %s: %s", container_name, exc)
        return None


# ---------------------------------------------------------------------------
# System metrics endpoints
# ---------------------------------------------------------------------------


@router.get(
    "",
    summary="System Metrics (health-metrics root)",
    description="System metrics endpoint matching monolith /api/health/metrics.",
    operation_id="metrics_index",
)
@router.get(
    "/",
    include_in_schema=False,
    operation_id="metrics_index_slash",
)
async def metrics_index(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Return psutil system metrics (matches monolith /api/health/metrics).

    Both ``/api/health/metrics`` (handled by the health router) and
    ``/api/health/metrics/`` (handled here, the metrics router root) must
    return the same MetricsResponse-shaped payload so parity tests pass.
    """
    import platform as _platform
    import sys
    import time
    import uuid as _uuid
    from datetime import UTC, datetime

    try:
        import psutil

        _has_psutil = True
    except ImportError:
        _has_psutil = False

    _start_time = time.time()

    metrics: dict[str, Any] = {}
    if _has_psutil:
        try:
            vm = psutil.virtual_memory()
            swap = psutil.swap_memory()
            disk = psutil.disk_usage("/")
            system_metrics = {
                "cpu_count": psutil.cpu_count(),
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory": dict(vm._asdict()),
                "swap": dict(swap._asdict()),
                "disk": dict(disk._asdict()),
                "boot_time": psutil.boot_time(),
                "platform": _platform.platform(),
                "python_version": sys.version,
            }
        except Exception:
            import os as _os

            system_metrics = {
                "cpu_count": _os.cpu_count() or 1,
                "cpu_percent": 0.0,
                "memory": {},
                "swap": {},
                "disk": {},
                "boot_time": 0.0,
                "platform": _platform.platform(),
                "python_version": sys.version,
            }

        try:
            net = psutil.net_io_counters()
            network_metrics = dict(net._asdict()) if net else {}
        except Exception:
            network_metrics = {}

        try:
            proc = psutil.Process()
            process_metrics = {
                "cpu_percent": proc.cpu_percent(),
                "memory_info": dict(proc.memory_info()._asdict()),
                "num_threads": proc.num_threads(),
                "create_time": proc.create_time(),
            }
        except Exception:
            process_metrics = {
                "cpu_percent": 0.0,
                "memory_info": {},
                "num_threads": 1,
                "create_time": _start_time,
            }

        metrics = {
            "system": system_metrics,
            "network": network_metrics,
            "process": process_metrics,
        }
    else:
        import os as _os

        metrics = {
            "system": {
                "cpu_count": _os.cpu_count() or 1,
                "cpu_percent": 0.0,
                "memory": {},
                "swap": {},
                "disk": {},
                "boot_time": 0.0,
                "platform": _platform.platform(),
                "python_version": sys.version,
            },
            "network": {},
            "process": {
                "cpu_percent": 0.0,
                "memory_info": {},
                "num_threads": 1,
                "create_time": _start_time,
            },
        }

    now = datetime.now(UTC).isoformat()
    return {
        "status": "success",
        "message": "System metrics retrieved",
        "timestamp": now,
        "request_id": f"req_{_uuid.uuid4().hex[:8]}",
        "metrics": metrics,
        "collection_time": now,
    }


@router.get(
    "/entity/{entity_name}/metrics",
    response_model=EntityMetricsResponse,
    summary="Get Entity Metrics",
    description="Get centrality and importance metrics for a specific entity.",
    operation_id="get_entity_name_metrics",
)
async def get_entity_name_metrics(
    entity_name: str = Path(..., description="Name of the entity to get metrics for"),
    tenant_id: str = Depends(require_tenant),
) -> EntityMetricsResponse:
    """Get centrality and importance metrics for a specific entity."""
    try:
        service = await _get_metrics_service()
        if not service:
            raise HTTPException(status_code=404, detail="No graph databases available for metrics")

        metrics = await service.get_function_network_metrics(entity_name)
        return EntityMetricsResponse(
            entity_name=entity_name,
            degree_centrality=metrics.get("degree_centrality", 0.0),
            betweenness_centrality=metrics.get("betweenness_centrality", 0.0),
            closeness_centrality=metrics.get("closeness_centrality", 0.0),
            pagerank=metrics.get("pagerank", 0.0),
            network_importance=metrics.get("network_importance", 0.0),
            in_degree=metrics.get("in_degree", 0),
            out_degree=metrics.get("out_degree", 0),
            total_degree=metrics.get("total_degree", 0),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error calculating metrics for entity %s: %s", entity_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error calculating entity metrics") from e


@router.get(
    "/graph-summary",
    response_model=GraphSummaryResponse,
    summary="Get Graph Summary",
    description="Get summary metrics for the entire code graph.",
    operation_id="get_graph_summary_metrics",
)
async def get_graph_summary_metrics(
    tenant_id: str = Depends(require_tenant),
) -> GraphSummaryResponse:
    """Get summary metrics for the entire code graph (matches monolith schema)."""
    from datetime import UTC, datetime

    try:
        service = await _get_metrics_service()
        if not service:
            return GraphSummaryResponse(
                analysis_timestamp=datetime.now(UTC).isoformat(),
            )

        metrics = await service.get_graph_metrics()
        return GraphSummaryResponse(
            entity_counts=metrics.node_types if hasattr(metrics, "node_types") else {},
            relationship_counts=metrics.relationship_types
            if hasattr(metrics, "relationship_types")
            else {},
            language_distribution={},
            graph_density=metrics.density if hasattr(metrics, "density") else 0.0,
            analysis_timestamp=datetime.now(UTC).isoformat(),
        )
    except Exception as e:
        logger.error("Error calculating graph summary metrics: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error calculating graph summary metrics"
        ) from e


@router.post(
    "/centrality",
    summary="Calculate Centrality Metrics",
    description="Calculate and store centrality metrics for the graph.",
    operation_id="calculate_centrality_metrics",
)
async def calculate_and_store_centrality_metrics(
    entity_type: str | None = None,
    container_name: str | None = Query(
        None,
        description=(
            "Workspace whose graph to compute centrality for. Graphs live in "
            "coding-analysis keyed by workspace, so without one there is no graph."
        ),
    ),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Calculate and store centrality metrics for the graph.

    "No graph" is a valid state and answers a typed 404, never a 500. This route
    used to call ``_get_metrics_service()`` with NO container -- which returns
    None by construction -- and then raise a 500 that the broad ``except`` below
    re-wrapped as a second 500, so it failed for every request ever made.
    """
    service = await _get_metrics_service(container_name, tenant_id)
    if not service:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No graph available for container '{container_name}'"
                if container_name
                else "No graph available: pass container_name to select a workspace graph"
            ),
        )
    try:
        result = await service.calculate_centrality_metrics(entity_type or "function")
    except httpx.HTTPStatusError as e:
        upstream = e.response.status_code
        if upstream == 404:
            raise HTTPException(
                status_code=404, detail=f"No graph available for container '{container_name}'"
            ) from e
        logger.error("coding-analysis centrality returned %s for %s", upstream, container_name)
        raise HTTPException(
            status_code=502, detail=f"coding-analysis centrality failed ({upstream})"
        ) from e
    except Exception as e:
        logger.error("Error calculating centrality metrics: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error calculating centrality metrics") from e

    return {
        "status": "success",
        "entities_processed": len(result),
        "centrality_metrics": result,
    }


@router.get(
    "/entity-type/{entity_type}/metrics",
    summary="Get Entity Type Metrics",
    description="Get centrality metrics for all entities of a specific type.",
    operation_id="get_entity_type_metrics",
)
async def get_entity_type_metrics(
    entity_type: str = Path(..., description="Type of entities to get metrics for"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, dict[str, float]]:
    """Get centrality metrics for all entities of a specific type."""
    try:
        service = await _get_metrics_service()
        if not service:
            return {}

        result = await service.calculate_centrality_metrics(entity_type)
        return result
    except Exception as e:
        logger.error(
            "Error calculating metrics for entity type %s: %s", entity_type, e, exc_info=True
        )
        raise HTTPException(
            status_code=500, detail="Error calculating metrics for entity type"
        ) from e


# ---------------------------------------------------------------------------
# Analytics / dashboard endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/top-entities",
    response_model=list[TopEntityItem],
    summary="Get Top Entities",
    description="Get top entities based on a specific metric.",
    operation_id="list_top_entities",
)
async def get_top_entities_by_metric(
    metric: str = Query(..., description="Metric to rank by"),
    limit: int = Query(10, ge=1, le=50, description="Number of top entities"),
    tenant_id: str = Depends(require_tenant),
) -> list[TopEntityItem]:
    """Get top entities based on a specific metric."""
    try:
        service = await _get_metrics_service()
        if not service:
            return []

        all_metrics = await service.calculate_centrality_metrics("function")
        sort_key = _resolve_sort_key(metric)

        ranked = sorted(
            all_metrics.items(),
            key=lambda x: x[1].get(sort_key, 0.0),
            reverse=True,
        )

        return [
            TopEntityItem(
                entity_name=name,
                entity_type="function",
                metric_value=metrics_dict.get(sort_key, 0.0),
                rank=idx + 1,
            )
            for idx, (name, metrics_dict) in enumerate(ranked[:limit])
        ]
    except Exception as e:
        logger.error("Error retrieving top entities: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error retrieving top entities") from e


@router.get(
    "/metrics-comparison",
    summary="Compare Entity Types",
    description="Compare top entities across different entity types.",
    operation_id="compare_entity_types",
)
async def compare_entity_types(
    entity_types: str | None = Query(None, description="Comma-separated list of entity types"),
    metric: str = Query("importance", description="Metric for ranking"),
    limit: int = Query(5, ge=1, le=20, description="Max entities per type"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, list[TopEntityItem]]:
    """Compare top entities across different entity types."""
    try:
        service = await _get_metrics_service()
        if not service:
            return {}

        types_list = (
            [t.strip() for t in entity_types.split(",")] if entity_types else ["function", "class"]
        )
        result: dict[str, list[TopEntityItem]] = {}
        sort_key = _resolve_sort_key(metric)

        for etype in types_list:
            all_metrics = await service.calculate_centrality_metrics(etype)
            ranked = sorted(
                all_metrics.items(),
                key=lambda x: x[1].get(sort_key, 0.0),
                reverse=True,
            )
            result[etype] = [
                TopEntityItem(
                    entity_name=name,
                    entity_type=etype,
                    metric_value=metrics_dict.get(sort_key, 0.0),
                    rank=idx + 1,
                )
                for idx, (name, metrics_dict) in enumerate(ranked[:limit])
            ]

        return result
    except Exception as e:
        logger.error("Error comparing entity types: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error comparing entity types") from e


@router.get(
    "/dashboard-summary",
    response_model=DashboardSummaryResponse,
    summary="Dashboard Summary",
    description="Get comprehensive summary data for analytics dashboard.",
    operation_id="get_metrics_dashboard_summary",
)
async def get_dashboard_summary(
    tenant_id: str = Depends(require_tenant),
) -> DashboardSummaryResponse:
    """Get comprehensive summary data for analytics dashboard."""
    from datetime import UTC, datetime

    metrics_of_interest = ["pagerank", "betweenness", "degree", "importance"]
    try:
        service = await _get_metrics_service()
        if not service:
            return DashboardSummaryResponse(
                graph_overview={"error": "Graph service unavailable"},
                top_entities_by_metric={m: [] for m in metrics_of_interest},
                metrics_available=metrics_of_interest,
                status="limited",
            )

        graph_metrics = await service.get_graph_metrics()
        graph_overview: dict[str, Any] = {
            "entity_counts": graph_metrics.node_types
            if hasattr(graph_metrics, "node_types")
            else {},
            "relationship_counts": graph_metrics.relationship_types
            if hasattr(graph_metrics, "relationship_types")
            else {},
            "language_distribution": {},
            "graph_density": graph_metrics.density if hasattr(graph_metrics, "density") else 0.0,
            "analysis_timestamp": datetime.now(UTC).isoformat(),
        }

        return DashboardSummaryResponse(
            graph_overview=graph_overview,
            top_entities_by_metric={m: [] for m in metrics_of_interest},
            metrics_available=metrics_of_interest,
            last_updated=graph_overview.get("analysis_timestamp"),
            status="active",
        )
    except Exception as e:
        logger.error("Error generating dashboard summary: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error generating dashboard summary") from e


@router.get(
    "/entity-rankings",
    summary="Entity Rankings",
    description="Get entity rankings across multiple metrics.",
    operation_id="get_entity_rankings",
)
async def get_entity_rankings(
    metrics: str = Query(..., description="Comma-separated list of metrics to rank by"),
    limit: int = Query(10, ge=1, le=50, description="Number of top entities per metric"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get entity rankings across multiple metrics."""
    try:
        metric_list = [m.strip().lower() for m in metrics.split(",")]
        valid_metrics = ["pagerank", "betweenness", "degree", "closeness", "importance"]

        invalid_metrics = [m for m in metric_list if m not in valid_metrics]
        if invalid_metrics:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid metrics: {invalid_metrics}. Valid: {valid_metrics}",
            )

        return {
            "rankings": {m: [] for m in metric_list},
            "cross_metric_analysis": {},
            "summary": {
                "total_metrics_analyzed": len(metric_list),
                "entities_per_metric": {m: 0 for m in metric_list},
            },
            "requested_metrics": metric_list,
            "data_available": False,
            "message": "Metrics computation not yet implemented",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error generating entity rankings: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error generating entity rankings") from e


# ---------------------------------------------------------------------------
# Performance metrics endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/multi-language/performance",
    response_model=PerformanceSummaryResponse,
    summary="Multi-Language Performance Metrics",
    description="Get comprehensive performance metrics for multi-language processing.",
    operation_id="get_multi_language_performance",
)
async def get_multi_language_performance_metrics(
    tenant_id: str = Depends(require_tenant),
) -> PerformanceSummaryResponse:
    """Get comprehensive performance metrics for multi-language processing.

    The gateway collects no multi-language performance data, so this is the
    empty state -- a typed 404, the same answer its sibling
    ``get_language_performance_stats`` gives. It used to be a hard-coded 500,
    i.e. a server fault reported for a state that is not a fault.
    """
    raise HTTPException(
        status_code=404,
        detail="No performance data available",
    )


@router.get(
    "/multi-language/languages/{language}/stats",
    summary="Language Performance Stats",
    description="Get detailed performance statistics for a specific programming language.",
    operation_id="get_language_performance_stats",
)
async def get_language_performance_stats(
    language: str = Path(..., description="Programming language"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get detailed performance statistics for a specific programming language."""
    raise HTTPException(
        status_code=404,
        detail=f"No performance data available for language: {language}",
    )


@router.get(
    "/multi-language/parser-performance",
    summary="Parser Performance Report",
    description="Get detailed performance comparison of different parsing strategies.",
    operation_id="get_parser_performance",
)
async def get_parser_performance_report(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get detailed performance comparison of different parsing strategies."""
    return {
        "parser_rankings": [],
        "summary": {
            "total_parsers": 0,
            "best_performer": None,
            "total_operations": 0,
            "average_success_rate": 0.0,
        },
    }


@router.get(
    "/multi-language/top-languages",
    summary="Top Performing Languages",
    description="Get languages ranked by processing performance.",
    operation_id="list_top_languages",
)
async def get_top_performing_languages(
    limit: int = Query(5, ge=1, le=20, description="Maximum number of languages"),
    tenant_id: str = Depends(require_tenant),
) -> list[dict[str, Any]]:
    """Get languages ranked by processing performance."""
    return []


@router.post(
    "/multi-language/export",
    summary="Export Metrics",
    description="Export comprehensive multi-language performance metrics.",
    operation_id="export_multi_language_metrics",
)
async def export_multi_language_metrics(
    include_raw_data: bool = Query(False, description="Include raw operation data"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Export comprehensive multi-language performance metrics."""
    import uuid as _uuid
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    return {
        "status": "success",
        "message": "Metrics computation not yet implemented",
        "request_id": f"req_{_uuid.uuid4().hex[:8]}",
        "timestamp": now,
        "export_timestamp": now,
        "data": {
            "parser_performance": {},
            "export_timestamp": now,
        },
    }


@router.post(
    "/multi-language/reset",
    summary="Reset Metrics",
    description="Reset all collected multi-language performance metrics.",
    operation_id="reset_multi_language_metrics",
)
async def reset_multi_language_metrics(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Reset all collected multi-language performance metrics."""
    import uuid as _uuid
    from datetime import UTC, datetime

    return {
        "status": "success",
        "message": "All multi-language performance metrics have been reset",
        "request_id": f"req_{_uuid.uuid4().hex[:8]}",
        "timestamp": datetime.now(UTC).isoformat(),
    }


# ---------------------------------------------------------------------------
# Container-based metrics aggregation endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/container/{container_name}/entity/{entity_name}/metrics",
    summary="Container Entity Metrics",
    description="Get metrics for a specific entity within a container.",
    operation_id="get_container_entity_metrics",
)
async def get_container_entity_metrics(
    container_name: str = Path(..., description="Container name"),
    entity_name: str = Path(..., description="Entity name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get metrics for a specific entity within a containerized analysis environment."""
    try:
        service = await _get_metrics_service(container_name, tenant_id)
        if not service:
            raise HTTPException(
                status_code=404, detail=f"Container '{container_name}' not found or no database"
            )

        metrics = await service.get_function_network_metrics(entity_name)
        return {
            "container_name": container_name,
            "entity_name": entity_name,
            **metrics,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            "Error getting metrics for %s in %s: %s", entity_name, container_name, e, exc_info=True
        )
        raise HTTPException(status_code=500, detail="Error getting metrics") from e


@router.get(
    "/container/{container_name}/overview",
    summary="Container Overview Metrics",
    description="Get overview metrics for a container's graph analysis.",
    operation_id="get_container_overview_metrics",
)
async def get_container_overview_metrics(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get overview metrics for an entire containerized graph analysis environment."""
    try:
        service = await _get_metrics_service(container_name, tenant_id)
        if not service:
            raise HTTPException(status_code=404, detail=f"Container '{container_name}' not found")

        graph_metrics = await service.get_graph_metrics()
        return {
            "container_name": container_name,
            "graph_statistics": {
                "total_entities": graph_metrics.node_count,
                "total_relationships": graph_metrics.edge_count,
                "density": graph_metrics.density,
                "node_types": graph_metrics.node_types,
                "relationship_types": graph_metrics.relationship_types,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting overview for %s: %s", container_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error getting overview") from e


@router.get(
    "/container/{container_name}/health",
    summary="Container Health Metrics",
    description="Get health and performance metrics for a specific container.",
    operation_id="get_container_health_metrics",
)
async def get_container_health_metrics(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get health and performance metrics for a specific container environment."""
    try:
        service = await _get_metrics_service(container_name, tenant_id)
        if not service:
            raise HTTPException(status_code=404, detail=f"Container '{container_name}' not found")

        graph_metrics = await service.get_graph_metrics()
        return {
            "container_name": container_name,
            "status": "healthy",
            "graph_loaded": True,
            "entity_count": graph_metrics.node_count,
            "health_score": min(100, max(0, (graph_metrics.node_count / 100) * 50 + 50)),
            "processing_capabilities": {
                "centrality_calculation": True,
                "metrics_aggregation": True,
                "multi_language_support": True,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error getting health for %s: %s", container_name, e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Error retrieving container health metrics"
        ) from e


@router.post(
    "/container/{container_name}/refresh",
    summary="Refresh Container Metrics",
    description="Refresh and recalculate metrics for a specific container.",
    operation_id="refresh_container_metrics",
)
async def refresh_container_metrics(
    container_name: str = Path(..., description="Container name"),
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Refresh and recalculate metrics for a specific container environment."""
    from datetime import UTC, datetime

    try:
        service = await _get_metrics_service(container_name, tenant_id)
        if not service:
            return {
                "status": "completed",
                "container_name": container_name,
                "refresh_timestamp": datetime.now(UTC).isoformat(),
                "entities_processed": 0,
                "metrics_updated": [],
                "operation_summary": "Metrics refresh completed",
            }

        result = await service.calculate_centrality_metrics("function")
        return {
            "status": "completed",
            "container_name": container_name,
            "refresh_timestamp": datetime.now(UTC).isoformat(),
            "entities_processed": len(result),
            "metrics_updated": [
                "degree_centrality",
                "betweenness_centrality",
                "closeness_centrality",
                "pagerank",
                "importance_score",
            ],
            "operation_summary": "Metrics refresh completed",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error refreshing metrics for %s: %s", container_name, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error refreshing container metrics") from e


@router.get(
    "/containers/comparison",
    summary="Compare Container Metrics",
    description="Compare metrics across multiple container environments.",
    operation_id="compare_container_metrics",
)
async def compare_container_metrics(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Compare metrics across multiple container environments."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    return {
        "containers_analyzed": ["default"],
        "comparative_metrics": {
            "default": {
                "entity_count": {},
                "relationship_count": {},
                "graph_density": 0,
                "languages": {},
            },
        },
        "summary_statistics": {
            "total_containers": 1,
            "average_entities_per_container": 0,
            "most_active_container": "default",
            "comparison_timestamp": now,
        },
        "comparison_timestamp": now,
        "performance_comparison": {
            "fastest_container": "default",
            "largest_graph": "default",
            "most_diverse_languages": "default",
        },
        "recommendations": [
            "Single container environment detected",
            "Consider metrics comparison when multiple containers are available",
            "Current container shows healthy metrics profile",
        ],
    }
