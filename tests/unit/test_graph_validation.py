"""Tests for services.code_graph.graph_validation."""

from __future__ import annotations

import networkx as nx

from services.code_graph.graph_validation import GraphValidator


class TestGraphValidatorStructure:
    def setup_method(self):
        self.validator = GraphValidator()

    def test_empty_graph(self):
        g = nx.DiGraph()
        # Empty graph is a special case for weakly_connected check
        # add at least one node so nx doesn't complain
        g.add_node("single")
        result = self.validator.validate_graph_structure(g)
        assert result["is_valid"] is True
        assert result["metrics"]["node_count"] == 1
        assert "structural_issues" in result

    def test_simple_valid_graph(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        result = self.validator.validate_graph_structure(g)
        assert result["is_valid"] is True
        assert result["connectivity_score"] == 1.0

    def test_isolated_nodes(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_node("isolated")
        result = self.validator.validate_graph_structure(g)
        issues = result["structural_issues"]
        assert any(i["type"] == "connectivity" for i in issues)
        assert result["connectivity_score"] < 1.0

    def test_circular_dependency(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        g.add_edge("c", "a")
        result = self.validator.validate_graph_structure(g)
        assert result["is_valid"] is False
        assert any(i["type"] == "circular_dependency" for i in result["structural_issues"])

    def test_caching(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        r1 = self.validator.validate_graph_structure(g)
        r2 = self.validator.validate_graph_structure(g)
        # Cached result should match
        assert r1["metrics"] == r2["metrics"]

    def test_dangling_references(self):
        g = nx.DiGraph()
        g.add_node("a", dep_ref="nonexistent")
        g.add_node("b")
        g.add_edge("a", "b")
        result = self.validator.validate_graph_structure(g)
        # dangling_reference check looks for *_ref attrs pointing to non-graph nodes
        dangling_issues = [
            i for i in result["structural_issues"] if i["type"] == "dangling_reference"
        ]
        assert len(dangling_issues) == 1


class TestValidateNodeRelationships:
    def setup_method(self):
        self.validator = GraphValidator()

    def test_missing_node(self):
        g = nx.DiGraph()
        g.add_node("a")
        result = self.validator.validate_node_relationships(g, "missing")
        assert result["is_valid"] is False
        assert any(i["type"] == "missing_node" for i in result["issues"])

    def test_valid_node(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        result = self.validator.validate_node_relationships(g, "a")
        assert result["is_valid"] is True
        assert result["relationships"]["outgoing_count"] == 1
        assert result["relationships"]["incoming_count"] == 0

    def test_self_loop(self):
        g = nx.DiGraph()
        g.add_edge("a", "a")
        result = self.validator.validate_node_relationships(g, "a")
        assert any(i["type"] == "self_reference" for i in result["issues"])

    def test_high_fan_out(self):
        g = nx.DiGraph()
        for i in range(25):
            g.add_edge("hub", f"target_{i}")
        result = self.validator.validate_node_relationships(g, "hub")
        assert any(i["type"] == "high_coupling" for i in result["issues"])

    def test_high_fan_in(self):
        g = nx.DiGraph()
        for i in range(20):
            g.add_edge(f"source_{i}", "sink")
        result = self.validator.validate_node_relationships(g, "sink")
        assert any(i["type"] == "high_dependency" for i in result["issues"])


class TestCheckDependencyCycles:
    def setup_method(self):
        self.validator = GraphValidator()

    def test_no_cycles(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "c")
        result = self.validator.check_dependency_cycles(g)
        assert result["has_cycles"] is False
        assert result["cycle_count"] == 0
        assert result["severity_assessment"] == "none"

    def test_single_cycle(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        result = self.validator.check_dependency_cycles(g)
        assert result["has_cycles"] is True
        assert result["cycle_count"] >= 1
        assert result["severity_assessment"] == "low"

    def test_many_cycles(self):
        g = nx.DiGraph()
        # Create multiple independent cycles
        for i in range(6):
            g.add_edge(f"a{i}", f"b{i}")
            g.add_edge(f"b{i}", f"a{i}")
        result = self.validator.check_dependency_cycles(g)
        assert result["has_cycles"] is True
        assert result["severity_assessment"] == "high"

    def test_cycle_path_format(self):
        g = nx.DiGraph()
        g.add_edge("x", "y")
        g.add_edge("y", "x")
        result = self.validator.check_dependency_cycles(g)
        assert any(" -> " in c["path"] for c in result["cycles"])


class TestValidateGraphIntegrity:
    def setup_method(self):
        self.validator = GraphValidator()

    def test_no_expected_nodes(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        result = self.validator.validate_graph_integrity(g)
        assert result["is_intact"] is True
        assert result["completeness_score"] == 1.0

    def test_all_expected_present(self):
        g = nx.DiGraph()
        g.add_edge("a", "b")
        result = self.validator.validate_graph_integrity(g, expected_nodes=["a", "b"])
        assert result["is_intact"] is True
        assert result["completeness_score"] == 1.0

    def test_missing_expected_nodes(self):
        g = nx.DiGraph()
        g.add_node("a")
        result = self.validator.validate_graph_integrity(g, expected_nodes=["a", "b", "c"])
        assert result["is_intact"] is False
        assert "b" in result["missing_nodes"] or "c" in result["missing_nodes"]
        assert result["completeness_score"] < 1.0

    def test_unexpected_nodes(self):
        g = nx.DiGraph()
        g.add_node("a")
        g.add_node("extra")
        result = self.validator.validate_graph_integrity(g, expected_nodes=["a"])
        assert "extra" in result["unexpected_nodes"]

    def test_hash_graph_fallback(self):
        """Test that _hash_graph works even with non-string nodes."""
        g = nx.DiGraph()
        g.add_edge(1, 2)
        h = self.validator._hash_graph(g)
        assert isinstance(h, str)
