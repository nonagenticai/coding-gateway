"""Tests for services.code_graph.validation_utilities."""

from __future__ import annotations

import time

import networkx as nx

from services.code_graph.validation_utilities import (
    ASTUtils,
    GraphUtils,
    HashUtils,
    TextAnalysisUtils,
    ValidationCache,
    ValidationScoreCalculator,
)

# ---------------------------------------------------------------------------
# ValidationCache
# ---------------------------------------------------------------------------


class TestValidationCache:
    def setup_method(self):
        self.cache = ValidationCache(max_size=5, default_ttl=60)

    def test_get_missing_key(self):
        assert self.cache.get("nope") is None

    def test_set_and_get(self):
        self.cache.set("k", {"val": 1})
        assert self.cache.get("k") == {"val": 1}

    def test_len(self):
        assert len(self.cache) == 0
        self.cache.set("a", 1)
        assert len(self.cache) == 1

    def test_clear(self):
        self.cache.set("a", 1)
        self.cache.set("b", 2)
        self.cache.clear()
        assert len(self.cache) == 0

    def test_ttl_expiry(self):
        self.cache.set("x", "val", ttl=0)
        # TTL=0 → expires immediately (time.time() > entry["expires"])
        time.sleep(0.01)
        assert self.cache.get("x") is None

    def test_eviction_on_max_size(self):
        for i in range(6):
            self.cache.set(f"k{i}", i)
        # Should have evicted the oldest, so max_size items remain
        assert len(self.cache) <= 5

    def test_evict_oldest_empty(self):
        # Should not raise
        self.cache._evict_oldest()

    def test_custom_ttl(self):
        self.cache.set("k", "v", ttl=3600)
        assert self.cache.get("k") == "v"


# ---------------------------------------------------------------------------
# ASTUtils
# ---------------------------------------------------------------------------


class TestASTUtils:
    def test_safe_parse_valid(self):
        tree = ASTUtils.safe_parse("x = 1")
        assert tree is not None

    def test_safe_parse_empty(self):
        assert ASTUtils.safe_parse("") is None
        assert ASTUtils.safe_parse("   ") is None

    def test_safe_parse_syntax_error(self):
        assert ASTUtils.safe_parse("def !!!") is None

    def test_safe_parse_none(self):
        assert ASTUtils.safe_parse(None) is None

    def test_extract_functions(self):
        tree = ASTUtils.safe_parse("def foo():\n    pass\ndef bar():\n    pass")
        funcs = ASTUtils.extract_functions(tree)
        assert len(funcs) == 2
        assert funcs[0].name == "foo"

    def test_extract_functions_none(self):
        assert ASTUtils.extract_functions(None) == []

    def test_extract_classes(self):
        tree = ASTUtils.safe_parse("class Foo:\n    pass")
        classes = ASTUtils.extract_classes(tree)
        assert len(classes) == 1
        assert classes[0].name == "Foo"

    def test_extract_classes_none(self):
        assert ASTUtils.extract_classes(None) == []

    def test_extract_imports(self):
        tree = ASTUtils.safe_parse("import os\nfrom sys import argv")
        imports = ASTUtils.extract_imports(tree)
        assert "os" in imports["imports"]
        assert "sys.argv" in imports["from_imports"]

    def test_extract_imports_none(self):
        result = ASTUtils.extract_imports(None)
        assert result == {"imports": [], "from_imports": []}

    def test_get_function_info_basic(self):
        tree = ASTUtils.safe_parse(
            'def greet(name: str) -> str:\n    """Say hello."""\n    return f"Hi {name}"'
        )
        func = ASTUtils.extract_functions(tree)[0]
        info = ASTUtils.get_function_info(func)
        assert info["name"] == "greet"
        assert info["return_annotation"] == "str"
        assert info["docstring"] == "Say hello."
        assert info["has_return_statements"] is True
        assert any(p["name"] == "name" for p in info["parameters"])

    def test_get_function_info_complexity(self):
        code = (
            "def process(x):\n"
            "    for i in range(x):\n"
            "        if i > 5:\n"
            "            try:\n"
            "                pass\n"
            "            except:\n"
            "                pass\n"
        )
        tree = ASTUtils.safe_parse(code)
        func = ASTUtils.extract_functions(tree)[0]
        info = ASTUtils.get_function_info(func)
        assert info["complexity_indicators"]["loops"] >= 1
        assert info["complexity_indicators"]["conditions"] >= 1
        assert info["complexity_indicators"]["try_except"] >= 1

    def test_get_function_info_decorators(self):
        code = "@staticmethod\ndef foo():\n    pass"
        tree = ASTUtils.safe_parse(code)
        func = ASTUtils.extract_functions(tree)[0]
        info = ASTUtils.get_function_info(func)
        assert "staticmethod" in info["decorators"]

    def test_get_class_info_basic(self):
        code = (
            "class Foo(Bar):\n"
            '    """A class."""\n'
            "    def __init__(self):\n"
            "        self.x = 1\n"
            "    def do_thing(self):\n"
            "        pass\n"
            "    @property\n"
            "    def prop(self):\n"
            "        return self.x\n"
        )
        tree = ASTUtils.safe_parse(code)
        cls = ASTUtils.extract_classes(tree)[0]
        info = ASTUtils.get_class_info(cls)
        assert info["name"] == "Foo"
        assert "Bar" in info["base_classes"]
        assert info["docstring"] == "A class."
        assert "x" in info["attributes"]
        assert any(m["name"] == "do_thing" for m in info["methods"])
        assert any(m["name"] == "prop" and m["is_property"] for m in info["methods"])

    def test_get_class_info_static_classmethod(self):
        code = (
            "class C:\n"
            "    @staticmethod\n"
            "    def s():\n"
            "        pass\n"
            "    @classmethod\n"
            "    def c(cls):\n"
            "        pass\n"
        )
        tree = ASTUtils.safe_parse(code)
        cls = ASTUtils.extract_classes(tree)[0]
        info = ASTUtils.get_class_info(cls)
        static = [m for m in info["methods"] if m["name"] == "s"][0]
        assert static["is_static"] is True
        cm = [m for m in info["methods"] if m["name"] == "c"][0]
        assert cm["is_class_method"] is True


# ---------------------------------------------------------------------------
# TextAnalysisUtils
# ---------------------------------------------------------------------------


class TestTextAnalysisUtils:
    def test_extract_keywords_empty(self):
        assert TextAnalysisUtils.extract_keywords("") == set()
        assert TextAnalysisUtils.extract_keywords(None) == set()

    def test_extract_keywords_filters_stopwords(self):
        kw = TextAnalysisUtils.extract_keywords("the function is used for parsing")
        assert "the" not in kw
        assert "parsing" in kw

    def test_extract_keywords_short_words(self):
        kw = TextAnalysisUtils.extract_keywords("a b cd efg")
        assert "efg" in kw
        assert "cd" not in kw  # len <= 2

    def test_calculate_text_similarity_empty(self):
        assert TextAnalysisUtils.calculate_text_similarity("", "hello") == 0.0
        assert TextAnalysisUtils.calculate_text_similarity("hello", "") == 0.0

    def test_calculate_text_similarity_identical(self):
        sim = TextAnalysisUtils.calculate_text_similarity(
            "the function processes data", "the function processes data"
        )
        assert sim == 1.0

    def test_calculate_text_similarity_partial(self):
        sim = TextAnalysisUtils.calculate_text_similarity(
            "function processes data", "function reads data"
        )
        assert 0.0 < sim < 1.0

    def test_detect_mentioned_entities_empty(self):
        assert TextAnalysisUtils.detect_mentioned_entities("", ["foo"]) == []
        assert TextAnalysisUtils.detect_mentioned_entities("text", []) == []

    def test_detect_mentioned_entities(self):
        text = "The Foo class uses the bar function"
        found = TextAnalysisUtils.detect_mentioned_entities(text, ["Foo", "bar", "baz"])
        assert "Foo" in found
        assert "bar" in found
        assert "baz" not in found

    def test_extract_code_patterns_empty(self):
        result = TextAnalysisUtils.extract_code_patterns("")
        assert result["function_names"] == []
        assert result["class_names"] == []

    def test_extract_code_patterns(self):
        text = "The MyClass calls process_data() and uses HTTP api"
        result = TextAnalysisUtils.extract_code_patterns(text)
        assert "MyClass" in result["class_names"]
        assert "web" in result["technologies"]


# ---------------------------------------------------------------------------
# GraphUtils
# ---------------------------------------------------------------------------


class TestGraphUtils:
    def test_create_dependency_graph_empty(self):
        graph = GraphUtils.create_dependency_graph({})
        assert graph.number_of_nodes() == 0

    def test_create_dependency_graph_with_import(self):
        modules = {
            "a": "import b",
            "b": "x = 1",
        }
        graph = GraphUtils.create_dependency_graph(modules)
        assert graph.has_node("a")
        assert graph.has_node("b")
        assert graph.has_edge("a", "b")

    def test_create_dependency_graph_from_import(self):
        modules = {
            "a": "from b import something",
            "b": "something = 1",
        }
        graph = GraphUtils.create_dependency_graph(modules)
        assert graph.has_edge("a", "b")

    def test_analyze_graph_metrics_empty(self):
        graph = nx.DiGraph()
        metrics = GraphUtils.analyze_graph_metrics(graph)
        assert metrics["node_count"] == 0
        assert metrics["is_connected"] is False

    def test_analyze_graph_metrics_simple(self):
        graph = nx.DiGraph()
        graph.add_edge("a", "b")
        graph.add_edge("b", "c")
        metrics = GraphUtils.analyze_graph_metrics(graph)
        assert metrics["node_count"] == 3
        assert metrics["edge_count"] == 2
        assert metrics["has_cycles"] is False

    def test_analyze_graph_metrics_with_cycle(self):
        graph = nx.DiGraph()
        graph.add_edge("a", "b")
        graph.add_edge("b", "a")
        metrics = GraphUtils.analyze_graph_metrics(graph)
        assert metrics["has_cycles"] is True
        assert len(metrics["cycles"]) > 0


# ---------------------------------------------------------------------------
# HashUtils
# ---------------------------------------------------------------------------


class TestHashUtils:
    def test_hash_code(self):
        h = HashUtils.hash_code("hello")
        assert isinstance(h, str)
        assert len(h) == 32  # md5 hex

    def test_hash_code_deterministic(self):
        assert HashUtils.hash_code("x") == HashUtils.hash_code("x")

    def test_hash_dict(self):
        h = HashUtils.hash_dict({"a": 1, "b": 2})
        assert isinstance(h, str)

    def test_hash_dict_order_independent(self):
        assert HashUtils.hash_dict({"a": 1, "b": 2}) == HashUtils.hash_dict({"b": 2, "a": 1})

    def test_create_cache_key_strings(self):
        key = HashUtils.create_cache_key("foo", "bar")
        assert key == "foo:bar"

    def test_create_cache_key_mixed(self):
        key = HashUtils.create_cache_key("prefix", {"a": 1})
        assert key.startswith("prefix:")


# ---------------------------------------------------------------------------
# ValidationScoreCalculator
# ---------------------------------------------------------------------------


class TestValidationScoreCalculator:
    def setup_method(self):
        self.calc = ValidationScoreCalculator()

    def test_no_issues_perfect_score(self):
        assert self.calc.calculate_consistency_score([]) == 1.0

    def test_issues_reduce_score(self):
        issues = [{"severity": "high"}]
        score = self.calc.calculate_consistency_score(issues)
        assert 0.0 < score < 1.0

    def test_context_weighting(self):
        issues = [{"severity": "medium"}]
        internal = self.calc.calculate_consistency_score(issues, "internal")
        public = self.calc.calculate_consistency_score(issues, "public_api")
        assert public < internal  # public_api has higher weight

    def test_many_issues_floor_at_zero(self):
        issues = [{"severity": "critical"}] * 50
        score = self.calc.calculate_consistency_score(issues)
        assert score == 0.0

    def test_calculate_quality_score_consistency_only(self):
        score = self.calc.calculate_quality_score({"consistency_score": 0.8})
        assert score == 0.8

    def test_calculate_quality_score_empty(self):
        score = self.calc.calculate_quality_score({})
        assert score == 0.5

    def test_calculate_quality_score_with_graph(self):
        metrics = {
            "consistency_score": 0.9,
            "graph_metrics": {"has_cycles": True, "connectivity_score": 0.5},
        }
        score = self.calc.calculate_quality_score(metrics)
        assert 0.0 < score < 1.0

    def test_calculate_quality_score_with_structural(self):
        metrics = {
            "consistency_score": 0.9,
            "structural_metrics": {"complexity": 15, "documentation_coverage": 0.3},
        }
        score = self.calc.calculate_quality_score(metrics)
        assert score < 0.9  # penalized
