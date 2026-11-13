"""Module for graph structure validation and integrity checking."""

from __future__ import annotations

import logging
import time
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


class GraphValidator:
    """
    Handles validation of graph structures and integrity checks.

    This module focuses specifically on validating graph relationships,
    node connectivity, and structural integrity of code graphs.
    """

    def __init__(self) -> None:
        """Initialize graph validator."""
        self.validation_cache: dict[str, Any] = {}

    def validate_graph_structure(self, graph: nx.DiGraph) -> dict[str, Any]:
        """
        Validate the structure and integrity of a code graph.

        Args:
            graph: NetworkX directed graph representing code relationships

        Returns:
            Dict containing validation results including connectivity,
            cycles, orphaned nodes, and structural metrics
        """
        start_time = time.time()

        # Use cache if available
        graph_hash = self._hash_graph(graph)
        cache_key = f"structure:{graph_hash}"
        if cache_key in self.validation_cache:
            return dict(self.validation_cache[cache_key])

        result: dict[str, Any] = {
            "is_valid": True,
            "connectivity_score": 1.0,
            "structural_issues": [],
            "metrics": {},
            "validation_time_ms": 0,
        }
        structural_issues: list[dict[str, Any]] = []

        try:
            # Basic graph metrics
            result["metrics"] = {
                "node_count": graph.number_of_nodes(),
                "edge_count": graph.number_of_edges(),
                "density": nx.density(graph) if graph.number_of_nodes() > 0 else 0.0,
                "is_connected": nx.is_weakly_connected(graph),
                "strongly_connected_components": nx.number_strongly_connected_components(graph),
            }

            # Check for structural issues
            issues: list[dict[str, Any]] = []

            # Check for isolated nodes
            isolated_nodes = list(nx.isolates(graph))
            if isolated_nodes:
                issues.append(
                    {
                        "type": "connectivity",
                        "severity": "medium",
                        "description": f"Found {len(isolated_nodes)} isolated nodes",
                        "affected_nodes": isolated_nodes[:10],  # Limit for readability
                    }
                )

            # Check for circular dependencies
            try:
                cycles = list(nx.simple_cycles(graph))
                if cycles:
                    for cycle in cycles[:5]:  # Limit cycles reported
                        issues.append(
                            {
                                "type": "circular_dependency",
                                "severity": "high",
                                "description": f"Circular dependency: {' -> '.join(cycle)}",
                                "affected_nodes": cycle,
                            }
                        )
            except nx.NetworkXNoCycle:
                pass  # No cycles is good

            # Check for dangling references
            dangling_refs = self._find_dangling_references(graph)
            if dangling_refs:
                issues.append(
                    {
                        "type": "dangling_reference",
                        "severity": "high",
                        "description": f"Found {len(dangling_refs)} dangling references",
                        "affected_nodes": dangling_refs[:10],
                    }
                )

            # Calculate connectivity score
            if graph.number_of_nodes() > 0:
                connected_ratio = (
                    graph.number_of_nodes() - len(isolated_nodes)
                ) / graph.number_of_nodes()
                result["connectivity_score"] = connected_ratio
            else:
                result["connectivity_score"] = 1.0

            result["structural_issues"] = issues
            result["is_valid"] = len([i for i in issues if i["severity"] == "high"]) == 0

        except Exception as e:
            logger.error(f"Graph validation failed: {e}")
            result["is_valid"] = False
            structural_issues.append(
                {
                    "type": "validation_error",
                    "severity": "high",
                    "description": f"Validation failed: {str(e)}",
                }
            )
            result["structural_issues"] = structural_issues

        result["validation_time_ms"] = int((time.time() - start_time) * 1000)

        # Cache result
        self.validation_cache[cache_key] = result

        return result

    def validate_node_relationships(self, graph: nx.DiGraph, node_id: str) -> dict[str, Any]:
        """
        Validate relationships for a specific node in the graph.

        Args:
            graph: NetworkX directed graph
            node_id: ID of the node to validate

        Returns:
            Dict containing node-specific validation results
        """
        if node_id not in graph:
            return {
                "is_valid": False,
                "issues": [
                    {
                        "type": "missing_node",
                        "severity": "high",
                        "description": f"Node '{node_id}' not found in graph",
                    }
                ],
            }

        relationships: dict[str, Any] = {
            "incoming_count": graph.in_degree(node_id),
            "outgoing_count": graph.out_degree(node_id),
            "incoming_nodes": list(graph.predecessors(node_id)),
            "outgoing_nodes": list(graph.successors(node_id)),
        }
        result: dict[str, Any] = {
            "is_valid": True,
            "node_id": node_id,
            "relationships": relationships,
            "issues": [],
        }

        # Check for relationship issues
        issues: list[dict[str, Any]] = []

        # Check for self-loops
        if graph.has_edge(node_id, node_id):
            issues.append(
                {
                    "type": "self_reference",
                    "severity": "medium",
                    "description": f"Node '{node_id}' has a self-reference",
                }
            )

        # Check for high fan-out (potential god object)
        outgoing_count = relationships["outgoing_count"]
        if outgoing_count > 20:
            issues.append(
                {
                    "type": "high_coupling",
                    "severity": "medium",
                    "description": f"Node '{node_id}' has high outgoing connections ({outgoing_count})",
                }
            )

        # Check for high fan-in (potential dependency bottleneck)
        incoming_count = relationships["incoming_count"]
        if incoming_count > 15:
            issues.append(
                {
                    "type": "high_dependency",
                    "severity": "low",
                    "description": f"Node '{node_id}' has many incoming dependencies ({incoming_count})",
                }
            )

        result["issues"] = issues
        result["is_valid"] = len([i for i in issues if i["severity"] == "high"]) == 0

        return result

    def check_dependency_cycles(self, graph: nx.DiGraph) -> dict[str, Any]:
        """
        Specifically check for and analyze dependency cycles in the graph.

        Args:
            graph: NetworkX directed graph

        Returns:
            Dict containing cycle analysis results
        """
        cycles_list: list[dict[str, Any]] = []
        affected_nodes_set: set[Any] = set()
        cycle_count = 0
        has_cycles = False
        severity_assessment = "none"
        error_msg: str | None = None

        try:
            cycles = list(nx.simple_cycles(graph))
            has_cycles = len(cycles) > 0
            cycle_count = len(cycles)

            for cycle in cycles:
                cycle_info: dict[str, Any] = {
                    "nodes": cycle,
                    "length": len(cycle),
                    "path": " -> ".join(cycle + [cycle[0]]),  # Complete the cycle
                }
                cycles_list.append(cycle_info)
                affected_nodes_set.update(cycle)

            # Assess severity
            if cycle_count == 0:
                severity_assessment = "none"
            elif cycle_count <= 2:
                severity_assessment = "low"
            elif cycle_count <= 5:
                severity_assessment = "medium"
            else:
                severity_assessment = "high"

        except Exception as e:
            logger.error(f"Cycle detection failed: {e}")
            error_msg = str(e)

        result: dict[str, Any] = {
            "has_cycles": has_cycles,
            "cycle_count": cycle_count,
            "cycles": cycles_list,
            "affected_nodes": list(affected_nodes_set),
            "severity_assessment": severity_assessment,
        }
        if error_msg is not None:
            result["error"] = error_msg

        return result

    def _hash_graph(self, graph: nx.DiGraph) -> str:
        """Generate a hash for graph caching."""
        try:
            # Create a simple hash based on nodes and edges
            nodes_str = ",".join(sorted(graph.nodes()))
            edges_str = ",".join(sorted([f"{u}->{v}" for u, v in graph.edges()]))
            return str(hash(f"{nodes_str}|{edges_str}"))
        except Exception:
            # Fallback to a simple representation
            return str(hash(f"nodes:{graph.number_of_nodes()},edges:{graph.number_of_edges()}"))

    def _find_dangling_references(self, graph: nx.DiGraph) -> list[str]:
        """Find nodes that reference non-existent targets."""
        dangling = []

        for node in graph.nodes():
            # Check if node data contains references to non-existent nodes
            node_data = graph.nodes.get(node, {})

            # Look for reference attributes that might point to missing nodes
            for key, value in node_data.items():
                if key.endswith("_ref") or key.endswith("_reference"):
                    if isinstance(value, str) and value not in graph:
                        dangling.append(f"{node}->{value}")
                    elif isinstance(value, list):
                        for ref in value:
                            if isinstance(ref, str) and ref not in graph:
                                dangling.append(f"{node}->{ref}")

        return dangling

    def validate_graph_integrity(
        self, graph: nx.DiGraph, expected_nodes: list[str] | None = None
    ) -> dict[str, Any]:
        """
        Comprehensive integrity check for the entire graph.

        Args:
            graph: NetworkX directed graph
            expected_nodes: Optional list of nodes that should be present

        Returns:
            Dict containing comprehensive integrity results
        """
        integrity_issues: list[dict[str, Any]] = []
        missing_nodes_list: list[str] = []
        unexpected_nodes_list: list[str] = []
        broken_edges_list: list[Any] = []
        completeness_score = 1.0
        is_intact = True

        try:
            # Check for expected nodes if provided
            if expected_nodes:
                actual_nodes = set(graph.nodes())
                expected_set = set(expected_nodes)

                missing = expected_set - actual_nodes
                unexpected = actual_nodes - expected_set

                if missing:
                    missing_nodes_list = list(missing)
                    integrity_issues.append(
                        {
                            "type": "missing_nodes",
                            "severity": "high",
                            "description": f"Missing {len(missing)} expected nodes",
                            "details": list(missing)[:10],
                        }
                    )

                if unexpected:
                    unexpected_nodes_list = list(unexpected)
                    integrity_issues.append(
                        {
                            "type": "unexpected_nodes",
                            "severity": "low",
                            "description": f"Found {len(unexpected)} unexpected nodes",
                            "details": list(unexpected)[:10],
                        }
                    )

                # Calculate completeness score
                if expected_nodes:
                    completeness_score = len(actual_nodes & expected_set) / len(expected_set)

            # Check for broken edges (edges pointing to non-existent nodes)
            broken_edges: list[Any] = []
            for u, v in graph.edges():
                if u not in graph.nodes() or v not in graph.nodes():
                    broken_edges.append((u, v))

            if broken_edges:
                broken_edges_list = broken_edges
                integrity_issues.append(
                    {
                        "type": "broken_edges",
                        "severity": "high",
                        "description": f"Found {len(broken_edges)} broken edges",
                        "details": broken_edges[:10],
                    }
                )

            # Overall integrity assessment
            high_severity_issues = [i for i in integrity_issues if i["severity"] == "high"]
            is_intact = len(high_severity_issues) == 0

        except Exception as e:
            logger.error(f"Graph integrity check failed: {e}")
            is_intact = False
            integrity_issues.append(
                {
                    "type": "validation_error",
                    "severity": "high",
                    "description": f"Integrity check failed: {str(e)}",
                }
            )

        result: dict[str, Any] = {
            "is_intact": is_intact,
            "completeness_score": completeness_score,
            "integrity_issues": integrity_issues,
            "missing_nodes": missing_nodes_list,
            "unexpected_nodes": unexpected_nodes_list,
            "broken_edges": broken_edges_list,
        }

        return result
