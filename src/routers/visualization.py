"""
Dependency Visualization API Endpoints

API endpoints for creating and managing cross-language dependency visualizations
and architectural diagrams.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from auth.dependencies import require_tenant
from config.settings import get_settings
from services.visualization import (
    EdgeType,
    NodeType,
    VisualizationType,
    get_dependency_visualizer,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class VisualizationRequest(BaseModel):
    """Request for creating a dependency visualization."""

    file_paths: list[str] = Field(..., description="List of file paths to analyze")
    visualization_type: str = Field(
        "dependency_graph", description="Type of visualization to create"
    )
    options: dict[str, Any] | None = Field(None, description="Visualization options and filters")
    export_formats: list[str] = Field(
        ["cytoscape", "json"], description="Export formats to generate"
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "file_paths": [
                    "/path/to/main.rs",
                    "/path/to/api.java",
                    "/path/to/client.ts",
                ],
                "visualization_type": "dependency_graph",
                "options": {
                    "languages": ["rust", "java", "typescript"],
                    "group_by_language": True,
                    "min_complexity": 0,
                    "layout": {"algorithm": "force-directed", "spacing": 100},
                },
                "export_formats": ["cytoscape", "d3", "json"],
            },
        }
    )


class VisualizationNodeModel(BaseModel):
    """Visualization node information."""

    id: str
    label: str
    type: str
    language: str
    file_path: str | None = None
    size: int = 1
    color: str | None = None
    shape: str = "box"
    group: str | None = None
    complexity: float = 0.0
    importance: float = 0.0
    properties: dict[str, Any] = Field(default_factory=dict)


class VisualizationEdgeModel(BaseModel):
    """Visualization edge information."""

    id: str
    source: str
    target: str
    type: str
    label: str | None = None
    weight: float = 1.0
    color: str | None = None
    style: str = "solid"
    properties: dict[str, Any] = Field(default_factory=dict)


class VisualizationResult(BaseModel):
    """Visualization analysis result."""

    id: str
    title: str
    type: str
    nodes: list[VisualizationNodeModel]
    edges: list[VisualizationEdgeModel]
    languages: list[str]
    statistics: dict[str, Any]
    layout_options: dict[str, Any]
    exports: dict[str, str]


class VisualizationResponse(BaseModel):
    """Response from visualization operation."""

    status: str = "success"
    message: str = ""
    result: VisualizationResult = Field(..., description="Visualization result")
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class VisualizationCapabilitiesResponse(BaseModel):
    """Response model for visualization capabilities."""

    visualization_types: list[str]
    node_types: list[str]
    edge_types: list[str]
    export_formats: list[str]
    layout_algorithms: list[str]
    supported_languages: list[str]


# ---------------------------------------------------------------------------
# Helper functions (extracted from monolith data_utilities)
# ---------------------------------------------------------------------------


def _validate_visualization_type(visualization_type: str) -> VisualizationType:
    """Validate and return the visualization type."""
    try:
        return VisualizationType(visualization_type)
    except ValueError as exc:
        valid_types = [t.value for t in VisualizationType]
        raise HTTPException(
            status_code=400,
            detail=f"Invalid visualization type. Valid types: {', '.join(valid_types)}",
        ) from exc


def _validate_file_paths(file_paths: list[str]) -> list[Path]:
    """Validate caller-supplied file paths via a positive containment allowlist.

    A file path is accepted ONLY when it resolves to a location UNDER the
    configured ``visualization_workspace_root``. This replaces the previous
    deny-list of system directories, which was a blocklist and therefore
    bypassable: a path outside the blocked prefixes (``/home``, ``/tmp``,
    ``/app``, secret mounts, …) leaked file existence + line count + the
    resolved absolute path to any authenticated tenant (an info-disclosure
    oracle). A positive containment check closes that: anything outside the
    root is rejected. When the root is unset (the default — repo source is
    never present on the gateway pod; parsing is meant to delegate to
    coding-analysis), EVERY path is rejected and the endpoint honestly 404s.
    """
    settings = get_settings()
    root_str = settings.visualization_workspace_root
    allowed_root: Path | None = None
    if root_str:
        try:
            allowed_root = Path(root_str).resolve()
        except (OSError, ValueError):  # fmt: skip
            allowed_root = None

    valid_files = []
    for file_path_str in file_paths:
        file_path = Path(file_path_str)
        try:
            resolved = file_path.resolve()
        except (OSError, ValueError):  # fmt: skip
            continue
        # Positive containment: reject anything not under the allowed root
        # (and reject everything when no root is configured).
        if allowed_root is None or not resolved.is_relative_to(allowed_root):
            logger.warning(
                "Rejected path outside the visualization workspace root: %s", file_path_str
            )
            continue
        if not resolved.exists():
            logger.warning("File not found: %s", file_path_str)
            continue
        valid_files.append(resolved)

    if not valid_files:
        raise HTTPException(status_code=404, detail="No valid files found")

    return valid_files


async def _parse_files(valid_files: list[Path]) -> list[Any]:
    """Parse all valid files and return parse results.

    In the gateway context, file parsing is done via the coding-analysis
    microservice or local tree-sitter processing. This is a simplified
    implementation that creates minimal parse result stubs for files that
    exist on disk.
    """
    # In the full monolith this uses MultiLanguageProcessorFactory + FileService.
    # In the gateway microservice, parsing would be delegated to coding-analysis.
    # For now, create minimal parse result structures so the visualizer can work.
    parse_results = []

    for file_path in valid_files:
        try:
            # Determine language from extension
            extension_map = {
                ".py": "python",
                ".js": "javascript",
                ".ts": "typescript",
                ".java": "java",
                ".rs": "rust",
                ".go": "go",
                ".c": "c",
                ".cpp": "cpp",
                ".cc": "cpp",
                ".cs": "csharp",
            }
            language = extension_map.get(file_path.suffix.lower(), "unknown")

            content = file_path.read_text(encoding="utf-8")
            lines = content.count("\n") + 1

            # Create a minimal parse result compatible with the visualizer
            class _ParseResult:
                def __init__(self, fp: Path, lang: str, loc: int) -> None:
                    self.success = True
                    self.file_path = str(fp)
                    self.language = lang
                    self.lines_of_code = loc
                    self.complexity_score = 0.0
                    self.entities: list[Any] = []
                    self.relationships: list[Any] = []

            parse_results.append(_ParseResult(file_path, language, lines))

        except (OSError, UnicodeDecodeError) as e:
            logger.error("Error processing %s: %s", file_path, str(e))
            continue

    if not parse_results:
        raise HTTPException(status_code=422, detail="No files could be successfully parsed")

    return parse_results


def _create_visualization_result(graph: Any, exports: dict[str, str]) -> VisualizationResult:
    """Create visualization result from graph and exports."""
    nodes = []
    for node in graph.nodes:
        viz_node = VisualizationNodeModel(
            id=node.id,
            label=node.label,
            type=node.type.value,
            language=node.language,
            file_path=node.file_path,
            size=node.size,
            color=node.color,
            shape=node.shape,
            group=node.group,
            complexity=node.complexity,
            importance=node.importance,
            properties=node.properties or {},
        )
        nodes.append(viz_node)

    edges = []
    for edge in graph.edges:
        viz_edge = VisualizationEdgeModel(
            id=edge.id,
            source=edge.source,
            target=edge.target,
            type=edge.type.value,
            label=edge.label,
            weight=edge.weight,
            color=edge.color,
            style=edge.style,
            properties=edge.properties or {},
        )
        edges.append(viz_edge)

    return VisualizationResult(
        id=graph.id,
        title=graph.title,
        type=graph.type.value,
        nodes=nodes,
        edges=edges,
        languages=list(graph.languages),
        statistics=graph.statistics,
        layout_options=graph.layout_options or {},
        exports=exports,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/create-graph",
    response_model=VisualizationResponse,
    summary="Create Dependency Visualization",
    description="Create cross-language dependency visualization from file paths.",
    operation_id="create_visualization_graph",
)
async def create_dependency_visualization(
    request: VisualizationRequest = Body(..., description="Visualization request"),
    tenant_id: str = Depends(require_tenant),
) -> VisualizationResponse:
    """Create a cross-language dependency visualization.

    Requires authentication. Creates visualizations from file paths.
    """
    try:
        if not request.file_paths:
            raise HTTPException(status_code=400, detail="At least one file path is required")

        # Validate visualization type
        vis_type = _validate_visualization_type(request.visualization_type)

        # Validate file paths
        valid_files = _validate_file_paths(request.file_paths)

        # Parse all files
        parse_results = await _parse_files(valid_files)

        # Create visualization
        visualizer = get_dependency_visualizer()
        graph = await visualizer.create_dependency_graph(
            parse_results, vis_type, request.options or {}
        )

        # Export to requested formats
        exports = await visualizer.export_to_formats(graph, request.export_formats)

        # Convert to response model
        result = _create_visualization_result(graph, exports)

        logger.info(
            "Visualization created: %s with %d nodes, %d edges",
            graph.id,
            len(graph.nodes),
            len(graph.edges),
        )

        return VisualizationResponse(
            status="success",
            message=f"Visualization created with {len(graph.nodes)} nodes and {len(graph.edges)} edges",
            result=result,
        )

    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError, OSError, UnicodeDecodeError) as e:
        logger.exception("Visualization creation failed: %s", str(e))
        raise HTTPException(status_code=500, detail="Visualization creation failed") from e


@router.get(
    "/capabilities",
    response_model=VisualizationCapabilitiesResponse,
    summary="Visualization Capabilities",
    description="Get available visualization types and features.",
    operation_id="get_visualization_capabilities",
)
async def get_visualization_capabilities(
    tenant_id: str = Depends(require_tenant),
) -> VisualizationCapabilitiesResponse:
    """Get visualization system capabilities."""
    try:
        return VisualizationCapabilitiesResponse(
            visualization_types=[t.value for t in VisualizationType],
            node_types=[t.value for t in NodeType],
            edge_types=[t.value for t in EdgeType],
            export_formats=["cytoscape", "d3", "graphviz", "mermaid", "json"],
            layout_algorithms=[
                "force-directed",
                "hierarchical",
                "circular",
                "grid",
                "tree",
                "layered",
            ],
            supported_languages=[
                "java",
                "rust",
                "go",
                "python",
                "javascript",
                "typescript",
                "csharp",
                "cpp",
                "c",
            ],
        )
    except (ValueError, TypeError, AttributeError) as e:
        logger.error("Error retrieving visualization capabilities: %s", str(e))
        raise HTTPException(status_code=500, detail="Error retrieving capabilities") from e


@router.get(
    "/examples",
    summary="Visualization Examples",
    description="Get example configurations and use cases for visualization.",
    operation_id="get_visualization_examples",
)
async def get_visualization_examples(
    tenant_id: str = Depends(require_tenant),
) -> dict[str, Any]:
    """Get visualization examples and templates."""
    try:
        examples = {
            "basic_dependency_graph": {
                "description": "Basic dependency graph for a single-language project",
                "request": {
                    "file_paths": ["/path/to/src/**/*.java"],
                    "visualization_type": "dependency_graph",
                    "options": {
                        "group_by_language": False,
                        "show_file_nodes": True,
                    },
                    "export_formats": ["cytoscape", "json"],
                },
                "use_case": "Understanding basic project structure and dependencies",
            },
            "cross_language_analysis": {
                "description": "Multi-language project with cross-language relationships",
                "request": {
                    "file_paths": [
                        "/path/to/backend/**/*.java",
                        "/path/to/frontend/**/*.ts",
                        "/path/to/service/**/*.rs",
                    ],
                    "visualization_type": "cross_language_map",
                    "options": {
                        "group_by_language": True,
                        "highlight_cross_language": True,
                        "languages": ["java", "typescript", "rust"],
                    },
                    "export_formats": ["cytoscape", "d3", "mermaid"],
                },
                "use_case": "Analyzing polyglot architecture and language boundaries",
            },
            "component_architecture": {
                "description": "High-level component and module relationships",
                "request": {
                    "file_paths": ["/path/to/project/**/*"],
                    "visualization_type": "component_diagram",
                    "options": {
                        "node_types": ["class", "interface", "module"],
                        "min_complexity": 5.0,
                        "layout": {
                            "algorithm": "hierarchical",
                            "direction": "LR",
                        },
                    },
                    "export_formats": ["graphviz", "mermaid"],
                },
                "use_case": "Architectural documentation and component design review",
            },
            "complexity_focused": {
                "description": "Visualization focusing on complex entities and relationships",
                "request": {
                    "file_paths": ["/path/to/src/**/*"],
                    "visualization_type": "dependency_graph",
                    "options": {
                        "min_complexity": 10.0,
                        "max_nodes": 50,
                        "highlight_hotspots": True,
                        "layout": {
                            "algorithm": "force-directed",
                            "gravity": 0.2,
                        },
                    },
                    "export_formats": ["cytoscape", "json"],
                },
                "use_case": "Identifying refactoring targets and complexity hotspots",
            },
            "api_integration_map": {
                "description": "Visualization of API integrations and service boundaries",
                "request": {
                    "file_paths": [
                        "/path/to/api/**/*.java",
                        "/path/to/client/**/*.ts",
                        "/path/to/gateway/**/*.go",
                    ],
                    "visualization_type": "call_flow",
                    "options": {
                        "edge_types": ["api_call", "rpc_call"],
                        "group_by_service": True,
                        "show_external_apis": True,
                    },
                    "export_formats": ["d3", "mermaid"],
                },
                "use_case": "Understanding service architecture and API dependencies",
            },
        }

        return {
            "examples": examples,
            "best_practices": {
                "large_codebases": "Use filtering options to focus on specific areas",
                "cross_language": "Enable language grouping for better organization",
                "documentation": "Use mermaid format for markdown integration",
                "interactive_analysis": "Use cytoscape format for web dashboards",
                "complexity_analysis": "Apply complexity thresholds to highlight problem areas",
            },
            "tips": [
                "Start with basic dependency graph to understand overall structure",
                "Use component diagrams for high-level architectural views",
                "Apply filters to reduce complexity for large codebases",
                "Combine multiple export formats for different use cases",
                "Use cross-language maps to identify integration points",
            ],
        }

    except (ValueError, TypeError, AttributeError) as e:
        logger.error("Error retrieving visualization examples: %s", str(e))
        raise HTTPException(status_code=500, detail="Error retrieving examples") from e
