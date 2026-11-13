"""Tests for services/visualization.py – DependencyVisualizer."""

from __future__ import annotations

import json

import pytest

from services.visualization import (
    CoreCodeEntity,
    DependencyVisualizer,
    EdgeType,
    EntityRelation,
    NodeType,
    ParseResult,
    VisualizationEdge,
    VisualizationGraph,
    VisualizationNode,
    VisualizationType,
    get_dependency_visualizer,
)


@pytest.fixture
def visualizer():
    return DependencyVisualizer()


def _make_entity(id="e1", name="MyClass", type="class", language="python", file_path="src/main.py"):
    return CoreCodeEntity(id=id, name=name, type=type, language=language, file_path=file_path)


def _make_relation(source_id="e1", target_id="e2", type="calls"):
    return EntityRelation(source_id=source_id, target_id=target_id, type=type)


def _make_parse_result(
    success=True,
    file_path="src/main.py",
    language="python",
    entities=None,
    relationships=None,
    lines_of_code=100,
    complexity_score=5.0,
):
    return ParseResult(
        success=success,
        file_path=file_path,
        language=language,
        entities=entities or [],
        relationships=relationships or [],
        lines_of_code=lines_of_code,
        complexity_score=complexity_score,
    )


# ---------------------------------------------------------------------------
# Empty graph
# ---------------------------------------------------------------------------


class TestEmptyGraph:
    async def test_empty_parse_results(self, visualizer):
        graph = await visualizer.create_dependency_graph([])
        assert isinstance(graph, VisualizationGraph)
        assert len(graph.nodes) == 0
        assert len(graph.edges) == 0
        assert len(graph.languages) == 0
        assert graph.statistics["total_nodes"] == 0
        assert graph.statistics["total_edges"] == 0

    async def test_failed_parse_results_ignored(self, visualizer):
        pr = _make_parse_result(success=False)
        graph = await visualizer.create_dependency_graph([pr])
        assert len(graph.nodes) == 0


# ---------------------------------------------------------------------------
# Node transformation
# ---------------------------------------------------------------------------


class TestNodeTransform:
    async def test_file_node_created(self, visualizer):
        pr = _make_parse_result()
        graph = await visualizer.create_dependency_graph([pr])
        file_nodes = [n for n in graph.nodes if n.type == NodeType.FILE]
        assert len(file_nodes) == 1
        assert file_nodes[0].id == "file:src/main.py"
        assert file_nodes[0].language == "python"

    async def test_entity_node_created(self, visualizer):
        entity = _make_entity(type="class")
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        entity_nodes = [n for n in graph.nodes if n.type == NodeType.CLASS]
        assert len(entity_nodes) == 1
        assert entity_nodes[0].label == "MyClass"

    async def test_function_entity_type(self, visualizer):
        entity = _make_entity(id="f1", type="function")
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        fn_nodes = [n for n in graph.nodes if n.type == NodeType.FUNCTION]
        assert len(fn_nodes) == 1

    async def test_interface_entity_type(self, visualizer):
        entity = _make_entity(id="i1", type="interface")
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        iface_nodes = [n for n in graph.nodes if n.type == NodeType.INTERFACE]
        assert len(iface_nodes) == 1

    async def test_module_entity_type(self, visualizer):
        entity = _make_entity(id="m1", type="module")
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        mod_nodes = [n for n in graph.nodes if n.type == NodeType.MODULE]
        assert len(mod_nodes) == 1

    async def test_namespace_entity_type(self, visualizer):
        entity = _make_entity(id="ns1", type="namespace")
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        ns_nodes = [n for n in graph.nodes if n.type == NodeType.NAMESPACE]
        assert len(ns_nodes) == 1


# ---------------------------------------------------------------------------
# Edge transformation
# ---------------------------------------------------------------------------


class TestEdgeTransform:
    async def test_containment_edge_created(self, visualizer):
        entity = _make_entity()
        pr = _make_parse_result(entities=[entity])
        graph = await visualizer.create_dependency_graph([pr])
        contain_edges = [e for e in graph.edges if e.type == EdgeType.CONTAINS]
        assert len(contain_edges) == 1

    async def test_relationship_edge_created(self, visualizer):
        e1 = _make_entity(id="e1")
        e2 = _make_entity(id="e2", name="Other")
        rel = _make_relation(source_id="e1", target_id="e2", type="calls")
        pr = _make_parse_result(entities=[e1, e2], relationships=[rel])
        graph = await visualizer.create_dependency_graph([pr])
        call_edges = [e for e in graph.edges if e.type == EdgeType.CALLS]
        assert len(call_edges) == 1

    async def test_imports_edge_type(self, visualizer):
        rel = _make_relation(type="imports")
        edge = await visualizer._create_relationship_edge(rel)
        assert edge.type == EdgeType.IMPORTS

    async def test_extends_edge_type(self, visualizer):
        rel = _make_relation(type="extends")
        edge = await visualizer._create_relationship_edge(rel)
        assert edge.type == EdgeType.EXTENDS

    async def test_implements_edge_type(self, visualizer):
        rel = _make_relation(type="implements")
        edge = await visualizer._create_relationship_edge(rel)
        assert edge.type == EdgeType.IMPLEMENTS

    async def test_unknown_edge_type_defaults_to_depends_on(self, visualizer):
        rel = _make_relation(type="unknown_type")
        edge = await visualizer._create_relationship_edge(rel)
        assert edge.type == EdgeType.DEPENDS_ON


# ---------------------------------------------------------------------------
# Format and layout
# ---------------------------------------------------------------------------


class TestFormatAndLayout:
    def test_layout_options_dependency_graph(self, visualizer):
        opts = visualizer._get_layout_options(VisualizationType.DEPENDENCY_GRAPH, {})
        assert opts["algorithm"] == "force-directed"

    def test_layout_options_component_diagram(self, visualizer):
        opts = visualizer._get_layout_options(VisualizationType.COMPONENT_DIAGRAM, {})
        assert opts["algorithm"] == "hierarchical"
        assert opts["direction"] == "LR"

    def test_layout_options_layer_architecture(self, visualizer):
        opts = visualizer._get_layout_options(VisualizationType.LAYER_ARCHITECTURE, {})
        assert opts["direction"] == "TB"

    def test_layout_custom_override(self, visualizer):
        opts = visualizer._get_layout_options(
            VisualizationType.DEPENDENCY_GRAPH, {"layout": {"spacing": 200}}
        )
        assert opts["spacing"] == 200

    def test_edge_color_mapping(self, visualizer):
        assert visualizer._get_edge_color(EdgeType.CALLS) == "#FF6347"
        assert visualizer._get_edge_color(EdgeType.IMPORTS) == "#999999"

    def test_edge_style_mapping(self, visualizer):
        assert visualizer._get_edge_style(EdgeType.IMPORTS) == "dashed"
        assert visualizer._get_edge_style(EdgeType.CALLS) == "solid"
        assert visualizer._get_edge_style(EdgeType.FFI_CALL) == "bold"

    def test_mermaid_shape(self, visualizer):
        assert visualizer._get_mermaid_shape("box") == ("[", "]")
        assert visualizer._get_mermaid_shape("circle") == ("((", "))")
        assert visualizer._get_mermaid_shape("unknown") == ("[", "]")

    def test_mermaid_arrow(self, visualizer):
        assert visualizer._get_mermaid_arrow("solid") == "-->"
        assert visualizer._get_mermaid_arrow("dashed") == "-.->"
        assert visualizer._get_mermaid_arrow("bold") == "==>"
        assert visualizer._get_mermaid_arrow("unknown") == "-->"

    def test_language_colors(self, visualizer):
        assert visualizer.LANGUAGE_COLORS["python"] == "#3776AB"
        assert visualizer.LANGUAGE_COLORS["java"] == "#ED8B00"

    def test_cross_language_color(self, visualizer):
        color = visualizer._get_cross_language_color("python", "rust")
        assert color.startswith("hsl(")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    async def test_export_cytoscape(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["cytoscape"])
        assert "cytoscape" in exports
        data = json.loads(exports["cytoscape"])
        assert "elements" in data
        assert "nodes" in data["elements"]
        assert "edges" in data["elements"]

    async def test_export_d3(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["d3"])
        data = json.loads(exports["d3"])
        assert "nodes" in data
        assert "links" in data

    async def test_export_graphviz(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["graphviz"])
        dot = exports["graphviz"]
        assert "digraph" in dot
        assert "->" in dot

    async def test_export_mermaid(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["mermaid"])
        mermaid = exports["mermaid"]
        assert "graph LR" in mermaid

    async def test_export_json(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["json"])
        data = json.loads(exports["json"])
        assert "nodes" in data
        assert "edges" in data
        assert "languages" in data

    async def test_export_default_formats(self, visualizer):
        pr = _make_parse_result(entities=[_make_entity()])
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph)
        assert "cytoscape" in exports
        assert "d3" in exports
        assert "graphviz" in exports
        assert "mermaid" in exports

    async def test_export_unknown_format_skipped(self, visualizer):
        pr = _make_parse_result()
        graph = await visualizer.create_dependency_graph([pr])
        exports = await visualizer.export_to_formats(graph, ["nonexistent"])
        assert "nonexistent" not in exports


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


class TestFiltering:
    async def test_filter_by_language(self, visualizer):
        py = _make_parse_result(
            language="python",
            entities=[_make_entity(id="py1", language="python")],
        )
        js = _make_parse_result(
            file_path="src/app.js",
            language="javascript",
            entities=[_make_entity(id="js1", language="javascript", file_path="src/app.js")],
        )
        graph = await visualizer.create_dependency_graph(
            [py, js], options={"languages": ["python"]}
        )
        for node in graph.nodes:
            assert node.language == "python"

    async def test_filter_by_min_complexity(self, visualizer):
        e1 = _make_entity(id="e1")
        pr = _make_parse_result(entities=[e1], complexity_score=20.0)
        graph = await visualizer.create_dependency_graph([pr], options={"min_complexity": 10})
        # File node has complexity=20.0 so passes; entity nodes have complexity=0
        # Only high-complexity nodes remain
        for node in graph.nodes:
            assert node.complexity >= 10

    async def test_filter_by_node_type(self, visualizer):
        e1 = _make_entity(id="e1", type="class")
        pr = _make_parse_result(entities=[e1])
        graph = await visualizer.create_dependency_graph(
            [pr], options={"node_types": [NodeType.CLASS]}
        )
        for node in graph.nodes:
            assert node.type == NodeType.CLASS

    async def test_group_by_language(self, visualizer):
        e1 = _make_entity(id="e1")
        pr = _make_parse_result(entities=[e1])
        graph = await visualizer.create_dependency_graph([pr], options={"group_by_language": True})
        lang_nodes = [n for n in graph.nodes if n.type == NodeType.LANGUAGE]
        assert len(lang_nodes) >= 1


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


class TestStatistics:
    async def test_complexity_distribution(self, visualizer):
        nodes = [
            VisualizationNode(id="1", label="a", type=NodeType.CLASS, language="py", complexity=3),
            VisualizationNode(id="2", label="b", type=NodeType.CLASS, language="py", complexity=10),
            VisualizationNode(id="3", label="c", type=NodeType.CLASS, language="py", complexity=20),
        ]
        dist = await visualizer._calculate_complexity_distribution(nodes)
        assert dist["low"] == 1
        assert dist["medium"] == 1
        assert dist["high"] == 1

    async def test_dependency_depth(self, visualizer):
        nodes = [
            VisualizationNode(id="1", label="a", type=NodeType.CLASS, language="py"),
            VisualizationNode(id="2", label="b", type=NodeType.CLASS, language="py"),
        ]
        edges = [
            VisualizationEdge(id="e1", source="1", target="2", type=EdgeType.CALLS),
        ]
        depth = await visualizer._calculate_dependency_depth(nodes, edges)
        assert isinstance(depth, int)


# ---------------------------------------------------------------------------
# Graph ID generation
# ---------------------------------------------------------------------------


class TestGraphId:
    def test_graph_id_deterministic(self, visualizer):
        pr = _make_parse_result()
        id1 = visualizer._generate_graph_id([pr], VisualizationType.DEPENDENCY_GRAPH, {})
        id2 = visualizer._generate_graph_id([pr], VisualizationType.DEPENDENCY_GRAPH, {})
        assert id1 == id2
        assert len(id1) == 12

    def test_different_type_different_id(self, visualizer):
        pr = _make_parse_result()
        id1 = visualizer._generate_graph_id([pr], VisualizationType.DEPENDENCY_GRAPH, {})
        id2 = visualizer._generate_graph_id([pr], VisualizationType.CALL_FLOW, {})
        assert id1 != id2


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_dependency_visualizer_returns_instance(self):
        v = get_dependency_visualizer()
        assert isinstance(v, DependencyVisualizer)

    def test_get_dependency_visualizer_same_instance(self):
        v1 = get_dependency_visualizer()
        v2 = get_dependency_visualizer()
        assert v1 is v2


# ---------------------------------------------------------------------------
# Edge weight
# ---------------------------------------------------------------------------


class TestEdgeWeight:
    def test_default_weight(self, visualizer):
        rel = _make_relation(type="calls")
        w = visualizer._calculate_edge_weight(rel)
        assert w == 1.0

    def test_extends_weight_boosted(self, visualizer):
        rel = EntityRelation(
            source_id="a",
            target_id="b",
            type="extends",
            properties={"key": "val"},
        )
        w = visualizer._calculate_edge_weight(rel)
        assert w == 2.0

    def test_ffi_call_weight_boosted(self, visualizer):
        rel = EntityRelation(
            source_id="a",
            target_id="b",
            type="ffi_call",
            properties={"key": "val"},
        )
        w = visualizer._calculate_edge_weight(rel)
        assert w == 3.0


# ---------------------------------------------------------------------------
# Dataclass defaults
# ---------------------------------------------------------------------------


class TestDataclassDefaults:
    def test_visualization_node_post_init(self):
        node = VisualizationNode(id="1", label="x", type=NodeType.CLASS, language="py")
        assert node.properties == {}

    def test_visualization_edge_post_init(self):
        edge = VisualizationEdge(id="1", source="a", target="b", type=EdgeType.CALLS)
        assert edge.properties == {}

    def test_visualization_graph_post_init(self):
        from datetime import UTC, datetime

        graph = VisualizationGraph(
            id="g1",
            title="Test",
            type=VisualizationType.DEPENDENCY_GRAPH,
            nodes=[],
            edges=[],
            languages=set(),
            created_at=datetime.now(UTC),
            statistics={},
        )
        assert graph.layout_options == {}
