"""Tests for graph lifecycle endpoints owned by the gateway.

Orphan cleanup now sources the graph inventory from coding-analysis over HTTP
(``_list_graphs_from_analysis``) and deletes via the analysis proxy, so these
tests mock those helpers rather than seeding local SQLite. The tenant-scoping
semantics are unchanged: only the current tenant's graphs are in scope, and
tenantless graphs are skipped only when auth is enforced.
"""

from __future__ import annotations

import sys
from importlib import import_module, reload
from typing import Any

import httpx
import pytest


def _import_graphs_module():
    if "routers.graphs" in sys.modules:
        return reload(sys.modules["routers.graphs"])
    return import_module("routers.graphs")


def _delete_response(nodes: int = 1, edges: int = 0) -> httpx.Response:
    request = httpx.Request("DELETE", "http://coding-analysis/api/analysis/graphs/x")
    return httpx.Response(
        200,
        json={
            "container_name": "x",
            "deleted": True,
            "nodes_deleted": nodes,
            "edges_deleted": edges,
        },
        request=request,
    )


def _graph(name: str, tenant_id: str | None) -> dict[str, Any]:
    return {"container_name": name, "tenant_id": tenant_id, "node_count": 1, "edge_count": 0}


@pytest.mark.asyncio
async def test_cleanup_orphaned_graphs_dry_run(monkeypatch):
    """Dry-run cleanup should only report orphaned graphs for the current tenant."""
    graphs = _import_graphs_module()

    async def _mock_active_container_names(request, tenant_id, settings=None):
        return {"live"}

    async def _mock_list_graphs(tenant_id, settings=None):
        return [
            _graph("orphan", "tenant-a"),
            _graph("live", "tenant-a"),
            _graph("foreign", "tenant-b"),
            _graph("tenantless", None),
        ]

    monkeypatch.setattr(graphs, "_list_active_container_names", _mock_active_container_names)
    monkeypatch.setattr(graphs, "_list_graphs_from_analysis", _mock_list_graphs)

    payload = await graphs.cleanup_orphaned_graphs(
        request=object(),
        dry_run=True,
        confirm=False,
        tenant_id="tenant-a",
    )

    # foreign is another tenant's graph (skipped); live is active; orphan and
    # tenantless (auth disabled in tests) are reported.
    assert sorted(payload["orphaned_containers"]) == ["orphan", "tenantless"]
    assert payload["cleaned_count"] == 0
    assert payload["dry_run"] is True
    assert payload["confirm"] is False


@pytest.mark.asyncio
async def test_cleanup_orphaned_graphs_confirmed_delete(monkeypatch):
    """Confirmed cleanup should delete orphaned graphs and report the count."""
    graphs = _import_graphs_module()

    async def _mock_active_container_names(request, tenant_id, settings=None):
        return set()

    async def _mock_list_graphs(tenant_id, settings=None):
        return [_graph("orphan", "tenant-a")]

    async def _mock_analysis_delete(path, tenant_id, settings=None):
        return _delete_response()

    monkeypatch.setattr(graphs, "_list_active_container_names", _mock_active_container_names)
    monkeypatch.setattr(graphs, "_list_graphs_from_analysis", _mock_list_graphs)
    monkeypatch.setattr(graphs, "_analysis_delete", _mock_analysis_delete)

    payload = await graphs.cleanup_orphaned_graphs(
        request=object(),
        dry_run=False,
        confirm=True,
        tenant_id="tenant-a",
    )

    assert payload["orphaned_containers"] == ["orphan"]
    assert payload["cleaned_count"] == 1
    assert payload["deletion_errors"] == []


@pytest.mark.asyncio
async def test_cleanup_orphaned_graphs_requires_confirm_for_delete(monkeypatch):
    """Deletion should not occur unless confirm=true is provided."""
    graphs = _import_graphs_module()

    async def _mock_active_container_names(request, tenant_id, settings=None):
        return set()

    async def _mock_list_graphs(tenant_id, settings=None):
        return [_graph("orphan", "tenant-a")]

    deleted: list[str] = []

    async def _mock_analysis_delete(path, tenant_id, settings=None):
        deleted.append(path)
        return _delete_response()

    monkeypatch.setattr(graphs, "_list_active_container_names", _mock_active_container_names)
    monkeypatch.setattr(graphs, "_list_graphs_from_analysis", _mock_list_graphs)
    monkeypatch.setattr(graphs, "_analysis_delete", _mock_analysis_delete)

    payload = await graphs.cleanup_orphaned_graphs(
        request=object(),
        dry_run=False,
        confirm=False,
        tenant_id="tenant-a",
    )

    assert payload["cleaned_count"] == 0
    assert "Deletion not performed" in payload["message"]
    assert deleted == []
