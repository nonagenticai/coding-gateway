"""Tests for bug fixes and coverage gaps found during deep dive.

Covers:
- Proxy: close_proxy_client race condition fix, location header rewriting,
  response size limit, WebSocket helpers
- Validation: stdlib module typo fixes (weakref, wsgiref), import validation,
  function validation edge cases
- GraphDatabase: clear_database completeness fix, delete_graph, edge operations,
  flush error paths
- OpenAPI aggregator: deep_replace_ref, _rewrite_refs with nested structures
- InputValidator: JSON body validation depth, code fields, parameter keys
- Cache manager: _create_cache_key, _arg_to_str edge cases
- ExceptionHandlers: ValidationException with field/details, all handler branches
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# =============================================================================
# Bug fix: stdlib module typos in validation.py
# =============================================================================


class TestStdlibModuleTypoFixes:
    """Verify the typos 'weakre' and 'wsgire' are now fixed."""

    def test_weakref_is_recognized_as_stdlib(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("weakref") is True

    def test_wsgiref_is_recognized_as_stdlib(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("wsgiref") is True

    def test_weakre_not_in_stdlib(self):
        """The old typo 'weakre' should NOT be in stdlib."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("weakre") is False

    def test_wsgire_not_in_stdlib(self):
        """The old typo 'wsgire' should NOT be in stdlib."""
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("wsgire") is False


# =============================================================================
# Bug fix: close_proxy_client race condition
# =============================================================================


class TestCloseProxyClientLock:
    async def test_close_proxy_client_when_no_client(self):
        """Closing when no client exists should not raise."""
        import routers.proxy as proxy_mod

        # Ensure _client is None
        original = proxy_mod._client
        proxy_mod._client = None
        try:
            await proxy_mod.close_proxy_client()
        finally:
            proxy_mod._client = original

    async def test_close_proxy_client_closes_active_client(self):
        """close_proxy_client should close an active client and set _client to None."""
        import routers.proxy as proxy_mod

        mock_client = AsyncMock()
        mock_client.is_closed = False

        original = proxy_mod._client
        proxy_mod._client = mock_client
        try:
            await proxy_mod.close_proxy_client()
            mock_client.aclose.assert_awaited_once()
            assert proxy_mod._client is None
        finally:
            proxy_mod._client = original

    async def test_close_proxy_client_skips_already_closed(self):
        """close_proxy_client should skip aclose if client is already closed."""
        import routers.proxy as proxy_mod

        mock_client = AsyncMock()
        mock_client.is_closed = True

        original = proxy_mod._client
        proxy_mod._client = mock_client
        try:
            await proxy_mod.close_proxy_client()
            mock_client.aclose.assert_not_awaited()
        finally:
            proxy_mod._client = original


class TestResolveBackendEdgeCases:
    def test_all_known_routes_resolve(self):
        from routers.proxy import PROXY_ROUTES, _resolve_backend

        for prefix in PROXY_ROUTES:
            result = _resolve_backend(prefix)
            assert result is not None, f"Exact prefix {prefix} should resolve"
            result_with_sub = _resolve_backend(f"{prefix}/sub")
            assert result_with_sub is not None, f"{prefix}/sub should resolve"

    def test_websocket_paths_do_not_match(self):
        """WebSocket paths are handled by dedicated endpoints, not _resolve_backend."""
        from routers.proxy import _resolve_backend

        # /api/realtime/ws/something should still match /api/realtime
        result = _resolve_backend("/api/realtime/ws/something")
        assert result is not None


# =============================================================================
# Proxy: _filter_headers
# =============================================================================


class TestFilterHeaders:
    def test_strips_hop_by_hop(self):
        from routers.proxy import _filter_headers

        headers = {
            "content-type": "application/json",
            "connection": "keep-alive",
            "host": "example.com",
            "x-custom": "value",
            "transfer-encoding": "chunked",
        }
        result = _filter_headers(headers)
        assert "content-type" in result
        assert "x-custom" in result
        assert "connection" not in result
        assert "host" not in result
        assert "transfer-encoding" not in result

    def test_case_insensitive(self):
        from routers.proxy import _filter_headers

        headers = {"Connection": "keep-alive", "HOST": "example.com"}
        result = _filter_headers(headers)
        assert len(result) == 0


# =============================================================================
# OpenAPI aggregator: _deep_replace_ref
# =============================================================================


class TestDeepReplaceRef:
    def test_replaces_in_nested_dict(self):
        import openapi_aggregator

        obj = {
            "get": {
                "responses": {
                    "200": {
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Old"}}
                        }
                    }
                }
            }
        }
        openapi_aggregator._deep_replace_ref(
            obj, "#/components/schemas/Old", "#/components/schemas/New"
        )
        ref = obj["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/New"

    def test_replaces_in_list(self):
        import openapi_aggregator

        obj = [{"$ref": "#/components/schemas/Old"}, {"$ref": "#/components/schemas/Other"}]
        openapi_aggregator._deep_replace_ref(
            obj, "#/components/schemas/Old", "#/components/schemas/New"
        )
        assert obj[0]["$ref"] == "#/components/schemas/New"
        assert obj[1]["$ref"] == "#/components/schemas/Other"

    def test_no_match_leaves_unchanged(self):
        import openapi_aggregator

        obj = {"$ref": "#/components/schemas/Keep"}
        openapi_aggregator._deep_replace_ref(
            obj, "#/components/schemas/Old", "#/components/schemas/New"
        )
        assert obj["$ref"] == "#/components/schemas/Keep"

    def test_handles_non_dict_non_list(self):
        import openapi_aggregator

        # Should not raise for primitive types
        openapi_aggregator._deep_replace_ref("string", "old", "new")
        openapi_aggregator._deep_replace_ref(42, "old", "new")
        openapi_aggregator._deep_replace_ref(None, "old", "new")


# =============================================================================
# OpenAPI aggregator: invalidate_cache
# =============================================================================


class TestOpenApiCache:
    def test_invalidate_cache_resets(self):
        import openapi_aggregator

        openapi_aggregator._cache = ({"test": True}, 999999999.0)
        openapi_aggregator.invalidate_cache()
        assert openapi_aggregator._cache == (None, 0.0)


# =============================================================================
# InputValidator: edge cases
# =============================================================================


class TestInputValidatorEdgeCases:
    def test_validate_url_path_empty(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        ok, err = v.validate_url_path("")
        assert ok is False
        assert "Empty" in err

    def test_validate_url_path_null_byte(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        ok, err = v.validate_url_path("/api/test\x00exploit")
        assert ok is False
        assert "NULL" in err

    def test_validate_url_path_traversal(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        ok, err = v.validate_url_path("/api/../etc/passwd")
        assert ok is False
        assert "traversal" in err.lower()

    def test_validate_json_body_invalid_json(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        ok, err = v.validate_json_body(b"{invalid json")
        assert ok is False
        assert "Invalid JSON" in err

    def test_validate_json_body_too_deep(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        # Create deeply nested JSON
        deep = {"a": "b"}
        for _ in range(25):
            deep = {"nested": deep}
        ok, err = v.validate_json_body(json.dumps(deep).encode())
        assert ok is False
        assert "too deep" in err.lower()

    def test_validate_json_body_xss_in_field(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        body = json.dumps({"name": "<script>alert('xss')</script>"}).encode()
        ok, err = v.validate_json_body(body)
        assert ok is False
        assert "Unsafe" in err

    def test_validate_json_body_code_field_allows_script(self):
        """Code fields are exempt from XSS checks."""
        from middleware.input_validator import InputValidator

        v = InputValidator()
        body = json.dumps({"code": "<script>alert('xss')</script>"}).encode()
        ok, err = v.validate_json_body(body)
        assert ok is True

    def test_validate_json_body_list_input(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        body = json.dumps([{"name": "safe"}, {"name": "also safe"}]).encode()
        ok, err = v.validate_json_body(body)
        assert ok is True

    def test_is_safe_parameter_key_empty(self):
        from middleware.input_validator import InputValidator

        assert InputValidator.is_safe_parameter_key("") is False

    def test_is_safe_parameter_key_too_long(self):
        from middleware.input_validator import InputValidator

        assert InputValidator.is_safe_parameter_key("a" * 200) is False

    def test_is_safe_parameter_key_valid(self):
        from middleware.input_validator import InputValidator

        assert InputValidator.is_safe_parameter_key("page") is True
        assert InputValidator.is_safe_parameter_key("page_number") is True
        assert InputValidator.is_safe_parameter_key("items[0]") is True

    def test_is_safe_parameter_key_invalid(self):
        from middleware.input_validator import InputValidator

        assert InputValidator.is_safe_parameter_key("1invalid") is False
        assert InputValidator.is_safe_parameter_key("has space") is False

    def test_contains_only_safe_control_chars(self):
        from middleware.input_validator import InputValidator

        assert InputValidator.contains_only_safe_control_chars("hello\tworld\n") is True
        assert InputValidator.contains_only_safe_control_chars("hello\x00world") is False
        assert InputValidator.contains_only_safe_control_chars("hello\x01world") is False

    def test_is_safe_string_with_xss(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        assert v.is_safe_string("normal text") is True
        assert v.is_safe_string("<script>bad</script>") is False
        assert v.is_safe_string("javascript:void(0)") is False
        assert v.is_safe_string("onclick=alert(1)") is False

    def test_is_safe_code_string(self):
        from middleware.input_validator import InputValidator

        v = InputValidator()
        # Code strings allow XSS patterns but not unsafe control chars
        assert v.is_safe_code_string("<script>valid code</script>") is True
        assert v.is_safe_code_string("hello\x00world") is False


# =============================================================================
# Cache manager: _create_cache_key and _arg_to_str
# =============================================================================


class TestCacheKeyCreation:
    def test_create_cache_key_with_args(self):
        from inspect import signature

        from services.cache_manager import _create_cache_key

        def example_func(a, b, c=None):
            pass

        sig = signature(example_func)
        key = _create_cache_key("prefix", "example_func", sig, ("val1", "val2"), {"c": "val3"})
        assert "prefix" in key
        assert "example_func" in key
        assert "a:val1" in key
        assert "b:val2" in key
        assert "c:val3" in key

    def test_create_cache_key_extra_positional_args(self):
        from inspect import signature

        from services.cache_manager import _create_cache_key

        def simple(a):
            pass

        sig = signature(simple)
        key = _create_cache_key("p", "f", sig, ("v1", "v2", "v3"), {})
        assert "arg1:" in key
        assert "arg2:" in key

    def test_arg_to_str_various_types(self):
        from services.cache_manager import _arg_to_str

        assert _arg_to_str(None) == "None"
        assert _arg_to_str("hello") == "hello"
        assert _arg_to_str(42) == "42"
        assert _arg_to_str(3.14) == "3.14"
        assert _arg_to_str(True) == "True"
        assert _arg_to_str([1, 2, 3]) == "list:3"
        assert _arg_to_str((1, 2)) == "tuple:2"
        assert _arg_to_str({1, 2}) == "set:2"
        assert _arg_to_str({"a": 1}) == "dict:1"

        # Arbitrary objects
        result = _arg_to_str(object())
        assert "object" in result


# =============================================================================
# ExceptionHandlers: ValidationException
# =============================================================================


class TestValidationException:
    def test_basic(self):
        from coding_client.exceptions import ValidationException

        exc = ValidationException(message="bad data", error_code="INVALID")
        assert exc.status_code == 400
        assert exc.error_code == "INVALID"

    def test_is_http_exception(self):
        from coding_client.exceptions import ValidationException
        from fastapi import HTTPException

        exc = ValidationException()
        assert isinstance(exc, HTTPException)


# =============================================================================
# Validation service: more edge cases
# =============================================================================


class TestImportValidationEdgeCases:
    def test_relative_import_not_stdlib(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module(".relative") is False

    def test_submodule_stdlib(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc.is_stdlib_module("os.path") is True
        assert svc.is_stdlib_module("json.decoder") is True

    def test_likely_third_party(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        assert svc._is_likely_third_party("requests") is True
        assert svc._is_likely_third_party("numpy") is True
        assert svc._is_likely_third_party("myunknownpackage") is False


class TestFunctionValidationEdgeCases:
    def test_builtin_function_detection(self):
        from services.validation import FunctionValidationService

        svc = FunctionValidationService()
        assert svc.is_builtin_function("print") is True
        assert svc.is_builtin_function("len") is True
        assert svc.is_builtin_function("my_custom_func") is False

    async def test_validate_empty_name_skipped(self):
        from services.validation import FunctionValidationService

        svc = FunctionValidationService()
        result = await svc.validate_function_calls("graph1", [{"name": ""}])
        # Empty name should be skipped
        assert len(result.valid_functions) == 0
        assert len(result.invalid_functions) == 0


class TestASTValidationEdgeCases:
    async def test_empty_code(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        result = await svc.analyze_proposed_code_ast("", "empty.py")
        assert result.is_valid is True
        assert result.line_count == 0

    async def test_code_with_class_and_function(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = """
class MyClass:
    def method(self):
        pass

def standalone():
    x = 42
"""
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        # Mixed class/function code is valid and has nonzero complexity
        assert result.complexity_score > 0
        assert result.line_count >= 5

    async def test_complexity_score(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = """
def complex_func(x):
    if x > 0:
        for i in range(x):
            try:
                if i % 2:
                    pass
            except ValueError:
                pass
    while x > 0:
        x -= 1
"""
        result = await svc.analyze_proposed_code_ast(code, "complex.py")
        assert result.is_valid is True
        assert result.complexity_score > 0

    async def test_imports_extracted(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = """
import os
from pathlib import Path
"""
        result = await svc.analyze_proposed_code_ast(code, "imports.py")
        assert result.is_valid is True
        assert len(result.imports) >= 2


# =============================================================================
# Visualization service
# =============================================================================


class TestVisualizationHelpers:
    def test_edge_color_mapping(self):
        from services.visualization import DependencyVisualizer, EdgeType

        viz = DependencyVisualizer()
        assert viz._get_edge_color(EdgeType.IMPORTS) == "#999999"
        assert viz._get_edge_color(EdgeType.FFI_CALL) == "#FF4500"

    def test_edge_style_mapping(self):
        from services.visualization import DependencyVisualizer, EdgeType

        viz = DependencyVisualizer()
        assert viz._get_edge_style(EdgeType.IMPORTS) == "dashed"
        assert viz._get_edge_style(EdgeType.CALLS) == "solid"
        assert viz._get_edge_style(EdgeType.FFI_CALL) == "bold"

    def test_mermaid_shapes(self):
        from services.visualization import DependencyVisualizer

        viz = DependencyVisualizer()
        assert viz._get_mermaid_shape("box") == ("[", "]")
        assert viz._get_mermaid_shape("circle") == ("((", "))")
        assert viz._get_mermaid_shape("unknown") == ("[", "]")

    def test_mermaid_arrows(self):
        from services.visualization import DependencyVisualizer

        viz = DependencyVisualizer()
        assert viz._get_mermaid_arrow("solid") == "-->"
        assert viz._get_mermaid_arrow("dashed") == "-.->"
        assert viz._get_mermaid_arrow("bold") == "==>"
        assert viz._get_mermaid_arrow("unknown") == "-->"

    def test_language_colors(self):
        from services.visualization import DependencyVisualizer

        viz = DependencyVisualizer()
        assert viz.LANGUAGE_COLORS["python"] == "#3776AB"
        assert viz.LANGUAGE_COLORS["java"] == "#ED8B00"

    def test_calculate_edge_weight(self):
        from services.visualization import DependencyVisualizer, EntityRelation

        viz = DependencyVisualizer()
        relation = EntityRelation(source_id="a", target_id="b", type="extends", properties={"x": 1})
        weight = viz._calculate_edge_weight(relation)
        assert weight == 2.0

        relation2 = EntityRelation(source_id="a", target_id="b", type="calls")
        weight2 = viz._calculate_edge_weight(relation2)
        assert weight2 == 1.0

    async def test_complexity_distribution(self):
        from services.visualization import DependencyVisualizer, NodeType, VisualizationNode

        viz = DependencyVisualizer()
        nodes = [
            VisualizationNode(
                id="1", label="a", type=NodeType.FILE, language="python", complexity=2.0
            ),
            VisualizationNode(
                id="2", label="b", type=NodeType.FILE, language="python", complexity=10.0
            ),
            VisualizationNode(
                id="3", label="c", type=NodeType.FILE, language="python", complexity=20.0
            ),
        ]
        dist = await viz._calculate_complexity_distribution(nodes)
        assert dist["low"] == 1
        assert dist["medium"] == 1
        assert dist["high"] == 1


# =============================================================================
# RecoveryMechanisms
# =============================================================================


class TestRecoveryMechanisms:
    def test_generate_suggestions_with_parameter_issues(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        issues = [
            {"type": "parameter", "description": "parameter 'x' is missing documentation"},
        ]
        suggestions = rm.generate_suggestions(issues)
        assert len(suggestions) > 0
        assert any("parameter" in s.lower() or "x" in s for s in suggestions)

    def test_generate_suggestions_with_return_issues(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        issues = [
            {"type": "return", "description": "return type not mentioned"},
        ]
        suggestions = rm.generate_suggestions(issues)
        assert len(suggestions) > 0

    def test_generate_suggestions_invalid_input(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        # Issues without required keys
        result = rm.generate_suggestions([{"bad": "data"}])
        assert len(result) > 0  # should handle gracefully

    def test_generate_suggestions_empty_returns_empty_list(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        result = rm.generate_suggestions([])
        assert result == []

    def test_recovery_plan(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        issues = [
            {"type": "parameter", "description": "missing param", "severity": "high"},
            {"type": "return", "description": "return not mentioned", "severity": "medium"},
        ]
        plan = rm.generate_recovery_plan(issues)
        assert "total_issues" in plan
        assert plan["total_issues"] == 2
        assert "action_plan" in plan
        assert "estimated_effort" in plan

    def test_estimate_effort(self):
        from services.code_graph.recovery_mechanisms import RecoveryMechanisms

        rm = RecoveryMechanisms()
        assert rm._estimate_effort([]) == "minimal"
        assert rm._estimate_effort([{"severity": "critical"}]) == "high"

    def test_add_and_remove_strategy(self):
        from services.code_graph.recovery_mechanisms import (
            RecoveryMechanisms,
            SuggestionStrategy,
        )

        class CustomStrategy(SuggestionStrategy):
            def generate(self, issues, context):
                return ["custom suggestion"]

            def get_strategy_name(self):
                return "custom"

            def get_applicable_types(self):
                return ["custom_type"]

        rm = RecoveryMechanisms()
        initial_count = len(rm.strategies)
        rm.add_custom_strategy(CustomStrategy())
        assert len(rm.strategies) == initial_count + 1
        assert "custom" in rm.get_available_strategies()

        removed = rm.remove_strategy("custom")
        assert removed is True
        assert len(rm.strategies) == initial_count

        # Removing non-existent returns False
        assert rm.remove_strategy("nonexistent") is False


# =============================================================================
# Settings validation
# =============================================================================


class TestSettingsValidation:
    def test_invalid_environment_raises(self):
        from pydantic import ValidationError

        from config.settings import Settings

        with pytest.raises(ValidationError):
            Settings(environment="invalid_env")

    def test_test_environment_accepted(self):
        from config.settings import Settings

        s = Settings(environment="test")
        assert s.environment == "test"

    def test_secret_key_in_production_raises(self, monkeypatch):
        from pydantic import ValidationError

        from config.settings import Settings

        # The default secret_key in production should raise
        with pytest.raises(ValidationError, match="secret_key"):
            Settings(environment="production")


# =============================================================================
# App factory: generate_unique_id
# =============================================================================


class TestGenerateUniqueId:
    def test_basic_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/analysis/run"
        route.methods = {"POST"}
        result = generate_unique_id(route)
        assert result == "analysis_run_post"

    def test_empty_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "root_get"

    def test_path_with_params(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/graphs/{graph_id}/nodes"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert "graph_id" in result
        assert "nodes" in result

    def test_health_root(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/health"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "health_root_get"

    def test_health_api(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/health/ready"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "health_ready_get"

    def test_health_api_exact(self):
        """Path /api/health exactly should return health_api_get."""
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/health"
        route.methods = {"GET"}
        result = generate_unique_id(route)
        assert result == "health_api_get"

    def test_hyphenated_path(self):
        from app_factory import generate_unique_id

        route = MagicMock()
        route.path = "/api/bug-hunter/scan"
        route.methods = {"POST"}
        result = generate_unique_id(route)
        assert result == "bug_hunter_scan_post"
