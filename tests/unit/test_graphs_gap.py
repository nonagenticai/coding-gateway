"""Gap tests for routers/graphs.py.

Every graph handler now forwards to coding-analysis over HTTP (the pod that
owns the Postgres code-graph store). These tests mock the thin proxy helpers
(`_analysis_get` / `_analysis_post` / `_analysis_delete`) to return canned
`httpx.Response` objects; no SQLite is involved anywhere in the gateway.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx


def _analysis_response(
    body: dict[str, Any] | None = None, status_code: int = 200
) -> httpx.Response:
    """Build an ``httpx.Response`` stand-in for the analysis-proxy mocks.

    Attach a synthetic ``Request`` so ``raise_for_status`` doesn't trip on
    its "no request on response" guard inside httpx.
    """
    request = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/mock")
    return httpx.Response(status_code, json=body or {}, request=request)


# ---------------------------------------------------------------------------
# GET /api/graphs — list graphs
# ---------------------------------------------------------------------------


class TestListGraphs:
    async def test_list_empty_returns_zero_total(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response({"graphs": [], "total": 0})),
        ):
            resp = await client.get("/api/graphs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["graphs"] == []
        assert body["status"] == "success"

    async def test_list_with_stats_returns_counts(self, client):
        upstream = {
            "graphs": [
                {
                    "container_name": "testdb",
                    "node_count": 1,
                    "edge_count": 1,
                    "tenant_id": "n8n-dev",
                }
            ],
            "total": 1,
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs?include_stats=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["graphs"][0]["node_count"] == 1

    async def test_list_without_stats_hides_counts(self, client):
        upstream = {
            "graphs": [{"container_name": "testdb", "node_count": 5, "edge_count": 3}],
            "total": 1,
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs?include_stats=false")
        assert resp.status_code == 200
        body = resp.json()
        assert body["graphs"][0]["node_count"] == -1


# ---------------------------------------------------------------------------
# GET /api/graphs/{container_name} — graph detail
# ---------------------------------------------------------------------------


class TestGetGraphDetails:
    async def test_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing")
        assert resp.status_code == 404

    async def test_found_returns_full_detail(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(
                return_value=_analysis_response(
                    {
                        "container_name": "testdb",
                        "exists": True,
                        "node_count": 1,
                        "edge_count": 0,
                    }
                )
            ),
        ):
            resp = await client.get("/api/graphs/testdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["graph"]["container_name"] == "testdb"
        assert body["graph"]["node_count"] == 1

    async def test_invalid_name_returns_400(self, client):
        resp = await client.get("/api/graphs/bad name!")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/graphs/initialize
# ---------------------------------------------------------------------------


class TestInitializeGraph:
    async def test_initialize_creates_graph(self, client):
        with patch(
            "routers.graphs._analysis_post",
            new=AsyncMock(
                return_value=_analysis_response(
                    {"container_name": "testdb", "created": True, "repo_node_id": "repo_testdb"},
                    status_code=201,
                )
            ),
        ):
            resp = await client.post("/api/graphs/initialize", json={"container_name": "testdb"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "success"
        assert body["container_name"] == "testdb"
        assert body["nodes_created"] == 1

    async def test_initialize_conflict_returns_409(self, client):
        with patch(
            "routers.graphs._analysis_post",
            new=AsyncMock(return_value=_analysis_response(status_code=409)),
        ):
            resp = await client.post("/api/graphs/initialize", json={"container_name": "existing"})
        assert resp.status_code == 409


# ---------------------------------------------------------------------------
# DELETE /api/graphs/{container_name}
# ---------------------------------------------------------------------------


class TestDeleteGraph:
    async def test_delete_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_delete",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.delete("/api/graphs/missing")
        assert resp.status_code == 404

    async def test_delete_success(self, client):
        with patch(
            "routers.graphs._analysis_delete",
            new=AsyncMock(
                return_value=_analysis_response(
                    {
                        "container_name": "testdb",
                        "deleted": True,
                        "nodes_deleted": 3,
                        "edges_deleted": 2,
                    }
                )
            ),
        ):
            resp = await client.delete("/api/graphs/testdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["cleanup_performed"] is True
        assert body["deleted_nodes"] == 3
        assert body["deleted_edges"] == 2


# ---------------------------------------------------------------------------
# DELETE /api/graphs/cleanup/orphaned
# ---------------------------------------------------------------------------


class TestCleanupOrphaned:
    async def test_cleanup_dry_run_returns_zero(self, client):
        with (
            patch(
                "routers.graphs._list_active_container_names",
                new=AsyncMock(return_value=set()),
            ),
            patch(
                "routers.graphs._analysis_get",
                new=AsyncMock(return_value=_analysis_response({"graphs": [], "total": 0})),
            ),
        ):
            resp = await client.delete("/api/graphs/cleanup/orphaned?dry_run=true")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cleaned_count"] == 0

    async def test_cleanup_detects_orphan_in_dry_run(self, client):
        # A graph for this tenant with no matching live container is orphaned.
        graphs = {
            "graphs": [
                {"container_name": "orphan", "node_count": 1, "edge_count": 0, "tenant_id": None}
            ],
            "total": 1,
        }
        with (
            patch(
                "routers.graphs._list_active_container_names",
                new=AsyncMock(return_value=set()),
            ),
            patch(
                "routers.graphs._analysis_get",
                new=AsyncMock(return_value=_analysis_response(graphs)),
            ),
        ):
            # enable_auth defaults False in tests, so a tenantless graph is in scope.
            resp = await client.delete("/api/graphs/cleanup/orphaned?dry_run=true")
        assert resp.status_code == 200
        body = resp.json()
        assert "orphan" in body["orphaned_containers"]
        # dry_run never deletes
        assert body["cleaned_count"] == 0


# ---------------------------------------------------------------------------
# GET /api/graphs/{container_name}/nodes
# ---------------------------------------------------------------------------


class TestGetNodes:
    async def test_nodes_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing/nodes")
        assert resp.status_code == 404

    async def test_nodes_returns_paginated_items(self, client):
        upstream = {
            "container_name": "testdb",
            "total": 2,
            "limit": 50,
            "offset": 0,
            "nodes": [
                {"id": "n1", "type": "module", "properties": {"name": "mod1"}},
                {"id": "n2", "type": "class", "properties": {"name": "Foo"}},
            ],
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs/testdb/nodes")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert len(body["items"]) == 2
        assert body["pagination"]["total"] == 2

    async def test_nodes_with_type_filter(self, client):
        upstream = {
            "container_name": "testdb",
            "total": 1,
            "limit": 50,
            "offset": 0,
            "nodes": [
                {"id": "n2", "type": "class", "properties": {"name": "Foo"}},
            ],
        }
        mock = AsyncMock(return_value=_analysis_response(upstream))
        with patch("routers.graphs._analysis_get", new=mock):
            resp = await client.get("/api/graphs/testdb/nodes?node_type=class")
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["type"] == "class"
        # Verify the filter was forwarded upstream
        assert mock.call_args.kwargs["params"].get("node_type") == "class"

    async def test_nodes_with_name_pattern_is_forwarded(self, client):
        upstream = {
            "container_name": "testdb",
            "total": 1,
            "limit": 50,
            "offset": 0,
            "nodes": [{"id": "n1", "type": "module", "properties": {"name": "my_module"}}],
        }
        mock = AsyncMock(return_value=_analysis_response(upstream))
        with patch("routers.graphs._analysis_get", new=mock):
            resp = await client.get("/api/graphs/testdb/nodes?name_pattern=my_")
        assert resp.status_code == 200
        # name_pattern is forwarded verbatim to coding-analysis, which does
        # the substring match + escape of LIKE wildcards upstream.
        assert mock.call_args.kwargs["params"].get("name_pattern") == "my_"


# ---------------------------------------------------------------------------
# GET /api/graphs/{container_name}/edges
# ---------------------------------------------------------------------------


class TestGetEdges:
    async def test_edges_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing/edges")
        assert resp.status_code == 404

    async def test_edges_returns_items(self, client):
        upstream = {
            "container_name": "testdb",
            "total": 1,
            "limit": 50,
            "offset": 0,
            "edges": [{"source": "n1", "target": "n2", "relationship": "calls", "properties": {}}],
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs/testdb/edges")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["items"]) == 1
        assert body["items"][0]["type"] == "calls"

    async def test_edges_with_type_filter(self, client):
        upstream = {
            "container_name": "testdb",
            "total": 1,
            "limit": 50,
            "offset": 0,
            "edges": [{"source": "n1", "target": "n2", "relationship": "calls", "properties": {}}],
        }
        mock = AsyncMock(return_value=_analysis_response(upstream))
        with patch("routers.graphs._analysis_get", new=mock):
            resp = await client.get("/api/graphs/testdb/edges?edge_type=calls")
        assert resp.status_code == 200
        body = resp.json()
        for item in body["items"]:
            assert item["type"] == "calls"
        # edge_type is translated to the upstream "relationship" param
        assert mock.call_args.kwargs["params"].get("relationship") == "calls"


# ---------------------------------------------------------------------------
# AI augmented  GET /api/graphs/{container_name}/ai-augmented
# ---------------------------------------------------------------------------


class TestAIAugmented:
    async def test_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing/ai-augmented")
        assert resp.status_code == 404

    async def test_success_returns_ai_insights(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response({"node_count": 2, "edge_count": 1})),
        ):
            resp = await client.get("/api/graphs/testdb/ai-augmented")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "ai_insights" in body["data"]


# ---------------------------------------------------------------------------
# Runtime augmented  GET /api/graphs/{container_name}/runtime-augmented
# ---------------------------------------------------------------------------


class TestRuntimeAugmented:
    async def test_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing/runtime-augmented")
        assert resp.status_code == 404

    async def test_success_returns_metrics(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response({"node_count": 2, "edge_count": 1})),
        ):
            resp = await client.get("/api/graphs/testdb/runtime-augmented")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert "metrics" in body["data"]


# ---------------------------------------------------------------------------
# Architecture augmented  GET /api/graphs/{container_name}/augmented
# ---------------------------------------------------------------------------


class TestArchitectureAugmented:
    async def test_not_found_returns_404(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/missing/augmented")
        assert resp.status_code == 404

    async def test_all_sections_returned(self, client):
        upstream = {
            "container_name": "testdb",
            "node_count": 1,
            "edge_count": 0,
            "nodes": [{"id": "n1", "type": "module", "properties": {"name": "mod"}}],
            "edges": [],
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get(
                "/api/graphs/testdb/augmented"
                "?include_architecture=true"
                "&include_dependencies=true"
                "&include_quality=true"
                "&include_runtime=true"
            )
        assert resp.status_code == 200
        body = resp.json()
        assert "architecture_metadata" in body
        assert "quality_summary" in body
        assert "dependency_summary" in body
        assert "runtime_summary" in body

    async def test_quality_threshold_filters_nodes(self, client):
        upstream = {
            "container_name": "testdb",
            "node_count": 2,
            "edge_count": 0,
            "nodes": [
                {
                    "id": "n1",
                    "type": "module",
                    "properties": {"name": "good", "quality_score": 9.0},
                },
                {
                    "id": "n2",
                    "type": "module",
                    "properties": {"name": "bad", "quality_score": 3.0},
                },
            ],
            "edges": [],
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs/testdb/augmented?quality_threshold=5.0")
        assert resp.status_code == 200
        body = resp.json()
        names = [n["properties"].get("name") for n in body["nodes"]]
        assert "bad" not in names

    async def test_hotspot_threshold_filters_nodes(self, client):
        upstream = {
            "container_name": "testdb",
            "node_count": 2,
            "edge_count": 0,
            "nodes": [
                {
                    "id": "n1",
                    "type": "module",
                    "properties": {"name": "hot", "hotspot_score": 8.0},
                },
                {
                    "id": "n2",
                    "type": "module",
                    "properties": {"name": "cold", "hotspot_score": 1.0},
                },
            ],
            "edges": [],
        }
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(upstream)),
        ):
            resp = await client.get("/api/graphs/testdb/augmented?hotspot_threshold=5.0")
        assert resp.status_code == 200
        body = resp.json()
        names = [n["properties"].get("name") for n in body["nodes"]]
        assert "cold" not in names


# ---------------------------------------------------------------------------
# Internal database counts  GET /api/graphs/internal/database/counts/{name}
# ---------------------------------------------------------------------------


class TestInternalCounts:
    async def test_db_not_found_returns_error_status(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/internal/database/counts/missing")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "error"

    async def test_db_found_returns_counts(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(
                return_value=_analysis_response(
                    {
                        "container_name": "testdb",
                        "exists": True,
                        "node_count": 1,
                        "edge_count": 0,
                    }
                )
            ),
        ):
            resp = await client.get("/api/graphs/internal/database/counts/testdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["node_count"] == 1


# ---------------------------------------------------------------------------
# Debug database verification  GET /api/graphs/debug/database/verify/{name}
# ---------------------------------------------------------------------------


class TestDebugVerification:
    async def test_graph_exists(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response({"node_count": 1, "edge_count": 0})),
        ):
            resp = await client.get("/api/graphs/debug/database/verify/testdb")
        assert resp.status_code == 200
        body = resp.json()
        assert body["file_exists"] is True
        assert body["database_accessible_and_valid_schema"] is True
        assert body["node_count"] == 1

    async def test_graph_not_exists(self, client):
        with patch(
            "routers.graphs._analysis_get",
            new=AsyncMock(return_value=_analysis_response(status_code=404)),
        ):
            resp = await client.get("/api/graphs/debug/database/verify/nonexistent")
        assert resp.status_code == 200
        body = resp.json()
        assert body["file_exists"] is False
