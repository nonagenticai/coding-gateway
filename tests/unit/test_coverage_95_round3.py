"""Round-3 coverage tests pushing remaining gaps toward 95%.

Targets:
- services/validation.py (89%) — entity processing in _analyze_python_ast,
  _check_import_in_graph with real SQLite, AST node error path
- services/code_graph/structure_storage.py (87%) — store_code_structure full
  flow with include_patterns, excluded files debug, progress logging,
  commit/verify path, _analyze_file edge cases
- routers/graphs.py (97%) — remaining untested lines
- app_factory.py — lifespan startup failure
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# =====================================================================
# services/validation.py — entity processing in _analyze_python_ast
# =====================================================================


class TestASTEntityProcessing:
    """Test entity extraction loop in _analyze_python_ast (lines 193-230)."""

    async def test_class_entity_extracted(self):
        """When extractor returns class entities, they are processed."""
        from services.validation import ASTValidationService

        mock_extractor = MagicMock()
        mock_extractor.extract_entities = AsyncMock(
            return_value=[
                {
                    "type": "class",
                    "name": "MyClass",
                    "start_line": 1,
                    "properties": {"bases": ["Base"], "decorators": ["@dataclass"]},
                    "docstring": "A class.",
                },
            ]
        )
        svc = ASTValidationService(entity_extractor=mock_extractor)
        code = "class MyClass(Base):\n    pass\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.classes) == 1
        assert result.classes[0]["name"] == "MyClass"
        assert result.classes[0]["bases"] == ["Base"]
        assert result.classes[0]["decorators"] == ["@dataclass"]

    async def test_function_entity_extracted(self):
        """When extractor returns function entities, they are processed."""
        from services.validation import ASTValidationService

        mock_extractor = MagicMock()
        mock_extractor.extract_entities = AsyncMock(
            return_value=[
                {
                    "type": "function",
                    "name": "my_func",
                    "start_line": 1,
                    "properties": {"parameters": ["x", "y"]},
                    "docstring": "Does stuff.",
                },
            ]
        )
        svc = ASTValidationService(entity_extractor=mock_extractor)
        code = "def my_func(x, y):\n    return x + y\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.functions) == 1
        assert result.functions[0]["name"] == "my_func"
        assert result.functions[0]["args"] == ["x", "y"]
        assert result.functions[0]["docstring"] == "Does stuff."

    async def test_method_entity_added_to_class(self):
        """Method entities with parent_class are added to the class methods list."""
        from services.validation import ASTValidationService

        mock_extractor = MagicMock()
        mock_extractor.extract_entities = AsyncMock(
            return_value=[
                {
                    "type": "class",
                    "name": "Foo",
                    "start_line": 1,
                    "properties": {},
                },
                {
                    "type": "method",
                    "name": "Foo.bar",
                    "start_line": 2,
                    "properties": {"parent_class": "Foo"},
                },
            ]
        )
        svc = ASTValidationService(entity_extractor=mock_extractor)
        code = "class Foo:\n    def bar(self): pass\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.classes) == 1
        assert "bar" in result.classes[0]["methods"]

    async def test_function_with_none_name(self):
        """Function entity with name=None uses 'unknown'."""
        from services.validation import ASTValidationService

        mock_extractor = MagicMock()
        mock_extractor.extract_entities = AsyncMock(
            return_value=[
                {
                    "type": "function",
                    "name": None,
                    "start_line": 1,
                    "properties": {},
                },
            ]
        )
        svc = ASTValidationService(entity_extractor=mock_extractor)
        code = "def x(): pass\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        assert result.is_valid is True
        assert len(result.functions) == 1
        assert result.functions[0]["name"] == "unknown"

    async def test_ast_walk_node_error_continues(self):
        """Error processing a single AST node logs warning and continues."""
        from services.validation import ASTValidationService

        mock_extractor = MagicMock()
        mock_extractor.extract_entities = AsyncMock(return_value=[])

        svc = ASTValidationService(entity_extractor=mock_extractor)

        # Patch _extract_import_info to raise on first call, then succeed
        call_count = 0
        original = svc._extract_import_info

        def patched_extract(node):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("node error")
            return original(node)

        svc._extract_import_info = patched_extract

        code = "import os\nimport sys\n"
        result = await svc.analyze_proposed_code_ast(code, "test.py")
        # Should still be valid, one import may have failed
        assert result.is_valid is True


class TestCheckImportInGraph:
    """Test _check_import_in_graph delegates to coding-analysis (_analysis_node_exists)."""

    async def test_import_found_in_graph(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService(project_root="/tmp/test")
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=True),
        ) as mock_exists:
            result = await svc._check_import_in_graph("mygraph", "my_module")
        assert result is True
        # Modules are looked up against module/file node types.
        mock_exists.assert_awaited_once_with("mygraph", "my_module", ("module", "file"))

    async def test_import_not_found_in_graph(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService(project_root="/tmp/test")
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=False),
        ):
            result = await svc._check_import_in_graph("mygraph", "nonexistent")
        assert result is False

    async def test_import_check_analysis_unreachable(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService(project_root="/tmp/test")
        # _analysis_node_exists swallows errors and returns False.
        with patch(
            "services.validation._analysis_node_exists",
            new=AsyncMock(return_value=False),
        ):
            result = await svc._check_import_in_graph("missing", "os")
        assert result is False


class TestImportValidationLocalImport:
    """Test validate_imports when module IS found in the graph (local import)."""

    async def test_local_import_detected(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService(project_root="/tmp/test")

        with patch.object(svc, "_check_import_in_graph", new_callable=AsyncMock, return_value=True):
            imports = [{"module": "my_local_module", "type": "import", "line": 1}]
            result = await svc.validate_imports("graph1", imports)

        assert "my_local_module" in result.local_imports
        assert len(result.valid_imports) == 1


class TestImportValidationThirdPartyWithPackageInfo:
    """Test suggestion when package_info is available for likely third-party."""

    async def test_package_info_suggestion(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()

        with (
            patch.object(svc, "_check_import_in_graph", new_callable=AsyncMock, return_value=False),
            patch.object(
                svc.dependency_parser,
                "get_package_info",
                return_value={"name": "requests", "version": "2.31"},
            ),
        ):
            imports = [{"module": "requests", "type": "import", "line": 1}]
            result = await svc.validate_imports("g1", imports)

        assert len(result.suggestions) > 0
        assert "may not be installed" in result.suggestions[0]


class TestImportValidationThirdPartyAvailable:
    """Test validate_imports when third-party IS available in packages."""

    async def test_third_party_available(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()

        with (
            patch.object(svc, "_check_import_in_graph", new_callable=AsyncMock, return_value=False),
            patch.object(
                svc, "_is_third_party_available", new_callable=AsyncMock, return_value=True
            ),
        ):
            imports = [{"module": "custom_pkg", "type": "import", "line": 1}]
            result = await svc.validate_imports("g1", imports)

        assert "custom_pkg" in result.third_party_imports
        assert len(result.valid_imports) == 1


# =====================================================================
# app_factory.py — startup failure
# =====================================================================


class TestLifespanStartupError:
    """When startup raises, the error is propagated."""

    async def test_startup_error_propagated(self):
        from fastapi import FastAPI

        from app_factory import lifespan

        test_app = FastAPI()

        with patch(
            "services.cache_manager.get_cache_manager",
            side_effect=RuntimeError("cache init fail"),
        ):
            with pytest.raises(RuntimeError, match="cache init fail"):
                async with lifespan(test_app):
                    pass  # Should not reach here


# =====================================================================
# routers/graphs.py — remaining edge cases
# =====================================================================


class TestGraphsDeleteInvalidName:
    """Delete with invalid container name returns 400."""

    async def test_delete_invalid_name(self, client):
        resp = await client.delete("/api/graphs/bad%20name!")
        assert resp.status_code == 400
