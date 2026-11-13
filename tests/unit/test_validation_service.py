"""Tests for services/validation.py."""

from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")

from unittest.mock import AsyncMock, patch

import pytest

from services.validation import (
    ASTAnalysisResult,
    ASTValidationService,
    FunctionValidationResult,
    FunctionValidationService,
    ImportValidationResult,
    ImportValidationService,
)

# =============================================================================
# ASTValidationService
# =============================================================================


@pytest.fixture
def ast_svc():
    """ASTValidationService with stub extractor."""
    return ASTValidationService()


class TestASTValidationService:
    async def test_valid_python(self, ast_svc):
        """Valid Python code produces a successful result."""
        code = "def hello():\n    return 42\n"
        result = await ast_svc.analyze_proposed_code_ast(code, "test.py")
        assert isinstance(result, ASTAnalysisResult)
        assert result.is_valid is True
        assert result.syntax_errors == []
        assert result.line_count == 2

    async def test_syntax_error(self, ast_svc):
        """Invalid Python produces is_valid=False with syntax errors."""
        code = "def hello(\n"
        result = await ast_svc.analyze_proposed_code_ast(code, "bad.py")
        assert result.is_valid is False
        assert len(result.syntax_errors) > 0

    async def test_unsupported_language(self, ast_svc):
        """Non-Python language returns is_valid=False."""
        result = await ast_svc.analyze_proposed_code_ast("fn main() {}", "test.rs", language="rust")
        assert result.is_valid is False
        assert "rust" in result.syntax_errors[0].lower()

    async def test_imports_extracted(self, ast_svc):
        """Imports are extracted from AST."""
        code = "import os\nfrom pathlib import Path\n"
        result = await ast_svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.imports) == 2

    async def test_variables_extracted(self, ast_svc):
        """Variable assignments are extracted."""
        code = "x = 10\ny = 20\n"
        result = await ast_svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.variables) == 2

    async def test_complexity_score(self, ast_svc):
        """Complexity score is calculated."""
        code = "def f():\n    if True:\n        for x in range(10):\n            pass\n"
        result = await ast_svc.analyze_proposed_code_ast(code, "test.py")
        assert result.complexity_score > 0

    async def test_empty_code(self, ast_svc):
        """Empty code is valid Python."""
        result = await ast_svc.analyze_proposed_code_ast("", "empty.py")
        assert result.is_valid is True

    async def test_exception_in_analysis(self, ast_svc):
        """Unexpected exception returns error result."""
        with patch.object(ast_svc, "_analyze_python_ast", side_effect=RuntimeError("boom")):
            result = await ast_svc.analyze_proposed_code_ast("x = 1", "test.py")
        assert result.is_valid is False
        assert "boom" in result.syntax_errors[0]


# =============================================================================
# ImportValidationService
# =============================================================================


@pytest.fixture
def import_svc():
    """ImportValidationService with default stubs."""
    return ImportValidationService(project_root="/tmp/test-project")


class TestImportValidationService:
    async def test_stdlib_import(self, import_svc):
        """Standard library imports are categorized correctly."""
        imports = [{"module": "os", "type": "import", "line": 1}]
        result = await import_svc.validate_imports("graph1", imports)
        assert isinstance(result, ImportValidationResult)
        assert "os" in result.stdlib_imports
        assert len(result.valid_imports) == 1

    async def test_empty_module_skipped(self, import_svc):
        """Empty module name is skipped."""
        imports = [{"module": "", "type": "import"}]
        result = await import_svc.validate_imports("graph1", imports)
        assert len(result.valid_imports) == 0

    async def test_unknown_third_party(self, import_svc):
        """Unknown third-party module goes to invalid."""
        imports = [{"module": "nonexistent_package_xyz", "type": "import", "line": 1}]
        result = await import_svc.validate_imports("graph1", imports)
        assert len(result.invalid_imports) == 1
        assert "nonexistent_package_xyz" in result.missing_dependencies

    async def test_known_third_party(self, import_svc):
        """Known third-party with suggestion."""
        imports = [{"module": "requests", "type": "import", "line": 1}]
        result = await import_svc.validate_imports("graph1", imports)
        # requests is in _is_likely_third_party set, so it goes to invalid+missing
        assert "requests" in result.missing_dependencies

    async def test_is_stdlib_module(self, import_svc):
        assert import_svc.is_stdlib_module("os") is True
        assert import_svc.is_stdlib_module("json") is True
        assert import_svc.is_stdlib_module("pathlib") is True
        assert import_svc.is_stdlib_module("numpy") is False

    async def test_relative_import_not_stdlib(self, import_svc):
        assert import_svc.is_stdlib_module(".local_module") is False

    async def test_exception_in_validation(self, import_svc):
        """Exception during validation adds to invalid_imports."""
        imports = [{"module": "os", "type": "import"}]
        with patch.object(import_svc, "is_stdlib_module", side_effect=Exception("fail")):
            result = await import_svc.validate_imports("g1", imports)
        assert len(result.invalid_imports) == 1

    async def test_multiple_imports(self, import_svc):
        """Multiple imports are processed."""
        imports = [
            {"module": "os", "type": "import", "line": 1},
            {"module": "sys", "type": "import", "line": 2},
            {"module": "json", "type": "import", "line": 3},
        ]
        result = await import_svc.validate_imports("g1", imports)
        assert len(result.stdlib_imports) == 3
        assert len(result.valid_imports) == 3


# =============================================================================
# FunctionValidationService
# =============================================================================


@pytest.fixture
def func_svc():
    return FunctionValidationService()


class TestFunctionValidationService:
    async def test_builtin_function(self, func_svc):
        """Built-in functions are recognized."""
        calls = [{"name": "len", "line": 1}]
        result = await func_svc.validate_function_calls("g1", calls)
        assert isinstance(result, FunctionValidationResult)
        assert "len" in result.builtin_functions
        assert len(result.valid_functions) == 1

    async def test_empty_name_skipped(self, func_svc):
        """Empty function name is skipped."""
        calls = [{"name": "", "line": 1}]
        result = await func_svc.validate_function_calls("g1", calls)
        assert len(result.valid_functions) == 0

    async def test_local_function_in_graph(self, func_svc):
        """Function found in graph (via coding-analysis) is categorized as local."""
        calls = [{"name": "my_func", "line": 1}]
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=True),
        ):
            result = await func_svc.validate_function_calls("g1", calls)
        assert "my_func" in result.local_functions

    async def test_external_function(self, func_svc):
        """Function not in graph and not builtin is external."""
        calls = [{"name": "custom_func", "line": 1}]
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=False),
        ):
            result = await func_svc.validate_function_calls("g1", calls)
        assert "custom_func" in result.external_functions

    async def test_exception_adds_to_invalid(self, func_svc):
        """Exception in the outer try block adds to invalid_functions."""
        # Patch is_builtin_function to raise, which triggers the outer except
        with patch.object(func_svc, "is_builtin_function", side_effect=Exception("boom")):
            calls = [{"name": "bad_func", "line": 1}]
            result = await func_svc.validate_function_calls("g1", calls)
        assert len(result.invalid_functions) == 1

    async def test_graph_miss_is_external(self, func_svc):
        """Graph miss: function goes to external (not builtin, analysis returns False)."""
        calls = [{"name": "some_func", "line": 1}]
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=False),
        ):
            result = await func_svc.validate_function_calls("g1", calls)
        assert "some_func" in result.external_functions

    def test_is_builtin_function(self, func_svc):
        assert func_svc.is_builtin_function("print") is True
        assert func_svc.is_builtin_function("len") is True
        assert func_svc.is_builtin_function("my_custom_func") is False

    async def test_multiple_calls(self, func_svc):
        """Multiple function calls are all processed."""
        calls = [
            {"name": "len", "line": 1},
            {"name": "custom1", "line": 2},
            {"name": "print", "line": 3},
        ]
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=False),
        ):
            result = await func_svc.validate_function_calls("g1", calls)
        assert len(result.builtin_functions) == 2  # len, print
        assert len(result.external_functions) == 1  # custom1
        assert len(result.valid_functions) == 3


# =============================================================================
# ASTValidationService - helper methods
# =============================================================================


class TestASTHelpers:
    def test_extract_import_info_import(self, ast_svc):
        """_extract_import_info handles ast.Import."""
        import ast

        node = ast.parse("import os").body[0]
        result = ast_svc._extract_import_info(node)
        assert len(result) == 1
        assert result[0]["module"] == "os"

    def test_extract_import_info_from(self, ast_svc):
        """_extract_import_info handles ast.ImportFrom."""
        import ast

        node = ast.parse("from os.path import join").body[0]
        result = ast_svc._extract_import_info(node)
        assert len(result) == 1
        assert "os.path.join" in result[0]["module"]

    def test_extract_variable_info(self, ast_svc):
        """_extract_variable_info extracts assignment targets."""
        import ast

        node = ast.parse("x = 1").body[0]
        result = ast_svc._extract_variable_info(node)
        assert len(result) == 1
        assert result[0]["name"] == "x"

    def test_calculate_complexity_score(self, ast_svc):
        """_calculate_complexity_score returns a float."""
        import ast

        tree = ast.parse("if True:\n    pass\n")
        score = ast_svc._calculate_complexity_score(tree)
        assert isinstance(score, float)
        assert score > 0

    def test_get_attribute_name_simple(self, ast_svc):
        """_get_attribute_name handles Name node."""
        import ast

        node = ast.parse("x", mode="eval").body
        assert ast_svc._get_attribute_name(node) == "x"

    def test_get_attribute_name_attribute(self, ast_svc):
        """_get_attribute_name handles Attribute node."""
        import ast

        node = ast.parse("os.path", mode="eval").body
        assert ast_svc._get_attribute_name(node) == "os.path"


# =============================================================================
# _analysis_node_exists — coding-analysis HTTP node-existence helper
# =============================================================================


def _node_resp(status, nodes=None):
    import httpx

    req = httpx.Request("GET", "http://coding-analysis/api/analysis/graphs/g/nodes")
    body = {"nodes": nodes or []}
    return httpx.Response(status, json=body, request=req)


class TestAnalysisNodeExists:
    async def test_returns_true_on_exact_name_match(self):
        from services.validation import _analysis_node_exists

        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_node_resp(200, [{"properties": {"name": "my_module"}}])
        )
        with patch("routers.proxy.get_proxy_client", AsyncMock(return_value=client)):
            result = await _analysis_node_exists("g1", "my_module", ("module", "file"))
        assert result is True

    async def test_returns_false_when_no_exact_match(self):
        """A substring/name_pattern hit that is not an exact name is not a match."""
        from services.validation import _analysis_node_exists

        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_node_resp(200, [{"properties": {"name": "my_module_helper"}}])
        )
        with patch("routers.proxy.get_proxy_client", AsyncMock(return_value=client)):
            result = await _analysis_node_exists("g1", "my_module", ("module",))
        assert result is False

    async def test_non_200_status_is_skipped(self):
        from services.validation import _analysis_node_exists

        client = AsyncMock()
        client.get = AsyncMock(return_value=_node_resp(404))
        with patch("routers.proxy.get_proxy_client", AsyncMock(return_value=client)):
            result = await _analysis_node_exists("g1", "anything", ("function",))
        assert result is False

    async def test_exception_returns_false(self):
        from services.validation import _analysis_node_exists

        client = AsyncMock()
        client.get = AsyncMock(side_effect=RuntimeError("unreachable"))
        with patch("routers.proxy.get_proxy_client", AsyncMock(return_value=client)):
            result = await _analysis_node_exists("g1", "anything", ("function",))
        assert result is False
