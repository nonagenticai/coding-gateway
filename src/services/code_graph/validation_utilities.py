"""Module for shared validation utilities and helper functions."""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
import threading
import time
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


class ValidationCache:
    """
    Caching system for validation results with TTL support and size limits.
    """

    def __init__(self, max_size: int = 1000, default_ttl: int = 3600):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Any | None:
        """Get cached value if still valid."""
        with self._lock:
            if key not in self._cache:
                return None

            entry = self._cache[key]
            if time.time() > entry["expires"]:
                del self._cache[key]
                return None

            entry["last_accessed"] = time.time()
            return entry["value"]

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        """Set cached value with optional TTL."""
        if ttl is None:
            ttl = self.default_ttl

        with self._lock:
            if len(self._cache) >= self.max_size:
                self._evict_oldest()

            self._cache[key] = {
                "value": value,
                "created": time.time(),
                "expires": time.time() + ttl,
                "last_accessed": time.time(),
            }

    def __len__(self) -> int:
        """Return the number of cached entries."""
        return len(self._cache)

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()

    def _evict_oldest(self) -> None:
        """Evict the oldest accessed entry. Must be called with lock held."""
        if not self._cache:
            return
        oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k]["last_accessed"])
        del self._cache[oldest_key]


class ASTUtils:
    """Utility functions for working with Abstract Syntax Trees."""

    @staticmethod
    def safe_parse(code: str) -> ast.AST | None:
        """
        Safely parse code into AST.

        Args:
            code: Source code to parse

        Returns:
            Parsed AST or None if parsing fails
        """
        if not code or not code.strip():
            return None

        try:
            return ast.parse(code)
        except SyntaxError as e:
            logger.debug(f"Syntax error in code: {e}")
            return None
        except Exception as e:
            logger.debug(f"Failed to parse code: {e}")
            return None

    @staticmethod
    def extract_functions(tree: ast.AST) -> list[ast.FunctionDef]:
        """Extract all function definitions from AST."""
        if not tree:
            return []

        return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]

    @staticmethod
    def extract_classes(tree: ast.AST) -> list[ast.ClassDef]:
        """Extract all class definitions from AST."""
        if not tree:
            return []

        return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    @staticmethod
    def extract_imports(tree: ast.AST) -> dict[str, list[str]]:
        """
        Extract import information from AST.

        Returns:
            Dict with 'imports' and 'from_imports' lists
        """
        if not tree:
            return {"imports": [], "from_imports": []}

        imports = []
        from_imports = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for alias in node.names:
                    from_imports.append(f"{module}.{alias.name}")

        return {"imports": imports, "from_imports": from_imports}

    @staticmethod
    def get_function_info(func_node: ast.FunctionDef) -> dict[str, Any]:
        """
        Extract comprehensive information about a function.

        Args:
            func_node: Function AST node

        Returns:
            Dict containing function metadata
        """
        parameters: list[dict[str, str]] = []
        decorators: list[str] = []
        complexity_indicators: dict[str, int] = {
            "loops": 0,
            "conditions": 0,
            "try_except": 0,
            "recursive_calls": 0,
        }
        info: dict[str, Any] = {
            "name": func_node.name,
            "line_number": getattr(func_node, "lineno", 0),
            "parameters": parameters,
            "return_annotation": None,
            "decorators": decorators,
            "docstring": None,
            "has_return_statements": False,
            "is_async": isinstance(func_node, ast.AsyncFunctionDef),
            "complexity_indicators": complexity_indicators,
        }

        # Extract parameters
        for arg in func_node.args.args:
            param_info: dict[str, str] = {"name": arg.arg}
            if arg.annotation:
                param_info["annotation"] = ast.unparse(arg.annotation)
            parameters.append(param_info)

        # Extract return annotation
        if func_node.returns:
            info["return_annotation"] = ast.unparse(func_node.returns)

        # Extract decorators
        for decorator in func_node.decorator_list:
            decorators.append(ast.unparse(decorator))

        # Extract docstring
        if (
            func_node.body
            and isinstance(func_node.body[0], ast.Expr)
            and isinstance(func_node.body[0].value, ast.Constant)
            and isinstance(func_node.body[0].value.value, str)
        ):
            info["docstring"] = func_node.body[0].value.value

        # Analyze function body
        for node in ast.walk(func_node):
            if isinstance(node, ast.Return) and node.value is not None:
                info["has_return_statements"] = True
            elif isinstance(node, (ast.For, ast.While)):
                complexity_indicators["loops"] += 1
            elif isinstance(node, ast.If):
                complexity_indicators["conditions"] += 1
            elif isinstance(node, ast.Try):
                complexity_indicators["try_except"] += 1
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == func_node.name:
                    complexity_indicators["recursive_calls"] += 1

        return info

    @staticmethod
    def get_class_info(class_node: ast.ClassDef) -> dict[str, Any]:
        """
        Extract comprehensive information about a class.

        Args:
            class_node: Class AST node

        Returns:
            Dict containing class metadata
        """
        base_classes: list[str] = []
        methods: list[dict[str, Any]] = []
        attributes: list[str] = []
        class_decorators: list[str] = []
        is_abstract = False

        info: dict[str, Any] = {
            "name": class_node.name,
            "line_number": getattr(class_node, "lineno", 0),
            "base_classes": base_classes,
            "methods": methods,
            "attributes": attributes,
            "decorators": class_decorators,
            "docstring": None,
            "is_abstract": is_abstract,
        }

        # Extract base classes
        for base in class_node.bases:
            if isinstance(base, ast.Name):
                base_classes.append(base.id)
            elif isinstance(base, ast.Attribute):
                base_classes.append(ast.unparse(base))

        # Extract methods
        for body_node in class_node.body:
            if isinstance(body_node, ast.FunctionDef):
                method_info: dict[str, Any] = {
                    "name": body_node.name,
                    "is_private": body_node.name.startswith("_"),
                    "is_property": any(
                        ast.unparse(dec) == "property" for dec in body_node.decorator_list
                    ),
                    "is_static": any(
                        ast.unparse(dec) == "staticmethod" for dec in body_node.decorator_list
                    ),
                    "is_class_method": any(
                        ast.unparse(dec) == "classmethod" for dec in body_node.decorator_list
                    ),
                }
                methods.append(method_info)

        # Extract attributes (from __init__ method)
        init_method = next(
            (
                body_node
                for body_node in class_node.body
                if isinstance(body_node, ast.FunctionDef) and body_node.name == "__init__"
            ),
            None,
        )

        if init_method:
            for walk_node in ast.walk(init_method):
                if isinstance(walk_node, ast.Assign) and any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    for target in walk_node.targets
                ):
                    for target in walk_node.targets:
                        if (
                            isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"
                        ):
                            attributes.append(target.attr)

        # Extract decorators
        for decorator in class_node.decorator_list:
            decorator_name = ast.unparse(decorator)
            class_decorators.append(decorator_name)
            if "abstractmethod" in decorator_name or "ABC" in decorator_name:
                info["is_abstract"] = True

        # Extract docstring
        if (
            class_node.body
            and isinstance(class_node.body[0], ast.Expr)
            and isinstance(class_node.body[0].value, ast.Constant)
            and isinstance(class_node.body[0].value.value, str)
        ):
            info["docstring"] = class_node.body[0].value.value

        return info


class TextAnalysisUtils:
    """Utility functions for analyzing text descriptions and documentation."""

    @staticmethod
    def extract_keywords(text: str) -> set[str]:
        """
        Extract meaningful keywords from text.

        Args:
            text: Input text to analyze

        Returns:
            Set of extracted keywords
        """
        if not text:
            return set()

        # Simple keyword extraction
        # Remove common stop words and extract meaningful terms
        stop_words = {
            "the",
            "a",
            "an",
            "and",
            "or",
            "but",
            "in",
            "on",
            "at",
            "to",
            "for",
            "of",
            "with",
            "by",
            "is",
            "are",
            "was",
            "were",
            "be",
            "been",
            "being",
            "have",
            "has",
            "had",
            "do",
            "does",
            "did",
            "will",
            "would",
            "could",
            "should",
            "may",
            "might",
            "can",
            "this",
            "that",
            "these",
            "those",
        }

        # Clean and tokenize text
        cleaned_text = re.sub(r"[^\w\s]", " ", text.lower())
        words = cleaned_text.split()

        # Filter out stop words and short words
        keywords = {word for word in words if len(word) > 2 and word not in stop_words}

        return keywords

    @staticmethod
    def calculate_text_similarity(text1: str, text2: str) -> float:
        """
        Calculate similarity between two texts using keyword overlap.

        Args:
            text1: First text
            text2: Second text

        Returns:
            Similarity score between 0.0 and 1.0
        """
        if not text1 or not text2:
            return 0.0

        keywords1 = TextAnalysisUtils.extract_keywords(text1)
        keywords2 = TextAnalysisUtils.extract_keywords(text2)

        if not keywords1 or not keywords2:
            return 0.0

        # Calculate Jaccard similarity
        intersection = keywords1 & keywords2
        union = keywords1 | keywords2

        return len(intersection) / len(union) if union else 0.0

    @staticmethod
    def detect_mentioned_entities(text: str, entities: list[str]) -> list[str]:
        """
        Detect which entities from a list are mentioned in text.

        Args:
            text: Text to search in
            entities: List of entities to look for

        Returns:
            List of entities found in text
        """
        if not text or not entities:
            return []

        text_lower = text.lower()
        mentioned = []

        for entity in entities:
            if entity.lower() in text_lower:
                mentioned.append(entity)

        return mentioned

    @staticmethod
    def extract_code_patterns(text: str) -> dict[str, list[str]]:
        """
        Extract code-related patterns from text.

        Args:
            text: Text to analyze

        Returns:
            Dict containing different types of patterns found
        """
        patterns: dict[str, list[str]] = {
            "function_names": [],
            "class_names": [],
            "variable_names": [],
            "operations": [],
            "technologies": [],
        }

        if not text:
            return patterns

        # Pattern matching for different code elements
        function_pattern = r"\b([a-z_][a-z0-9_]*)\s*\("
        class_pattern = r"\b([A-Z][a-zA-Z0-9]*)\b"

        # Extract function names
        function_matches = re.findall(function_pattern, text)
        patterns["function_names"] = list(set(function_matches))

        # Extract potential class names (capitalized words)
        class_matches = re.findall(class_pattern, text)
        # Filter out common words that aren't likely class names
        common_words = {
            "The",
            "This",
            "That",
            "When",
            "Where",
            "How",
            "What",
            "API",
            "HTTP",
        }
        patterns["class_names"] = [
            match for match in set(class_matches) if match not in common_words
        ]

        # Extract operations and technologies
        tech_keywords = {
            "database": ["database", "db", "sql", "query", "table", "schema"],
            "web": ["http", "api", "rest", "json", "xml", "url", "endpoint"],
            "file": ["file", "json", "csv", "xml", "yaml", "config"],
            "async": ["async", "await", "asynchronous", "concurrent", "parallel"],
            "network": ["network", "socket", "tcp", "udp", "request", "response"],
        }

        text_lower = text.lower()
        for tech_type, keywords in tech_keywords.items():
            if any(keyword in text_lower for keyword in keywords):
                patterns["technologies"].append(tech_type)

        return patterns


class GraphUtils:
    """Utility functions for working with NetworkX graphs."""

    @staticmethod
    def create_dependency_graph(modules: dict[str, str]) -> nx.DiGraph[str]:
        """
        Create a dependency graph from module code.

        Args:
            modules: Dict mapping module names to their code

        Returns:
            NetworkX directed graph representing dependencies
        """
        graph: nx.DiGraph[str] = nx.DiGraph()

        # Parse all modules and extract imports
        module_imports: dict[str, dict[str, list[str]]] = {}
        for module_name, code in modules.items():
            tree = ASTUtils.safe_parse(code)
            if tree:
                imports = ASTUtils.extract_imports(tree)
                module_imports[module_name] = imports
                graph.add_node(module_name)

        # Add edges for dependencies
        for module_name, imports in module_imports.items():
            for imported_module in imports["imports"]:
                if imported_module in modules:
                    graph.add_edge(module_name, imported_module)

            for from_import in imports["from_imports"]:
                # Extract module name from from_import
                if "." in from_import:
                    base_module = from_import.split(".")[0]
                    if base_module in modules:
                        graph.add_edge(module_name, base_module)

        return graph

    @staticmethod
    def analyze_graph_metrics(graph: nx.DiGraph) -> dict[str, Any]:
        """
        Analyze graph metrics for dependency analysis.

        Args:
            graph: NetworkX directed graph

        Returns:
            Dict containing various graph metrics
        """
        if not graph.nodes():
            return {
                "node_count": 0,
                "edge_count": 0,
                "density": 0.0,
                "is_connected": False,
                "cycles": [],
                "strongly_connected_components": 0,
            }

        metrics = {
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
            "density": nx.density(graph),
            "is_connected": nx.is_weakly_connected(graph),
            "strongly_connected_components": nx.number_strongly_connected_components(graph),
        }

        # Find cycles
        try:
            cycles = list(nx.simple_cycles(graph))
            metrics["cycles"] = cycles
            metrics["has_cycles"] = len(cycles) > 0
        except Exception:
            metrics["cycles"] = []
            metrics["has_cycles"] = False

        # Calculate centrality metrics for key nodes
        if graph.nodes():
            try:
                in_degree_centrality = nx.in_degree_centrality(graph)
                out_degree_centrality = nx.out_degree_centrality(graph)

                metrics["most_depended_on"] = (
                    max(in_degree_centrality.items(), key=lambda x: x[1])[0]
                    if in_degree_centrality
                    else None
                )

                metrics["most_dependent"] = (
                    max(out_degree_centrality.items(), key=lambda x: x[1])[0]
                    if out_degree_centrality
                    else None
                )

            except Exception:
                metrics["most_depended_on"] = None
                metrics["most_dependent"] = None

        return metrics


class HashUtils:
    """Utility functions for creating hashes and checksums."""

    @staticmethod
    def hash_code(code: str) -> str:
        """Create a hash of code content."""
        return hashlib.md5(code.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def hash_dict(data: dict[str, Any]) -> str:
        """Create a hash of dictionary content."""
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.md5(json_str.encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def create_cache_key(*args) -> str:
        """Create a cache key from multiple arguments."""
        key_parts = []
        for arg in args:
            if isinstance(arg, str):
                key_parts.append(arg)
            elif isinstance(arg, dict):
                key_parts.append(HashUtils.hash_dict(arg))
            else:
                key_parts.append(str(hash(str(arg))))

        return ":".join(key_parts)


class ValidationScoreCalculator:
    """Utility for calculating standardized validation scores."""

    def __init__(self):
        """Initialize score calculator with default weights."""
        self.severity_weights = {
            "critical": 1.0,
            "high": 0.7,
            "medium": 0.4,
            "low": 0.1,
        }
        self.context_weights = {
            "public_api": 1.2,
            "internal": 1.0,
            "test": 0.8,
            "example": 0.6,
        }

    def calculate_consistency_score(
        self, issues: list[dict[str, Any]], context: str = "internal"
    ) -> float:
        """
        Calculate a consistency score based on detected issues.

        Args:
            issues: List of issues with severity information
            context: Context type for weighting

        Returns:
            Score between 0.0 (worst) and 1.0 (perfect)
        """
        if not issues:
            return 1.0

        total_penalty = 0.0
        context_weight = self.context_weights.get(context, 1.0)

        for issue in issues:
            severity = issue.get("severity", "medium")
            severity_weight = self.severity_weights.get(severity, 0.5)
            penalty = (
                severity_weight * context_weight * 0.1
            )  # Each issue reduces score by up to 10%
            total_penalty += penalty

        # Calculate final score
        score = 1.0 - min(total_penalty, 1.0)
        return max(0.0, score)

    def calculate_quality_score(self, metrics: dict[str, Any]) -> float:
        """
        Calculate an overall quality score from various metrics.

        Args:
            metrics: Dict containing various quality metrics

        Returns:
            Quality score between 0.0 and 1.0
        """
        scores = []

        # Consistency score
        if "consistency_score" in metrics:
            scores.append(metrics["consistency_score"])

        # Structural score (based on code organization)
        if "structural_metrics" in metrics:
            structural = metrics["structural_metrics"]
            struct_score = 1.0

            # Penalize for high complexity
            if structural.get("complexity", 0) > 10:
                struct_score -= 0.2

            # Penalize for missing documentation
            if structural.get("documentation_coverage", 1.0) < 0.5:
                struct_score -= 0.3

            scores.append(max(0.0, struct_score))

        # Graph health score
        if "graph_metrics" in metrics:
            graph = metrics["graph_metrics"]
            graph_score = 1.0

            # Penalize for cycles
            if graph.get("has_cycles", False):
                graph_score -= 0.4

            # Penalize for poor connectivity
            if graph.get("connectivity_score", 1.0) < 0.7:
                graph_score -= 0.2

            scores.append(max(0.0, graph_score))

        # Return average of all scores
        return sum(scores) / len(scores) if scores else 0.5


# Create global instances for common use
validation_cache = ValidationCache()
score_calculator = ValidationScoreCalculator()
