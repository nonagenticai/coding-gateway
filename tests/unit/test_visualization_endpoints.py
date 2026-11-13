"""Tests for the visualization router — capabilities, examples, create-graph,
and helper functions.

All endpoint tests send real HTTP requests via the ``client`` fixture.
Helper function tests call the functions directly.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.visualization import EdgeType, NodeType, VisualizationType


@pytest.fixture
def viz_root(tmp_path, monkeypatch):
    """Configure the visualization workspace root to ``tmp_path``.

    The endpoint accepts a file path ONLY when it resolves under this root
    (positive containment allowlist). Files written under ``tmp_path`` therefore
    validate; anything outside is rejected. Yields ``tmp_path`` so tests use it
    as both the root and the place to write fixtures.
    """
    monkeypatch.setenv("VISUALIZATION_WORKSPACE_ROOT", str(tmp_path))
    from config.settings import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# GET /api/visualization/capabilities
# ---------------------------------------------------------------------------


class TestVisualizationCapabilities:
    async def test_returns_all_capability_fields(self, client):
        resp = await client.get("/api/visualization/capabilities")
        assert resp.status_code == 200
        body = resp.json()
        # visualization_types should contain all enum values
        for vt in VisualizationType:
            assert vt.value in body["visualization_types"]
        # node_types and edge_types
        for nt in NodeType:
            assert nt.value in body["node_types"]
        for et in EdgeType:
            assert et.value in body["edge_types"]
        # export formats
        assert "cytoscape" in body["export_formats"]
        assert "mermaid" in body["export_formats"]
        # layout algorithms
        assert "force-directed" in body["layout_algorithms"]
        # supported languages
        assert "python" in body["supported_languages"]
        assert "rust" in body["supported_languages"]


# ---------------------------------------------------------------------------
# GET /api/visualization/examples
# ---------------------------------------------------------------------------


class TestVisualizationExamples:
    async def test_returns_examples_and_tips(self, client):
        resp = await client.get("/api/visualization/examples")
        assert resp.status_code == 200
        body = resp.json()
        assert "examples" in body
        assert "best_practices" in body
        assert "tips" in body
        assert len(body["examples"]) >= 3
        # Each example should have description, request, use_case keys
        for name, example in body["examples"].items():
            assert "description" in example, f"Missing description in {name}"
            assert "request" in example, f"Missing request in {name}"
            assert "use_case" in example, f"Missing use_case in {name}"


# ---------------------------------------------------------------------------
# POST /api/visualization/create-graph
# ---------------------------------------------------------------------------


def _mock_graph():
    """Build a mock graph with real enum types so serialization works."""
    node = MagicMock()
    node.id = "node-1"
    node.label = "main.py"
    node.type = NodeType.FILE
    node.language = "python"
    node.file_path = "/src/main.py"
    node.size = 100
    node.color = "#3572A5"
    node.shape = "box"
    node.group = "python"
    node.complexity = 5.0
    node.importance = 0.8
    node.properties = {}

    edge = MagicMock()
    edge.id = "edge-1"
    edge.source = "node-1"
    edge.target = "node-2"
    edge.type = EdgeType.IMPORTS
    edge.label = "imports"
    edge.weight = 1.0
    edge.color = "#999"
    edge.style = "solid"
    edge.properties = {}

    graph = MagicMock()
    graph.id = "viz-abc123"
    graph.title = "Test Visualization"
    graph.type = VisualizationType.DEPENDENCY_GRAPH
    graph.nodes = [node]
    graph.edges = [edge]
    graph.languages = {"python"}
    graph.statistics = {"total_nodes": 1, "total_edges": 1}
    graph.layout_options = {"algorithm": "force-directed"}
    return graph


class TestCreateVisualization:
    async def test_success_returns_graph(self, client, viz_root):
        test_file = viz_root / "main.py"
        test_file.write_text("def hello(): pass\n")

        mock_visualizer = AsyncMock()
        mock_visualizer.create_dependency_graph = AsyncMock(return_value=_mock_graph())
        mock_visualizer.export_to_formats = AsyncMock(
            return_value={"cytoscape": '{"nodes": []}', "json": "{}"}
        )

        with patch(
            "routers.visualization.get_dependency_visualizer",
            return_value=mock_visualizer,
        ):
            resp = await client.post(
                "/api/visualization/create-graph",
                json={
                    "file_paths": [str(test_file)],
                    "visualization_type": "dependency_graph",
                    "export_formats": ["cytoscape", "json"],
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        result = body["result"]
        assert result["id"] == "viz-abc123"
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["type"] == "file"
        assert result["nodes"][0]["language"] == "python"
        assert len(result["edges"]) == 1
        assert result["edges"][0]["type"] == "imports"
        assert "cytoscape" in result["exports"]

    async def test_empty_file_paths_returns_400(self, client):
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": [],
                "visualization_type": "dependency_graph",
            },
        )
        assert resp.status_code == 400

    async def test_invalid_visualization_type_returns_400(self, client, tmp_path):
        test_file = tmp_path / "main.py"
        test_file.write_text("x = 1\n")
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": [str(test_file)],
                "visualization_type": "invalid_type",
            },
        )
        assert resp.status_code == 400

    async def test_nonexistent_files_returns_404(self, client):
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": ["/nonexistent/path/to/file.py"],
                "visualization_type": "dependency_graph",
            },
        )
        assert resp.status_code == 404

    async def test_all_invalid_files_returns_404(self, client):
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": ["/nonexistent/a.py", "/nonexistent/b.rs"],
                "visualization_type": "dependency_graph",
            },
        )
        assert resp.status_code == 404

    async def test_visualizer_error_returns_500(self, client, viz_root):
        test_file = viz_root / "main.py"
        test_file.write_text("def hello(): pass\n")

        mock_visualizer = AsyncMock()
        mock_visualizer.create_dependency_graph = AsyncMock(
            side_effect=ValueError("rendering error")
        )

        with patch(
            "routers.visualization.get_dependency_visualizer",
            return_value=mock_visualizer,
        ):
            resp = await client.post(
                "/api/visualization/create-graph",
                json={
                    "file_paths": [str(test_file)],
                    "visualization_type": "dependency_graph",
                },
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Helper function unit tests
# ---------------------------------------------------------------------------


class TestValidateVisualizationType:
    @pytest.mark.parametrize(
        "vis_type",
        [vt.value for vt in VisualizationType],
    )
    def test_valid_types_accepted(self, vis_type):
        from routers.visualization import _validate_visualization_type

        result = _validate_visualization_type(vis_type)
        assert result.value == vis_type

    def test_invalid_type_raises_400(self):
        from fastapi import HTTPException

        from routers.visualization import _validate_visualization_type

        with pytest.raises(HTTPException) as exc_info:
            _validate_visualization_type("bogus_type")
        assert exc_info.value.status_code == 400
        assert "invalid visualization type" in exc_info.value.detail.lower()


class TestValidateFilePaths:
    """Containment-allowlist semantics (S7): a path is accepted ONLY when it
    resolves under the configured ``visualization_workspace_root``; with no root
    configured (the default) every path is rejected → the oracle is closed."""

    def test_nonexistent_files_raise_404(self, viz_root):
        from fastapi import HTTPException

        from routers.visualization import _validate_file_paths

        # Inside the root but nonexistent → rejected.
        with pytest.raises(HTTPException) as exc_info:
            _validate_file_paths([str(viz_root / "nope.py")])
        assert exc_info.value.status_code == 404

    def test_existing_file_returned(self, viz_root):
        from routers.visualization import _validate_file_paths

        f = viz_root / "test.py"
        f.write_text("x = 1\n")
        result = _validate_file_paths([str(f)])
        assert len(result) == 1
        assert result[0].name == "test.py"

    def test_no_root_configured_rejects_all(self, tmp_path):
        """With the default (unset) root, even a real, existing file that used to
        pass the old deny-list (e.g. under /tmp) is now rejected — the oracle is
        closed. No VISUALIZATION_WORKSPACE_ROOT is set here (no viz_root fixture)."""
        from fastapi import HTTPException

        from routers.visualization import _validate_file_paths

        f = tmp_path / "exists.py"
        f.write_text("z = 3\n")
        with pytest.raises(HTTPException) as exc_info:
            _validate_file_paths([str(f)])
        assert exc_info.value.status_code == 404

    def test_existing_file_outside_root_rejected(self, viz_root, tmp_path_factory):
        """The core oracle-closing property: a file that EXISTS on disk but
        resolves OUTSIDE the configured root is rejected, so a caller cannot
        probe existence/line-count of arbitrary pod paths."""
        from fastapi import HTTPException

        from routers.visualization import _validate_file_paths

        outside = tmp_path_factory.mktemp("outside") / "secret.py"
        outside.write_text("api_key = 1\n")
        assert outside.exists()
        with pytest.raises(HTTPException) as exc_info:
            _validate_file_paths([str(outside)])
        assert exc_info.value.status_code == 404

    def test_traversal_out_of_root_rejected(self, viz_root):
        """A ../ traversal that escapes the root resolves outside it → rejected."""
        from fastapi import HTTPException

        from routers.visualization import _validate_file_paths

        with pytest.raises(HTTPException) as exc_info:
            _validate_file_paths([str(viz_root / ".." / "etc" / "passwd")])
        assert exc_info.value.status_code == 404

    def test_system_paths_rejected(self, viz_root):
        """System paths like /etc/hostname resolve outside the root → rejected."""
        from fastapi import HTTPException

        from routers.visualization import _validate_file_paths

        with pytest.raises(HTTPException) as exc_info:
            _validate_file_paths(["/etc/hostname"])
        assert exc_info.value.status_code == 404

    def test_mixed_inside_outside_keeps_only_contained(self, viz_root):
        from routers.visualization import _validate_file_paths

        f = viz_root / "real.py"
        f.write_text("y = 2\n")
        result = _validate_file_paths(["/etc/hostname", str(viz_root / "nope.py"), str(f)])
        assert len(result) == 1
        assert result[0].name == "real.py"


# ---------------------------------------------------------------------------
# POST /api/visualization/create-graph validation
# ---------------------------------------------------------------------------


class TestCreateGraphValidation:
    """Test input validation for the create-graph endpoint."""

    async def test_empty_file_paths_returns_400(self, client):
        """Empty file_paths list returns 400."""
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": [],
                "visualization_type": "dependency_graph",
            },
        )
        assert resp.status_code == 400
        assert "file path" in resp.json()["detail"].lower()

    async def test_invalid_visualization_type_returns_400(self, client):
        """Invalid visualization type returns 400 with valid types listed."""
        resp = await client.post(
            "/api/visualization/create-graph",
            json={
                "file_paths": ["/some/file.py"],
                "visualization_type": "invalid_type",
            },
        )
        assert resp.status_code == 400
        assert "invalid visualization type" in resp.json()["detail"].lower()
