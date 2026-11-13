"""Final coverage push targeting TestingConfigurationService and remaining gaps.

Part 4: Targeting:
- services/cache_manager.py (86%): TestingConfigurationService methods (1029-1168)
- services/validation.py (86%): _analyze_python_ast deeper, validate_imports branches
- services/code_graph/multi_language_graph_service.py (82%): analyze_directory flow
- routers/graphs.py (86%): _list_active_container_names, more endpoints
- services/code_graph/structure_storage.py (86%): _should_exclude_path branches
"""

from __future__ import annotations

from unittest.mock import AsyncMock

# =====================================================================
# services/cache_manager.py — TestingConfigurationService
# =====================================================================


class TestTestingConfigurationService:
    """Cover TestingConfigurationService methods."""

    def _make_service(self):
        from services.cache_manager import RedisCacheManager, TestingConfigurationService

        mgr = RedisCacheManager()
        mgr.get = AsyncMock(return_value=None)
        mgr.set = AsyncMock(return_value=True)
        mgr.delete = AsyncMock(return_value=True)
        mgr.delete_pattern = AsyncMock(return_value=0)
        svc = TestingConfigurationService(mgr)
        return svc

    async def test_get_testing_config_default(self):
        svc = self._make_service()
        config = await svc.get_testing_config()
        assert config is not None
        assert config.default_timeout > 0

    async def test_get_testing_config_cached(self):
        svc = self._make_service()
        svc.cache_manager.get = AsyncMock(
            return_value={
                "default_timeout": 60,
                "max_memory_mb": 512,
                "max_cpu_quota": 50000,
                "quality_score_threshold": 7.0,
                "cache_ttl_seconds": 3600,
                "enabled_python_tools": ["pytest"],
                "enabled_js_tools": ["jest"],
                "enabled_ts_tools": ["jest"],
                "base_images": {"python": "python:3.14-slim"},
            }
        )
        config = await svc.get_testing_config()
        assert config.default_timeout == 60

    async def test_get_testing_config_error(self):
        svc = self._make_service()
        svc.cache_manager.get = AsyncMock(side_effect=RuntimeError("redis down"))
        config = await svc.get_testing_config()
        # Should return default config on error
        assert config is not None

    async def test_update_testing_config_success(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        config = TestingConfig()
        result = await svc.update_testing_config(config)
        assert result is True

    async def test_update_testing_config_validation_fails(self):
        """When validate_configuration returns invalid, update returns False."""
        from services.cache_manager import ConfigValidationResult, TestingConfig

        svc = self._make_service()
        config = TestingConfig()
        # Override validate_configuration to return invalid
        svc.validate_configuration = AsyncMock(
            return_value=ConfigValidationResult(valid=False, errors=["test error"])
        )
        result = await svc.update_testing_config(config)
        assert result is False

    async def test_update_testing_config_error(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        svc.cache_manager.set = AsyncMock(side_effect=RuntimeError("redis down"))
        config = TestingConfig()
        result = await svc.update_testing_config(config)
        assert result is False

    async def test_update_testing_config_cache_fail(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        svc.cache_manager.set = AsyncMock(return_value=False)
        config = TestingConfig()
        result = await svc.update_testing_config(config)
        assert result is False

    async def test_validate_configuration_valid(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        config = TestingConfig()
        result = await svc.validate_configuration(config)
        assert result.valid is True
        assert len(result.errors) == 0

    async def test_validate_configuration_valid_defaults(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        config = TestingConfig()
        result = await svc.validate_configuration(config)
        assert result.valid is True

    async def test_validate_configuration_no_tools(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        config = TestingConfig(enabled_python_tools=[], enabled_js_tools=[], enabled_ts_tools=[])
        result = await svc.validate_configuration(config)
        assert any("no quality tools" in w.lower() for w in result.warnings)

    async def test_validate_configuration_bad_image(self):
        from services.cache_manager import TestingConfig

        svc = self._make_service()
        config = TestingConfig(base_images={"python": "bad_image_no_tag"})
        result = await svc.validate_configuration(config)
        assert any("image" in w.lower() for w in result.warnings)

    async def test_get_quality_tool_config_default(self):
        svc = self._make_service()
        config = await svc.get_quality_tool_config("pytest")
        assert config.name == "pytest"

    async def test_get_quality_tool_config_cached(self):
        svc = self._make_service()
        svc.cache_manager.get = AsyncMock(
            return_value={"name": "pytest", "config_file": "pytest.ini"}
        )
        config = await svc.get_quality_tool_config("pytest")
        assert config.name == "pytest"

    async def test_get_quality_tool_config_error(self):
        svc = self._make_service()
        svc.cache_manager.get = AsyncMock(side_effect=RuntimeError("redis down"))
        config = await svc.get_quality_tool_config("pytest")
        assert config.name == "pytest"  # Fallback

    async def test_update_quality_tool_config(self):
        from services.cache_manager import ToolConfig

        svc = self._make_service()
        config = ToolConfig(name="ruff", config_file="ruff.toml")
        result = await svc.update_quality_tool_config("ruff", config)
        assert result is True

    async def test_update_quality_tool_config_fail(self):
        from services.cache_manager import ToolConfig

        svc = self._make_service()
        svc.cache_manager.set = AsyncMock(return_value=False)
        config = ToolConfig(name="ruff")
        result = await svc.update_quality_tool_config("ruff", config)
        assert result is False

    async def test_update_quality_tool_config_error(self):
        from services.cache_manager import ToolConfig

        svc = self._make_service()
        # Make set throw (get is called for get_testing_config first)
        svc.cache_manager.set = AsyncMock(side_effect=RuntimeError("error"))
        config = ToolConfig(name="ruff")
        result = await svc.update_quality_tool_config("ruff", config)
        assert result is False

    async def test_get_enabled_tools_python(self):
        svc = self._make_service()
        tools = await svc.get_enabled_tools_for_language("python")
        assert isinstance(tools, list)

    async def test_get_enabled_tools_javascript(self):
        svc = self._make_service()
        tools = await svc.get_enabled_tools_for_language("javascript")
        assert isinstance(tools, list)

    async def test_get_enabled_tools_typescript(self):
        svc = self._make_service()
        tools = await svc.get_enabled_tools_for_language("typescript")
        assert isinstance(tools, list)

    async def test_get_enabled_tools_unknown(self):
        svc = self._make_service()
        tools = await svc.get_enabled_tools_for_language("cobol")
        assert tools == []

    async def test_get_enabled_tools_error(self):
        svc = self._make_service()
        # get_testing_config first calls get(), which returns None, then set()
        # so we need set() to fail to trigger the error path
        svc.cache_manager.set = AsyncMock(side_effect=RuntimeError("error"))
        # But actually get_testing_config catches exceptions and returns default
        # So this should still work. Let's make get_testing_config itself fail:
        svc.get_testing_config = AsyncMock(side_effect=RuntimeError("error"))
        tools = await svc.get_enabled_tools_for_language("python")
        assert tools == []

    async def test_is_tool_enabled_returns_false_for_unconfigured(self):
        svc = self._make_service()
        result = await svc.is_tool_enabled("pytest", "python")
        # No config cached, so default behavior should return False
        assert result is False

    async def test_is_tool_enabled_error(self):
        svc = self._make_service()
        svc.cache_manager.get = AsyncMock(side_effect=RuntimeError("error"))
        result = await svc.is_tool_enabled("pytest", "python")
        assert result is False

    async def test_clear_configuration_cache(self):
        svc = self._make_service()
        result = await svc.clear_configuration_cache()
        assert result is True

    async def test_clear_configuration_cache_error(self):
        svc = self._make_service()
        svc.cache_manager.delete = AsyncMock(side_effect=RuntimeError("error"))
        result = await svc.clear_configuration_cache()
        assert result is False

    def test_get_default_config(self):
        svc = self._make_service()
        config = svc._get_default_config()
        assert config is not None
        # Second call returns same instance
        config2 = svc._get_default_config()
        assert config is config2

    def test_get_default_tool_config(self):
        svc = self._make_service()
        config = svc._get_default_tool_config("ty")
        assert config.name == "ty"
        assert config.config_file is None


# =====================================================================
# services/validation.py — deeper coverage of import validation
# =====================================================================


class TestValidationServiceDeeper:
    """Cover deeper branches in validation.py."""

    async def test_analyze_python_ast_with_complex_code(self):
        from services.validation import ASTValidationService

        svc = ASTValidationService()
        code = """
import os
from pathlib import Path

x = 42
y = "hello"

class MyClass:
    \"\"\"A test class.\"\"\"
    def method(self):
        pass

def standalone():
    \"\"\"A standalone function.\"\"\"
    if True:
        for i in range(10):
            try:
                pass
            except Exception:
                pass
"""
        result = await svc.analyze_proposed_code_ast(code, "complex.py")
        assert result.is_valid is True
        assert len(result.imports) >= 2
        assert result.complexity_score > 0
        assert result.line_count > 0

    async def test_import_validation_third_party_known(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc.validate_imports(
            "test-graph",
            [
                {"module": "numpy", "type": "import"},
                {"module": "pandas", "type": "import"},
            ],
        )
        # numpy and pandas are "likely third party" but may not be installed
        total = len(result.valid_imports) + len(result.invalid_imports)
        assert total >= 1

    async def test_import_validation_dotted_module(self):
        from services.validation import ImportValidationService

        svc = ImportValidationService()
        result = await svc.validate_imports(
            "test-graph",
            [{"module": "os.path", "type": "from_import"}],
        )
        assert "os.path" in result.stdlib_imports or "os" in result.stdlib_imports
