"""
V2 Validation API endpoints.

Provides code validation and consistency checking to prevent AI hallucinations.

Key Features:
- File, function, and class reference validation, scoped to the CALLER's workspace
- Function and class description consistency checking (documentation completeness)
- Explicit refusals wherever a verdict cannot be computed

Container-dependent validation (execution, multi-line snippets) is proxied
to coding-containers:8691 via the proxy router.

-----------------------------------------------------------------------------
THE DEFECT THIS FILE WAS REWRITTEN FOR (measured live 2026-09-17 against the
deployed pod, through the public API -- not inferred from reading the code)
-----------------------------------------------------------------------------

    request                                            | result
    ---------------------------------------------------+---------------------
    function-reference, real symbol + real container   | 200 valid=false (13ms)
    function-reference, "zz_nonexistent_fn_4711"       | 200 valid=false
    function-reference, fake container / no container  | 200 valid=false
    class-reference, FastMCP vs ZzNonexistentClass4711 | 200 valid=false both
    file-reference, pyproject.toml vs a fake path      | 200 valid=false both
    code-snippet                                       | 400 "'/app/data/repo'
                                                       |      is not a directory"
    code, real file / missing file / /etc/passwd       | 200 valid=TRUE every time
    function-consistency, `return a-b` as "Adds two    | 200 consistent=true,
      numbers" vs the correct `return a+b`             |     0.91, SAME 3 issues

Three distinct gates, none of which could fail:

1. **The reference endpoints answered from the gateway's own filesystem.**
   ``_resolve_codebase_path`` did not drop ``container_name`` -- it used it as a
   *flag* ("a container was named, so probe the workspace dirs") and then chose
   between three hard-coded LOCAL paths (``/app/analysis_workspace``,
   ``/app/data/repo``, ``_CODEBASE_PATH``). None exists in the gateway pod, so
   every request fell through to the default, ``ReferenceValidator`` indexed an
   empty tree, and every symbol was absent from it. Two different containers
   resolved to the same directory; a container that did not exist resolved
   there too. 3-13 ms of latency was the tell: no container was ever consulted.

2. **``validate_code`` certified without computing.** It built
   ``validation_results = {"valid": True, "errors": []}`` literally and returned
   it, so a nonexistent file in a nonexistent container was "valid".
   ``validate_architecture`` did the same with ``violations: []``.

3. **``check*Consistency`` could not discriminate.** See
   the "What the consistency checker actually measures" block below -- the
   score reflects only whether the *description mentions* what the code does,
   never what the code MEANS.

The fixes, in order of the response they now produce:
- resolvable workspace  -> a real answer, computed in the caller's container
- named-but-unreadable  -> 404 (a refusal, never ``valid=false``)
- nothing named         -> 400 naming the missing input
- backend unreachable   -> 503 ("I could not look" is not "it is not there")
- search output clipped -> 503 carrying ``error.details.tool`` /
                           ``error.details.bytes_omitted`` on the wire
                           ("I could not FINISH looking" is not it either, and
                            it arrives wearing exit 0). ⚠️ The raised
                            ``error_code`` does NOT survive the response
                            envelope — see ``_workspace_error``.
- not computable at all -> 501, or an explicit ``not_computed`` abstention
"""

from __future__ import annotations

import ast
import logging
import os
import posixpath
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.dependencies import require_tenant
from services.architecture_rules import evaluate_architecture
from services.code_graph.consistency_checker import CodeConsistencyChecker
from services.code_graph.reference_validator import (
    ReferenceValidator,
)
from services.container_workspace import (
    POLICY_REFUSED_REASON,
    PYTHON_ONLY,
    SEARCHED_LANGUAGES,
    VENDOR_EXCLUDE_DIRS,
    WORKSPACE_ROOT,
    ContainerNotResolvableError,
    WorkspaceBackendUnavailableError,
    WorkspaceSearchIncompleteError,
    count_lines,
    find_directory,
    find_file,
    find_module_files,
    grep_class_in_files,
    grep_definition,
    grep_files_mentioning,
    grep_lines,
    lint_python_file,
    read_workspace_file_checked,
    validate_identifier,
    validate_relative_path,
    validate_snippet_in_workspace,
)
from services.dotted_names import (
    MODULE_RESOLUTION_EXTERNAL,
    MODULE_RESOLUTION_IN_REPO,
    MODULE_SEARCH_ROOTS,
    all_candidates,
    is_dotted,
    module_file_candidates,
    split_dotted_class_name,
    top_level_package_candidates,
)
from services.symbol_index import FileMatch, fetch_symbols, match_symbol, symbol_language
from services.symbol_walk import (
    MAX_BASE_HOPS,
    MAX_REEXPORT_HOPS,
    UnresolvedHopError,
    WalkOps,
    python_module_candidates,
)
from services.symbol_walk import resolve as resolve_walk

logger = logging.getLogger(__name__)

router = APIRouter()

# How a codebase was resolved for a given response. Published on every
# reference response so a caller can tell a real workspace answer from an
# explicit-path answer -- they have different trust properties and used to be
# indistinguishable.
RESOLUTION_CONTAINER = "container_workspace"
RESOLUTION_EXPLICIT = "explicit_codebase_path"

# ---------------------------------------------------------------------------
# What the consistency checker actually measures
#
# INVESTIGATED, not assumed. `def add(a, b): return a - b` described as "Adds
# two numbers" scores 0.91 / consistent=true with the SAME three issues as the
# correct `return a + b`, and the arithmetic says why:
# ValidationScoreCalculator.calculate_consistency_score
# (services/code_graph/validation_utilities.py:648-677) is
# `1.0 - sum(severity_weight * context_weight * 0.1)`. Two medium issues (0.4)
# plus one low (0.1) at context_weight 1.0 = 0.09 penalty = **exactly 0.91**.
# The score is a function of the ISSUE LIST and nothing else.
#
# So the question is whether any rule can produce an issue from `-` vs `+`.
# The five rules (services/code_graph/consistency_rules.py) are:
#   ParameterConsistencyRule  :29  - are the params named in the description?
#   ReturnConsistencyRule     :98  - does the description say "return"?
#   ExceptionConsistencyRule  :170 - are raised exceptions mentioned?
#   LogicOperationConsistencyRule :252 - does the description mention that the
#       code touches database / file / network / async / recursion? It matches
#       those five categories by KEYWORD against the unparsed source (:339-347).
#   NamingConsistencyRule     :402 - does the name agree with the description?
# None of them reads an operator, a comparison, or a return expression. The
# BinOp is never inspected by anything. `a - b` and `a + b` are byte-identical
# inputs to all five rules, so identical output is CORRECT behaviour for the
# checker -- and a confidently wrong answer at the API boundary.
#
# This is a documentation-completeness checker ("does the prose mention what
# the code does?"), which is a genuinely useful and genuinely computed thing.
# It is NOT a semantic-equivalence checker ("does the code do what the prose
# claims?"), and only the second one can answer the question the endpoint's
# name promises. Making it answer the second is a research task, not a bug fix.
#
# Therefore: keep computing the real thing, publish it under its real name, and
# ABSTAIN on the claim we cannot support. An honest abstention is acceptable; a
# confident wrong answer is not.
#
# THE STRUCTURAL PART (what `consistent` now carries, when it is not null).
# One narrow claim IS computable without understanding semantics: when the
# description EXPLICITLY names parameters (`:param x:`, `@param x`, an
# `Args:`/`Parameters:` section entry, or "parameter `x`" in quotes/backticks),
# every one of them must exist in the signature parsed with `ast`. A described
# parameter the code does not have is a hallucination the endpoint can catch.
# `structural_consistent` reports that check; `consistent` mirrors it ONLY when
# at least one parameter was named (otherwise it stays null with
# `consistent_reason` -- an empty check is not a pass), and `semantic` stays
# "not_computed": `return a - b` described as "Adds `a` and `b`" is still
# structurally consistent, and nothing here claims otherwise.
# ---------------------------------------------------------------------------
_CONSISTENCY_CHECKS_PERFORMED = [
    "parameters_mentioned_in_description",
    "return_mentioned_in_description",
    "exceptions_mentioned_in_description",
    "io_operations_mentioned_in_description",
    "naming_agrees_with_description",
]

_CONSISTENCY_NOT_COMPUTED_REASON = (
    "Semantic equivalence between the description and the implementation was "
    "NOT computed. The five rules behind this endpoint check only whether the "
    "description MENTIONS the code's parameters, returns, exceptions, I/O "
    "categories and name -- none of them inspects what the code actually "
    "computes. A function whose body contradicts its description (e.g. "
    "'Adds two numbers' over `return a - b`) is indistinguishable here from a "
    "correct one, so this endpoint does not assert `consistent`. Use "
    "`documentation_consistent` for the verdict that WAS computed."
)


# ---------------------------------------------------------------------------
# Local Pydantic models (self-contained for the gateway)
# ---------------------------------------------------------------------------


class ValidateFileReferenceRequest(BaseModel):
    """Request for file reference validation."""

    file_path: str
    container_name: str | None = None
    codebase_path: str | None = Field(
        default=None,
        description=(
            "Explicit codebase directory, for callers that legitimately have one. "
            "Must be supplied deliberately -- the gateway never falls back to its "
            "own filesystem."
        ),
    )
    line: int | None = Field(
        default=None,
        description=(
            "Optional 1-based line number. It is PART OF THE CLAIM: a file without that "
            "line makes `valid` false, and a line count that could not be computed makes "
            "`valid` null."
        ),
    )
    allow_dir: bool = Field(
        default=False,
        description=(
            "When true, a DIRECTORY at `file_path` satisfies the reference. Default false: "
            "`src/models` (a directory) must not pass a claim about the file "
            "`src/models.py`."
        ),
    )


class ValidateFileReferenceResponse(BaseModel):
    """Response for file reference validation."""

    valid: bool | None = Field(
        description=(
            "True: the file exists (and has `line`, when requested). False: it does not, "
            "or it lacks that line. Null: part of the claim could not be computed (see "
            "`note`) -- never a pass."
        ),
    )
    file_path: str
    suggestion: str | None = None
    resolution: str = RESOLUTION_CONTAINER
    is_dir: bool | None = Field(
        default=False,
        description=(
            "True when `file_path` names a DIRECTORY in the workspace. A directory makes "
            "`valid` true only when the request set `allow_dir`. Null when the directory "
            "check could not be computed."
        ),
    )
    note: str | None = None
    line_exists: bool | None = Field(
        default=None,
        description=(
            "Only when `line` was requested: whether the file has that line. Null when no "
            "line was requested, the path is not a file, or the count could not be computed."
        ),
    )
    line_count: int | None = None
    searched_languages: list[str] = Field(
        default_factory=lambda: ["*"],
        description="File existence is a filesystem question; every language is covered.",
    )
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Directory basename globs NOT searched (vendored/generated code: .venv, "
            "site-packages, node_modules, ...). A name that exists only there is "
            "reported as absent. Empty for explicit-path resolution, which walks the "
            "whole tree."
        ),
    )


class ValidateFunctionReferenceRequest(BaseModel):
    """Request for function reference validation."""

    function_name: str
    container_name: str | None = None
    codebase_path: str | None = None
    file_path: str | None = Field(
        default=None,
        description=(
            "Optional workspace-relative file the definition is claimed to live in. When "
            "set, `valid` is true only if a definition is found IN THAT FILE (see "
            "`found_in` / `in_file`). Container mode only. Python, JavaScript, "
            "TypeScript, Go, Java, Rust, C/C++ and C# files are searched; any other "
            "extension is not: `language_supported` is false and `valid` is null."
        ),
    )


class ValidateFunctionReferenceResponse(BaseModel):
    """Response for function reference validation."""

    valid: bool | None = Field(
        description=(
            "True: a definition was found. False: every searched language was searched "
            "completely and no definition was found. Null: not computed -- an unsupported "
            "file type (`language_supported` false), an incomplete read or search, a "
            "symbol-extraction failure, a parse error that can hide a definition, the "
            "name exists only as another kind, or an inherited / re-exported definition "
            "whose base or source is outside the workspace (see `note`)."
        ),
    )
    function_name: str
    details: dict[str, Any] | None = None
    found_in: list[str] = Field(
        default_factory=list,
        description="Every workspace-relative file a matching definition was found in.",
    )
    in_file: bool | None = Field(
        default=None,
        description="Only when `file_path` was requested: whether a definition is in that file.",
    )
    language_supported: bool = Field(
        default=True,
        description=(
            "False when the requested `file_path` has an extension no searched language "
            "covers (see `searched_languages`), so `valid` is then null (not computed) -- "
            "callers MUST read this field before treating a non-true `valid` as 'does not "
            "exist'."
        ),
    )
    resolution: str = RESOLUTION_CONTAINER
    searched_languages: list[str] = Field(
        default_factory=lambda: list(SEARCHED_LANGUAGES),
        description=(
            "Languages searched for this answer. Container mode: Python by `def`/`class` "
            "grep, the rest by candidate-file grep confirmed through coding-analysis "
            "symbol extraction; a located `file_path` searches only its own language. "
            "A dotted `Owner.member` whose owner type is found without the member follows "
            "the owner's bases (and Rust `impl Trait for Owner` traits) inside the "
            "workspace, at most 5 hops: found on a base -> true (`found_in` the base's "
            "file, note 'inherited from'); every base resolved and parsed, member absent "
            "-> false; any base external, qualified, ambiguous or unresolvable, or a "
            "language where members can be declared outside the type (Go, Rust, C# "
            "partial, JS/TS prototype/this assignments, TS interfaces) -> null. "
            "A name absent from a JS/TS barrel, Python package `__init__` or Rust "
            "`pub use` but re-exported there is followed to its source, at most 3 hops: "
            "found -> true ('re-exported via'); chain complete and absent -> false; a "
            "package or path-alias hop -> null. A Rust/C/C++ file that mentions the name "
            "otherwise is null (macros are not expanded). "
            "Explicit `codebase_path` mode: python only, bases not followed."
        ),
    )
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Directory basename globs NOT searched (vendored/generated code: .venv, "
            "site-packages, node_modules, ...). A name that exists only there is "
            "reported as absent. Empty for explicit-path resolution, which walks the "
            "whole tree."
        ),
    )
    note: str | None = Field(
        default=None,
        description="Why `valid` is false or null: what was searched and what was not.",
    )


class ValidateClassReferenceRequest(BaseModel):
    """Request for class reference validation."""

    class_name: str
    container_name: str | None = None
    codebase_path: str | None = None
    file_path: str | None = Field(
        default=None,
        description=(
            "Optional workspace-relative file the definition is claimed to live in. When "
            "set, `valid` is true only if a definition is found IN THAT FILE (see "
            "`found_in` / `in_file`). Container mode only. Python, JavaScript, "
            "TypeScript, Go, Java, Rust, C/C++ and C# files are searched; any other "
            "extension is not: `language_supported` is false and `valid` is null."
        ),
    )


class ValidateClassReferenceResponse(BaseModel):
    """Response for class reference validation.

    A dotted ``class_name`` (``module.Class``) additionally sets
    ``module_resolution``, ``module`` and ``name``; an undotted one leaves them
    ``null`` and behaves exactly as before. ``valid`` is ``null`` only when
    ``module_resolution == "external"``: the module is not in this repo
    (stdlib or a dependency), so existence was NOT computed and the caller
    decides. See ``services.dotted_names``.
    """

    valid: bool | None
    class_name: str
    found_in: list[str] = Field(
        default_factory=list,
        description="Every workspace-relative file a matching definition was found in.",
    )
    in_file: bool | None = Field(
        default=None,
        description="Only when `file_path` was requested: whether a definition is in that file.",
    )
    language_supported: bool = Field(
        default=True,
        description=(
            "False when the requested `file_path` has an extension no searched language "
            "covers (see `searched_languages`), so `valid` is then null (not computed) -- "
            "callers MUST read this field before treating a non-true `valid` as 'does not "
            "exist'."
        ),
    )
    module_resolution: str | None = Field(
        default=None,
        description=(
            "Dotted names only. 'in_repo': the module's top-level package is in the "
            "repo and the class was looked for in that module's file only. "
            "'external': the module is not in the repo; valid is null. "
            "Null for an undotted class_name."
        ),
    )
    module: str | None = None
    name: str | None = None
    module_search_roots: list[str] = Field(default_factory=list)
    details: dict[str, Any] | None = None
    resolution: str = RESOLUTION_CONTAINER
    searched_languages: list[str] = Field(
        default_factory=lambda: list(SEARCHED_LANGUAGES),
        description=(
            "Languages searched for this answer. Container mode: Python by `def`/`class` "
            "grep, the rest by candidate-file grep confirmed through coding-analysis "
            "symbol extraction; a located `file_path` searches only its own language. "
            "A class absent from a JS/TS barrel, Python package `__init__` (`pkg.Cls`) or "
            "Rust `pub use` but re-exported there is followed to its source, at most 3 "
            "hops: found -> true ('re-exported via'); chain complete and absent -> false; "
            "a package or path-alias hop -> null. A dotted `Owner.Inner` whose owner is "
            "present follows the owner's bases like a function reference. A C++20 "
            "`concept` counts as a class/type. A Rust/C/C++ file that mentions the name "
            "otherwise is null (macros are not expanded). "
            "Explicit `codebase_path` mode: python only."
        ),
    )
    excluded_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Directory basename globs NOT searched (vendored/generated code: .venv, "
            "site-packages, node_modules, ...). A name that exists only there is "
            "reported as absent. Empty for explicit-path resolution, which walks the "
            "whole tree."
        ),
    )
    note: str | None = None


class ValidateCodeSnippetRequest(BaseModel):
    """Request for code snippet validation."""

    code_snippet: str
    codebase_path: str | None = None
    container_name: str | None = None


class ValidateCodeSnippetResponse(BaseModel):
    """Response for code snippet validation."""

    valid: bool = False
    issues: list[dict[str, Any]] = []
    suggestion: str | None = None
    overall_score: float = 0.0
    resolution: str = RESOLUTION_EXPLICIT
    verdict: str | None = Field(
        default=None,
        description=(
            "'valid' | 'invalid' | 'not_computed'. Container mode: coding-containers' "
            "verdict (syntax + import resolution in the workspace); `valid` is true only "
            "when this is 'valid'. 'not_computed' means some import could not be checked "
            "-- it is NOT a pass."
        ),
    )
    imports: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Container mode: each import {module, name, line, status, ...}.",
    )
    unresolved_imports: list[str] = Field(default_factory=list)
    import_resolution: str | None = None
    not_computed_reason: str | None = None


class CheckFunctionConsistencyRequest(BaseModel):
    """Request for function consistency check."""

    function_code: str
    description: str
    context: str | None = None


class _ConsistencyResponse(BaseModel):
    """Shared shape for the three consistency endpoints.

    ``consistent`` is ``None`` -- not ``True``, not ``False`` -- because this
    endpoint cannot compute the claim that field names. See
    ``_CONSISTENCY_NOT_COMPUTED_REASON``.
    """

    consistent: bool | None = Field(
        default=None,
        description=(
            "The STRUCTURAL verdict (`structural_consistent`) when the description names "
            "at least one parameter explicitly; otherwise null (see `consistent_reason`). "
            "Never a semantic verdict -- `semantic` is always 'not_computed'."
        ),
    )
    consistent_reason: str | None = None
    structural_consistent: bool | None = Field(
        default=None,
        description=(
            "Every parameter the description explicitly names exists in the parsed "
            "signature. Null when no parameter is named or the code has no parseable "
            "function/class signature."
        ),
    )
    semantic: str = "not_computed"
    described_parameters: list[str] = Field(default_factory=list)
    signature_parameters: list[str] = Field(default_factory=list)
    missing_parameters: list[str] = Field(
        default_factory=list,
        description="Parameters the description names that the signature does not have.",
    )
    not_computed: bool = True
    not_computed_reason: str = _CONSISTENCY_NOT_COMPUTED_REASON
    documentation_consistent: bool = False
    documentation_score: float = 0.0
    checks_performed: list[str] = Field(default_factory=lambda: list(_CONSISTENCY_CHECKS_PERFORMED))
    confidence: float = Field(
        default=0.0,
        description=(
            "Retained for compatibility. This is the DOCUMENTATION-completeness "
            "score, not confidence in `consistent`."
        ),
    )
    issues: list[str] = []
    suggestion: str | None = None


class CheckFunctionConsistencyResponse(_ConsistencyResponse):
    """Response for function consistency check."""


class CheckClassConsistencyRequest(BaseModel):
    """Request for class consistency check."""

    class_code: str
    description: str
    context: str | None = None


class CheckClassConsistencyResponse(_ConsistencyResponse):
    """Response for class consistency check."""


class CheckModuleConsistencyRequest(BaseModel):
    """Request for module consistency check."""

    modules: list[str]
    module_paths: list[str]


class CheckModuleConsistencyResponse(_ConsistencyResponse):
    """Response for module consistency check."""


class CodeValidationRequest(BaseModel):
    """Code validation request."""

    container_name: str
    file_path: str
    validation_type: str


class CodeValidationResponse(BaseModel):
    """Code validation response."""

    validation_results: dict[str, Any]


class ArchitectureValidationRequest(BaseModel):
    """Architecture validation request."""

    container_name: str
    rules: list[str]
    scope: str | None = None
    severity_threshold: str | None = None
    include_suggestions: bool | None = False


class ArchitectureValidationResponse(BaseModel):
    """Architecture validation response."""

    validation_results: dict[str, Any]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidCodebasePathError(Exception):
    """Custom exception for when a codebase path is invalid or inaccessible."""


class ContainerNotFoundException(Exception):  # noqa: N818
    """Container not found."""

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name
        super().__init__(f"Container '{container_name}' not found")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_codebase_input(what: str) -> HTTPException:
    """400 naming the input that was missing.

    Deliberately NOT a 200 with ``valid=false``. "I cannot check" and "it is not
    there" are different claims; answering the first with the shape of the
    second is what made every one of these endpoints unfalsifiable.
    """
    return HTTPException(
        status_code=400,
        detail=(
            f"Cannot validate {what}: no codebase was supplied. Provide "
            f"'container_name' (the workspace to check against) or an explicit "
            f"'codebase_path'. This endpoint no longer falls back to the "
            f"gateway's own filesystem, which previously made every answer "
            f"'valid=false' regardless of truth."
        ),
    )


def _validate_explicit_path(explicit_path: str) -> str:
    """Validate a caller-supplied codebase directory, or raise HTTPException.

    Kept working on purpose: a caller that legitimately has a path may pass one.
    What is gone is the *silent default* to a path the caller never named.
    """
    if "\x00" in explicit_path:
        raise HTTPException(
            status_code=400, detail="Invalid codebase path: embedded null character detected"
        )

    normalized_path = os.path.normpath(explicit_path)
    if normalized_path == os.path.normpath("/") or (
        len(Path(normalized_path).parts) <= 2 and normalized_path.startswith("/")
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid codebase path: '{explicit_path}' is too broad or shallow.",
        )

    if not os.path.isdir(explicit_path):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid codebase path: '{explicit_path}' is not a directory.",
        )
    return explicit_path


def _workspace_error(exc: Exception) -> HTTPException:
    """Map a workspace failure to the status code that states the right claim.

    404 -- we asked, and this tenant has no such workspace.
    503 -- we could not finish looking, so we do not know. Never a verdict.

    ⭐ WHY THE CLIPPED-SEARCH CASE GETS A TYPED BODY AND THE OTHERS DO NOT (yet).
    A truncated search is "I could not finish", which belongs on the 503 side
    with the timeout and the dead backend — it is emphatically not a 200
    ``valid=false`` and not a 404. But a caller that has to tell them apart
    would, on a bare 503, be left grepping English out of ``detail``. So this
    one is raised with the house-standard structured body
    (``routers/jobs.py:138-143``): ``error_code``, ``message`` and a ``details``
    map naming which tool was clipped and by how much, which is the one thing a
    caller can act on (retry a narrower query, or raise the workspace's output
    budget).

    ⚠️ WHAT ACTUALLY REACHES A CALLER — READ THIS BEFORE TRUSTING THE FIELD NAMES
    ABOVE. ⛔ **Correction.** This docstring, and the commit that introduced it,
    both claimed: *"The four outcomes are nonetheless distinguishable at the
    caller today: 200, 404, 503 with a dict ``detail`` (clipped), 503 with a
    string ``detail`` (timeout or backend)."* **That is false of the deployed
    service.** It was true only of the test application, because
    ``tests/conftest.py`` replaces ``register_exception_handlers`` with a no-op,
    so endpoint tests see FastAPI's raw ``{"detail": ...}``. The real
    ``create_app`` installs ``coding_client.api.exception_handlers``, whose
    ``_handle_http`` rebuilds every ``HTTPException`` as
    ``{"error": {code, message, timestamp, request_id, details?}}``, forwarding
    only ``message`` and ``details`` and setting ``code = f"HTTP_{status}"``.
    Measured end to end against the real envelope
    (``tests/unit/test_validation_error_envelope.py``) and on the live pod:

    - **there is no ``detail`` key on the wire at all**;
    - ``error_code`` is **dropped** — the string ``WORKSPACE_SEARCH_INCOMPLETE``
      appears nowhere in the response;
    - ``code`` is ``"HTTP_503"`` for the clipped, the timed-out and the dead-backend
      case alike.

    So the discriminator that SHIPS is: **the presence of ``error.details``, and
    within it ``error.details.tool`` plus ``error.details.bytes_omitted``.** A
    clipped search is the only one of the three that carries ``details`` at all,
    because the other two raise a string ``detail`` which ``_handle_http`` maps to
    ``details=None`` (and an omitted key). That separation is **incidental, not
    structural**: it falls out of "this branch happens to pass a dict", would be
    reproduced by any unrelated ``HTTPException`` whose detail dict has a
    ``details`` key, and is dropped outright by ``_global``'s
    ``BaseExceptionGroup`` branch, which never passes ``details=`` at all.

    **A timeout and a dead backend are NOT separable as deployed.** Both are
    ``503 {"error": {"code": "HTTP_503", "message": <english>}}`` with byte-identical
    structure; only the free text differs. The deferral below is therefore about
    those two, and it is a real gap, not a cosmetic one.

    ⛔ The envelope dropping ``error_code`` is a HOUSE-WIDE defect, not this PR's:
    the cited house standard ``routers/jobs.py:138`` loses ``TOO_MANY_JOBS`` the same
    way, and ``code`` becomes ``HTTP_429``. The pattern was copied faithfully; the
    pattern is broken. Fixing it changes every error body this service emits, so it
    is its own unit in its own PR — deliberately NOT ridden along here.

    That leaves the 124-timeout and the backend-error sharing one untyped 503,
    which an audit of this PR flagged and which is NOT fixed here: giving those
    two their own codes changes the body of a response that is already shipped
    and consumed, so it is a separate, deliberate contract change rather than a
    rider on this one.
    """
    if isinstance(exc, ContainerNotResolvableError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, WorkspaceSearchIncompleteError):
        return HTTPException(
            status_code=503,
            detail={
                "status": "error",
                "message": str(exc),
                "error_code": "WORKSPACE_SEARCH_INCOMPLETE",
                "details": {
                    "container_name": exc.container_name,
                    "tool": exc.tool,
                    "bytes_omitted": exc.bytes_omitted,
                },
            },
        )
    return HTTPException(status_code=503, detail=str(exc))


# Explicit parameter cues in a description. Deliberately narrow: a bare word
# after "parameter" is NOT taken ("adds the parameters together" must not name
# a parameter called `together`), only quoted/backticked names and docstring
# field syntax.
_PARAM_CUES = (
    re.compile(r":param\s+(?:[^\s:]+\s+)?([A-Za-z_]\w*)\s*:"),
    re.compile(r"@param\s+(?:\{[^}]*\}\s*)?([A-Za-z_]\w*)"),
    re.compile(r"\b(?:param(?:eter)?|arg(?:ument)?)s?\s+[`'\"]([A-Za-z_]\w*)[`'\"]", re.I),
)
_SECTION_HEADER = re.compile(r"^\s*(Args|Arguments|Parameters|Params)\s*:?\s*$")
_OTHER_HEADER = re.compile(
    r"^\s*(Returns?|Yields?|Raises|Examples?|Notes?|Attributes|See Also|Warnings?)\s*:?\s*$"
)
_SECTION_ENTRY = re.compile(r"^\s*\*{0,2}([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:")
_IMPLICIT_PARAMS = frozenset({"self", "cls"})


def _described_parameters(description: str) -> list[str]:
    """Parameter names the description EXPLICITLY names, in first-seen order."""
    names: list[str] = []
    for cue in _PARAM_CUES:
        names.extend(m.group(1) for m in cue.finditer(description))
    in_section = False
    entry_indent: int | None = None
    for line in description.splitlines():
        if _SECTION_HEADER.match(line):
            in_section, entry_indent = True, None
            continue
        if _OTHER_HEADER.match(line) or (in_section and not line.strip()):
            in_section = False
            continue
        if not in_section or set(line.strip()) <= {"-", "="}:
            continue  # outside a section, or a numpydoc underline
        indent = len(line) - len(line.lstrip())
        # Only lines at the FIRST entry's indentation are entries; a deeper
        # continuation line such as "    Default: 5" is prose, not a parameter.
        if (m := _SECTION_ENTRY.match(line)) and entry_indent in (None, indent):
            entry_indent = indent
            names.append(m.group(1))
    return list(dict.fromkeys(n for n in names if n not in _IMPLICIT_PARAMS))


def _arg_names(args: ast.arguments) -> list[str]:
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    names += [a.arg for a in (args.vararg, args.kwarg) if a is not None]
    return [n for n in names if n not in _IMPLICIT_PARAMS]


def _signature_parameters(code: str, kind: str) -> list[str] | None:
    """Parameters of the first top-level function (or class constructor) in *code*.

    A class's parameters are its ``__init__``'s; a class with no ``__init__``
    (dataclass / pydantic style) uses its annotated class attributes. ``None``
    when the code does not parse or has no such definition.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError, ValueError:
        return None
    for node in tree.body:
        if kind == "function" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _arg_names(node.args)
        if kind == "class" and isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    item.name == "__init__"
                ):
                    return _arg_names(item.args)
            return [
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            ]
    return None


def _structural_consistency(code: str | None, description: str | None, kind: str) -> dict[str, Any]:
    """The structural verdict described in the module comment, as response fields."""
    if code is None or description is None:
        return {
            "consistent": None,
            "structural_consistent": None,
            "consistent_reason": (
                "No single signature to check: structural consistency applies to one "
                "function or class against its description."
            ),
        }
    described = _described_parameters(description)
    signature = _signature_parameters(code, kind)
    if signature is None:
        return {
            "consistent": None,
            "structural_consistent": None,
            "described_parameters": described,
            "consistent_reason": f"The code has no parseable top-level {kind} definition.",
        }
    if not described:
        return {
            "consistent": None,
            "structural_consistent": None,
            "signature_parameters": signature,
            "consistent_reason": (
                "The description names no parameter explicitly (`:param x:`, `@param x`, "
                "an Args:/Parameters: entry, or parameter `x` in quotes/backticks), so "
                "there is nothing structural to check; semantic consistency is not computed."
            ),
        }
    missing = [name for name in described if name not in signature]
    structural = not missing
    return {
        "consistent": structural,
        "structural_consistent": structural,
        "described_parameters": described,
        "signature_parameters": signature,
        "missing_parameters": missing,
        "consistent_reason": None
        if structural
        else f"The description names parameter(s) the signature does not have: {missing}.",
    }


def _consistency_payload(
    result: dict[str, Any],
    *,
    code: str | None = None,
    description: str | None = None,
    kind: str = "function",
) -> dict[str, Any]:
    """Map a checker result onto the response shape.

    The computed part (documentation completeness) is passed through under a
    name that says what it is. ``consistent`` carries only the STRUCTURAL
    verdict (``_structural_consistency``), never a semantic one.
    """
    score = float(result.get("consistency_score", 0.0) or 0.0)
    return {
        **_structural_consistency(code, description, kind),
        "semantic": "not_computed",
        "not_computed": True,
        "not_computed_reason": _CONSISTENCY_NOT_COMPUTED_REASON,
        "documentation_consistent": bool(result.get("is_consistent", False)),
        "documentation_score": score,
        "confidence": score,
        "checks_performed": list(_CONSISTENCY_CHECKS_PERFORMED),
        "issues": [
            issue.get("description", str(issue)) if isinstance(issue, dict) else str(issue)
            for issue in result.get("issues", [])
        ],
        "suggestion": result["suggestions"][0] if result.get("suggestions") else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/file-reference",
    response_model=ValidateFileReferenceResponse,
    summary="Validate File Reference",
    description="Check if a file exists in the caller's workspace.",
    operation_id="validate_file_reference",
)
async def validate_file_reference(
    request: ValidateFileReferenceRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate File Reference - Check if a file exists in the caller's workspace."""
    if request.codebase_path:
        path = _validate_explicit_path(request.codebase_path)
        try:
            validator = ReferenceValidator(path)
            exists, correct_path = validator.validate_file_reference(request.file_path)
        except InvalidCodebasePathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error validating file reference: {e}"
            ) from e
        is_dir, line_count = _explicit_dir_and_lines(path, request.file_path, exists, request.line)
        valid, line_exists, note = _file_reference_verdict(
            exists=exists,
            is_dir=is_dir,
            line=request.line,
            line_count=line_count,
            allow_dir=request.allow_dir,
        )
        return ValidateFileReferenceResponse(
            valid=valid,
            file_path=request.file_path,
            suggestion=correct_path if not (exists or is_dir) else None,
            resolution=RESOLUTION_EXPLICIT,
            is_dir=is_dir,
            line_count=line_count,
            line_exists=line_exists,
            note=note,
        )

    if not request.container_name:
        raise _no_codebase_input("file reference")

    is_dir: bool | None = False
    line_count: int | None = None
    try:
        exists, candidates = await find_file(request.container_name, request.file_path, tenant_id)
        if not exists:
            # find_file looks for regular files only; a directory is a real path too.
            is_dir = await find_directory(request.container_name, request.file_path, tenant_id)
        elif request.line is not None:
            line_count = await count_lines(request.container_name, request.file_path, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        raise _workspace_error(e) from e

    suggestion = None
    if not exists and not is_dir and candidates:
        suggestion = f"Not found, did you mean one of these? {', '.join(candidates)}"

    valid, line_exists, note = _file_reference_verdict(
        exists=exists,
        is_dir=is_dir,
        line=request.line,
        line_count=line_count,
        allow_dir=request.allow_dir,
    )
    return ValidateFileReferenceResponse(
        valid=valid,
        file_path=request.file_path,
        suggestion=suggestion,
        resolution=RESOLUTION_CONTAINER,
        excluded_dirs=_excluded_dirs(RESOLUTION_CONTAINER),
        is_dir=is_dir,
        line_count=line_count,
        line_exists=line_exists,
        note=note,
    )


def _file_reference_verdict(
    *,
    exists: bool,
    is_dir: bool | None,
    line: int | None,
    line_count: int | None,
    allow_dir: bool,
) -> tuple[bool | None, bool | None, str | None]:
    """``(valid, line_exists, note)`` for a file reference. Never a false pass.

    - A directory is not a file: it makes ``valid`` true only with ``allow_dir``
      (``src/models`` must not satisfy ``src/models.py``).
    - ``line`` is part of the claim: out of range -> false; count not computed
      -> null, never "the file exists, so yes".
    - A directory check that was refused -> null under ``allow_dir`` (it could
      have been the answer); without ``allow_dir`` it cannot change the verdict.
    """
    if exists:
        if line is None:
            return True, None, None
        line_exists = _line_exists(line, line_count)
        if line_exists is None:
            return (
                None,
                None,
                f"The file exists, but its line count could not be computed, so whether "
                f"it has line {line} is NOT computed (valid=null). If coding-containers "
                f"refused the command, it was {POLICY_REFUSED_REASON}.",
            )
        if not line_exists:
            return False, False, f"The file has {line_count} line(s); line {line} does not exist."
        return True, True, None
    if not allow_dir:
        note = None
        if is_dir:
            note = (
                "A DIRECTORY exists at this path, but a file was claimed; set allow_dir=true "
                "if a directory satisfies the reference."
            )
        return False, None, note
    if is_dir is None:
        return (
            None,
            None,
            f"No file at this path, and the directory check was {POLICY_REFUSED_REASON}, "
            "so whether a directory exists here is NOT computed (valid=null).",
        )
    if is_dir:
        if line is not None:
            return False, None, f"Line {line} was requested, but the path is a directory."
        return True, None, None
    return False, None, None


def _line_exists(line: int | None, line_count: int | None) -> bool | None:
    """Whether 1-based *line* is within *line_count*; null when either is unknown."""
    if line is None or line_count is None:
        return None
    return 1 <= line <= line_count


def _explicit_dir_and_lines(
    codebase: str, file_path: str, is_file: bool, line: int | None
) -> tuple[bool, int | None]:
    """Explicit-path mode: is *file_path* a directory, and how many lines has the file.

    Confined to *codebase*: a path that resolves outside it is treated as absent.
    """
    try:
        relative = validate_relative_path(file_path)
    except ValueError:
        return False, None
    root = os.path.realpath(codebase)
    target = os.path.realpath(os.path.join(root, relative))
    if os.path.commonpath([root, target]) != root:
        return False, None
    if not is_file:
        return os.path.isdir(target), None
    if line is None or not os.path.isfile(target):
        return False, None
    with open(target, encoding="utf-8", errors="replace") as fh:
        return False, sum(1 for _ in fh)


async def _validate_symbol_reference(
    *,
    name: str,
    kind: str,
    container_name: str | None,
    codebase_path: str | None,
    tenant_id: str,
) -> tuple[bool, dict[str, Any] | None, str, str | None]:
    """Shared body for function/class reference validation.

    Returns ``(valid, details, resolution, note)``.
    """
    if codebase_path:
        path = _validate_explicit_path(codebase_path)
        try:
            validator = ReferenceValidator(path)
            if kind == "class":
                exists, details = validator.validate_class_reference(name)
            else:
                exists, details = validator.validate_function_reference(name)
        except InvalidCodebasePathError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error validating {kind} reference: {e}"
            ) from e
        return exists, details, RESOLUTION_EXPLICIT, _language_note(exists, RESOLUTION_EXPLICIT)

    if not container_name:
        raise _no_codebase_input(f"{kind} reference")

    # `Class.method` is a legitimate function reference; search the method name.
    search_name = name.rsplit(".", 1)[-1] if kind == "function" else name

    try:
        matches = await grep_definition(container_name, search_name, tenant_id, kind=kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        raise _workspace_error(e) from e

    if matches:
        primary = matches[0]
        details: dict[str, Any] | None = {
            "file": primary["file"],
            "line": primary["line"],
            "source": primary["source"],
            "definition_count": len(matches),
            "definitions": matches,
        }
        return True, details, RESOLUTION_CONTAINER, None

    return False, None, RESOLUTION_CONTAINER, _language_note(False, RESOLUTION_CONTAINER)


def _excluded_dirs(resolution: str) -> list[str]:
    """What the search did NOT look into, for this resolution mode.

    Only the container search prunes ``VENDOR_EXCLUDE_DIRS``; the explicit-path
    ``ReferenceValidator`` walks everything, so claiming exclusions there would be false.
    """
    return list(VENDOR_EXCLUDE_DIRS) if resolution == RESOLUTION_CONTAINER else []


def _language_note(exists: bool, resolution: str) -> str | None:
    """Explain the scope of a negative result, so absence is not overclaimed."""
    if exists:
        return None
    if resolution == RESOLUTION_EXPLICIT:
        return (
            "Searched Python definition syntax in *.py files only (explicit "
            f"codebase_path mode, searched_languages={list(PYTHON_ONLY)}). A symbol "
            "defined in another language would not be found here, so valid=false means "
            "'no Python definition found', not 'this symbol does not exist'."
        )
    return "No Python definition found (def/class grep over *.py)."


_NOT_SEARCHABLE_NOTE = (
    "not searchable: the requested file_path's extension is not a supported source "
    f"language. Supported: {list(SEARCHED_LANGUAGES)} (.py .pyw .js .jsx .mjs .cjs .ts "
    ".tsx .go .java .rs .c .h .cc .cpp .cxx .c++ .hh .hpp .hxx .h++ .cs). Existence was "
    "NOT computed (valid=null, language_supported=false). Absence was not established."
)


# Candidate files confirmed through symbol extraction per unlocated search.
_SYMBOL_CANDIDATE_CAP = 20


def _symbol_details(definitions: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not definitions:
        return None
    primary = definitions[0]
    return {
        "file": primary["file"],
        "line": primary["line"],
        "kind": primary["kind"],
        "parent": primary["parent"],
        "source": "symbols",
        "definition_count": len(definitions),
        "definitions": definitions,
    }


async def _symbols_in_file(
    container_name: str,
    path: str,
    name: str,
    kind: str,
    tenant_id: str,
    ops: WalkOps | None = None,
) -> tuple[FileMatch, bool]:
    """Read *path* (checked) and match *name* in its coding-analysis symbols.

    Returns ``(match, upstream_failed)``. Only a confirmed absence of the FILE
    is ``False``; an incomplete read or an upstream failure is ``None`` with a
    note. An unknown workspace is still a 404.
    """
    try:
        content = await read_workspace_file_checked(container_name, path, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ContainerNotResolvableError as e:
        raise _workspace_error(e) from e
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError) as e:
        return FileMatch(
            None,
            f"'{path}' could not be read completely ({e}), so whether '{name}' is "
            "defined there was NOT computed (valid=null).",
        ), False
    if content is None:
        return FileMatch(False, f"'{path}' does not exist in the workspace."), False
    fetched = await fetch_symbols(content, path, tenant_id)
    if fetched.body is None:
        return FileMatch(
            None,
            f"Symbol extraction for '{path}' failed ({fetched.error}), so whether "
            f"'{name}' is defined there was NOT computed (valid=null).",
        ), True
    match = match_symbol(fetched.body, name, kind, path, content)
    walk = ops if ops is not None else _walk_ops(container_name, tenant_id)
    return await resolve_walk(match, name, kind, walk), False


def _walk_ops(container_name: str, tenant_id: str) -> WalkOps:
    """Workspace operations for one inheritance / re-export walk (``services.symbol_walk``).

    ``fetch_symbols`` is looked up at call time (this module's name), like
    every other symbols call here.
    """

    async def read(path: str) -> str | None:
        return await read_workspace_file_checked(container_name, path, tenant_id)

    async def symbols(code: str, path: str):
        return await fetch_symbols(code, path, tenant_id)

    async def mentions(word: str) -> list[str]:
        return await grep_files_mentioning(container_name, word, tenant_id)

    async def existing(candidates: list[str]) -> set[str]:
        return await find_module_files(container_name, candidates, tenant_id)

    async def lines(pattern: str, extensions: tuple[str, ...]) -> list[dict[str, Any]]:
        return await grep_lines(container_name, pattern, extensions, tenant_id)

    return WalkOps(read=read, symbols=symbols, mentions=mentions, existing=existing, lines=lines)


async def _python_monkeypatch_note(
    container_name: str, qualname: str, tenant_id: str
) -> str | None:
    """Why a Python ``Owner.member`` absence is not ``False``: an assignment
    ``Owner.member = ...`` or a ``setattr(Owner, ...)`` anywhere in the workspace
    (or the search for one could not finish). ``None``: no such line exists."""
    *owners, member = qualname.split(".")
    owner = owners[-1]
    if not (owner.isidentifier() and member.isidentifier()):
        return None
    o, m = re.escape(owner), re.escape(member)
    pattern = rf"\b{o}\.{m}[[:space:]]*=([^=]|$)|\bsetattr\([[:space:]]*{o}[[:space:]]*,"
    try:
        hits = await grep_lines(container_name, pattern, (".py", ".pyw"), tenant_id)
    except (ValueError, WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError) as e:
        return (
            f"'{member}' is not defined on '{owner}', but the search for monkeypatching "
            f"(`{owner}.{member} = ...` / `setattr({owner}, ...)`) could not finish ({e}), "
            "so absence is NOT computed (valid=null)."
        )
    if not hits:
        return None
    where = [f"{h['file']}:{h['line']}" for h in hits[:3]]
    return (
        f"'{member}' is not defined on '{owner}' as a class member, but it may be "
        f"monkeypatched onto it at {where} (`{owner}.{member} = ...` / `setattr`), so "
        "absence is NOT computed (valid=null)."
    )


async def _discover_in_other_languages(
    container_name: str, name: str, kind: str, tenant_id: str
) -> tuple[bool | None, str, list[dict[str, Any]]]:
    """Unlocated search of every non-Python language: ``(verdict, note, definitions)``.

    One ``grep -rlw`` lists the files containing the (last segment of the) name;
    up to ``_SYMBOL_CANDIDATE_CAP`` of them are confirmed through symbol
    extraction. True if any confirms. False only when every candidate was
    checked and refuted and the list was not capped. Otherwise null -- including
    when a file of a type without symbol extraction (``.vue``, ``.kt``, ...)
    mentions the name: a definition there could not have been seen.

    A dotted function name (``Owner.member``) ALSO lists the files mentioning the
    OWNER: a file that defines the owner type without the member makes the
    answer null (the member may be inherited or embedded from a base that is not
    followed), so false needs the owner to be defined in no candidate file.
    """
    words = [name.rsplit(".", 1)[-1]]
    if kind == "function" and "." in name:
        words.append(name.split(".")[-2])
    try:
        found: set[str] = set()
        for word in dict.fromkeys(words):
            found.update(await grep_files_mentioning(container_name, word, tenant_id))
        candidates = sorted(found)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ContainerNotResolvableError as e:
        raise _workspace_error(e) from e
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError) as e:
        return (
            None,
            (
                f"The non-Python candidate search could not finish ({e}), so other "
                "languages were NOT searched (valid=null)."
            ),
            [],
        )
    unsearched = [c for c in candidates if symbol_language(c) is None]
    candidates = [c for c in candidates if symbol_language(c) is not None]
    checked = candidates[:_SYMBOL_CANDIDATE_CAP]
    confirmed: list[dict[str, Any]] = []
    implicit: list[str] = []
    undecided: list[str] = []
    upstream_down = False
    # ONE walk budget (and file cache) shared by every candidate's inheritance /
    # re-export follow-up, so an unlocated search stays bounded.
    walk = _walk_ops(container_name, tenant_id)
    for path in checked:
        if upstream_down:
            undecided.append(f"{path}: not checked (symbol extraction unavailable)")
            continue
        match, upstream_failed = await _symbols_in_file(
            container_name, path, name, kind, tenant_id, walk
        )
        upstream_down = upstream_failed
        if match.verdict is True:
            if match.definitions:
                confirmed += match.definitions
            else:
                # A true with no definition site (a member supplied implicitly by
                # the language root, e.g. java.lang.Object.toString): still true.
                implicit.append(match.note or path)
        elif match.verdict is None:
            undecided.append(match.note or path)
    if confirmed:
        files = sorted({d["file"] for d in confirmed})
        return True, f"Defined in {files} (confirmed by symbol extraction).", confirmed
    if implicit:
        return True, " ".join(dict.fromkeys(implicit)), []
    capped = len(candidates) > len(checked)
    if undecided or capped or unsearched:
        reasons = list(undecided)
        if unsearched:
            types = sorted({os.path.splitext(p)[1].lower() for p in unsearched})
            reasons.append(
                f"{len(unsearched)} file(s) of types whose definitions are not searched "
                f"({' '.join(types)}) mention it: {unsearched[:5]}"
            )
        if capped:
            reasons.append(
                f"only the first {len(checked)} of {len(candidates)} candidate files were checked"
            )
        return (
            None,
            (
                "Other languages: no checked file confirms a definition, but "
                + "; ".join(reasons)
                + " (valid=null)."
            ),
            [],
        )
    if not checked:
        what = "the name" if len(words) == 1 else "the name (neither the member nor the owner)"
        return False, f"Other languages: no non-Python source file contains {what}.", []
    return (
        False,
        (
            f"Other languages: {len(checked)} candidate file(s) contain the word but none "
            f"defines '{name}' as a {kind}: {checked}."
        ),
        [],
    )


def _combine_with_other_languages(
    python_valid: bool | None,
    python_note: str | None,
    other: tuple[bool | None, str, list[dict[str, Any]]],
) -> tuple[bool | None, str | None, list[dict[str, Any]]]:
    """Unlocated verdict over Python + every other language."""
    verdict, note, definitions = other
    if verdict is True:
        # With no definition site the true is a language-root member (implicit_root):
        # keep the note, it is the only evidence the caller gets.
        return True, (None if definitions else note), definitions
    if python_valid is False and verdict is False:
        return False, f"{python_note or 'No Python definition found.'} {note}", []
    python_part = python_note or (
        "No Python definition found." if python_valid is False else "Python: not computed."
    )
    return None, f"{python_part} {note}".strip(), []


def _located_file(file_path: str | None, codebase_path: str | None) -> str | None:
    """Normalise a requested definition file to a workspace-relative path.

    Accepts ``src/a.py``, ``./src/a.py`` and ``/workspace/src/a.py``. Any other
    absolute path or traversal is a 400. Container mode only: the explicit-path
    index keeps ONE entry per name (the last one indexed), so it cannot say
    whether a definition is in a given file -- refusing beats a guess.
    """
    if file_path is None:
        return None
    if codebase_path:
        raise HTTPException(
            status_code=400,
            detail=(
                "'file_path' is supported with 'container_name' only: the explicit "
                "'codebase_path' index keeps one definition per name and cannot say "
                "whether a definition is in a given file."
            ),
        )
    cleaned = file_path.strip()
    prefix = WORKSPACE_ROOT.rstrip("/") + "/"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    try:
        return validate_relative_path(cleaned)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


def _is_python_file(path: str) -> bool:
    return path.endswith(".py")


def _apply_location(
    valid: bool | None, details: dict[str, Any] | None, located: str | None
) -> tuple[bool | None, list[str], bool | None, str | None]:
    """Narrow a repo-wide verdict to the requested file.

    Returns ``(valid, found_in, in_file, note)``. ``found_in`` is every file a
    definition was found in (container mode); with no *located* file the
    verdict is unchanged and ``in_file`` is null.
    """
    definitions = (details or {}).get("definitions") or []
    found_in = sorted({d["file"] for d in definitions if isinstance(d, dict) and d.get("file")})
    if located is None or valid is None:
        return valid, found_in, None, None
    in_file = located in found_in
    note = None
    if not in_file:
        note = f"No Python definition in '{located}'" + (
            f"; found in {found_in}." if found_in else "; none found anywhere searched."
        )
    return in_file, found_in, in_file, note


_OWNER_NOT_CHECKED_NOTE = (
    "Dotted name: only the LAST segment was searched as a Python definition; the "
    "owner class (or module) was NOT checked, so a same-named method on another class "
    "satisfies this (explicit `codebase_path` mode). Container mode checks the owner class."
)


def _scope_statements(body: list[ast.stmt]) -> list[ast.stmt]:
    """Statements of a module/class body, descending into if/try/with blocks
    (conditional definitions) but never into nested defs or classes."""
    out: list[ast.stmt] = []
    for node in body:
        out.append(node)
        if isinstance(node, ast.If):
            out += _scope_statements(node.body) + _scope_statements(node.orelse)
        elif isinstance(node, ast.Try | ast.TryStar):
            for block in (node.body, node.orelse, node.finalbody):
                out += _scope_statements(block)
            for handler in node.handlers:
                out += _scope_statements(handler.body)
        elif isinstance(node, ast.With | ast.AsyncWith):
            out += _scope_statements(node.body)
    return out


def _defines(body: list[ast.stmt], name: str) -> bool:
    """Does this scope define *name* (def / async def / assignment)?"""
    for node in _scope_statements(body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return True
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return True
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            return True
    return False


_DYNAMIC_ATTRIBUTE_HOOKS = ("__getattr__", "__getattribute__")


def _slots_literal(value: ast.expr | None) -> list[str] | None:
    """Slot names of a literal ``__slots__`` value, else None (not a literal)."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, ast.Tuple | ast.List | ast.Set):
        elts: list[ast.expr | None] = list(value.elts)
    elif isinstance(value, ast.Dict):  # ``{'name': 'doc'}``: the keys are the slots
        elts = list(value.keys)
    else:
        return None
    names = [e.value for e in elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return names if len(names) == len(elts) else None


def _slots_assignments(cls: ast.ClassDef) -> list[ast.Assign | ast.AnnAssign]:
    return [
        node
        for node in _scope_statements(cls.body)
        if isinstance(node, ast.Assign | ast.AnnAssign)
        and any(
            isinstance(t, ast.Name) and t.id == "__slots__"
            for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]


def _method_receiver(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The first parameter of a method (the instance, or the class of a
    ``@classmethod``); None for a ``@staticmethod`` -- its first parameter is
    an ordinary argument, so ``x.stat = 1`` sets nothing on the class."""
    for d in fn.decorator_list:
        if (isinstance(d, ast.Name) and d.id == "staticmethod") or (
            isinstance(d, ast.Attribute) and d.attr == "staticmethod"
        ):
            return None
    args = fn.args.posonlyargs + fn.args.args
    return args[0].arg if args else None


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_scope_nodes(body: list[ast.stmt]) -> list[ast.AST]:
    """Every node of *body*, without descending into nested functions, classes or
    lambdas: a ``self`` there is a different binding (its own parameter)."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        out.append(node)
        if isinstance(node, _NESTED_SCOPES):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


def _binding_targets(target: ast.expr) -> list[ast.expr]:
    """*target* with tuple / list / starred unpacking flattened."""
    if isinstance(target, ast.Tuple | ast.List):
        return [t for e in target.elts for t in _binding_targets(e)]
    if isinstance(target, ast.Starred):
        return _binding_targets(target.value)
    return [target]


def _creates_attribute(node: ast.AST, receiver: str, name: str) -> bool:
    """*node* creates ``<receiver>.<name>``: a direct (possibly unpacked) target of
    an assignment, an annotated assignment WITH a value, a ``for`` / ``with``
    target, or ``setattr(<receiver>, '<name>', ...)``. Not ``+=`` (needs the
    attribute already), not ``receiver.name[k] = ...`` or ``receiver.name.x = ...``
    (those read it)."""
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    elif isinstance(node, ast.For | ast.AsyncFor):
        targets = [node.target]
    elif isinstance(node, ast.withitem) and node.optional_vars is not None:
        targets = [node.optional_vars]
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
    ):
        obj, attr = node.args[0], node.args[1]
        return (
            isinstance(obj, ast.Name)
            and obj.id == receiver
            and isinstance(attr, ast.Constant)
            and attr.value == name
        )
    return any(
        (
            isinstance(t, ast.Attribute)
            and t.attr == name
            and isinstance(t.value, ast.Name)
            and t.value.id == receiver
        )
        or (  # ``self.__dict__['name'] = ...`` / ``vars(self)['name'] = ...``
            isinstance(t, ast.Subscript)
            and _is_receiver_dict(t.value, receiver)
            and isinstance(t.slice, ast.Constant)
            and t.slice.value == name
        )
        for target in targets
        for t in _binding_targets(target)
    )


def _instance_attribute_line(cls: ast.ClassDef, name: str) -> int | None:
    """Line where a method of *cls* creates ``self.<name>`` / ``cls.<name>``, else None.

    ``_defines`` reads the class body only, so an attribute created in ``__init__``
    (``self.size = 3``) answered False: a real attribute rejected as a phantom.
    Literal ``__slots__`` entries count too. Only the method's OWN scope counts
    (not nested functions / classes / lambdas), only its receiver (not a
    ``@staticmethod``'s first argument), and only statements that create the
    attribute (see ``_creates_attribute``).
    """
    for node in _slots_assignments(cls):
        if name in (_slots_literal(node.value) or []):
            return node.lineno
    for node in _scope_statements(cls.body):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        receiver = _method_receiver(node)
        if receiver is None:
            continue
        for sub in _own_scope_nodes(node.body):
            if _creates_attribute(sub, receiver, name):
                return getattr(sub, "lineno", None) or node.lineno
    return None


def _is_receiver_dict(node: ast.expr, receiver: str) -> bool:
    """``<receiver>.__dict__`` or ``vars(<receiver>)``."""
    if isinstance(node, ast.Attribute) and node.attr == "__dict__":
        return isinstance(node.value, ast.Name) and node.value.id == receiver
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "vars"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == receiver
    )


def _dynamic_store(node: ast.AST, receiver: str) -> str | None:
    """*node* creates an attribute of *receiver* whose name is not a literal:
    ``setattr(self, <expr>, ...)``, ``self.__setattr__(<expr>, ...)`` /
    ``object.__setattr__(self, <expr>, ...)``, ``self.__dict__.update(...)`` /
    ``vars(self).update(...)`` / ``.setdefault(...)``, or
    ``self.__dict__[<expr>] = ...``. None when it is none of those."""
    if isinstance(node, ast.Call):
        func, args = node.func, node.args
        if isinstance(func, ast.Name) and func.id == "setattr" and len(args) >= 2:
            obj, attr = args[0], args[1]
            if isinstance(obj, ast.Name) and obj.id == receiver:
                if not (isinstance(attr, ast.Constant) and isinstance(attr.value, str)):
                    return "setattr() with a computed name"
        if isinstance(func, ast.Attribute) and func.attr == "__setattr__":
            if isinstance(func.value, ast.Name) and func.value.id == receiver:
                name_arg = args[0] if args else None
            elif args and isinstance(args[0], ast.Name) and args[0].id == receiver:
                name_arg = args[1] if len(args) > 1 else None
            else:
                name_arg = ast.Constant(value="")
            if not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str)):
                return "__setattr__() with a computed name"
        if (
            isinstance(func, ast.Attribute)
            and func.attr in ("update", "setdefault")
            and _is_receiver_dict(func.value, receiver)
        ):
            return f"{func.attr}() on its __dict__"
        return None
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign | ast.AugAssign):
        targets = [node.target]
    for target in targets:
        for t in _binding_targets(target):
            if isinstance(t, ast.Subscript) and _is_receiver_dict(t.value, receiver):
                key = t.slice
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    return "an item store into its __dict__"
    return None


def _dynamic_attribute_hook(cls: ast.ClassDef) -> str | None:
    """``__getattr__`` / ``__getattribute__`` defined on *cls*, a ``__slots__``
    that is not a literal or that lists ``__dict__``, or a method that creates
    attributes with computed names (``_dynamic_store``): any name may resolve,
    absence cannot be proven."""
    if hook := next((h for h in _DYNAMIC_ATTRIBUTE_HOOKS if _defines(cls.body, h)), None):
        return hook
    slots = _slots_assignments(cls)
    if any(n.value is not None and _slots_literal(n.value) is None for n in slots):
        return "a __slots__ that is not a literal"
    if any("__dict__" in (_slots_literal(n.value) or []) for n in slots):
        return "a __slots__ that lists __dict__"
    for node in _scope_statements(cls.body):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        receiver = _method_receiver(node)
        if receiver is None:
            continue
        for sub in _own_scope_nodes(node.body):
            if why := _dynamic_store(sub, receiver):
                return f"a method ({node.name}) that creates attributes via {why}"
    return None


def _class_in(body: list[ast.stmt], name: str) -> ast.ClassDef | None:
    return next(
        (n for n in _scope_statements(body) if isinstance(n, ast.ClassDef) and n.name == name),
        None,
    )


def _resolve_owner_chain(tree: ast.Module, owners: list[str]) -> ast.ClassDef | None:
    """Class ``owners[0].owners[1]...`` in *tree* (non-empty *owners*), or None."""
    scope: list[ast.stmt] = tree.body
    match: ast.ClassDef | None = None
    for owner in owners:
        match = _class_in(scope, owner)
        if match is None:
            return None
        scope = match.body
    return match


def _module_parts(located: str) -> list[str]:
    parts = located[: -len(".py")].split("/")
    return parts[:-1] if parts[-1] == "__init__" else parts


# Attributes every Python class has from ``object`` -- ``dir(object)`` common to
# CPython 3.8+ (``__getstate__``, 3.11+, left out: absent there it would be a wrong true).
_OBJECT_MEMBERS = frozenset(
    {"__class__", "__delattr__", "__dir__", "__doc__", "__eq__", "__format__", "__ge__",
     "__getattribute__", "__gt__", "__hash__", "__init__", "__init_subclass__", "__le__",
     "__lt__", "__ne__", "__new__", "__reduce__", "__reduce_ex__", "__repr__",
     "__setattr__", "__sizeof__", "__str__", "__subclasshook__"}
)  # fmt: skip


def _explicit_bases(cls: ast.ClassDef) -> list[str]:
    """Base expressions of *cls*, ``object`` excluded (it defines no user member)."""
    return [b for b in (ast.unparse(base) for base in cls.bases) if b != "object"]


def _owner_verdict(source: str, located: str, qualname: str) -> tuple[bool | None, str | None]:
    """Is ``Owner[.Inner].member`` defined ON that class in *source*?

    The class chain is resolved FIRST: when the first segment names a class in
    the file, no module prefix is stripped (so ``Cls.meth`` in ``a/Cls.py`` is
    not satisfied by a module-level ``meth``). Only when no such class exists is
    a leading module qualifier (``pkg.mod.Cls.meth``) accepted, and only as a
    suffix of *located*'s own dotted module path, so ``wrong.Cls.meth`` cannot
    pass by stripping. ``None`` when the file does not parse here, or when the
    owner class does not define the member but has bases it may inherit it from
    (bases are not followed).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        return None, (
            f"'{located}' does not parse, so whether '{qualname}' is defined on its owner "
            "class there was NOT computed (valid=null)."
        )
    *owners, member = qualname.split(".")
    module = _module_parts(located)
    if _class_in(tree.body, owners[0]) is not None:
        chains = [owners]
    else:
        chains = [owners[k:] for k in range(1, len(owners) + 1) if module[-k:] == owners[:k]]
    inherited: list[str] = []
    dynamic: list[str] = []
    for chain in chains:
        if not chain:
            # The whole qualifier is this file's module path: a module-level member.
            if _defines(tree.body, member):
                return True, None
            if _defines(tree.body, "__getattr__"):  # PEP 562: any module attribute
                dynamic.append(f"module '{located}' defines __getattr__")
            continue
        cls = _resolve_owner_chain(tree, chain)
        if cls is None:
            continue
        if _defines(cls.body, member):
            return True, None
        if (line := _instance_attribute_line(cls, member)) is not None:
            return True, (
                f"'{member}' is an attribute assigned by a method of '{'.'.join(chain)}' "
                f"('{located}':{line})."
            )
        if member in _OBJECT_MEMBERS:
            return True, (
                f"'{member}' is not defined on '{'.'.join(chain)}' in '{located}'; every class "
                "inherits it from `object` (implicit_root, valid=true)."
            )
        if hook := _dynamic_attribute_hook(cls):
            dynamic.append(f"'{'.'.join(chain)}' defines {hook}")
            continue
        bases = _explicit_bases(cls)
        if bases:
            inherited.append(f"{'.'.join(chain)}({', '.join(bases)})")
    owner = ".".join(owners)
    if dynamic:
        return None, (
            f"'{member}' is not defined on '{owner}' in '{located}', but "
            f"{'; '.join(dynamic)}, so any attribute name may resolve at runtime "
            "(dynamic_attribute, valid=null)."
        )
    if inherited:
        return None, (
            f"'{member}' is not defined on '{owner}' in '{located}', but it may be "
            f"inherited from {'; '.join(inherited)}; bases were not checked "
            "(valid=null)."
        )
    return False, (
        f"No class '{owner}' in '{located}' defines '{member}' (the owner class is "
        "absent, or the member is defined elsewhere -- another class or module level)."
    )


def _inheriting_owner_classes(tree: ast.Module, located: str, qualname: str) -> list[ast.ClassDef]:
    """The owner classes ``_owner_verdict`` resolved that lack the member but have bases."""
    *owners, member = qualname.split(".")
    module = _module_parts(located)
    if _class_in(tree.body, owners[0]) is not None:
        chains = [owners]
    else:
        chains = [owners[k:] for k in range(1, len(owners) + 1) if module[-k:] == owners[:k]]
    out: list[ast.ClassDef] = []
    for chain in chains:
        cls = _resolve_owner_chain(tree, chain) if chain else None
        if cls is not None and not _defines(cls.body, member) and _explicit_bases(cls):
            out.append(cls)
    return out


_COMPOUND_STATEMENTS = (
    ast.If, ast.Try, ast.TryStar, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While,
    ast.Match,
)  # fmt: skip
_Binding = tuple[str, Any]


def _stmt_binding(node: ast.stmt, name: str) -> _Binding | None:
    """How the module-level statement *node* binds *name*, if it does.

    ``("class", ClassDef)``, ``("from", (module, real))``, ``("star", module)`` (it MAY
    bind it), or ``("other", why)`` (a def, an assignment, a plain import, ``del``, or
    any binding inside a conditional / loop / with / try block).
    """
    if isinstance(node, ast.ClassDef):
        return ("class", node) if node.name == name else None
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return ("other", f"a `def {name}`") if node.name == name else None
    if isinstance(node, ast.ImportFrom):
        module = "." * node.level + (node.module or "")
        out: _Binding | None = None
        for alias in node.names:
            if alias.name == "*":
                out = ("star", module)
            elif (alias.asname or alias.name) == name:
                out = ("from", (module, alias.name))
        return out
    if isinstance(node, ast.Import):
        for alias in node.names:
            if (alias.asname or alias.name.split(".", 1)[0]) == name:
                return ("other", f"`import {alias.name}` (a module)")
        return None
    if isinstance(node, _COMPOUND_STATEMENTS):
        for sub in ast.walk(node):
            if sub is node or not isinstance(sub, ast.stmt | ast.Name | ast.alias):
                continue
            hit = (
                isinstance(sub, ast.Name) and sub.id == name and not isinstance(sub.ctx, ast.Load)
            ) or (
                isinstance(sub, ast.alias) and (sub.name == "*" or (sub.asname or sub.name) == name)
            )
            if not hit and isinstance(sub, ast.stmt):
                hit = _stmt_binding(sub, name) is not None and not isinstance(
                    sub, _COMPOUND_STATEMENTS
                )
            if hit:
                return ("other", "a binding inside a conditional / loop / with / try block")
        return None
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == name and not isinstance(sub.ctx, ast.Load):
            return ("other", "an assignment or `del`")
    return None


def _same_class(node: ast.AST, cls: ast.ClassDef) -> bool:
    """*node* is *cls* (or the same class statement from another parse of the file)."""
    return node is cls or (
        isinstance(node, ast.ClassDef)
        and (node.name, node.lineno, node.col_offset) == (cls.name, cls.lineno, cls.col_offset)
    )


def _last_binding(tree: ast.Module, name: str, child: ast.ClassDef | None) -> _Binding | None:
    """The binding of module-level *name* in effect where *child*'s bases are evaluated
    (*child* ``None``: at the end of the module) -- the LAST one in statement order.

    A star import after it, or a binding in the statement that encloses *child*
    (not *child* itself), makes it ``("other", why)``: not decidable here.
    """
    last: _Binding | None = None
    for node in tree.body:
        if child is not None and any(_same_class(n, child) for n in ast.walk(node)):
            if not _same_class(node, child) and _stmt_binding(node, name) is not None:
                return ("other", "a binding in the block that encloses the class")
            break
        found = _stmt_binding(node, name)
        if found is None:
            continue
        if found[0] == "star":
            if last is not None:
                last = ("other", f"a later `from {found[1]} import *` may rebind it")
            continue
        last = found
    return last


def _star_modules(tree: ast.Module) -> list[str]:
    return [
        "." * node.level + (node.module or "")
        for node in _scope_statements(tree.body)
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names)
    ]


async def _python_module_file(ops: WalkOps, importer: str, module: str) -> str:
    candidates = python_module_candidates(importer, module) or []
    why = f"'{module}' (imported in {importer}) is not a module of this workspace (external)"
    if not candidates:
        raise UnresolvedHopError(why)
    try:
        present = await ops.existing(candidates)
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError, ValueError) as e:
        raise UnresolvedHopError(f"the module lookup for '{module}' could not finish ({e})") from e
    hit = next((c for c in candidates if c in present), None)
    if hit is None:
        raise UnresolvedHopError(why)
    return hit


async def _python_tree(ops: WalkOps, path: str) -> ast.Module:
    try:
        source = await ops.read(path)
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError, ValueError) as e:
        raise UnresolvedHopError(f"'{path}' could not be read completely ({e})") from e
    if source is None:
        raise UnresolvedHopError(f"'{path}' does not exist")
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError) as e:
        raise UnresolvedHopError(f"'{path}' does not parse") from e


_PyClass = tuple[str, ast.ClassDef]


async def _python_find_class(
    ops: WalkOps, path: str, name: str, hops: int = 0, visited: set | None = None
) -> tuple[bool | None, str, _PyClass | None]:
    """Is ``class <name>`` defined in *path*, or re-exported by it (``from M import``)?

    Re-exports (named, aliased, ``*``) are followed at most ``MAX_REEXPORT_HOPS``
    hops. ``False`` only when every followed module is in the workspace, parses,
    and none defines the class; an external or unreadable hop is ``None``.
    """
    visited = visited if visited is not None else set()
    if (path, name) in visited:
        return None, f"re-export cycle through '{path}'", None
    visited.add((path, name))
    try:
        tree = await _python_tree(ops, path)
    except UnresolvedHopError as e:
        return None, str(e), None
    last = _last_binding(tree, name, None)
    binding: tuple[str, str] | None = None
    if last is not None and last[0] == "class":
        cls = last[1]
        return True, f"'{name}' is defined in '{path}' (line {cls.lineno})", (path, cls)
    if last is not None and last[0] == "from":
        binding = last[1]
    elif last is not None:
        return None, f"'{name}' is bound in '{path}', but not by a `class` statement", None
    stars = _star_modules(tree) if binding is None else []
    if (binding or stars) and hops >= MAX_REEXPORT_HOPS:
        return (
            None,
            f"the re-export chain of '{name}' is longer than {MAX_REEXPORT_HOPS} hops",
            None,
        )
    if binding is not None:
        module, real = binding
        try:
            target = await _python_module_file(ops, path, module)
        except UnresolvedHopError as e:
            return None, str(e), None
        verdict, why, found = await _python_find_class(ops, target, real, hops + 1, visited)
        if verdict is True:
            return True, f"'{name}' is re-exported via '{path}'; {why}", found
        return verdict, why, None
    outcomes: list[tuple[bool | None, str, _PyClass | None]] = []
    for module in stars:
        try:
            target = await _python_module_file(ops, path, module)
        except UnresolvedHopError as e:
            outcomes.append((None, str(e), None))
            continue
        outcomes.append(await _python_find_class(ops, target, name, hops + 1, visited))
    for verdict, why, found in outcomes:
        if verdict is True:
            return True, f"'{name}' is re-exported via '{path}' (`import *`); {why}", found
    undecided = [why for verdict, why, _ in outcomes if verdict is None]
    if undecided:
        return None, "; ".join(undecided), None
    return False, f"no class '{name}' is defined or re-exported in '{path}'", None


async def _python_walk_bases(
    ops: WalkOps, located: str, tree: ast.Module, classes: list[ast.ClassDef], member: str
) -> tuple[bool | None, str]:
    """Follow the bases of *classes* (in *located*) for *member*, ``MAX_BASE_HOPS`` deep.

    A base is located by name: a class of the same module, else the module a
    ``from M import Base`` names (in-repo modules only; re-exports followed). A
    dotted base (``models.Model``), a builtin (``Exception``), a star-imported
    or external one is unresolved -> null. Decorators and metaclass keywords
    can inject members, so they block a false.
    """
    queue: list[tuple[str, str, ast.Module, int, ast.ClassDef]] = [
        (b, located, tree, 1, cls) for cls in classes for b in _explicit_bases(cls)
    ]
    visited = {(located, cls.lineno) for cls in classes}
    blockers = [
        f"'{cls.name}' has a decorator or metaclass that may supply it"
        for cls in classes
        if cls.decorator_list or cls.keywords
    ] + [
        f"'{cls.name}' defines {hook}, so any attribute name may resolve (dynamic_attribute)"
        for cls in classes
        if (hook := _dynamic_attribute_hook(cls))
    ]
    unresolved: list[str] = []
    searched: list[str] = []
    while queue:
        base, path, module_tree, hop, child_cls = queue.pop(0)
        child = child_cls.name
        if hop > MAX_BASE_HOPS:
            unresolved.append(f"'{base}' is more than {MAX_BASE_HOPS} inheritance hops away")
            continue
        name = re.sub(r"\[.*$", "", base).strip()
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            unresolved.append(f"base '{base}' of '{child}' is qualified or an expression")
            continue
        found: _PyClass | None = None
        # The binding in effect where the class statement evaluates its bases.
        last = _last_binding(module_tree, name, child_cls)
        if last is not None and last[0] == "class":
            found = (path, last[1])
        elif last is not None and last[0] == "from":
            binding = last[1]
            try:
                target = await _python_module_file(ops, path, binding[0])
            except UnresolvedHopError as e:
                unresolved.append(f"base '{name}' of '{child}': {e}")
                continue
            verdict, why, found = await _python_find_class(ops, target, binding[1])
            if verdict is not True or found is None:
                unresolved.append(f"base '{name}' of '{child}': {why}")
                continue
        elif last is not None:
            unresolved.append(f"base '{name}' of '{child}' is bound in '{path}' by {last[1]}")
            continue
        else:
            unresolved.append(
                f"base '{name}' of '{child}' is not defined or imported by name in '{path}' "
                "(a builtin, a star import or an external class)"
            )
            continue
        base_path, cls = found
        if (base_path, cls.lineno) in visited:
            continue
        visited.add((base_path, cls.lineno))
        searched.append(f"{cls.name} ({base_path})")
        if _defines(cls.body, member):
            return True, (f"'{member}' is inherited from {cls.name} ({base_path}:{cls.lineno}).")
        if (line := _instance_attribute_line(cls, member)) is not None:
            return True, (
                f"'{member}' is an attribute assigned by a method of base {cls.name} "
                f"({base_path}:{line})."
            )
        if hook := _dynamic_attribute_hook(cls):
            blockers.append(
                f"base '{cls.name}' defines {hook}, so any attribute name may resolve "
                "(dynamic_attribute)"
            )
        if cls.decorator_list or cls.keywords:
            blockers.append(f"'{cls.name}' has a decorator or metaclass that may supply it")
        if _explicit_bases(cls):
            base_tree = (
                module_tree if base_path == path else await _python_tree_or_none(ops, base_path)
            )
            if base_tree is None:
                unresolved.append(f"'{base_path}' could not be re-read")
                continue
            queue += [(b, base_path, base_tree, hop + 1, cls) for b in _explicit_bases(cls)]
    owners = [cls.name for cls in classes]
    bases = f"its bases {searched}" if searched else "its bases"
    if unresolved:
        return None, (
            f"'{member}' is not defined on {owners} in '{located}' nor on {bases}, but it may "
            f"be inherited from a base that was not resolved: {'; '.join(unresolved)} "
            "(valid=null)."
        )
    if blockers:
        return None, (
            f"'{member}' is not defined on {owners} in '{located}' nor on {bases}, but "
            f"{'; '.join(dict.fromkeys(blockers))} (valid=null)."
        )
    return False, (
        f"'{member}' is not defined on {owners} in '{located}' nor on {bases}; every base "
        "was resolved in the workspace."
    )


async def _python_tree_or_none(ops: WalkOps, path: str) -> ast.Module | None:
    try:
        return await _python_tree(ops, path)
    except UnresolvedHopError:
        return None


async def _owner_verdict_walked(
    container_name: str, source: str, located: str, qualname: str, tenant_id: str
) -> tuple[bool | None, str | None, set[int]]:
    """``_owner_verdict``, with an "inherited" null decided by following the bases.

    Returns ``(verdict, note, owner class lines a walked False covered)``.
    """
    verdict, why = _owner_verdict(source, located, qualname)
    if verdict is not None:
        return verdict, why, set()
    try:
        tree = ast.parse(source)
    except SyntaxError, ValueError:
        return verdict, why, set()
    classes = _inheriting_owner_classes(tree, located, qualname)
    if not classes:
        return verdict, why, set()
    member = qualname.rsplit(".", 1)[-1]
    walked, note = await _python_walk_bases(
        _walk_ops(container_name, tenant_id), located, tree, classes, member
    )
    return walked, note, ({c.lineno for c in classes} if walked is False else set())


async def _check_owner_in_file(
    container_name: str, located: str, qualname: str, tenant_id: str
) -> tuple[bool | None, str | None]:
    """Read *located* (checked) and require its owner class to define the member.

    The member WAS found in this file by grep, so a file the read reports absent
    (a race) or cannot read completely is "not computed", never a verdict.
    """
    try:
        source = await read_workspace_file_checked(container_name, located, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        return None, (
            f"'{located}' could not be read completely ({e}), so whether '{qualname}' is "
            "defined on its owner class there was NOT computed (valid=null)."
        )
    if source is None:
        return None, (
            f"'{located}' could not be read back, so whether '{qualname}' is defined on its "
            "owner class there was NOT computed (valid=null)."
        )
    verdict, why, _ = await _owner_verdict_walked(
        container_name, source, located, qualname, tenant_id
    )
    return verdict, why


_OWNER_CHECK_FILE_CAP = 10


async def _check_owner_in_found_files(
    container_name: str, found_in: list[str], qualname: str, tenant_id: str
) -> tuple[bool | None, str]:
    """No ``file_path``: run the owner check on every file the member was found in.

    True if ANY file's owner class defines the member. Otherwise null if any file
    could not be checked (unreadable, unparseable, possibly inherited) or the
    file list was capped; false only when every file was checked and refuted.
    """
    checked = found_in[:_OWNER_CHECK_FILE_CAP]
    capped = len(found_in) > _OWNER_CHECK_FILE_CAP
    undecided: list[str] = []
    for path in checked:
        verdict, _ = await _check_owner_in_file(container_name, path, qualname, tenant_id)
        if verdict is True:
            return True, (
                f"Dotted name: the owner of '{qualname}' was checked; it is defined on "
                f"its owner class in '{path}'."
            )
        if verdict is None:
            undecided.append(path)
    if undecided or capped or not checked:
        reasons = []
        if undecided:
            reasons.append(
                f"{undecided} could not be decided (unreadable, unparseable, or the "
                "member may be inherited from a base class that was not checked)"
            )
        if capped:
            reasons.append(
                f"only the first {_OWNER_CHECK_FILE_CAP} of {len(found_in)} files "
                "containing the member were checked"
            )
        if not checked:
            reasons.append("no file location was reported for the definition")
        return None, (
            f"Dotted name: no checked file defines '{qualname}' on its owner class, but "
            + "; ".join(reasons)
            + ", so the owner check was NOT computed (valid=null)."
        )
    return False, (
        f"Dotted name: '{qualname.rsplit('.', 1)[-1]}' is defined in {checked}, but in "
        f"none of them on the owner '{qualname.rsplit('.', 1)[0]}'."
    )


async def _check_located_owner_without_member(
    container_name: str, located: str, qualname: str, tenant_id: str, note: str | None
) -> tuple[bool | None, str | None]:
    """*located* has no ``def <member>``: does its owner class still define it?

    The grep sees only ``def``; an assignment in the class body, or a base class
    the member may be inherited from, is decided by ``_owner_verdict`` (true /
    null). A file confirmed absent keeps the grep's false.
    """
    try:
        source = await read_workspace_file_checked(container_name, located, tenant_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        return None, (
            f"'{located}' could not be read completely ({e}), so whether '{qualname}' is "
            "defined on its owner class there was NOT computed (valid=null)."
        )
    if source is None:
        return False, note
    verdict, why, walked = await _owner_verdict_walked(
        container_name, source, located, qualname, tenant_id
    )
    if verdict is not False:
        return verdict, why
    return _owner_fallback_verdict(source, located, qualname, (verdict, why), walked)


def _owner_fallback_verdict(
    source: str,
    located: str,
    qualname: str,
    computed: tuple[bool | None, str | None] | None = None,
    walked: set[int] | frozenset[int] = frozenset(),
) -> tuple[bool | None, str | None]:
    """``_owner_verdict``, except that a false is null when ANY class named like the
    owner in *source* (nested ones included) has a base, a decorator or a
    metaclass keyword -- each can supply the member -- or (nested, so not the
    owner the name resolves to) defines it itself. *computed*: the verdict
    already decided (by ``_owner_verdict_walked``); *walked*: the lines of owner
    classes whose bases that walk resolved completely (their bases do not block)."""
    verdict, why = computed if computed is not None else _owner_verdict(source, located, qualname)
    if verdict is not False:
        return verdict, why
    *owners, member = qualname.split(".")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef) or node.name != owners[-1]:
            continue
        if node.lineno in walked:
            continue
        if _defines(node.body, member) or _instance_attribute_line(node, member) is not None:
            return None, (
                f"A class '{node.name}' in '{located}' (line {node.lineno}) defines "
                f"'{member}', but it is not the class '{'.'.join(owners)}' resolves to "
                "(nested or qualified differently), so valid=null."
            )
        if (
            _explicit_bases(node)
            or node.decorator_list
            or node.keywords
            or _dynamic_attribute_hook(node)
        ):
            return None, (
                f"'{member}' is not defined on the class '{node.name}' in '{located}' "
                f"(line {node.lineno}), but that class has a base, decorator, metaclass or "
                "__getattr__ that may supply it (inherited or injected); bases were not checked "
                "(valid=null)."
            )
    return verdict, why


async def _check_owner_class_without_member(
    container_name: str, qualname: str, tenant_id: str, note: str | None
) -> tuple[bool | None, str | None]:
    """No ``def <member>`` anywhere: look up the OWNER class of ``Owner.member``.

    Owner class defined nowhere (Python): the grep's false stands. Otherwise the
    owner check runs on every file defining the owner: true if one defines the
    member (an assignment), null if one has bases the member may be inherited
    from or could not be checked, false only when every owner class was checked,
    has no explicit base and does not define the member.
    """
    owner = qualname.split(".")[-2]
    try:
        matches = await grep_definition(container_name, owner, tenant_id, kind="class")
    except ValueError:
        return False, note  # not an identifier: no Python class can be named so
    except ContainerNotResolvableError as e:
        raise _workspace_error(e) from e
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError) as e:
        return None, (
            f"No Python 'def {qualname.rsplit('.', 1)[-1]}' was found, but the search for "
            f"its owner class '{owner}' could not finish ({e}), so valid=null."
        )
    owner_files = sorted({m["file"] for m in matches if m.get("file")})
    if not owner_files:
        return False, note
    checked = owner_files[:_OWNER_CHECK_FILE_CAP]
    undecided: list[str] = []
    for path in checked:
        verdict, why = await _check_located_owner_without_member(
            container_name, path, qualname, tenant_id, None
        )
        if verdict is True:
            # The located check says HOW (assignment, instance attribute, inherited,
            # or `object`); a bare "defined on its owner class" was wrong for the last two.
            return True, why or (
                f"Dotted name: '{qualname}' is defined on its owner class in '{path}' "
                "(not as a `def`)."
            )
        if verdict is None:
            undecided.append(why or path)
        elif why is None:  # grep found the class there, the read says absent: a race
            undecided.append(f"'{path}' could not be read back")
    if undecided or len(owner_files) > len(checked):
        reasons = list(undecided)
        if len(owner_files) > len(checked):
            reasons.append(
                f"only the first {len(checked)} of {len(owner_files)} files defining "
                f"'{owner}' were checked"
            )
        return None, (
            f"The owner class '{owner}' is defined in {owner_files}, but no `def` of "
            f"'{qualname.rsplit('.', 1)[-1]}' is on it, and it could not be decided: "
            f"{'; '.join(reasons)} (valid=null)."
        )
    return False, (
        f"The owner class '{owner}' is defined in {checked}, and none of them defines "
        f"'{qualname.rsplit('.', 1)[-1]}', on the class or on any base (every base was "
        "resolved in the workspace)."
    )


@router.post(
    "/function-reference",
    response_model=ValidateFunctionReferenceResponse,
    summary="Validate Function Reference",
    description="Check if a function or method exists in the caller's workspace.",
    operation_id="validate_function_reference",
)
async def validate_function_reference(
    request: ValidateFunctionReferenceRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate Function Reference - Check if a function exists in the caller's workspace."""
    located = _located_file(request.file_path, request.codebase_path)
    if located is not None and not _is_python_file(located):
        return ValidateFunctionReferenceResponse(
            **await _located_symbols_answer(
                request.function_name, "function", request.container_name, located, tenant_id
            ),
            function_name=request.function_name,
        )
    valid, details, resolution, note = await _validate_symbol_reference(
        name=request.function_name,
        kind="function",
        container_name=request.container_name,
        codebase_path=request.codebase_path,
        tenant_id=tenant_id,
    )
    located_valid, found_in, in_file, located_note = _apply_location(valid, details, located)
    if "." in request.function_name:
        if located is not None and located_valid is True:
            located_valid, located_note = await _check_owner_in_file(
                request.container_name or "", located, request.function_name, tenant_id
            )
        elif located is not None and located_valid is False:
            located_valid, located_note = await _check_located_owner_without_member(
                request.container_name or "",
                located,
                request.function_name,
                tenant_id,
                located_note,
            )
        elif located is None and valid and resolution == RESOLUTION_CONTAINER:
            located_valid, note = await _check_owner_in_found_files(
                request.container_name or "", found_in, request.function_name, tenant_id
            )
            if located_valid is False:
                # The member is defined, but not on the owner in those files: the
                # owner class may live elsewhere and inherit it (bases followed).
                located_valid, note = await _check_owner_class_without_member(
                    request.container_name or "", request.function_name, tenant_id, note
                )
        elif located is None and not found_in and resolution == RESOLUTION_CONTAINER:
            located_valid, note = await _check_owner_class_without_member(
                request.container_name or "", request.function_name, tenant_id, note
            )
        elif located is None and valid:
            note = _OWNER_NOT_CHECKED_NOTE
        if located_valid is False and resolution == RESOLUTION_CONTAINER:
            patched = await _python_monkeypatch_note(
                request.container_name or "", request.function_name, tenant_id
            )
            if patched is not None:
                located_valid, located_note = None, patched
    note = located_note or note
    if located is None and resolution == RESOLUTION_CONTAINER and located_valid is not True:
        located_valid, note, definitions = _combine_with_other_languages(
            located_valid,
            note,
            await _discover_in_other_languages(
                request.container_name or "", request.function_name, "function", tenant_id
            ),
        )
        if definitions:
            details = _symbol_details(definitions)
            found_in = sorted({d["file"] for d in definitions})
    return ValidateFunctionReferenceResponse(
        valid=located_valid,
        function_name=request.function_name,
        details=details,
        resolution=resolution,
        note=note,
        excluded_dirs=_excluded_dirs(resolution),
        found_in=found_in,
        in_file=in_file,
        searched_languages=_searched_languages(resolution, located),
    )


@router.post(
    "/class-reference",
    response_model=ValidateClassReferenceResponse,
    summary="Validate Class Reference",
    description="Check if a class exists in the caller's workspace.",
    operation_id="validate_class_reference",
)
async def validate_class_reference(
    request: ValidateClassReferenceRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate Class Reference - Check if a class exists in the caller's workspace."""
    located = _located_file(request.file_path, request.codebase_path)
    if located is not None and not _is_python_file(located):
        return ValidateClassReferenceResponse(
            **await _located_symbols_answer(
                request.class_name, "class", request.container_name, located, tenant_id
            ),
            class_name=request.class_name,
        )
    if is_dotted(request.class_name):
        response = await _validate_dotted_class_reference(request, tenant_id)
        valid, found_in, in_file, located_note = _apply_location(
            response.valid, response.details, located
        )
        update: dict[str, Any] = {
            "valid": valid,
            "found_in": found_in,
            "in_file": in_file,
            "note": located_note or response.note,
            "searched_languages": _searched_languages(response.resolution, located),
        }
        if located is None and response.resolution == RESOLUTION_CONTAINER and valid is not True:
            update["valid"], update["note"], definitions = _combine_with_other_languages(
                valid,
                update["note"],
                await _discover_in_other_languages(
                    request.container_name or "", request.class_name, "class", tenant_id
                ),
            )
            if definitions:
                update["details"] = _symbol_details(definitions)
                update["found_in"] = sorted({d["file"] for d in definitions})
        return response.model_copy(update=update)
    valid, details, resolution, note = await _validate_symbol_reference(
        name=request.class_name,
        kind="class",
        container_name=request.container_name,
        codebase_path=request.codebase_path,
        tenant_id=tenant_id,
    )
    located_valid, found_in, in_file, located_note = _apply_location(valid, details, located)
    note = located_note or note
    if located is None and resolution == RESOLUTION_CONTAINER and located_valid is not True:
        located_valid, note, definitions = _combine_with_other_languages(
            located_valid,
            note,
            await _discover_in_other_languages(
                request.container_name or "", request.class_name, "class", tenant_id
            ),
        )
        if definitions:
            details = _symbol_details(definitions)
            found_in = sorted({d["file"] for d in definitions})
    return ValidateClassReferenceResponse(
        valid=located_valid,
        class_name=request.class_name,
        details=details,
        resolution=resolution,
        note=note,
        excluded_dirs=_excluded_dirs(resolution),
        found_in=found_in,
        in_file=in_file,
        searched_languages=_searched_languages(resolution, located),
    )


def _searched_languages(resolution: str, located: str | None) -> list[str]:
    """What this answer actually searched (see the response field description)."""
    if resolution == RESOLUTION_EXPLICIT:
        return list(PYTHON_ONLY)
    if located is not None:
        return [symbol_language(located) or "python"]
    return list(SEARCHED_LANGUAGES)


async def _located_symbols_answer(
    name: str, kind: str, container_name: str | None, located: str, tenant_id: str
) -> dict[str, Any]:
    """Response fields for a reference located in a NON-``.py`` file.

    A supported extension is answered from coding-analysis' symbol extraction
    of that one file (``services.symbol_index``); any other extension is not
    searchable (``language_supported=false``, ``valid=null``).
    """
    language = symbol_language(located)
    if language is None:
        return {
            "valid": None,
            "resolution": RESOLUTION_CONTAINER,
            "note": _NOT_SEARCHABLE_NOTE,
            "language_supported": False,
        }
    if not container_name:
        raise _no_codebase_input(f"{kind} reference")
    try:
        validate_relative_path(located)
        for segment in name.split("."):
            validate_identifier(segment, kind)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    match, _ = await _symbols_in_file(container_name, located, name, kind, tenant_id)
    return {
        "valid": match.verdict,
        "in_file": match.verdict,
        "found_in": sorted({d["file"] for d in match.definitions}),
        "details": _symbol_details(match.definitions),
        "note": match.note,
        "resolution": RESOLUTION_CONTAINER,
        "excluded_dirs": _excluded_dirs(RESOLUTION_CONTAINER),
        "language_supported": True,
        "searched_languages": [language],
    }


async def _python_reexported_class(
    container_name: str, module_files: list[str], class_name: str, tenant_id: str
) -> tuple[bool | None, str | None, list[dict[str, Any]]]:
    """``pkg.Cls`` whose package ``__init__.py`` re-exports ``Cls`` (``from .m import Cls``).

    Returns ``(valid, note, definitions)``; ``note`` is ``None`` when no module
    file is a package ``__init__`` (the plain "not defined there" false stands).
    """
    inits = [f for f in module_files if posixpath.basename(f) == "__init__.py"]
    if not inits:
        return False, None, []
    ops = _walk_ops(container_name, tenant_id)
    undecided: list[str] = []
    for init in inits:
        verdict, why, found = await _python_find_class(ops, init, class_name)
        if verdict is True and found is not None:
            path, cls = found
            definition = {
                "file": path,
                "line": cls.lineno,
                "source": f"class {cls.name}",
            }
            return True, f"'{class_name}' is re-exported via '{init}'. {why}.", [definition]
        if verdict is None:
            undecided.append(why)
    if undecided:
        return (
            None,
            (
                f"No Python class '{class_name}' is defined in {inits}, and its re-exports could "
                f"not be followed to the end: {'; '.join(undecided)} (valid=null)."
            ),
            [],
        )
    return False, None, []


async def _validate_dotted_class_reference(
    request: ValidateClassReferenceRequest, tenant_id: str
) -> ValidateClassReferenceResponse:
    """``module.Class``: resolve the module in-repo, then look ONLY in its file.

    ⛔ The last segment is never searched repo-wide. ``pathlib.Path`` must not be
    satisfied by an unrelated ``class Path`` elsewhere in the repo -- that is a
    false pass. A module that is not in the repo is reported ``external`` with
    ``valid=null`` (not computed), never ``valid=false`` ("it is not there").
    """
    try:
        module, class_name = split_dotted_class_name(request.class_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    note: str | None = None
    reexport_note: str | None = None
    if request.codebase_path:
        path = _validate_explicit_path(request.codebase_path)
        try:
            validator = ReferenceValidator(path)
            valid, module_resolution, details = validator.validate_dotted_class_reference(
                request.class_name
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error validating class reference: {e}"
            ) from e
        resolution = RESOLUTION_EXPLICIT
    else:
        if not request.container_name:
            raise _no_codebase_input("class reference")
        try:
            present = await find_module_files(
                request.container_name, all_candidates(module), tenant_id
            )
            module_files = sorted(p for p in module_file_candidates(module) if p in present)
            details: dict[str, Any] = {"module_files": module_files}
            if module_files:
                module_resolution = MODULE_RESOLUTION_IN_REPO
                matches = await grep_class_in_files(
                    request.container_name, class_name, module_files, tenant_id
                )
                valid = bool(matches)
                if not matches:
                    valid, reexport_note, matches = await _python_reexported_class(
                        request.container_name, module_files, class_name, tenant_id
                    )
                if matches:
                    details.update(
                        {
                            "file": matches[0]["file"],
                            "line": matches[0]["line"],
                            "source": matches[0]["source"],
                            "definition_count": len(matches),
                            "definitions": matches,
                        }
                    )
            elif any(p in present for p in top_level_package_candidates(module)):
                module_resolution, valid = MODULE_RESOLUTION_IN_REPO, False
            else:
                module_resolution, valid = MODULE_RESOLUTION_EXTERNAL, None
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except (
            ContainerNotResolvableError,
            WorkspaceBackendUnavailableError,
            WorkspaceSearchIncompleteError,
        ) as e:
            raise _workspace_error(e) from e
        resolution = RESOLUTION_CONTAINER

    if module_resolution == MODULE_RESOLUTION_EXTERNAL:
        note = (
            f"Module '{module}' is not in this repository (no module file under "
            f"search roots {list(MODULE_SEARCH_ROOTS)}); it is stdlib or a dependency. "
            f"Existence of '{class_name}' was NOT computed (valid=null). The class was "
            f"deliberately not searched repo-wide by its last segment."
        )
    elif reexport_note is not None:
        note = reexport_note
    elif valid is False:
        note = (
            f"Module '{module}' resolves into this repository, and no Python class "
            f"'{class_name}' is defined in its module file(s) "
            f"{details.get('module_files') or '(module file not found)'}."
        )

    return ValidateClassReferenceResponse(
        valid=valid,
        class_name=request.class_name,
        details=details,
        resolution=resolution,
        note=note,
        excluded_dirs=_excluded_dirs(resolution),
        module_resolution=module_resolution,
        module=module,
        name=class_name,
        module_search_roots=list(MODULE_SEARCH_ROOTS),
    )


@router.post(
    "/code-snippet",
    response_model=ValidateCodeSnippetResponse,
    summary="Validate Code Snippet",
    description="Check if code references real entities in an explicitly-supplied codebase.",
    operation_id="validate_code_snippet",
)
async def validate_code_snippet(
    request: ValidateCodeSnippetRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate Code Snippet - Check if code references real entities in a codebase.

    Two modes, never mixed:

    - ``codebase_path`` (explicit): ``ReferenceValidator`` indexes that local
      directory. Unchanged.
    - ``container_name``: DELEGATED to coding-containers'
      ``POST /api/validation/multi-line-code-snippet`` (the same backend the
      proxy route forwards to), which parses the snippet and resolves every
      import INSIDE the caller's workspace (coding-containers#171). The gateway
      does not build a second resolver. ``valid`` is true only when that
      service's ``verdict`` is ``"valid"``; a missing verdict (an older
      coding-containers) is ``not_computed``, never a pass. Execution is not
      requested.

    This endpoint used to REFUSE the container mode with a 400, because the
    gateway cannot build a whole-codebase index from a remote workspace; the
    delegation answers the question in the workspace instead.
    """
    if not request.code_snippet or not isinstance(request.code_snippet, str):
        raise HTTPException(
            status_code=400, detail="Invalid code snippet: must be a non-empty string"
        )

    max_snippet_length = 100000
    if len(request.code_snippet) > max_snippet_length:
        raise HTTPException(
            status_code=400,
            detail=f"Code snippet too large: maximum {max_snippet_length} characters",
        )

    try:
        request.code_snippet.encode("utf-8").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise HTTPException(
            status_code=400, detail="Invalid code snippet: contains malformed Unicode"
        ) from exc

    if not request.codebase_path:
        if request.container_name:
            return await _validate_snippet_in_container(
                request.code_snippet, request.container_name, tenant_id
            )
        raise HTTPException(
            status_code=400, detail="Must provide either 'codebase_path' or 'container_name'"
        )

    codebase_path = _validate_explicit_path(request.codebase_path)

    try:
        validator = ReferenceValidator(codebase_path)
        result = validator.validate_code_snippet(request.code_snippet)
    except InvalidCodebasePathError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error validating code snippet: {e}") from e

    return ValidateCodeSnippetResponse(
        **result,
        resolution=RESOLUTION_EXPLICIT,
        verdict="valid" if result.get("valid") else "invalid",
    )


_VERDICTS = ("valid", "invalid", "not_computed")


def _upstream_verdict(body: dict[str, Any]) -> tuple[str, str | None]:
    """``(verdict, not_computed_reason)`` from a multi-line-code-snippet body.

    Anything other than an explicit ``valid``/``invalid``/``not_computed`` --
    including a body from a coding-containers that predates import resolution
    and carries only ``valid`` -- is ``not_computed``. A bare upstream ``valid``
    never becomes a pass here: before #171 it meant "parses", not "imports exist".
    """
    static = body.get("static_analysis") or {}
    verdict = body.get("verdict")
    if verdict not in _VERDICTS:
        return "not_computed", (
            "coding-containers returned no verdict (it predates workspace import "
            "resolution), so imports were NOT checked."
        )
    if verdict == "not_computed":
        return verdict, static.get("not_computed_reason") or "imports were not all checked"
    return verdict, None


async def _validate_snippet_in_container(
    code: str, container_name: str, tenant_id: str
) -> ValidateCodeSnippetResponse:
    """Container mode of ``/code-snippet``: delegate to coding-containers and map."""
    if len(code.strip()) < 3:
        raise HTTPException(
            status_code=400, detail="Invalid code snippet: too short to be meaningful"
        )
    try:
        body = await validate_snippet_in_workspace(container_name, code, tenant_id)
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        raise _workspace_error(e) from e

    verdict, reason = _upstream_verdict(body)
    static = body.get("static_analysis") or {}
    suggestions = body.get("suggestions") or []
    return ValidateCodeSnippetResponse(
        valid=verdict == "valid",
        verdict=verdict,
        issues=list(static.get("issues") or []),
        suggestion=suggestions[0] if suggestions else None,
        overall_score=float(static.get("overall_score") or 0.0),
        resolution=RESOLUTION_CONTAINER,
        imports=list(static.get("imports") or []),
        unresolved_imports=list(static.get("unresolved_imports") or []),
        import_resolution=static.get("import_resolution"),
        not_computed_reason=reason,
    )


# ---------------------------------------------------------------------------
# Consistency Check Endpoints
#
# All three ABSTAIN on `consistent` -- see "What the consistency checker
# actually measures" at the top of this module for the investigation and the
# arithmetic behind the abstention.
# ---------------------------------------------------------------------------


@router.post(
    "/function-consistency",
    response_model=CheckFunctionConsistencyResponse,
    summary="Check Function Consistency",
    description=(
        "Check whether a function's DESCRIPTION mentions what the code does. "
        "Does NOT verify that the implementation matches the description."
    ),
    operation_id="check_function_consistency",
)
async def check_function_description_consistency(
    request: CheckFunctionConsistencyRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Check Function Consistency - documentation completeness, with abstention."""
    try:
        checker = CodeConsistencyChecker()
        result = checker.check_function_description(
            request.function_code, request.description, request.context
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error checking function consistency: {e}"
        ) from e

    return CheckFunctionConsistencyResponse(
        **_consistency_payload(
            result, code=request.function_code, description=request.description, kind="function"
        )
    )


@router.post(
    "/class-consistency",
    response_model=CheckClassConsistencyResponse,
    summary="Check Class Consistency",
    description=(
        "Check whether a class's DESCRIPTION mentions what the code does. "
        "Does NOT verify that the implementation matches the description."
    ),
    operation_id="check_class_consistency",
)
async def check_class_description_consistency(
    request: CheckClassConsistencyRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Check Class Consistency - documentation completeness, with abstention."""
    try:
        checker = CodeConsistencyChecker()
        result = checker.check_class_description(
            request.class_code, request.description, request.context
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking class consistency: {e}") from e

    return CheckClassConsistencyResponse(
        **_consistency_payload(
            result, code=request.class_code, description=request.description, kind="class"
        )
    )


@router.post(
    "/module-consistency",
    response_model=CheckModuleConsistencyResponse,
    summary="Check Module Consistency",
    description="Validate naming/import consistency across multiple modules.",
    operation_id="check_module_consistency",
)
async def check_module_consistency(
    request: CheckModuleConsistencyRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Check Module Consistency - Validate consistency across multiple modules."""
    if len(request.modules) != len(request.module_paths):
        raise HTTPException(
            status_code=422,
            detail="modules and module_paths must have the same length",
        )

    try:
        checker = CodeConsistencyChecker()
        modules_dict = dict(zip(request.module_paths, request.modules))
        paths_dict = {path: path for path in request.module_paths}
        result = checker.check_module_consistency(modules_dict, paths_dict)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error checking module consistency: {e}"
        ) from e

    return CheckModuleConsistencyResponse(**_consistency_payload(result))


# ---------------------------------------------------------------------------
# Simple Code and Architecture Validation Endpoints
# ---------------------------------------------------------------------------

# Each type answers independently with status valid|invalid|not_computed; see
# ``validate_code``. Order is the response order.
_COMPUTABLE_VALIDATION_TYPES = ("syntax", "imports", "lint")


def _requested_types(validation_type: str) -> list[str]:
    """``"syntax"`` or a comma-separated list (``"syntax,imports,lint"``), de-duplicated."""
    return list(dict.fromkeys(t.strip() for t in validation_type.split(",") if t.strip()))


def _syntax_result(content: str) -> dict[str, Any]:
    try:
        ast.parse(content)
    except SyntaxError as exc:
        return {
            "status": "invalid",
            "errors": [
                {"type": "syntax", "message": exc.msg, "line": exc.lineno, "offset": exc.offset}
            ],
            "syntax_errors": [f"{exc.msg} (line {exc.lineno})"],
        }
    return {"status": "valid", "errors": [], "syntax_errors": []}


def _has_imports(content: str) -> bool:
    return any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(ast.parse(content)))


def _package_of(file_path: str) -> list[str]:
    """Dotted package of *file_path*'s directory, workspace-relative, as parts."""
    cleaned = file_path.strip()
    prefix = WORKSPACE_ROOT.rstrip("/") + "/"
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    return validate_relative_path(cleaned).split("/")[:-1]


def _absolute_module(package: list[str], node: ast.ImportFrom) -> str | None:
    """Absolute module for a relative ``ImportFrom``, or None when over the root.

    Level 1 is the file's own package (its directory -- the same for a module
    and for ``__init__.py``), each extra level one parent up. A leading ``src``
    segment is stripped because the resolver searches both the root and src/.
    """
    if node.level - 1 > len(package):
        return None
    base = package[: len(package) - (node.level - 1)]
    if base and base[0] == "src":
        base = base[1:]
    if not base:
        return None
    return ".".join(base + ([node.module] if node.module else []))


def _absolutize_relative_imports(content: str, file_path: str) -> str:
    """Rewrite every resolvable relative import in *content* to its absolute form.

    The resolver upstream sees only a snippet, never the file's location, so a
    relative import is unverifiable there and every package file came back
    ``not_computed``. Only the import statements' own source spans are
    replaced; every other byte stays, and a multi-line import is padded with
    blank lines so all line numbers after it are unchanged. An import that
    climbs above the workspace root is left relative (unverifiable, as before).
    """
    try:
        package = _package_of(file_path)
        tree = ast.parse(content)
    except SyntaxError, ValueError:
        return content
    nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level > 0]
    if not nodes:
        return content
    # ast counts \r\n, \r and \n as line ends -- nothing else (str.splitlines
    # would also split on \f, \v, ... and shift every line number after them).
    lines = re.findall(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+$", content)
    # Bottom-up (and right-to-left on one line) so earlier offsets stay valid.
    for node in sorted(nodes, key=lambda n: (n.lineno, n.col_offset), reverse=True):
        module = _absolute_module(package, node)
        if module is None or node.end_lineno is None or node.end_col_offset is None:
            continue
        new = ast.unparse(ast.ImportFrom(module=module, names=node.names, level=0))
        # col offsets are UTF-8 BYTE offsets.
        head = lines[node.lineno - 1].encode()[: node.col_offset].decode()
        tail = lines[node.end_lineno - 1].encode()[node.end_col_offset :].decode()
        span = node.end_lineno - node.lineno
        if span:
            # Whatever followed the statement (`; x = 1`, a comment) stays on the
            # same logical line; the freed lines become blank so numbering holds.
            body = tail.rstrip("\r\n")
            ending = tail[len(body) :] or "\n"
            replacement = [head + new + body + "\n"] + ["\n"] * (span - 1) + [ending]
        else:
            replacement = [head + new + tail]
        lines[node.lineno - 1 : node.end_lineno] = replacement
    return "".join(lines)


async def _imports_result(
    container_name: str, content: str, tenant_id: str, file_path: str
) -> dict[str, Any]:
    """Resolve the file's imports in the workspace via coding-containers (#171).

    Relative imports are first rewritten to absolute ones from *file_path*
    (``_absolutize_relative_imports``) -- the snippet resolver never sees where
    the file lives.
    """
    try:
        if not _has_imports(content):
            return {
                "status": "valid",
                "imports": [],
                "unresolved_imports": [],
                "note": "no imports",
            }
    except SyntaxError:
        return {
            "status": "not_computed",
            "reason": "syntax error: imports cannot be read from a file that does not parse",
        }
    forwarded = _absolutize_relative_imports(content, file_path)
    body = await validate_snippet_in_workspace(container_name, forwarded, tenant_id)
    verdict, reason = _upstream_verdict(body)
    static = body.get("static_analysis") or {}
    result: dict[str, Any] = {
        "status": verdict,
        "imports": list(static.get("imports") or []),
        "unresolved_imports": list(static.get("unresolved_imports") or []),
        "import_resolution": static.get("import_resolution"),
    }
    if reason:
        result["reason"] = reason
    return result


def _overall_status(statuses: list[str]) -> str:
    if "invalid" in statuses:
        return "invalid"
    if "not_computed" in statuses:
        return "not_computed"
    return "valid"


@router.post(
    "/code",
    response_model=CodeValidationResponse,
    summary="Validate Code",
    description=(
        "Validate a file in the caller's workspace. validation_type: 'syntax', 'imports', "
        "'lint', or a comma-separated list of them."
    ),
    operation_id="validate_code",
)
async def validate_code(
    request: CodeValidationRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate Code - check a real file in the caller's workspace.

    This used to build ``{"valid": True, "errors": []}`` as a literal and return
    it, so a nonexistent file in a nonexistent container was certified valid.
    It now reads the file out of the caller's container and checks it, or
    refuses.

    Types, each reported under ``validation_results.results[<type>]`` with a
    ``status`` of ``valid`` | ``invalid`` | ``not_computed`` (+ ``reason``):

    - ``syntax``  -- ``ast.parse`` of the file.
    - ``imports`` -- every import resolved IN THE WORKSPACE by coding-containers'
      multi-line-code-snippet resolver (the gateway does not build its own).
    - ``lint``    -- ``python3 -m pyflakes``, else ``ruff check --no-fix``; if
      neither is installed, ``not_computed`` with the reason. Never a silent pass.

    The top level carries the combined ``status`` (any invalid -> invalid, else
    any not_computed -> not_computed, else valid). ``valid`` is present only
    when the combined status is computed; ``not_computed: true`` otherwise.
    """
    if not request.container_name or not request.file_path or not request.validation_type:
        raise HTTPException(
            status_code=422,
            detail="Missing required fields: container_name, file_path, and validation_type",
        )

    requested = _requested_types(request.validation_type)
    unsupported = [t for t in requested if t not in _COMPUTABLE_VALIDATION_TYPES]
    if unsupported or not requested:
        raise HTTPException(
            status_code=501,
            detail=(
                f"validation_type '{request.validation_type}' is not computed by this "
                f"service. Supported: {list(_COMPUTABLE_VALIDATION_TYPES)} (or a "
                f"comma-separated list of them). Semantic and style analysis live in "
                f"coding-analysis (/api/analysis/...), security analysis in coding-security "
                f"(/api/security/...). Refusing rather than returning an empty "
                f"issue list, which previously read as a clean bill of health."
            ),
        )

    common = {
        "validation_type": request.validation_type,
        "file_path": request.file_path,
        "container_name": request.container_name,
    }
    try:
        content = await read_workspace_file_checked(
            request.container_name, request.file_path, tenant_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except WorkspaceSearchIncompleteError as e:
        # GAP-2: a read that timed out, was clipped, failed for another reason or
        # came back without an exit code is NOT a verdict about the file -- every
        # requested type is not_computed, and there is no `valid` key at all.
        reason = f"The file could not be read completely, so nothing was checked: {e}"
        return CodeValidationResponse(
            validation_results={
                "not_computed": True,
                "status": "not_computed",
                "reason": reason,
                "results": {t: {"status": "not_computed", "reason": reason} for t in requested},
                **common,
            }
        )
    except (ContainerNotResolvableError, WorkspaceBackendUnavailableError) as e:
        raise _workspace_error(e) from e

    if content is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{request.file_path}' does not exist in workspace "
                f"'{request.container_name}'. Refusing to report a validity verdict "
                f"for a file that is not there."
            ),
        )

    if not request.file_path.endswith(".py"):
        reason = (
            "Validation is implemented for Python (*.py) only (python only). No "
            "verdict is asserted for this file type."
        )
        return CodeValidationResponse(
            validation_results={
                "not_computed": True,
                "status": "not_computed",
                "reason": reason,
                "results": {t: {"status": "not_computed", "reason": reason} for t in requested},
                "searched_languages": list(PYTHON_ONLY),
                **common,
            }
        )

    results: dict[str, dict[str, Any]] = {}
    try:
        for vtype in requested:
            if vtype == "syntax":
                results[vtype] = _syntax_result(content)
            elif vtype == "imports":
                results[vtype] = await _imports_result(
                    request.container_name, content, tenant_id, request.file_path
                )
            else:
                results[vtype] = await lint_python_file(
                    request.container_name, request.file_path, tenant_id, content=content
                )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (
        ContainerNotResolvableError,
        WorkspaceBackendUnavailableError,
        WorkspaceSearchIncompleteError,
    ) as e:
        raise _workspace_error(e) from e

    status = _overall_status([r["status"] for r in results.values()])
    syntax = results.get("syntax", {})
    validation_results: dict[str, Any] = {
        "status": status,
        "not_computed": status == "not_computed",
        "errors": list(syntax.get("errors", [])),
        "results": results,
        **common,
    }
    if "syntax" in results:
        validation_results["syntax_errors"] = list(syntax.get("syntax_errors", []))
    if status != "not_computed":
        validation_results["valid"] = status == "valid"
    return CodeValidationResponse(validation_results=validation_results)


_ARCHITECTURE_DESCRIPTION = """Evaluate import rules against the caller's LIVE workspace.

Rule grammar (globs are fnmatch on workspace-relative paths; a pattern without
glob characters also matches as a directory prefix):

- `no_import:<from_glob>-><to_glob>` -- no file matching from_glob imports a
  target matching to_glob
- `layers:<a>><b>><c>` -- `a` is the TOP layer; a file in a lower layer must not
  import a higher layer
- `no_cycles` or `no_cycles:<glob>` -- no import cycle among internal files
- `max_fan_out:<n>` or `max_fan_out:<n>:<glob>` -- no file imports more than n
  distinct internal files
- anything else -> that rule is `not_computed` ("unparsed rule: <text>")

A target matches to_glob by its resolved internal path or by its raw module
string with `.` and `::` turned into `/`. Imports are scanned in Python,
JavaScript/TypeScript, Java, Rust, C#, C/C++ and Go. Internal resolution (import
-> workspace file) exists for: Python (absolute, relative, `from X import sub`),
JS/TS incl. `.mts`/`.cts` and `.vue`/`.svelte`/`.astro` components (relative,
tsconfig/jsconfig `paths` + `baseUrl`, workspace package.json `name`s), Go (via
`go.mod`),
Rust (`crate::`/`super::`/`self::`, `use` groups), Java single-type imports,
C# (`using X;` / `global using` / `using static X.T;` / `using A = X.T;` -> every
file declaring namespace X, found by one `namespace` grep; an undeclared
namespace sharing its first two segments with a declared one is an unresolved
internal import) and C/C++ quoted includes. Java / C# FULLY-QUALIFIED references
to an internal type with no import / `using` line (`new com.acme.infra.Db()`,
`Acme.Infra.Db.Open()`) are edges too (raw module `fq:<name>`, with file:line),
found by one more sharded grep per family for the declared packages /
namespaces followed by `.<Type>`; Java resolves like a single-type import, C#
like `using static`. A reference into the file's OWN package / namespace is no
edge (same-package is not an import); string literal contents and `//`
comments are ignored. A C# PARTIALLY qualified name (`Infra.Db` inside
`namespace Acme.Domain`) binds C#-style: its first segment through the
enclosing namespaces, innermost first (`Acme.Domain.Infra`, then
`Acme.Infra`), then through the file's `using` namespaces; several using-based
readings make the rules over the file `not_computed`. A literal dynamic import (`import('x')`,
`require('x')`, `import_module('x')`, `__import__('x')`) is an edge; a
non-literal one makes the rules over its importer `not_computed`.

Large workspaces: every find/grep runs over its whole directory first and is
split into child directories (plus the directory's own files) only when its
output is clipped at the exec output budget, recursively, within 200 search
calls and 120 s; with `scope`, the import greps start at the scope glob's
literal directory prefix (the file listing always covers the workspace, which
resolution needs). `scan: {files, exec_calls, shards, complete}` reports the
cost; a truncated shard is never counted as complete.

A rule is `not_computed` (never a pass) when: the listing, a search or a config
read is incomplete (a clipped single file or directory listing, a failed or
timed-out search), the scan budget is exhausted, or there are more than 50000
source files; a from_glob or layer glob matches no scanned file; a to_glob
matches no file and no import ("to_glob matches nothing"); an import in its
scope could not be read (an "unresolved alias", an in-module Go package with no
files, a multi-line Rust `use` group, a C# `global using` of an internal
namespace -- it applies project-wide, which is not modelled); or, for
`no_cycles` / `max_fan_out`, the scope has no resolved internal edge or
contains Go outside any go.mod. `verdict` is invalid if any rule fails, else not_computed if any
rule was not computed (listed in `notes`), and valid only when EVERY rule
passes. Violations
are capped at 50 per rule (`truncated`)."""


@router.post(
    "/architecture",
    response_model=ArchitectureValidationResponse,
    summary="Validate Architecture",
    description=_ARCHITECTURE_DESCRIPTION,
    operation_id="validate_architecture",
)
async def validate_architecture(
    request: ArchitectureValidationRequest = Body(...),
    tenant_id: str = Depends(require_tenant),
):
    """Validate Architecture - evaluate import rules over the caller's workspace.

    This endpoint used to return ``{"valid": True, "violations": []}`` built as
    a literal for ANY container and ANY rule list, and then a 501. It now builds
    the import graph from the caller's container (``services.architecture_rules``,
    a bounded number of exec calls) and evaluates each rule, refusing to pass a
    rule it could not compute. ``scope`` (a glob or directory prefix) restricts
    the importing files; ``severity_threshold`` and ``include_suggestions`` are
    accepted for compatibility and do not change the evaluation.
    """
    if not request.container_name:
        raise HTTPException(status_code=422, detail="Missing required field: container_name")

    if not request.rules or not isinstance(request.rules, list) or len(request.rules) == 0:
        raise HTTPException(
            status_code=422, detail="Invalid rules: rules must be a non-empty array"
        )

    try:
        results = await evaluate_architecture(
            request.container_name, request.rules, tenant_id, scope=request.scope
        )
    except ContainerNotResolvableError as e:
        raise _workspace_error(e) from e
    if request.severity_threshold or request.include_suggestions:
        results["notes"].append(
            "severity_threshold / include_suggestions are accepted but not used: every "
            "rule violation is reported, with no suggestions."
        )
    return ArchitectureValidationResponse(
        validation_results={**results, "container_name": request.container_name}
    )
