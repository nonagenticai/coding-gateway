"""Tests for services.code_graph.reference_validator."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

from services.code_graph.reference_validator import (
    PackageReferenceValidator,
    ReferenceValidator,
    ValidationTimeoutError,
    timeout,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_temp_codebase(files: dict[str, str]) -> str:
    """Create a temp directory with the given files and return its path."""
    tmpdir = tempfile.mkdtemp()
    for rel_path, content in files.items():
        full = os.path.join(tmpdir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return tmpdir


# ---------------------------------------------------------------------------
# timeout context manager
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_no_timeout(self):
        with timeout(5):
            x = 1 + 1
        assert x == 2

    def test_timeout_error_type(self):
        assert issubclass(ValidationTimeoutError, Exception)


# ---------------------------------------------------------------------------
# ReferenceValidator
# ---------------------------------------------------------------------------


class TestReferenceValidator:
    def setup_method(self):
        self.tmpdir = _create_temp_codebase(
            {
                "main.py": (
                    "import os\n"
                    "from helpers import util_func\n"
                    "\n"
                    "class MyClass:\n"
                    "    def method_one(self):\n"
                    "        pass\n"
                    "\n"
                    "def standalone_func():\n"
                    "    return 1\n"
                ),
                "helpers.py": ("def util_func():\n    return 42\n"),
            }
        )
        self.validator = ReferenceValidator(self.tmpdir)

    # -- file validation --
    def test_validate_existing_file(self):
        exists, path = self.validator.validate_file_reference("main.py")
        assert exists is True
        assert path == "main.py"

    def test_validate_missing_file(self):
        exists, path = self.validator.validate_file_reference("nonexistent.py")
        assert exists is False

    def test_validate_absolute_existing_file(self):
        abs_path = os.path.join(self.tmpdir, "main.py")
        exists, path = self.validator.validate_file_reference(abs_path)
        assert exists is True

    def test_validate_absolute_missing_file(self):
        exists, path = self.validator.validate_file_reference("/no/such/file.py")
        assert exists is False
        assert path is None

    def test_validate_similar_file(self):
        exists, suggestion = self.validator.validate_file_reference("main")
        # "main" is a substring of "main.py"
        assert exists is False
        if suggestion:
            assert "main.py" in suggestion

    # -- function validation --
    def test_validate_existing_function(self):
        exists, details = self.validator.validate_function_reference("standalone_func")
        assert exists is True
        assert details["file"] == "main.py"

    def test_validate_existing_function_in_helpers(self):
        exists, details = self.validator.validate_function_reference("util_func")
        assert exists is True

    def test_validate_missing_function(self):
        exists, details = self.validator.validate_function_reference("no_such_func")
        assert exists is False

    def test_validate_similar_function(self):
        exists, details = self.validator.validate_function_reference("standalone")
        # "standalone" is a substring of "standalone_func"
        if details:
            assert "similar_functions" in details

    def test_validate_method_dotted(self):
        exists, details = self.validator.validate_function_reference("MyClass.method_one")
        assert exists is True

    # -- class validation --
    def test_validate_existing_class(self):
        exists, details = self.validator.validate_class_reference("MyClass")
        assert exists is True
        assert "method_one" in details["methods"]

    def test_validate_missing_class(self):
        exists, details = self.validator.validate_class_reference("NoClass")
        assert exists is False

    def test_validate_similar_class(self):
        exists, details = self.validator.validate_class_reference("My")
        if details:
            assert "similar_classes" in details

    # -- code snippet validation --
    def test_validate_snippet_empty(self):
        result = self.validator.validate_code_snippet("")
        assert result["valid"] is False

    def test_validate_snippet_none(self):
        result = self.validator.validate_code_snippet(None)
        assert result["valid"] is False

    def test_validate_snippet_syntax_error(self):
        result = self.validator.validate_code_snippet("def !!!")
        assert result["valid"] is False
        assert result["overall_score"] == 0.0

    def test_validate_snippet_valid_no_refs(self):
        result = self.validator.validate_code_snippet("x = 1 + 2")
        assert result["valid"] is True
        assert result["overall_score"] == 1.0

    def test_validate_snippet_with_known_import(self):
        result = self.validator.validate_code_snippet("import os")
        # os is in entity_index["imports"] because main.py imports it
        assert isinstance(result["overall_score"], float)

    def test_validate_snippet_with_unknown_call(self):
        result = self.validator.validate_code_snippet("nonexistent_func()")
        # The function doesn't exist in the codebase
        assert any(i["type"] == "function" for i in result["issues"])


# ---------------------------------------------------------------------------
# PackageReferenceValidator
# ---------------------------------------------------------------------------


class TestPackageReferenceValidator:
    def setup_method(self):
        self.tmpdir = _create_temp_codebase(
            {
                "app.py": "import os\nfrom json import dumps\n",
            }
        )

    def test_init_no_discovery(self):
        pv = PackageReferenceValidator(self.tmpdir)
        assert pv.package_discovery is None
        assert pv.container_packages == {}

    def test_validate_import_source_found(self):
        pv = PackageReferenceValidator(self.tmpdir)
        valid, details = pv.validate_import_statement("os")
        assert details["import_name"] == "os"
        # os is found in the entity_index since app.py imports it
        assert details["found_in_source"] is True

    def test_validate_import_not_found(self):
        pv = PackageReferenceValidator(self.tmpdir)
        valid, details = pv.validate_import_statement("nonexistent_pkg")
        assert details["found_in_source"] is False
        assert details["found_in_packages"] is False
        assert details["resolution"] == "not_found"

    def test_validate_import_with_stdlib(self):
        mock_discovery = MagicMock()
        mock_discovery.is_standard_library_module = MagicMock(return_value=True)
        pv = PackageReferenceValidator(self.tmpdir, package_discovery=mock_discovery)
        valid, details = pv.validate_import_statement("sys")
        assert valid is True
        assert details["found_in_stdlib"] is True

    def test_is_package_importable_no_packages(self):
        pv = PackageReferenceValidator(self.tmpdir)
        assert pv._is_package_importable("something") is False

    def test_is_package_importable_direct(self):
        pv = PackageReferenceValidator(self.tmpdir)
        pv.container_packages = {
            "modules_index": {"requests": {"modules": ["requests"], "version": "2.28"}}
        }
        assert pv._is_package_importable("requests") is True

    def test_is_package_importable_submodule(self):
        pv = PackageReferenceValidator(self.tmpdir)
        pv.container_packages = {
            "modules_index": {"requests": {"modules": ["requests"], "version": "2.28"}}
        }
        assert pv._is_package_importable("requests.sessions") is True

    def test_get_package_info_empty(self):
        pv = PackageReferenceValidator(self.tmpdir)
        assert pv._get_package_info("anything") == {}

    def test_get_package_info_found(self):
        pv = PackageReferenceValidator(self.tmpdir)
        pv.container_packages = {
            "modules_index": {
                "numpy": {"modules": ["numpy"], "version": "1.24", "type": "third_party"}
            }
        }
        info = pv._get_package_info("numpy")
        assert "version" in info or "providing_package" in info

    async def test_initialize_for_container(self):
        mock_discovery = AsyncMock()
        mock_discovery.discover_container_packages = AsyncMock(return_value={"packages": []})
        pv = PackageReferenceValidator(self.tmpdir, package_discovery=mock_discovery)
        await pv.initialize_for_container("test-container")
        mock_discovery.discover_container_packages.assert_called_once_with("test-container")

    async def test_validate_code_snippet_comprehensive_syntax_error(self):
        pv = PackageReferenceValidator(self.tmpdir)
        result = await pv.validate_code_snippet_comprehensive("def !!!")
        assert result["valid"] is False

    async def test_validate_code_snippet_comprehensive_valid(self):
        pv = PackageReferenceValidator(self.tmpdir)
        result = await pv.validate_code_snippet_comprehensive("import os\nx = 1")
        assert "issues" in result
        assert "overall_score" in result

    async def test_validate_code_snippet_with_container(self):
        mock_discovery = AsyncMock()
        mock_discovery.discover_container_packages = AsyncMock(return_value={"modules_index": {}})
        mock_discovery.is_standard_library_module = MagicMock(return_value=False)
        pv = PackageReferenceValidator(self.tmpdir, package_discovery=mock_discovery)
        result = await pv.validate_code_snippet_comprehensive("import os", container_name="test")
        assert "issues" in result
