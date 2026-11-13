"""
Cross-Language Dependency Visualization Service

Creates visual representations of dependencies and relationships between
code entities across multiple programming languages.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Gateway-local stub models for the visualization service.
#
# The gateway's models.schemas uses field names like ``entity_id``,
# ``entity_type``, and ``relation_type`` which differ from the simple
# ``id`` / ``type`` / ``source_id`` / ``target_id`` names the
# visualizer expects.  These lightweight stubs bridge that gap so the
# visualizer can work with data from any parser without importing from
# coding-analysis.
# ---------------------------------------------------------------------------


class CoreCodeEntity:
    """Gateway-local entity stub for the visualization service.

    Field names (``id``, ``name``, ``type``, ``language``, etc.) are
    intentionally different from the gateway ``schemas.py`` Pydantic
    models (which use ``entity_id``, ``entity_type``, ...).  This stub
    exists so the visualizer can consume parser output directly without
    depending on coding-analysis types.
    """

    id: str = ""
    name: str = ""
    type: str = ""
    language: str = ""
    file_path: str = ""
    line_number: int = 0
    is_abstract: bool = False
    is_static: bool = False
    is_async: bool = False
    visibility: str = "public"

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class EntityRelation:
    """Gateway-local relation stub for the visualization service.

    Field names (``source_id``, ``target_id``, ``type``) differ from
    the gateway ``schemas.py`` relation models.  This stub provides
    the minimal interface the visualizer needs to build edges without
    importing from coding-analysis.
    """

    source_id: str = ""
    target_id: str = ""
    type: str = ""
    weight: float = 1.0
    properties: dict[str, Any] | None = None

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


class ParseResult:
    """Gateway-local ParseResult stub for the visualization service.

    ``ParseResult`` is a coding-analysis type.  This stub carries just
    enough state (``success``, ``file_path``, ``language``, entities,
    relationships, etc.) for the visualizer to build graphs.  It will be
    replaced once inter-service shared types are available.
    """

    success: bool = False
    file_path: str = ""
    language: str = ""
    lines_of_code: int = 0
    complexity_score: float = 0.0

    def __init__(self, **kwargs: Any) -> None:
        # Set non-mutable defaults
        self.success = False
        self.file_path = ""
        self.language = ""
        self.lines_of_code = 0
        self.complexity_score = 0.0
        self.entities: list = []
        self.relationships: list = []
        # Override with kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


logger = logging.getLogger(__name__)


class VisualizationType(StrEnum):
    """Types of visualizations available."""

    DEPENDENCY_GRAPH = "dependency_graph"
    COMPONENT_DIAGRAM = "component_diagram"
    LAYER_ARCHITECTURE = "layer_architecture"
    CALL_FLOW = "call_flow"
    MODULE_STRUCTURE = "module_structure"
    CROSS_LANGUAGE_MAP = "cross_language_map"


class NodeType(StrEnum):
    """Types of nodes in visualization."""

    FILE = "file"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION = "function"
    MODULE = "module"
    PACKAGE = "package"
    NAMESPACE = "namespace"
    LANGUAGE = "language"


class EdgeType(StrEnum):
    """Types of edges in visualization."""

    IMPORTS = "imports"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    CALLS = "calls"
    DEPENDS_ON = "depends_on"
    CONTAINS = "contains"
    FFI_CALL = "ffi_call"
    API_CALL = "api_call"


@dataclass
class VisualizationNode:
    """A node in the dependency visualization."""

    id: str
    label: str
    type: NodeType
    language: str
    file_path: str | None = None
    size: int = 1
    color: str | None = None
    shape: str = "box"
    group: str | None = None

    # Additional metadata
    properties: dict[str, Any] | None = None
    complexity: float = 0.0
    importance: float = 0.0

    def __post_init__(self) -> None:
        if self.properties is None:
            self.properties = {}


# Type aliases for backward compatibility - classes are defined above and below


@dataclass
class VisualizationEdge:
    """An edge in the dependency visualization."""

    id: str
    source: str
    target: str
    type: EdgeType
    label: str | None = None
    weight: float = 1.0
    color: str | None = None
    style: str = "solid"

    # Additional metadata
    properties: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.properties is None:
            self.properties = {}


# Type alias for backward compatibility
DependencyVisualizerServiceVisualizationEdge = VisualizationEdge


@dataclass
class VisualizationGraph:
    """Complete visualization graph."""

    id: str
    title: str
    type: VisualizationType
    nodes: list[VisualizationNode]
    edges: list[VisualizationEdge]

    # Metadata
    languages: set[str]
    created_at: datetime
    statistics: dict[str, Any]
    layout_options: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.layout_options is None:
            self.layout_options = {}


class DependencyVisualizer:
    """Service for creating cross-language dependency visualizations."""

    # Language-specific colors
    LANGUAGE_COLORS = {
        "java": "#ED8B00",
        "rust": "#CE422B",
        "go": "#00ADD8",
        "python": "#3776AB",
        "javascript": "#F7DF1E",
        "typescript": "#3178C6",
        "csharp": "#239120",
        "cpp": "#00599C",
        "c": "#A8B9CC",
    }

    # Node type shapes
    NODE_SHAPES = {
        NodeType.FILE: "ellipse",
        NodeType.CLASS: "box",
        NodeType.INTERFACE: "diamond",
        NodeType.FUNCTION: "circle",
        NodeType.MODULE: "hexagon",
        NodeType.PACKAGE: "folder",
        NodeType.NAMESPACE: "triangle",
        NodeType.LANGUAGE: "star",
    }

    def __init__(self) -> None:
        """Initialize the dependency visualizer."""
        self.node_cache: dict[str, VisualizationNode] = {}
        self.edge_cache: dict[str, VisualizationEdge] = {}

    async def create_dependency_graph(
        self,
        parse_results: list[ParseResult],
        visualization_type: VisualizationType = VisualizationType.DEPENDENCY_GRAPH,
        options: dict[str, Any] | None = None,
    ) -> VisualizationGraph:
        """Create a dependency visualization graph."""

        if options is None:
            options = {}

        # Clear caches
        self.node_cache.clear()
        self.edge_cache.clear()

        # Extract nodes and edges
        nodes = []
        edges = []
        languages = set()

        # Create nodes for all entities
        for parse_result in parse_results:
            if not parse_result.success:
                continue

            languages.add(parse_result.language)

            # Create file node
            file_node = await self._create_file_node(parse_result)
            if file_node.id not in self.node_cache:
                nodes.append(file_node)
                self.node_cache[file_node.id] = file_node

            # Create entity nodes
            for entity in parse_result.entities:
                entity_node = await self._create_entity_node(entity, parse_result)
                if entity_node.id not in self.node_cache:
                    nodes.append(entity_node)
                    self.node_cache[entity_node.id] = entity_node

                # Create containment edge (file contains entity)
                containment_edge = VisualizationEdge(
                    id=f"{file_node.id}-contains-{entity_node.id}",
                    source=file_node.id,
                    target=entity_node.id,
                    type=EdgeType.CONTAINS,
                    style="dashed",
                    color="#999999",
                )
                if containment_edge.id not in self.edge_cache:
                    edges.append(containment_edge)
                    self.edge_cache[containment_edge.id] = containment_edge

            # Create relationship edges
            for relation in parse_result.relationships:
                relation_edge = await self._create_relationship_edge(relation)
                if relation_edge and relation_edge.id not in self.edge_cache:
                    edges.append(relation_edge)
                    self.edge_cache[relation_edge.id] = relation_edge

        # Create cross-language edges
        cross_lang_edges = await self._create_cross_language_edges(parse_results)
        edges.extend(cross_lang_edges)

        # Apply filtering and grouping based on options
        nodes, edges = await self._apply_visualization_options(nodes, edges, options)

        # Calculate statistics
        statistics = {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "languages_count": len(languages),
            "cross_language_edges": len(
                [e for e in edges if e.type in [EdgeType.FFI_CALL, EdgeType.API_CALL]]
            ),
            "complexity_distribution": await self._calculate_complexity_distribution(nodes),
            "dependency_depth": await self._calculate_dependency_depth(nodes, edges),
        }

        # Generate unique ID
        graph_id = self._generate_graph_id(parse_results, visualization_type, options)

        return VisualizationGraph(
            id=graph_id,
            title=f"Dependency Graph - {visualization_type.value}",
            type=visualization_type,
            nodes=nodes,
            edges=edges,
            languages=languages,
            created_at=datetime.now(UTC),
            statistics=statistics,
            layout_options=self._get_layout_options(visualization_type, options),
        )

    async def export_to_formats(
        self, graph: VisualizationGraph, formats: list[str] | None = None
    ) -> dict[str, str]:
        """Export visualization to various formats."""

        if formats is None:
            formats = ["cytoscape", "d3", "graphviz", "mermaid"]

        exports = {}

        for format_name in formats:
            if format_name == "cytoscape":
                exports[format_name] = await self._export_to_cytoscape(graph)
            elif format_name == "d3":
                exports[format_name] = await self._export_to_d3(graph)
            elif format_name == "graphviz":
                exports[format_name] = await self._export_to_graphviz(graph)
            elif format_name == "mermaid":
                exports[format_name] = await self._export_to_mermaid(graph)
            elif format_name == "json":
                exports[format_name] = await self._export_to_json(graph)

        return exports

    async def _create_file_node(self, parse_result: ParseResult) -> VisualizationNode:
        """Create a node representing a file."""

        file_path = Path(parse_result.file_path)

        return VisualizationNode(
            id=f"file:{parse_result.file_path}",
            label=file_path.name,
            type=NodeType.FILE,
            language=parse_result.language,
            file_path=parse_result.file_path,
            size=max(1, parse_result.lines_of_code // 10),
            color=self.LANGUAGE_COLORS.get(parse_result.language, "#808080"),
            shape=self.NODE_SHAPES[NodeType.FILE],
            group=parse_result.language,
            properties={
                "lines_of_code": parse_result.lines_of_code,
                "entity_count": len(parse_result.entities),
                "complexity": parse_result.complexity_score,
            },
            complexity=parse_result.complexity_score or 0.0,
        )

    async def _create_entity_node(
        self, entity: CoreCodeEntity, parse_result: ParseResult
    ) -> VisualizationNode:
        """Create a node representing a code entity."""

        # Determine node type
        node_type = NodeType.CLASS
        if entity.type in ["function", "method"]:
            node_type = NodeType.FUNCTION
        elif entity.type == "interface":
            node_type = NodeType.INTERFACE
        elif entity.type in ["module", "package"]:
            node_type = NodeType.MODULE
        elif entity.type == "namespace":
            node_type = NodeType.NAMESPACE

        # Calculate importance based on relationships
        importance = len(
            [r for r in parse_result.relationships if entity.id in (r.source_id, r.target_id)]
        )

        return VisualizationNode(
            id=entity.id,
            label=entity.name,
            type=node_type,
            language=entity.language,
            file_path=entity.file_path,
            size=max(1, importance),
            color=self.LANGUAGE_COLORS.get(entity.language, "#808080"),
            shape=self.NODE_SHAPES[node_type],
            group=f"{entity.language}_{entity.type}",
            properties={
                "entity_type": entity.type,
                "is_abstract": entity.is_abstract,
                "is_static": entity.is_static,
                "is_async": entity.is_async,
                "visibility": entity.visibility,
                "line_number": entity.line_number,
            },
            importance=importance,
        )

    async def _create_relationship_edge(self, relation: EntityRelation) -> VisualizationEdge | None:
        """Create an edge representing a relationship."""

        # Map relationship types to edge types
        edge_type_map = {
            "imports": EdgeType.IMPORTS,
            "extends": EdgeType.EXTENDS,
            "implements": EdgeType.IMPLEMENTS,
            "calls": EdgeType.CALLS,
            "depends_on": EdgeType.DEPENDS_ON,
            "ffi_call": EdgeType.FFI_CALL,
            "api_endpoint": EdgeType.API_CALL,
        }

        edge_type = edge_type_map.get(relation.type, EdgeType.DEPENDS_ON)

        # Determine edge styling
        color = self._get_edge_color(edge_type)
        style = self._get_edge_style(edge_type)
        weight = self._calculate_edge_weight(relation)

        return VisualizationEdge(
            id=f"{relation.source_id}-{relation.type}-{relation.target_id}",
            source=relation.source_id,
            target=relation.target_id,
            type=edge_type,
            label=relation.type,
            weight=weight,
            color=color,
            style=style,
            properties=relation.properties or {},
        )

    async def _create_cross_language_edges(
        self, parse_results: list[ParseResult]
    ) -> list[VisualizationEdge]:
        """Create edges representing cross-language dependencies."""

        edges = []

        # Group by language
        by_language: dict[str, list[Any]] = {}
        for result in parse_results:
            if result.success:
                by_language[result.language] = by_language.get(result.language, []) + [result]

        # Look for cross-language patterns
        for lang1, results1 in by_language.items():
            for lang2, results2 in by_language.items():
                if lang1 != lang2:
                    cross_edges = await self._detect_cross_language_dependencies(
                        results1, results2, lang1, lang2
                    )
                    edges.extend(cross_edges)

        return edges

    async def _detect_cross_language_dependencies(
        self,
        results1: list[ParseResult],
        results2: list[ParseResult],
        lang1: str,
        lang2: str,
    ) -> list[VisualizationEdge]:
        """Detect dependencies between two languages."""

        edges = []

        # Extract entities from results for cross-language analysis
        entities1 = [entity for result in results1 if result.success for entity in result.entities]
        entities2 = [entity for result in results2 if result.success for entity in result.entities]

        # Common cross-language patterns
        patterns = [
            (["rust", "c"], EdgeType.FFI_CALL, "Rust-C FFI"),
            (["go", "c"], EdgeType.FFI_CALL, "Go CGO"),
            (["java", "c"], EdgeType.FFI_CALL, "Java JNI"),
            (["javascript", "java"], EdgeType.API_CALL, "JS-Java REST API"),
            (["typescript", "java"], EdgeType.API_CALL, "TS-Java REST API"),
            (["python", "rust"], EdgeType.FFI_CALL, "Python-Rust bindings"),
        ]

        for pattern_langs, edge_type, description in patterns:
            if {lang1, lang2} == set(pattern_langs):
                # Create symbolic edge between languages
                edge = VisualizationEdge(
                    id=f"{lang1}-{lang2}-{edge_type.value}",
                    source=f"lang:{lang1}",
                    target=f"lang:{lang2}",
                    type=edge_type,
                    label=description,
                    weight=2.0,
                    color=self._get_cross_language_color(lang1, lang2),
                    style="bold",
                    properties={
                        "cross_language": True,
                        "pattern_type": description,
                        "source_language": lang1,
                        "target_language": lang2,
                        "entity_count_source": len(entities1),
                        "entity_count_target": len(entities2),
                    },
                )
                edges.append(edge)

        return edges

    async def _apply_visualization_options(
        self,
        nodes: list[VisualizationNode],
        edges: list[VisualizationEdge],
        options: dict[str, Any],
    ) -> tuple[list[VisualizationNode], list[VisualizationEdge]]:
        """Apply filtering and grouping options."""

        # Filter by language
        if "languages" in options:
            target_languages = set(options["languages"])
            nodes = [n for n in nodes if n.language in target_languages]
            # Filter edges that connect to remaining nodes
            node_ids = {n.id for n in nodes}
            edges = [e for e in edges if e.source in node_ids and e.target in node_ids]

        # Filter by complexity
        if "min_complexity" in options:
            min_complexity = options["min_complexity"]
            nodes = [n for n in nodes if n.complexity >= min_complexity]

        # Filter by node type
        if "node_types" in options:
            target_types = set(options["node_types"])
            nodes = [n for n in nodes if n.type in target_types]

        # Group by language (create language nodes)
        if options.get("group_by_language", False):
            language_nodes = await self._create_language_nodes(nodes)
            nodes.extend(language_nodes)

        return nodes, edges

    async def _create_language_nodes(
        self, nodes: list[VisualizationNode]
    ) -> list[VisualizationNode]:
        """Create parent nodes for each language."""

        language_nodes = []
        languages = {n.language for n in nodes}

        for language in languages:
            lang_nodes = [n for n in nodes if n.language == language]

            language_node = VisualizationNode(
                id=f"lang:{language}",
                label=language.title(),
                type=NodeType.LANGUAGE,
                language=language,
                size=len(lang_nodes),
                color=self.LANGUAGE_COLORS.get(language, "#808080"),
                shape=self.NODE_SHAPES[NodeType.LANGUAGE],
                group=f"language_{language}",
                properties={
                    "entity_count": len(lang_nodes),
                    "total_complexity": sum(n.complexity for n in lang_nodes),
                },
            )
            language_nodes.append(language_node)

        return language_nodes

    def _get_edge_color(self, edge_type: EdgeType) -> str:
        """Get color for edge type."""
        colors = {
            EdgeType.IMPORTS: "#999999",
            EdgeType.EXTENDS: "#2E8B57",
            EdgeType.IMPLEMENTS: "#4169E1",
            EdgeType.CALLS: "#FF6347",
            EdgeType.DEPENDS_ON: "#808080",
            EdgeType.CONTAINS: "#D3D3D3",
            EdgeType.FFI_CALL: "#FF4500",
            EdgeType.API_CALL: "#32CD32",
        }
        return colors.get(edge_type, "#808080")

    def _get_edge_style(self, edge_type: EdgeType) -> str:
        """Get style for edge type."""
        styles = {
            EdgeType.IMPORTS: "dashed",
            EdgeType.EXTENDS: "solid",
            EdgeType.IMPLEMENTS: "dotted",
            EdgeType.CALLS: "solid",
            EdgeType.DEPENDS_ON: "solid",
            EdgeType.CONTAINS: "dashed",
            EdgeType.FFI_CALL: "bold",
            EdgeType.API_CALL: "bold",
        }
        return styles.get(edge_type, "solid")

    def _calculate_edge_weight(self, relation: EntityRelation) -> float:
        """Calculate edge weight based on relationship properties."""
        base_weight = 1.0

        if relation.properties:
            # Boost weight for important relationships
            if relation.type in ["extends", "implements"]:
                base_weight = 2.0
            elif relation.type in ["ffi_call", "api_endpoint"]:
                base_weight = 3.0

        return base_weight

    def _get_cross_language_color(self, lang1: str, lang2: str) -> str:
        """Get color for cross-language edge."""
        # Create a blended color based on both languages
        # Use a hash of both language names for consistency
        combined = f"{lang1}-{lang2}"
        hash_val = hash(combined) % 360  # Get hue value
        # Return HSL-based color for variety
        return f"hsl({hash_val}, 70%, 50%)"

    async def _calculate_complexity_distribution(
        self, nodes: list[VisualizationNode]
    ) -> dict[str, int]:
        """Calculate complexity distribution across nodes."""
        distribution = {"low": 0, "medium": 0, "high": 0}

        for node in nodes:
            if node.complexity <= 5:
                distribution["low"] += 1
            elif node.complexity <= 15:
                distribution["medium"] += 1
            else:
                distribution["high"] += 1

        return distribution

    async def _calculate_dependency_depth(
        self, nodes: list[VisualizationNode], edges: list[VisualizationEdge]
    ) -> int:
        """Calculate maximum dependency depth."""
        # Basic implementation - could be augmented with proper graph traversal
        return len(edges) // max(len(nodes), 1)

    def _generate_graph_id(
        self,
        parse_results: list[ParseResult],
        vis_type: VisualizationType,
        options: dict[str, Any],
    ) -> str:
        """Generate unique ID for the graph."""
        content = f"{vis_type.value}_{len(parse_results)}_{hash(str(options))}"
        return hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()[:12]

    def _get_layout_options(
        self, vis_type: VisualizationType, options: dict[str, Any]
    ) -> dict[str, Any]:
        """Get layout options for visualization type."""
        layouts = {
            VisualizationType.DEPENDENCY_GRAPH: {
                "algorithm": "force-directed",
                "spacing": 100,
                "gravity": 0.1,
            },
            VisualizationType.COMPONENT_DIAGRAM: {
                "algorithm": "hierarchical",
                "direction": "LR",
                "spacing": 150,
            },
            VisualizationType.LAYER_ARCHITECTURE: {
                "algorithm": "hierarchical",
                "direction": "TB",
                "spacing": 120,
            },
        }

        base_layout = layouts.get(vis_type, layouts[VisualizationType.DEPENDENCY_GRAPH])
        base_layout.update(options.get("layout", {}))
        return base_layout

    async def _export_to_cytoscape(self, graph: VisualizationGraph) -> str:
        """Export graph to Cytoscape.js format."""
        cytoscape_data = {
            "elements": {
                "nodes": [
                    {
                        "data": {
                            "id": node.id,
                            "label": node.label,
                            "type": node.type.value,
                            "language": node.language,
                            "size": node.size,
                            "complexity": node.complexity,
                        },
                        "style": {
                            "background-color": node.color,
                            "shape": node.shape,
                        },
                    }
                    for node in graph.nodes
                ],
                "edges": [
                    {
                        "data": {
                            "id": edge.id,
                            "source": edge.source,
                            "target": edge.target,
                            "label": edge.label,
                            "type": edge.type.value,
                            "weight": edge.weight,
                        },
                        "style": {
                            "line-color": edge.color,
                            "line-style": edge.style,
                            "width": edge.weight * 2,
                        },
                    }
                    for edge in graph.edges
                ],
            },
            "layout": graph.layout_options,
            "metadata": {
                "title": graph.title,
                "type": graph.type.value,
                "statistics": graph.statistics,
            },
        }

        return json.dumps(cytoscape_data, indent=2)

    async def _export_to_d3(self, graph: VisualizationGraph) -> str:
        """Export graph to D3.js format."""
        d3_data = {
            "nodes": [
                {
                    "id": node.id,
                    "name": node.label,
                    "type": node.type.value,
                    "language": node.language,
                    "size": node.size,
                    "color": node.color,
                    "complexity": node.complexity,
                }
                for node in graph.nodes
            ],
            "links": [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "type": edge.type.value,
                    "weight": edge.weight,
                    "color": edge.color,
                }
                for edge in graph.edges
            ],
            "metadata": {"title": graph.title, "statistics": graph.statistics},
        }

        return json.dumps(d3_data, indent=2)

    async def _export_to_graphviz(self, graph: VisualizationGraph) -> str:
        """Export graph to Graphviz DOT format."""
        dot_lines = [
            f'digraph "{graph.title}" {{',
            "  rankdir=LR;",
            "  node [shape=box, style=filled];",
            "",
        ]

        # Add nodes
        for node in graph.nodes:
            attrs = [
                f'label="{node.label}"',
                f'fillcolor="{node.color}"',
                f'shape="{node.shape}"',
            ]
            dot_lines.append(f'  "{node.id}" [{", ".join(attrs)}];')

        dot_lines.append("")

        # Add edges
        for edge in graph.edges:
            attrs = [
                f'label="{edge.label or ""}"',
                f'color="{edge.color}"',
                f'style="{edge.style}"',
                f'penwidth="{edge.weight}"',
            ]
            dot_lines.append(f'  "{edge.source}" -> "{edge.target}" [{", ".join(attrs)}];')

        dot_lines.append("}")

        return "\n".join(dot_lines)

    async def _export_to_mermaid(self, graph: VisualizationGraph) -> str:
        """Export graph to Mermaid diagram format."""
        mermaid_lines = ["graph LR", ""]

        # Add nodes with styling
        for node in graph.nodes:
            shape_start, shape_end = self._get_mermaid_shape(node.shape)
            mermaid_lines.append(f"  {node.id}{shape_start}{node.label}{shape_end}")

        mermaid_lines.append("")

        # Add edges
        for edge in graph.edges:
            arrow = self._get_mermaid_arrow(edge.style)
            label = f"|{edge.label}|" if edge.label else ""
            mermaid_lines.append(f"  {edge.source} {arrow}{label} {edge.target}")

        return "\n".join(mermaid_lines)

    async def _export_to_json(self, graph: VisualizationGraph) -> str:
        """Export graph to JSON format."""
        return json.dumps(
            {
                "id": graph.id,
                "title": graph.title,
                "type": graph.type.value,
                "nodes": [asdict(node) for node in graph.nodes],
                "edges": [asdict(edge) for edge in graph.edges],
                "languages": list(graph.languages),
                "statistics": graph.statistics,
                "created_at": graph.created_at.isoformat(),
            },
            indent=2,
            default=str,
        )

    def _get_mermaid_shape(self, shape: str) -> tuple[str, str]:
        """Get Mermaid shape delimiters."""
        shapes = {
            "box": ("[", "]"),
            "circle": ("((", "))"),
            "diamond": ("{", "}"),
            "hexagon": ("{{", "}}"),
            "ellipse": ("([", "])"),
        }
        return shapes.get(shape, ("[", "]"))

    def _get_mermaid_arrow(self, style: str) -> str:
        """Get Mermaid arrow style."""
        arrows = {
            "solid": "-->",
            "dashed": "-.->",
            "dotted": "-.->",
            "bold": "==>",
        }
        return arrows.get(style, "-->")


# Module-level service instance with lazy initialization
class _DependencyVisualizerSingleton:
    """Singleton holder for dependency visualizer."""

    _instance: DependencyVisualizer | None = None

    @classmethod
    def get_instance(cls) -> DependencyVisualizer:
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = DependencyVisualizer()
            logger.info("Initialized dependency visualizer")
        return cls._instance


def get_dependency_visualizer() -> DependencyVisualizer:
    """Get or create the global dependency visualizer."""
    return _DependencyVisualizerSingleton.get_instance()
