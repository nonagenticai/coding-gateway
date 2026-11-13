"""Validation services for code analysis.

This module consolidates AST validation, import validation, and function
validation services extracted from the monolith's code_analysis package.
"""

from __future__ import annotations

import ast
import logging
import os
from typing import Any

from pydantic import BaseModel

# PythonEntityExtractor lives in the coding-analysis microservice.  The gateway
# imports it when running inside the analysis pod; otherwise a lightweight stub
# is used so that ASTValidationService can still operate (returning empty entity
# lists).  This is intentional and not a temporary workaround.
try:
    from services.code_graph.python_entity_extractor import PythonEntityExtractor
except ImportError:

    class PythonEntityExtractor:
        """Gateway-local stub for PythonEntityExtractor.

        The real implementation resides in the coding-analysis microservice.
        This stub allows ASTValidationService to function in the gateway
        context by returning empty entity lists for all extraction calls.
        """

        async def extract_entities(
            self, tree: ast.AST, filename: str, source: str
        ) -> list[dict[str, Any]]:
            """Return an empty entity list (no extraction in gateway context)."""
            return []


# DependencyParser lives in a shared service module.  The gateway imports it
# when the shared module is available; otherwise a lightweight stub is used so
# that ImportValidationService can still operate (treating all packages as
# unknown).  This is intentional and not a temporary workaround.
try:
    from services.dependency_parser import DependencyParser
except ImportError:

    class DependencyParser:
        """Gateway-local stub for DependencyParser.

        The real implementation resides in a shared service module.
        This stub allows ImportValidationService to function in the gateway
        context by reporting no known packages for dependency lookups.
        """

        def __init__(self, project_root: str) -> None:
            self.project_root = project_root

        async def get_available_packages(self) -> dict[str, Any]:
            return {}

        def get_package_info(self, module_name: str) -> dict[str, Any] | None:
            return None

        def get_similar_packages(self, module_name: str) -> list[str]:
            return []


from clients.outbound_identity import identity_headers, service_tenant
from config.settings import get_settings

settings = get_settings()


logger = logging.getLogger(__name__)


async def _analysis_node_exists(code_graph_id: str, name: str, node_types: tuple[str, ...]) -> bool:
    """Best-effort check that a node named *name* of one of *node_types* exists.

    The graph lives in coding-analysis (Postgres) now, so this asks its
    ``/api/analysis/graphs/{cn}/nodes`` reader (substring ``name_pattern`` filter)
    and confirms an exact ``properties.name`` match client-side. Graphs are keyed
    by container, so the tenant header is authz-only — the default tenant suffices.
    Returns False on any error (matching the old SQLite helpers' best-effort
    semantics).
    """
    try:
        from routers.proxy import get_proxy_client

        client = await get_proxy_client()
        base = f"{settings.analysis_url.rstrip('/')}/api/analysis/graphs/{code_graph_id}/nodes"
        headers = identity_headers(service_tenant())
        for node_type in node_types:
            resp = await client.get(
                base, headers=headers, params={"node_type": node_type, "name_pattern": name}
            )
            if resp.status_code != 200:
                continue
            for node in resp.json().get("nodes", []):
                if node.get("properties", {}).get("name") == name:
                    return True
        return False
    except Exception as e:
        logger.error("Error checking node in graph via coding-analysis: %s", e)
        return False


# =============================================================================
# AST Validation Service
# =============================================================================


class ASTIssue(BaseModel):
    """Single issue found during AST analysis."""

    description: str
    line_number: int = 0


class ASTAnalysisResult(BaseModel):
    """Model for AST analysis results.

    This model captures the comprehensive results of AST analysis including
    syntax validation, structural components, and complexity metrics.
    """

    is_valid: bool
    syntax_errors: list[str]
    imports: list[dict[str, Any]]
    functions: list[dict[str, Any]]
    classes: list[dict[str, Any]]
    variables: list[dict[str, Any]]
    complexity_score: float
    line_count: int
    issues: list[ASTIssue] = []


class ASTValidationService:
    """Service for comprehensive AST parsing and validation.

    This service provides advanced AST analysis capabilities including:
    - Syntax validation and error detection
    - Structural component extraction
    - Complexity analysis and metrics
    - Code quality assessment
    - Multi-language AST support

    The service integrates with entity extractors to provide detailed
    insights into code structure and syntax correctness.
    """

    def __init__(self, entity_extractor: PythonEntityExtractor | None = None):
        """Initialize the ASTValidationService service.

        Args:
            entity_extractor (Optional[PythonEntityExtractor]): Entity extractor
        """
        self.entity_extractor = entity_extractor or PythonEntityExtractor()

    async def analyze_proposed_code_ast(
        self, code: str, filename: str, language: str = "python"
    ) -> ASTAnalysisResult:
        """
        Analyze the AST structure of proposed code.

        Args:
            code: The code content to analyze
            filename: Name of the file
            language: Programming language (currently only python supported)

        Returns:
            ASTAnalysisResult with analysis details
        """
        try:
            if language.lower() != "python":
                return ASTAnalysisResult(
                    is_valid=False,
                    syntax_errors=[f"Language {language} not supported for AST analysis"],
                    imports=[],
                    functions=[],
                    classes=[],
                    variables=[],
                    complexity_score=0.0,
                    line_count=len(code.splitlines()),
                )

            return await self._analyze_python_ast(code)

        except Exception as e:
            logger.error("Error analyzing AST for %s: %s", filename, e)
            return ASTAnalysisResult(
                is_valid=False,
                syntax_errors=[f"AST analysis failed: {str(e)}"],
                imports=[],
                functions=[],
                classes=[],
                variables=[],
                complexity_score=0.0,
                line_count=len(code.splitlines()) if code else 0,
            )

    async def _analyze_python_ast(self, code: str) -> ASTAnalysisResult:
        """Analyze Python AST structure."""
        syntax_errors: list[str] = []
        imports: list[Any] = []
        functions: list[Any] = []
        classes: list[Any] = []
        variables: list[Any] = []
        try:
            # Parse the AST
            tree = ast.parse(code)

            # Use our entity extractor to extract entities
            entities = await self.entity_extractor.extract_entities(
                tree,
                "memory_file.py",
                "",  # Pass empty string instead of empty list
            )

            # Process the extracted entities
            for entity in entities:
                entity_type = entity.get("type")
                if entity_type == "class":
                    classes.append(
                        {
                            "name": entity.get("name"),
                            "line": entity.get("start_line"),
                            "bases": entity.get("properties", {}).get("bases", []),
                            "methods": [],  # Will be populated later
                            "decorators": entity.get("properties", {}).get("decorators", []),
                            "docstring": entity.get("docstring"),
                        }
                    )
                elif entity_type in ["function", "method"]:
                    entity_name = entity.get("name")
                    if entity_name is not None:
                        function_name = entity_name.split(".")[-1]
                    else:
                        function_name = "unknown"

                    func_info = {
                        "name": function_name,
                        "line": entity.get("start_line"),
                        "args": entity.get("properties", {}).get("parameters", []),
                        "defaults": 0,  # Not easily available
                        "is_async": False,  # Not easily available
                        "decorators": entity.get("properties", {}).get("decorators", []),
                        "docstring": entity.get("docstring"),
                        "returns": None,  # Not easily available
                    }

                    # If it's a method, add it to the class methods list
                    parent_class = entity.get("properties", {}).get("parent_class")
                    if parent_class:
                        for cls in classes:
                            if cls["name"] == parent_class:
                                cls["methods"].append(func_info["name"])
                    else:
                        functions.append(func_info)

            # Walk through the AST nodes to extract imports and variables
            for node in ast.walk(tree):
                try:
                    if isinstance(node, (ast.Import, ast.ImportFrom)):
                        imports.extend(self._extract_import_info(node))
                    elif isinstance(node, ast.Assign):
                        variables.extend(self._extract_variable_info(node))
                except Exception as e:
                    logger.warning("Error processing AST node %s: %s", type(node), e)
                    continue

            # Calculate complexity score
            complexity_score = self._calculate_complexity_score(tree)

            return ASTAnalysisResult(
                is_valid=True,
                syntax_errors=syntax_errors,
                imports=imports,
                functions=functions,
                classes=classes,
                variables=variables,
                complexity_score=complexity_score,
                line_count=len(code.splitlines()),
            )

        except SyntaxError as e:
            logger.error("Syntax error in Python code: %s", e)
            syntax_errors.append(f"Line {e.lineno}, column {e.offset}: {e.msg}")
            return ASTAnalysisResult(
                is_valid=False,
                syntax_errors=syntax_errors,
                imports=[],
                functions=[],
                classes=[],
                variables=[],
                complexity_score=0.0,
                line_count=len(code.splitlines()) if code else 0,
            )

    def _extract_import_info(self, node: ast.AST) -> list[dict[str, Any]]:
        """Extract import information from AST node."""
        imports: list[Any] = []
        if isinstance(node, ast.Import):
            for name in node.names:
                imports.append(
                    {
                        "module": name.name,
                        "alias": name.asname,
                        "line": node.lineno,
                        "type": "import",
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for name in node.names:
                imports.append(
                    {
                        "module": (f"{module}.{name.name}" if module else name.name),
                        "alias": name.asname,
                        "line": node.lineno,
                        "type": "from_import",
                        "level": node.level,
                    }
                )

        return imports

    def _extract_variable_info(self, node: ast.Assign) -> list[dict[str, Any]]:
        """Extract variable assignment information."""
        variables: list[Any] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                variables.append(
                    {
                        "name": target.id,
                        "line": node.lineno,
                        "type": "assignment",
                    }
                )
        return variables

    def _get_attribute_name(self, node: ast.AST) -> str:
        """Get attribute name from AST node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return f"{self._get_attribute_name(node.value)}.{node.attr}"
        return "unknown"

    def _calculate_complexity_score(self, tree: ast.AST) -> float:
        """Calculate a basic complexity score based on AST structure."""
        complexity: float = 0

        for node in ast.walk(tree):
            # Control flow adds complexity
            if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
                complexity += 1
            # Nested functions add complexity
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity += 0.5
            # Exception handling adds complexity
            elif isinstance(node, ast.ExceptHandler):
                complexity += 0.5

        # Normalize by number of lines (rough estimate)
        total_lines = max(1, len([n for n in ast.walk(tree) if hasattr(n, "lineno")]))
        return min(10.0, complexity / total_lines * 10)  # Scale to 0-10


# =============================================================================
# Import Validation Service
# =============================================================================


class ImportValidationResult(BaseModel):
    """Model for import validation results.

    This model captures the comprehensive results of import validation
    including categorized imports, validation status, and recommendations.
    """

    valid_imports: list[dict[str, Any]]
    invalid_imports: list[dict[str, Any]]
    missing_dependencies: list[str]
    stdlib_imports: list[str]
    third_party_imports: list[str]
    local_imports: list[str]
    suggestions: list[str]


class ImportValidationService:
    """Service for comprehensive import and dependency validation.

    This service provides advanced import validation capabilities including:
    - Standard library import validation
    - Third-party package verification
    - Local dependency resolution
    - Missing dependency detection
    - Import categorization and analysis

    The service integrates with dependency parsers to provide detailed
    insights into import structure and dependency management.
    """

    def __init__(self, project_root: str | None = None):
        """Initialize the ImportValidationService service.

        This initializes the ImportValidationService with required dependencies
        and configuration for service operations.

        Args:
            project_root (Optional[str]): Project root
        """
        self.stdlib_modules = self._get_stdlib_modules()
        self.project_root = project_root or os.getcwd()
        self.dependency_parser = DependencyParser(self.project_root)

    async def validate_imports(
        self, code_graph_id: str, imports: list[dict[str, Any]]
    ) -> ImportValidationResult:
        """
        Validate imports against the codebase graph and standard libraries.

        Args:
            code_graph_id: ID of the code graph
            imports: List of import information from AST analysis

        Returns:
            ImportValidationResult with validation details
        """
        valid_imports: list[Any] = []
        invalid_imports: list[Any] = []
        missing_dependencies: list[Any] = []
        stdlib_imports: list[Any] = []
        third_party_imports: list[Any] = []
        local_imports: list[Any] = []
        suggestions: list[Any] = []
        for import_info in imports:
            try:
                module_name = import_info.get("module", "")
                import_info.get("type", "")
                if not module_name:
                    continue
                if self.is_stdlib_module(module_name):
                    stdlib_imports.append(module_name)
                    valid_imports.append(import_info)
                    continue
                is_in_graph = await self._check_import_in_graph(code_graph_id, module_name)
                if is_in_graph:
                    local_imports.append(module_name)
                    valid_imports.append(import_info)
                    continue
                available_packages = await self.dependency_parser.get_available_packages()
                if await self._is_third_party_available(module_name, available_packages):
                    third_party_imports.append(module_name)
                    valid_imports.append(import_info)
                elif self._is_likely_third_party(module_name):
                    invalid_imports.append(import_info)
                    missing_dependencies.append(module_name)
                    package_info = self.dependency_parser.get_package_info(module_name)
                    if package_info:
                        suggestions.append(
                            f"Module '{module_name}' found in dependencies but may not be installed."
                        )
                    else:
                        similar_packages = self.dependency_parser.get_similar_packages(module_name)
                        if similar_packages:
                            suggestions.append(
                                f"Module '{module_name}' not found. Did you mean: {', '.join(similar_packages)}?"
                            )
                        else:
                            suggestions.append(
                                f"Module '{module_name}' not found. Add it to dependencies or check spelling."
                            )
                else:
                    invalid_imports.append(import_info)
                    missing_dependencies.append(module_name)
                    similar_packages = self.dependency_parser.get_similar_packages(module_name)
                    if similar_packages:
                        suggestions.append(
                            f"Module '{module_name}' not found. Did you mean: {', '.join(similar_packages)}?"
                        )
                    else:
                        suggestions.append(
                            f"Module '{module_name}' not found. Consider adding it to dependencies."
                        )
            except Exception as e:
                logger.warning(f"Error validating import {import_info}: {e}")
                invalid_imports.append(import_info)
        return ImportValidationResult(
            valid_imports=valid_imports,
            invalid_imports=invalid_imports,
            missing_dependencies=missing_dependencies,
            stdlib_imports=stdlib_imports,
            third_party_imports=third_party_imports,
            local_imports=local_imports,
            suggestions=suggestions,
        )

    async def _check_import_in_graph(self, code_graph_id: str, module_name: str) -> bool:
        """Check if an import exists in the code graph (via coding-analysis)."""
        return await _analysis_node_exists(code_graph_id, module_name, ("module", "file"))

    def is_stdlib_module(self, module_name: str) -> bool:
        """Check if a module is part of the Python standard library."""
        if module_name.startswith("."):
            return False
        top_level = module_name.split(".")[0]
        return top_level in self.stdlib_modules

    async def _is_third_party_available(
        self, module_name: str, available_packages: dict[str, Any]
    ) -> bool:
        """Check if a third-party module is available in project dependencies."""
        top_level = module_name.split(".")[0]
        if top_level.lower() in available_packages:
            return True
        import_to_package_map = {
            "cv2": "opencv-python",
            "PIL": "pillow",
            "yaml": "pyyaml",
            "sklearn": "scikit-learn",
            "bs4": "beautifulsoup4",
            "jwt": "pyjwt",
            "dotenv": "python-dotenv",
            "jose": "python-jose",
            "multipart": "python-multipart",
            "redis": "redis",
            "pytest": "pytest",
        }
        mapped_package = import_to_package_map.get(top_level) or import_to_package_map.get(
            top_level.lower()
        )
        if mapped_package and mapped_package.lower() in available_packages:
            return True
        for package_name in available_packages.keys():
            # Match subpackage imports: e.g. "numpy.linalg" → top_level "numpy" starts with package "numpy"
            if top_level.lower().startswith(package_name):
                return True
        return False

    def _is_likely_third_party(self, module_name: str) -> bool:
        """Heuristic to determine if a module is likely third-party."""
        common_third_party = {
            "requests",
            "numpy",
            "pandas",
            "flask",
            "django",
            "fastapi",
            "sqlalchemy",
            "pydantic",
            "click",
            "pytest",
            "matplotlib",
            "scipy",
            "sklearn",
            "tensorflow",
            "torch",
            "redis",
            "celery",
            "aiohttp",
            "uvloop",
            "gunicorn",
            "uvicorn",
            "pyastgrep",
            "sqlite_utils",
            "python_multipart",
            "python_jose",
            "passlib",
            "openpyxl",
            "timm",
            "prometheus_client",
            "starlette_exporter",
            "networkx",
            "astpretty",
            "astor",
            "jinja2",
            "directory_tree",
            "docker",
            "schemathesis",
            "github",
            "aiofiles",
            "psutil",
        }
        top_level = module_name.split(".")[0]
        return top_level in common_third_party

    def _get_stdlib_modules(self) -> set[Any]:
        """Get set of standard library module names."""
        stdlib_modules = {
            "__future__",
            "_thread",
            "abc",
            "aifc",
            "argparse",
            "array",
            "ast",
            "asynchat",
            "asyncio",
            "asyncore",
            "atexit",
            "audioop",
            "base64",
            "bdb",
            "binascii",
            "binhex",
            "bisect",
            "builtins",
            "bz2",
            "calendar",
            "cgi",
            "cgitb",
            "chunk",
            "cmath",
            "cmd",
            "code",
            "codecs",
            "codeop",
            "collections",
            "colorsys",
            "compileall",
            "concurrent",
            "configparser",
            "contextlib",
            "copy",
            "copyreg",
            "cProfile",
            "crypt",
            "csv",
            "ctypes",
            "curses",
            "dataclasses",
            "datetime",
            "dbm",
            "decimal",
            "difflib",
            "dis",
            "distutils",
            "doctest",
            "email",
            "encodings",
            "ensurepip",
            "enum",
            "errno",
            "faulthandler",
            "fcntl",
            "filecmp",
            "fileinput",
            "fnmatch",
            "formatter",
            "fractions",
            "ftplib",
            "functools",
            "gc",
            "getopt",
            "getpass",
            "gettext",
            "glob",
            "grp",
            "gzip",
            "hashlib",
            "heapq",
            "hmac",
            "html",
            "http",
            "imaplib",
            "imghdr",
            "imp",
            "importlib",
            "inspect",
            "io",
            "ipaddress",
            "itertools",
            "json",
            "keyword",
            "lib2to3",
            "linecache",
            "locale",
            "logging",
            "lzma",
            "mailbox",
            "mailcap",
            "marshal",
            "math",
            "mimetypes",
            "mmap",
            "modulefinder",
            "msilib",
            "msvcrt",
            "multiprocessing",
            "netrc",
            "nis",
            "nntplib",
            "numbers",
            "operator",
            "optparse",
            "os",
            "ossaudiodev",
            "parser",
            "pathlib",
            "pdb",
            "pickle",
            "pickletools",
            "pipes",
            "pkgutil",
            "platform",
            "plistlib",
            "poplib",
            "posix",
            "pprint",
            "profile",
            "pstats",
            "pty",
            "pwd",
            "py_compile",
            "pyclbr",
            "pydoc",
            "queue",
            "quopri",
            "random",
            "re",
            "readline",
            "reprlib",
            "resource",
            "rlcompleter",
            "runpy",
            "sched",
            "secrets",
            "select",
            "selectors",
            "shelve",
            "shlex",
            "shutil",
            "signal",
            "site",
            "smtpd",
            "smtplib",
            "sndhdr",
            "socket",
            "socketserver",
            "spwd",
            "sqlite3",
            "ssl",
            "stat",
            "statistics",
            "string",
            "stringprep",
            "struct",
            "subprocess",
            "sunau",
            "symbol",
            "symtable",
            "sys",
            "sysconfig",
            "syslog",
            "tabnanny",
            "tarfile",
            "telnetlib",
            "tempfile",
            "termios",
            "test",
            "textwrap",
            "threading",
            "time",
            "timeit",
            "tkinter",
            "token",
            "tokenize",
            "trace",
            "traceback",
            "tracemalloc",
            "tty",
            "turtle",
            "turtledemo",
            "types",
            "typing",
            "unicodedata",
            "unittest",
            "urllib",
            "uu",
            "uuid",
            "venv",
            "warnings",
            "wave",
            "weakref",
            "webbrowser",
            "winreg",
            "winsound",
            "wsgiref",
            "xdrlib",
            "xml",
            "xmlrpc",
            "zipapp",
            "zipfile",
            "zipimport",
            "zlib",
            "zoneinfo",
        }
        return stdlib_modules


# =============================================================================
# Function Validation Service
# =============================================================================


class FunctionValidationResult(BaseModel):
    """Model for function validation results.

    This model captures the comprehensive results of function call validation
    including categorized function types, validation status, and suggestions.
    """

    valid_functions: list[dict[str, Any]]
    invalid_functions: list[dict[str, Any]]
    builtin_functions: list[str]
    external_functions: list[str]
    local_functions: list[str]
    suggestions: list[str]


class FunctionValidationService:
    """Service for comprehensive function call validation.

    This service provides advanced function validation capabilities including:
    - Function call validation against code graphs
    - Builtin function detection and categorization
    - External dependency validation
    - Local function resolution
    - Validation suggestions and recommendations

    The service integrates with graph database connections to provide
    detailed insights into function usage and validation status.
    """

    def __init__(self) -> None:
        """Initialize the service with built-in functions."""
        self.builtin_functions = self._get_builtin_functions()

    async def validate_function_calls(
        self,
        code_graph_id: str,
        function_calls: list[dict[str, Any]],
    ) -> FunctionValidationResult:
        """Validate function calls against known functions in the code graph.

        Args:
            code_graph_id: Identifier for the code graph
            function_calls: List of function calls to validate

        Returns:
            FunctionValidationResult: Validation results with categorized functions

        Note:
            This method performs comprehensive validation of function calls
            against the code graph, categorizing them by type and providing
            detailed validation feedback.
        """
        valid_functions: list[Any] = []
        invalid_functions: list[Any] = []
        builtin_functions: list[Any] = []
        external_functions: list[Any] = []
        local_functions: list[Any] = []
        suggestions: list[str] = []
        for func_call in function_calls:
            try:
                func_name = func_call.get("name", "")
                if not func_name:
                    continue
                if self.is_builtin_function(func_name):
                    builtin_functions.append(func_name)
                    valid_functions.append(func_call)
                    continue
                is_in_graph = await self._check_function_in_graph_direct(code_graph_id, func_name)
                if is_in_graph:
                    local_functions.append(func_name)
                    valid_functions.append(func_call)
                    continue
                external_functions.append(func_name)
                valid_functions.append(func_call)
            except Exception:
                invalid_functions.append(func_call)
        return FunctionValidationResult(
            valid_functions=valid_functions,
            invalid_functions=invalid_functions,
            builtin_functions=builtin_functions,
            external_functions=external_functions,
            local_functions=local_functions,
            suggestions=suggestions,
        )

    async def _check_function_in_graph_direct(self, code_graph_id: str, function_name: str) -> bool:
        """Check if a function exists in the code graph (via coding-analysis)."""
        return await _analysis_node_exists(code_graph_id, function_name, ("function",))

    def is_builtin_function(self, function_name: str) -> bool:
        """Check if a function is a Python built-in."""
        return function_name in self.builtin_functions

    def _get_builtin_functions(self) -> set[Any]:
        """Get set of Python built-in function names."""
        return {
            "abs",
            "all",
            "any",
            "ascii",
            "bin",
            "bool",
            "bytearray",
            "bytes",
            "callable",
            "chr",
            "classmethod",
            "compile",
            "complex",
            "delattr",
            "dict",
            "dir",
            "divmod",
            "enumerate",
            "eval",
            "exec",
            "filter",
            "float",
            "format",
            "frozenset",
            "getattr",
            "globals",
            "hasattr",
            "hash",
            "help",
            "hex",
            "id",
            "input",
            "int",
            "isinstance",
            "issubclass",
            "iter",
            "len",
            "list",
            "locals",
            "map",
            "max",
            "memoryview",
            "min",
            "next",
            "object",
            "oct",
            "open",
            "ord",
            "pow",
            "print",
            "property",
            "range",
            "repr",
            "reversed",
            "round",
            "set",
            "setattr",
            "slice",
            "sorted",
            "staticmethod",
            "str",
            "sum",
            "super",
            "tuple",
            "type",
            "vars",
            "zip",
            "__import__",
        }
