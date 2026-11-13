"""validateArchitecture: import rules evaluated over the caller's LIVE workspace.

Rule grammar (globs are ``fnmatch`` on workspace-relative paths; a pattern with
no glob characters also matches as a directory prefix, so ``src/domain``
covers ``src/domain/x.py``):

- ``no_import:<from_glob>-><to_glob>`` -- no file matching ``from_glob`` imports
  a target matching ``to_glob``.
- ``layers:<a>><b>><c>`` -- ``a`` is the TOP layer; a file in a lower layer must
  not import a higher one.
- ``no_cycles`` / ``no_cycles:<glob>`` -- no import cycle among internal files
  (within ``glob``).
- ``max_fan_out:<n>`` / ``max_fan_out:<n>:<glob>`` -- no file imports more than
  ``n`` distinct internal files.
- anything else -> that rule is ``not_computed`` ("unparsed rule: <text>").

A target matches ``to_glob`` by its resolved internal path, or by its raw module
string with ``.`` and ``::`` turned into ``/`` (a path-like specifier --
anything already containing ``/`` -- is matched as written).

INTERNAL RESOLUTION (an import turned into the workspace file(s) it names):

- Python: absolute imports by longest existing module prefix under every
  package root -- the repo root, ``src/``, and the parent of each top-level
  package (``backend/app/__init__.py`` makes ``backend`` a root); a directory of
  .py files without ``__init__.py`` is a PEP 420 namespace package (a leaf
  target). ``from X import a, b`` tries the submodule ``X.a`` before ``X``;
  relative imports against the importing file's package directory; ``;``-joined
  statements are split.
- JS/TS (``.js .jsx .mjs .cjs .ts .tsx .mts .cts`` plus the ``<script>`` of
  ``.vue .svelte .astro`` components, which are importers and graph nodes too):
  static ``from '...'`` / ``import '...'`` and ``import('...')`` /
  ``require('...')`` anywhere on a line. Relative specifiers, then
  ``tsconfig.json`` / ``jsconfig.json`` ``compilerOptions.paths`` (``@/*`` ->
  ``src/*``) and ``baseUrl`` from the nearest-ancestor config (a relative
  ``extends`` is followed), then workspace packages (pnpm / npm / yarn): every
  listed ``package.json`` maps its ``name`` to its directory; ``name`` resolves
  through ``exports["."]``, ``module``, ``main``, ``types``, ``src/index.*``,
  ``index.*`` and ``name/sub`` through ``exports["./sub"]``, ``<dir>/sub``,
  ``<dir>/src/sub`` (the root package.json's name is skipped when the root has
  no JS/TS source of its own). Each with the extension and ``index``
  candidates, then as an existing non-code file (``ASSET_EXTENSIONS``: css,
  json, svg, ...) -- a leaf target. A specifier that matches a ``paths`` key or
  a workspace package name but resolves to no file is unresolved, as is a
  ``#subpath`` import.
- Dynamic imports: a literal ``import('x')`` / ``require('x')`` (JS/TS) or
  ``import_module('x')`` / ``__import__('x')`` (Python) is an edge; a
  NON-literal one (a variable, a template or f-string, a concatenation) makes
  every rule whose scope contains that importer ``not_computed`` ("non-literal
  dynamic import in <file>:<line>").
- Go: the module path from each ``go.mod``; ``<module>/<dir>`` names every
  non-test ``.go`` file in that directory. A trailing ``// comment`` is allowed.
- Rust: ``crate::``, ``super::``, ``self::``, paths starting with a child
  module, and paths starting with a workspace crate name (``[package]`` /
  ``[lib]`` ``name`` of each ``Cargo.toml``, ``-`` -> ``_``, else the directory
  name of each ``<dir>/src/lib.rs|main.rs``), to ``<m>.rs`` / ``<m>/mod.rs``
  (longest existing prefix); ``use`` groups ``{x, y}`` expand to each path.
- Java: the class file of the longest prefix naming a class (so ``import
  static a.b.C.m`` and nested classes resolve to ``C.java``), via the declared
  ``package`` of every internal file; ``import a.b.*`` names every file of the
  package.
- C/C++: quoted ``#include "..."`` relative to the file, then to the root.
- C#: one grep for ``namespace X`` declarations (block and file-scoped) maps
  each namespace to its declaring files; ``using X;`` / ``global using X;`` is
  an edge to EVERY file declaring X (like a Java wildcard); ``using static X.T;``
  / ``using A = X.T;`` to the files of X.T, else of the longest declared prefix
  (narrowed to ``T.cs`` when exactly one). A namespace that is only a parent of
  internal ones imports nothing internal. A ``using X;`` after a ``namespace N``
  declaration of its file is resolved relative to N and its enclosing
  namespaces first (``namespace Acme { using Infra;`` -> ``Acme.Infra``); a
  relative candidate that is only internal-looking is unresolved, never
  external. A file declaring MORE than one ``namespace`` (nesting is not
  tracked) makes every rule over that file not computed, and a ``using`` of a
  name such a file may declare by nesting (``A.B`` from ``namespace A {
  namespace B {``) is unresolved. A ``global using`` of an internal namespace
  applies project-wide, which is not modelled: every rule over a C# file is
  then not computed.
- Fully-qualified references (Java, C#): a reference to an internal type with
  no import / ``using`` line (``new com.acme.infra.Db()``,
  ``Acme.Infra.Db.Open()``) is an edge too, raw module ``fq:<name>``, found by
  ONE more sharded grep per family for the declared internal packages /
  namespaces (C#: also every partially qualified tail of one, ``Infra`` /
  ``Domain.Infra`` of ``Acme.Domain.Infra``) followed by ``.<Uppercase>``
  (above ``FQ_ALTERNATION_CAP`` names the grep uses Java first-two-segment
  roots / C# namespace segments, then any dotted name, and the hits are
  filtered here). Java ``a.b.C`` resolves like ``import a.b.C;`` (an internal
  one resolving to no file is unresolved); C# ``X.Y.T`` like ``using static
  X.Y.T;``. A C# PARTIALLY qualified name binds C#-style: its first segment
  is looked up as a namespace in the enclosing namespaces innermost to
  outermost, then the global one (``Infra.Db`` inside ``namespace
  Acme.Domain`` -> ``Acme.Domain.Infra.Db`` if that namespace exists, else
  ``Acme.Infra.Db``), then through the file's ``using`` namespaces (more
  conservative than C#, which does not import nested namespaces through a
  using: a false FAIL, never a false pass); more than one using-based reading
  of an internal type is unresolved. ``global::`` is absolute. A
  reference into the file's OWN package / namespace is no edge: same-package /
  same-namespace use is not an import, by definition. ``package`` / ``import`` /
  ``using`` / ``namespace`` lines are skipped (the import grep reads them), and
  string / char literal contents, ``//`` and one-line ``/* */`` comments are
  blanked before matching. NOT tracked (documented holes): same-package /
  same-namespace references, a qualified name split across lines, and a C#
  name whose first segment binds to a TYPE rather than a namespace (types are
  not indexed). A property / local / class spelled like a namespace tail
  (``Infra.Db`` with a member ``Infra``) can add an edge: a false FAIL, never
  a false pass. Text inside multi-line strings (Java text
  blocks) or multi-line comments (javadoc ``{@link a.b.C}``) can add an edge:
  a false FAIL or not computed, never a false pass.

POSSIBLY INTERNAL. An import that names an internal package but resolves to no
file is UNRESOLVED, never external: C# ``using`` of an undeclared namespace
sharing its first two segments with a declared one; Python whose first segment is a directory
segment or file stem of a Python file (stdlib names excepted) or any relative
import; JS/TS relative specifiers, ``paths`` keys, workspace package names and
``#subpath`` imports; Go inside a module; Rust ``crate``/``self``/``super``, a
child module or a workspace crate; Java sharing
its first two segments with a declared internal package. Every rule whose scope
contains that importer is ``not_computed`` ("unresolved internal import ...").

The import graph is built with a BOUNDED number of exec calls: one ``find``
listing every source file plus every ``go.mod`` / ``tsconfig.json`` /
``jsconfig.json``, one ``cat`` per such config (plus a relative tsconfig
``extends`` chain), one ``cat`` per ``package.json`` when JS/TS is present
(cap 100), then one ``grep -rnE`` per language family present (plus
one ``grep -rnE -A 40`` for the multi-line statements of Python and Rust), one
``cat`` per ``Cargo.toml`` (cap 100), one ``grep`` for Java ``package`` lines, one
for C# ``namespace`` lines, one fully-qualified reference grep per Java / C#
family, and one ``find`` of non-code files when JS/TS is present (incomplete:
asset imports become unresolved).

LARGE REPOS: every ``find`` / ``grep`` is a SHARD (``_Scanner``). It first runs
over its whole directory; only when the output is clipped at the containers'
exec output budget is that directory split into its children (vendored dirs
pruned) plus its own files (``find -maxdepth 1``; explicit-file greps of
``GREP_FILE_CHUNK``), recursively. The scan stops at ``EXEC_CALL_BUDGET`` search
calls or ``SCAN_SECONDS_BUDGET`` seconds -> ``not_computed`` with the reason.
With a ``scope`` the import greps start at the scope glob's literal directory
prefix; the file listing and the Java/C# declaration greps always cover the
whole workspace, because resolving an in-scope import needs every target.
The response carries ``scan: {files, exec_calls, shards, complete}``.

Any incomplete search or read (a non-zero exit, a 124 timeout, clipped output
that cannot be split further), an unparseable config, an exhausted scan
budget, or more than ``FILE_CAP`` source files makes the WHOLE verdict
``not_computed`` -- an import graph with holes would report "no violations"
for edges it never saw.

A rule that cannot fail is not a pass, so a rule is ``not_computed`` when:

- its globs match no scanned file (``from_glob``, any ``layers`` glob), or a
  ``to_glob`` matches no scanned file, no resolved target and no raw import
  specifier ("to_glob matches nothing");
- an importing file in its scope has an import that could not be read
  completely: an unresolved alias, an unresolvable in-module Go package, or a
  multi-line Python ``from X import (`` / Rust ``use`` not closed within
  ``BLOCK_CONTEXT`` lines;
- an importing file in its scope has an unresolved internal import (above);
- a file in its scope is in a language without internal resolution (C#, or Go
  outside any ``go.mod``) -- their edges are invisible;
- ``no_cycles`` / ``max_fan_out``: the scope has ZERO resolved internal edges.
  Leaf targets (assets, namespace dirs) and self-imports are not graph edges.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
import sys
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from services.container_workspace import (
    LANGUAGE_EXTENSIONS,
    VENDOR_EXCLUDE_DIRS,
    ContainerNotResolvableError,
    WorkspaceBackendUnavailableError,
    WorkspaceSearchIncompleteError,
    grep_blocks,
    grep_lines,
    list_child_dirs,
    list_source_files,
    read_workspace_file_checked,
    validate_relative_path,
)
from services.dotted_names import MODULE_SEARCH_ROOTS

FILE_CAP = 50000
# The sharded scan (see ``_Scanner``): at most this many search execs (find /
# grep, splits included; config reads are capped separately by CONFIG_CAP) and
# this much wall-clock time, after which the verdict is not_computed. 200 calls
# covers a 50k-file tree split two levels deep for the seven families; 120s
# keeps the endpoint under the gateway's upstream read timeouts.
EXEC_CALL_BUDGET = 200
SCAN_SECONDS_BUDGET = 120.0
# Files per explicit-file grep when a directory's own files are searched apart.
GREP_FILE_CHUNK = 200
CONFIG_CAP = 100
EXTENDS_DEPTH = 5
VIOLATION_CAP = 50

# JS/TS source files: the module flavours ``.mts`` / ``.cts`` included.
JS_TS_EXTENSIONS: tuple[str, ...] = (
    LANGUAGE_EXTENSIONS["javascript"] + LANGUAGE_EXTENSIONS["typescript"] + (".mts", ".cts")
)
# Single-file components: their ``<script>`` blocks use the JS/TS import syntax,
# so they are importers and graph nodes like any JS/TS file.
JS_COMPONENT_EXTENSIONS: tuple[str, ...] = (".vue", ".svelte", ".astro")

# family -> (extensions, grep ERE capturing candidate import lines). An import
# statement is matched at the start of a line OR after a ``;`` (``package p;
# import q.*;``); ``split_statements`` then parses every statement on the line.
FAMILIES: dict[str, tuple[tuple[str, ...], str]] = {
    "python": (
        LANGUAGE_EXTENSIONS["python"],
        r"(^|;)[[:space:]]*(import|from)[[:space:]]+[A-Za-z_.]"
        r"|(^|[^A-Za-z0-9_])(import_module|__import__)[[:space:]]*\(",
    ),
    "javascript_typescript": (
        JS_TS_EXTENSIONS + JS_COMPONENT_EXTENSIONS,
        r"""(from[[:space:]]*['"]|(^|;)[[:space:]]*import[[:space:]]*['"]"""
        r"""|(^|[^A-Za-z0-9_$.])(import|require)[[:space:]]*\()""",
    ),
    "java": (LANGUAGE_EXTENSIONS["java"], r"(^|;)[[:space:]]*import[[:space:]]"),
    "rust": (LANGUAGE_EXTENSIONS["rust"], r"(^|;)[[:space:]]*(pub[[:space:]]+)?use[[:space:]]"),
    "c_sharp": (
        LANGUAGE_EXTENSIONS["c_sharp"],
        r"(^|;)[[:space:]]*(global[[:space:]]+)?using[[:space:]]+[A-Za-z]",
    ),
    "cpp": (LANGUAGE_EXTENSIONS["cpp"], r"^[[:space:]]*#[[:space:]]*include[[:space:]]*[<\"]"),
    "go": (
        LANGUAGE_EXTENSIONS["go"],
        r'^[[:space:]]*(import[[:space:]]+)?([[:alnum:]_.]+[[:space:]]+)?"[^"]+"[[:space:]]*(//.*)?$'
        r'|;[[:space:]]*import[[:space:]]+([[:alnum:]_.]+[[:space:]]+)?"',
    ),
}

# family -> (grep ERE for the OPENING line of a statement that continues over
# several lines, the character that closes it). One extra ``grep -A`` per family
# reads the continuation; see ``join_continuations``.
BLOCK_CONTEXT = 40
CONTINUATIONS: dict[str, tuple[str, str]] = {
    # ``from X import (`` with the closing ``)`` on a later line.
    "python": (
        r"^[[:space:]]*from[[:space:]]+[A-Za-z_.]+[[:space:]]+import[[:space:]]*\([^)#]*(#.*)?$",
        ")",
    ),
    # ``use a::{`` (any ``use`` without its ``;``) -- the statement ends at ``;``.
    "rust": (r"^[[:space:]]*(pub(\([^)]*\))?[[:space:]]+)?use[[:space:]]+[^;/]*(//.*)?$", ";"),
}

ALL_EXTENSIONS: tuple[str, ...] = tuple(e for exts, _ in FAMILIES.values() for e in exts)

# Families whose imports are never resolved to internal files (none since C#
# namespace resolution landed; kept so a future family can opt out explicitly).
UNRESOLVED_FAMILIES: frozenset[str] = frozenset()

GO_MOD = "go.mod"
CARGO_TOML = "Cargo.toml"
TS_CONFIG_NAMES = ("tsconfig.json", "jsconfig.json")
CONFIG_NAMES: tuple[str, ...] = (GO_MOD, CARGO_TOML, *TS_CONFIG_NAMES)
PACKAGE_JSON = "package.json"

# Non-code files a JS/TS module can import (bundler assets). An import of one
# that exists resolves to it as a LEAF: it counts for ``no_import`` / ``layers``
# globs but is not a node of the cycle / fan-out graph.
ASSET_EXTENSIONS: tuple[str, ...] = (
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".styl",
    ".pcss",
    ".json",
    ".svg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".avif",
    ".ico",
    ".bmp",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".wav",
    ".webm",
    ".txt",
    ".md",
    ".mdx",
    ".html",
    ".graphql",
    ".gql",
    ".yaml",
    ".yml",
    ".wasm",
)

# One grep over .java files: the package each internal file declares.
JAVA_PACKAGE_PATTERN = r"^[[:space:]]*package[[:space:]]+[A-Za-z_]"
_JAVA_PACKAGE = re.compile(r"^\s*package\s+([A-Za-z_][\w.]*)\s*;")

# One grep over .cs files: the namespace(s) each internal file declares, block
# (``namespace A.B {``) and file-scoped (``namespace A.B;``).
CS_NAMESPACE_PATTERN = r"^[[:space:]]*namespace[[:space:]]+[A-Za-z_]"
_CS_NAMESPACE = re.compile(r"^(\s*)namespace\s+([A-Za-z_][\w.]*)\s*(?:[;{]|$)")

# Python top-level names that are the standard library: never "possibly internal".
_STDLIB = frozenset(sys.stdlib_module_names)


@dataclass(frozen=True)
class Edge:
    file: str
    line: int
    module: str  # the raw imported module / specifier
    target: str | None  # resolved internal file, when resolvable
    unresolved: str | None = None  # why this import could not be read completely
    # The target is not a source file (a Python namespace-package directory or
    # a JS/TS asset): it counts for globs, never as a cycle / fan-out node.
    leaf: bool = False


@dataclass
class TsConfig:
    """``compilerOptions`` that steer module resolution, with absolute dirs."""

    base_url: str | None = None  # workspace-relative directory
    paths: dict[str, list[str]] = field(default_factory=dict)
    paths_base: str = ""  # directory the ``paths`` targets are relative to


@dataclass
class ResolveContext:
    """What resolution needs beyond the file list: go.mod modules, TS configs."""

    go_modules: dict[str, str] = field(default_factory=dict)  # module dir -> module path
    ts_configs: dict[str, TsConfig] = field(default_factory=dict)  # config dir -> options
    rust_crates: dict[str, str] = field(default_factory=dict)  # crate name -> its src/ dir
    java_packages: dict[str, list[str]] = field(default_factory=dict)  # package -> files
    cs_namespaces: dict[str, list[str]] = field(default_factory=dict)  # namespace -> files
    # file -> its ``namespace X`` declarations ``(line, X)`` in file order.
    cs_declarations: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    # Names a multi-namespace file may declare by nesting (``cs_nested_names``).
    cs_nested: frozenset[str] = frozenset()
    # file -> its plain ``using X;`` directives ``(line, X)`` (no static / alias /
    # global): the namespaces a partially qualified name may resolve through.
    cs_usings: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    # Every listed non-code file JS/TS may import; ``None``: the listing was incomplete.
    assets: frozenset[str] | None = frozenset()
    # Workspace (pnpm / npm / yarn) packages: ``name`` -> (package dir, package.json).
    packages: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)


class GraphNotComputedError(Exception):
    """The import graph could not be built completely."""


def family_of(path: str) -> str | None:
    lowered = path.lower()
    return next((f for f, (exts, _) in FAMILIES.items() if lowered.endswith(exts)), None)


def _dir_of(path: str) -> str:
    return posixpath.dirname(path)


def _join(*parts: str) -> str:
    kept = [p for p in parts if p]
    return posixpath.normpath(posixpath.join(*kept)) if kept else ""


def _under(path: str, directory: str) -> bool:
    return not directory or path.startswith(directory + "/")


def _nearest(directories: dict[str, Any], file: str) -> str | None:
    """The deepest key of *directories* that is an ancestor directory of *file*."""
    owning: list[str] = [d for d in directories if _under(file, d)]
    return max(owning, key=lambda d: len(d)) if owning else None


def _exists(files: frozenset[str], candidates: list[str]) -> str | None:
    return next((c for c in candidates if c in files), None)


# ---------------------------------------------------------------------------
# Multi-line statements
# ---------------------------------------------------------------------------

# Where a line comment starts, per family that has continuations.
_LINE_COMMENT = {"python": "#", "rust": "//"}

# (file, opening line) -> the whole statement on one line, or ``None`` when its
# closing token was not found within the context grep printed.
Continuations = dict[tuple[str, int], str | None]


def join_continuations(family: str, blocks: list[dict[str, Any]]) -> Continuations:
    """Join each multi-line statement opened in *blocks* into one source line."""
    closer = CONTINUATIONS[family][1]
    comment = _LINE_COMMENT[family]
    out: Continuations = {}
    for block in blocks:
        parts = [block["source"].split(comment, 1)[0].rstrip()]
        joined: str | None = None
        for text in block["after"]:
            code = text.split(comment, 1)[0].strip()
            if code:
                parts.append(code)
            if closer in code:
                joined = " ".join(parts)
                break
        out[(block["file"], block["line"])] = joined
    return out


def _unclosed(file: str, line: int, source: str) -> str:
    return (
        f"unread multi-line import: '{source.strip()}' in {file}:{line} is not closed "
        f"within {BLOCK_CONTEXT} lines"
    )


# Families where ``'`` opens a string; elsewhere only a char literal (``';'``,
# ``'\\n'``) is one, so a Rust lifetime ``'a`` is plain text.
_QUOTE_STRING_FAMILIES = frozenset({"python", "javascript_typescript"})
_CHAR_LITERAL = re.compile(r"'(?:\\[^']{1,10}|[^'\\])'")


def split_statements(family: str, source: str) -> list[str]:
    """*source* split at every ``;`` outside string / char literals and comments.

    Each piece keeps its ``;``; a line comment ends the line (``#`` in Python,
    ``//`` elsewhere) and a ``/* */`` comment is skipped, so an import written
    after another statement (``package p; import q.*;``) is parsed like one at
    the start of a line, and one inside a string or comment is not.

    The scan is lexical, not a parser: a ``//`` inside a JS regex literal reads
    as a comment. Callers therefore UNION its statements' edges with the
    whole-line scan (``union_edges``), so splitting can only add an edge.
    """
    return _scan_statements(family, source)[0]


def code_part(family: str, source: str) -> str:
    """*source* up to its line comment, found outside string / char literals."""
    return source[: _scan_statements(family, source)[1]]


def union_edges(*groups: list[Edge]) -> list[Edge]:
    """The multiset union of edge lists: each edge as often as the most any group has it.

    Keyed on ``(file, line, module, target)``, first occurrence kept, so a
    second scan of the same line can add edges but never remove or double one.
    """
    best: Counter[tuple[str, int, str, str | None]] = Counter()
    first: dict[tuple[str, int, str, str | None], list[Edge]] = {}
    for group in groups:
        seen: Counter[tuple[str, int, str, str | None]] = Counter()
        for edge in group:
            key = (edge.file, edge.line, edge.module, edge.target)
            seen[key] += 1
            first.setdefault(key, []).append(edge)
        for key, count in seen.items():
            best[key] = max(best[key], count)
    out: list[Edge] = []
    emitted: Counter[tuple[str, int, str, str | None]] = Counter()
    for group in groups:
        for edge in group:
            key = (edge.file, edge.line, edge.module, edge.target)
            if emitted[key] < best[key]:
                out.append(first[key][emitted[key]])
                emitted[key] += 1
    return out


def _scan_statements(family: str, source: str) -> tuple[list[str], int]:
    """``split_statements`` plus the offset where the code ends (its line comment)."""
    pieces: list[str] = []
    start = i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if family == "python" and ch == "#":
            n = i
            break
        if family != "python" and source.startswith("//", i):
            n = i
            break
        if family != "python" and source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if (
            family == "rust"
            and not (i and (source[i - 1].isalnum() or source[i - 1] == "_"))
            and (m := _RUST_RAW_OPEN.match(source, i))
        ):
            end = source.find('"' + m.group(1), m.end())
            i = n if end < 0 else end + 1 + len(m.group(1))
            continue
        if ch == "'" and family not in _QUOTE_STRING_FAMILIES:
            m = _CHAR_LITERAL.match(source, i)
            i = m.end() if m else i + 1
            continue
        if ch in "\"'`" and (ch != "`" or family in ("javascript_typescript", "go")):
            verbatim = ch == '"' and family == "c_sharp" and "@" in source[max(0, i - 2) : i]
            raw = ch == "`"
            j = i + 1
            while j < n:
                if source[j] == "\\" and not (verbatim or raw):
                    j += 2
                    continue
                if source[j] == ch:
                    if verbatim and source.startswith(ch * 2, j):
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
            continue
        if ch == ";":
            pieces.append(source[start : i + 1])
            start = i + 1
        i += 1
    pieces.append(source[start:n])
    return [p for p in pieces if p.strip()], n


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

_PY_FROM = re.compile(r"^\s*from\s+(\.*)([A-Za-z_][\w.]*)?\s+import\s*(.*)$")
_PY_IMPORT = re.compile(
    r"^\s*import\s+([A-Za-z_][\w.]*(?:\s+as\s+\w+)?(?:\s*,\s*[A-Za-z_][\w.]*(?:\s+as\s+\w+)?)*)"
    r"\s*(?:#.*)?\\?$"
)


def _python_from_names(rest: str) -> list[str]:
    """The imported names of ``from X import <rest>`` (``*`` and partial lists dropped)."""
    rest = rest.split("#", 1)[0].strip().rstrip("\\").strip()
    rest = rest.strip("()").strip()
    names = []
    for part in rest.split(","):
        tokens = part.split()
        if tokens and re.fullmatch(r"[A-Za-z_]\w*", tokens[0]):
            names.append(tokens[0])
    return names


def _python_imports(source: str) -> list[tuple[int, str, list[str]]]:
    """``(level, module, names)``; level > 0 for a relative import, names for ``from``."""
    if m := _PY_FROM.match(source):
        dots, module = m.group(1), m.group(2) or ""
        if not dots and not module:
            return []
        return [(len(dots), module, _python_from_names(m.group(3)))]
    if m := _PY_IMPORT.match(source):
        return [(0, part.split()[0], []) for part in m.group(1).split(",")]
    return []


@dataclass(frozen=True)
class PythonIndex:
    """What Python resolution derives from the file listing alone."""

    roots: tuple[str, ...]  # directories a top-level import is looked up under
    dirs: frozenset[str]  # every directory holding a .py file (namespace packages)
    names: frozenset[str]  # "possibly internal" top-level names


def python_index(files: frozenset[str]) -> PythonIndex:
    """Package roots, package dirs and possibly-internal names of the Python files.

    A root is the repo root, ``src/``, and the parent of every TOP-LEVEL package
    (a directory with an ``__init__.py`` whose parent has none), so
    ``backend/app/__init__.py`` makes ``backend`` a root. The possibly-internal
    names are every directory segment and file stem of a Python file, minus the
    standard library.
    """
    py = [f for f in files if f.endswith((".py", ".pyw"))]
    names: set[str] = set()
    dirs: set[str] = set()
    for f in py:
        parts = f.split("/")
        names.update(parts[:-1])
        names.add(posixpath.splitext(parts[-1])[0])
        dirs.update("/".join(parts[:k]) for k in range(1, len(parts)))
    packages = {_dir_of(f) for f in py if posixpath.basename(f) == "__init__.py"}
    roots = list(MODULE_SEARCH_ROOTS)
    for package in sorted(packages):
        parent = _dir_of(package)
        if package and parent not in packages and parent not in roots:
            roots.append(parent)
    names.discard("__init__")
    return PythonIndex(tuple(roots), frozenset(dirs), frozenset(names - _STDLIB))


def _python_package_dir(file: str, level: int) -> str | None:
    """The directory a relative import of *level* dots starts from, or ``None``."""
    base = file.split("/")[:-1]
    if level - 1 > len(base):
        return None
    return "/".join(base[: len(base) - (level - 1)])


def _py_file_at(base: str, rel: str, files: frozenset[str]) -> str | None:
    path = _join(base, rel)
    return _exists(files, [f"{path}.py", f"{path}/__init__.py"]) if path else None


def _py_namespace_at(base: str, rel: str, idx: PythonIndex) -> str | None:
    """A PEP 420 namespace package: a directory of .py files without ``__init__.py``."""
    path = _join(base, rel)
    return path if path and path in idx.dirs else None


def _python_exact(
    file: str, level: int, module: str, files: frozenset[str], idx: PythonIndex
) -> str | None:
    """The file (or namespace-package dir) that IS *module* (no prefix fallback)."""
    rel = module.replace(".", "/")
    if level:
        base = _python_package_dir(file, level)
        if base is None:
            return None
        return _py_file_at(base, rel, files) or _py_namespace_at(base, rel, idx)
    for root in idx.roots:
        if hit := _py_file_at(root, rel, files):
            return hit
    for root in idx.roots:
        if hit := _py_namespace_at(root, rel, idx):
            return hit
    return None


def _resolve_python(
    file: str, level: int, module: str, files: frozenset[str], idx: PythonIndex
) -> str | None:
    """The internal file a Python import names (longest existing module prefix)."""
    parts = module.split(".") if module else []
    for k in range(len(parts), 0, -1):
        if hit := _python_exact(file, level, ".".join(parts[:k]), files, idx):
            return hit
    if level and not parts:
        base = _python_package_dir(file, level)
        if base:
            return _exists(files, [f"{base}/__init__.py"]) or _py_namespace_at(base, "", idx)
    return None


def _unresolved_internal(module: str, file: str) -> str:
    return f"unresolved internal import {module} in {file}"


def _edge(file: str, line: int, module: str, target: str, files: frozenset[str]) -> Edge:
    return Edge(file, line, module, target, leaf=target not in files)


def _python_module_edges(
    file: str,
    line: int,
    level: int,
    module: str,
    names: list[str],
    files: frozenset[str],
    idx: PythonIndex,
) -> list[Edge]:
    """The edges of one ``import X`` / ``from X import names`` (``level`` dots)."""
    edges: list[Edge] = []
    raw = "." * level + module
    # No names: the edge to X itself is kept, because names may live in X.
    unmatched = not names
    for name in names:
        sub = f"{module}.{name}" if module else name
        if target := _python_exact(file, level, sub, files, idx):
            edges.append(_edge(file, line, "." * level + sub, target, files))
        else:
            unmatched = True
    if not unmatched:
        return edges
    if target := _resolve_python(file, level, module, files, idx):
        edges.append(_edge(file, line, raw, target, files))
    elif level or module.split(".", 1)[0] in idx.names:
        edges.append(Edge(file, line, raw, None, unresolved=_unresolved_internal(raw, file)))
    else:
        edges.append(Edge(file, line, raw, None))
    return edges


# ``importlib.import_module(...)`` / ``import_module(...)`` / ``__import__(...)``;
# group 2 is the module when the first argument is ONE plain string literal.
_PY_DYNAMIC = re.compile(
    r"(?<![\w])(?:import_module|__import__)\s*\(\s*(?:([rRuU]?)(['\"])([^'\"\\]*)\2(?=\s*[,)]))?"
)
_PY_MODULE_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")


def _non_literal(file: str, line: int) -> Edge:
    return Edge(
        file,
        line,
        "<non-literal dynamic import>",
        None,
        unresolved=f"non-literal dynamic import in {file}:{line}",
    )


def _python_dynamic_edges(
    file: str, line: int, code: str, files: frozenset[str], idx: PythonIndex
) -> list[Edge]:
    """Edges of the dynamic imports on one line; a non-literal one is unread."""
    edges: list[Edge] = []
    for m in _PY_DYNAMIC.finditer(code):
        literal = m.group(3)
        if literal is None:
            edges.append(_non_literal(file, line))
        elif literal.startswith("."):
            edges.append(
                Edge(
                    file,
                    line,
                    literal,
                    None,
                    unresolved=(
                        f"relative dynamic import '{literal}' in {file}:{line}: its anchor is "
                        "the `package` argument, which is not read"
                    ),
                )
            )
        elif _PY_MODULE_NAME.fullmatch(literal):
            edges += _python_module_edges(file, line, 0, literal, [], files, idx)
        else:
            edges.append(_non_literal(file, line))
    return edges


def _python_edges(
    file: str, line: int, source: str, files: frozenset[str], idx: PythonIndex
) -> list[Edge]:
    # Two scans, unioned so neither can lose the other's edge: the whole-line
    # one (code cut at the first ``#``, split at every ``;``) and the lexical
    # one (``#`` / ``;`` only outside strings, so ``x = "#"; import y`` is read).
    whole = source.split("#", 1)[0]
    dynamic = _python_dynamic_edges(file, line, whole, files, idx)
    if "(" in whole and ")" not in whole and not dynamic:
        # A parenthesised list whose continuation was not joined: its names are unread.
        return [Edge(file, line, whole.strip(), None, unresolved=_unclosed(file, line, whole))]
    whole_edges: list[Edge] = []
    for statement in whole.split(";"):
        for level, module, names in _python_imports(statement):
            whole_edges += _python_module_edges(file, line, level, module, names, files, idx)
    code = code_part("python", source)
    lexical_dynamic = _python_dynamic_edges(file, line, code, files, idx)
    lexical: list[Edge] = []
    if "(" in code and ")" not in code and not lexical_dynamic:
        lexical.append(Edge(file, line, code.strip(), None, unresolved=_unclosed(file, line, code)))
    for statement in split_statements("python", code):
        for level, module, names in _python_imports(statement.rstrip().rstrip(";")):
            lexical += _python_module_edges(file, line, level, module, names, files, idx)
    return union_edges(whole_edges + dynamic, lexical + lexical_dynamic)


# ---------------------------------------------------------------------------
# JavaScript / TypeScript
# ---------------------------------------------------------------------------

_JS_SPEC = re.compile(r"""(?:\bfrom\s*|^\s*import\s*)(['"])([^'"]+)\1""")
# ``import(...)`` / ``require(...)`` anywhere on a line (``await import('./x')``,
# ``dynamic(() => import('@/x'))``, ``import x = require('y')``). Group 3 is the
# specifier when the first argument is ONE plain string literal (block comments
# such as webpack's ``/* webpackChunkName */`` allowed before it); otherwise the
# call is non-literal and its target is unknown.
_JS_CALL = re.compile(
    r"""(?<![\w$.])(import|require)\s*\((?:\s*/\*.*?\*/)*\s*"""
    r"""(?:(['"])([^'"]+)\2(?=\s*[,)]))?"""
)
_JS_EXTS = JS_TS_EXTENSIONS


def js_path_candidates(path: str) -> list[str]:
    """The files *path* (workspace-relative, normalised) may name, in resolution order:
    itself, the TS source of a ``.js`` ESM path, then extensions and index files."""
    if not path or path == ".." or path.startswith("../"):
        return []
    candidates = [path]
    stem, ext = posixpath.splitext(path)
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        # TS ESM: `./x.js` names the compiled output of `./x.ts`.
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    candidates += [path + e for e in _JS_EXTS]
    candidates += [f"{path}/index{e}" for e in _JS_EXTS]
    return candidates


def _resolve_js_path(path: str, files: frozenset[str]) -> str | None:
    """*path* (workspace-relative, normalised) as a file, with extensions and index files."""
    return _exists(files, js_path_candidates(path))


def _resolve_js_target(path: str, files: frozenset[str], ctx: ResolveContext) -> str | None:
    """*path* as a code file, else as an existing non-code file (an asset leaf)."""
    if hit := _resolve_js_path(path, files):
        return hit
    if ctx.assets and path in ctx.assets:
        return path
    return None


def _js_unresolved(file: str, spec: str, ctx: ResolveContext) -> str:
    why = _unresolved_internal(spec, file)
    if ctx.assets is None:
        why += " (the non-code file listing was incomplete, so assets could not be checked)"
    return why


def _match_paths_key(key: str, spec: str) -> str | None:
    """The ``*`` capture when *spec* matches the ``paths`` key, else ``None``."""
    if "*" not in key:
        return "" if spec == key else None
    prefix, _, suffix = key.partition("*")
    if spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix + suffix):
        return spec[len(prefix) : len(spec) - len(suffix)]
    return None


def _js_edge(file: str, line: int, spec: str, files: frozenset[str], ctx: ResolveContext) -> Edge:
    bare = spec.split("?", 1)[0].split("#", 1)[0]  # `./x.svg?react`, `./x.css#id`
    if spec.startswith("."):
        if hit := _resolve_js_target(_join(_dir_of(file), bare), files, ctx):
            return _edge(file, line, spec, hit, files)
        return Edge(file, line, spec, None, unresolved=_js_unresolved(file, spec, ctx))
    config_dir = _nearest(ctx.ts_configs, file)
    if config_dir is not None and (
        edge := _ts_config_edge(file, line, spec, files, ctx, config_dir)
    ):
        return edge
    return _package_edge(file, line, spec, files, ctx)


def _ts_config_edge(
    file: str, line: int, spec: str, files: frozenset[str], ctx: ResolveContext, config_dir: str
) -> Edge | None:
    """*spec* through the nearest tsconfig ``paths`` / ``baseUrl``; ``None``: no match."""
    bare = spec.split("?", 1)[0].split("#", 1)[0]
    config = ctx.ts_configs[config_dir]
    # The bare catch-all key "*" (typically `"*": ["types/*"]`) matches every
    # package specifier; TypeScript then falls back to node_modules, so a miss on
    # it is an external package, not an unresolved alias.
    matches = [
        (key, capture)
        for key in config.paths
        if (capture := _match_paths_key(key, spec)) is not None
    ]
    if matches and all(key == "*" for key, _ in matches):
        for target in config.paths["*"]:
            if hit := _resolve_js_target(
                _join(config.paths_base, target.replace("*", bare, 1)), files, ctx
            ):
                return _edge(file, line, spec, hit, files)
        matches = []
    if matches:
        # TypeScript uses the key with the longest prefix before the `*`.
        key, capture = max(matches, key=lambda kc: len(kc[0].partition("*")[0]))
        capture = capture.split("?", 1)[0].split("#", 1)[0]
        for target in config.paths[key]:
            path = _join(config.paths_base, target.replace("*", capture, 1))
            if hit := _resolve_js_target(path, files, ctx):
                return _edge(file, line, spec, hit, files)
        return Edge(
            file,
            line,
            spec,
            None,
            unresolved=(
                f"unresolved alias: '{spec}' in {file} matches the paths key '{key}' "
                f"of {_join(config_dir, 'tsconfig/jsconfig')} but resolves to no file"
            ),
        )
    if config.base_url is not None:
        if hit := _resolve_js_target(_join(config.base_url, bare), files, ctx):
            return _edge(file, line, spec, hit, files)
    return None


def _flatten_exports(value: Any) -> list[str]:
    """Every target string of an ``exports`` value (conditions in object order)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [t for item in value for t in _flatten_exports(item)]
    if isinstance(value, dict):
        return [t for item in value.values() for t in _flatten_exports(item)]
    return []


def _export_targets(exports: Any, subpath: str) -> list[str]:
    """The ``exports`` targets of *subpath* (``"."`` or ``"./x"``), wildcards expanded."""
    if exports is None:
        return []
    if not isinstance(exports, dict) or not any(str(k).startswith(".") for k in exports):
        # A string, an array or a conditions object: all of it is the "." export.
        return _flatten_exports(exports) if subpath == "." else []
    if subpath in exports:
        return _flatten_exports(exports[subpath])
    out: list[str] = []
    for key, value in exports.items():
        capture = _match_paths_key(str(key), subpath) if "*" in str(key) else None
        if capture is not None:
            out += [t.replace("*", capture) for t in _flatten_exports(value)]
    return out


def _package_edge(
    file: str, line: int, spec: str, files: frozenset[str], ctx: ResolveContext
) -> Edge:
    """*spec* as a workspace package (``name`` or ``name/sub``), else external."""
    owners: list[str] = [n for n in ctx.packages if spec == n or spec.startswith(n + "/")]
    if not owners and spec.startswith("#"):
        # A Node subpath import (package.json ``imports``) is internal by definition.
        return Edge(
            file,
            line,
            spec,
            None,
            unresolved=f"unresolved subpath import: '{spec}' in {file} (package.json "
            "`imports` are not resolved)",
        )
    if not owners:
        return Edge(file, line, spec, None)
    name = max(owners, key=lambda n: len(n))
    directory, data = ctx.packages[name]
    sub = spec[len(name) :].lstrip("/").split("?", 1)[0].split("#", 1)[0]
    exports = data.get("exports")
    if not sub:
        candidates = _export_targets(exports, ".")
        candidates += [
            v for k in ("module", "main", "types", "typings") if isinstance(v := data.get(k), str)
        ]
        candidates += ["src/index", "index"]
    else:
        candidates = _export_targets(exports, f"./{sub}") + [sub, f"src/{sub}"]
    for candidate in candidates:
        if hit := _resolve_js_target(_join(directory, candidate), files, ctx):
            return _edge(file, line, spec, hit, files)
    return Edge(
        file,
        line,
        spec,
        None,
        unresolved=(
            f"unresolved workspace package import: '{spec}' in {file} names the workspace "
            f"package '{name}' ({_join(directory, PACKAGE_JSON)}) but resolves to no file"
        ),
    )


def parse_jsonc(text: str) -> Any:
    """JSON with ``//`` / ``/* */`` comments and trailing commas (tsconfig syntax)."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(ch)
            i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------

_GO = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"([^"]+)"\s*(?://.*)?$')
_GO_MODULE = re.compile(r"^\s*module\s+\"?([^\s\"]+)\"?", re.MULTILINE)


def _go_edges(
    file: str, line: int, spec: str, files: frozenset[str], ctx: ResolveContext
) -> list[Edge]:
    owners = [
        (d, mod) for d, mod in ctx.go_modules.items() if spec == mod or spec.startswith(mod + "/")
    ]
    if not owners:
        return [Edge(file, line, spec, None)]
    module_dir, module = max(owners, key=lambda dm: len(dm[1]))
    package_dir = _join(module_dir, spec[len(module) :].lstrip("/"))
    package_files = sorted(
        f
        for f in files
        if f.endswith(".go") and not f.endswith("_test.go") and _dir_of(f) == package_dir
    )
    if not package_files:
        return [
            Edge(
                file,
                line,
                spec,
                None,
                unresolved=(
                    f"unresolved Go package: '{spec}' in {file} is inside module '{module}' "
                    f"but '{package_dir or '.'}' has no non-test .go file"
                ),
            )
        ]
    return [Edge(file, line, spec, target) for target in package_files]


# ---------------------------------------------------------------------------
# Rust
# ---------------------------------------------------------------------------

_RUST = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?use\s+(.*)$")
# A raw (``r"…"``, ``r#"…"#``) or byte-raw (``br#"…"#``) string opener.
_RUST_RAW_OPEN = re.compile(r'b?r(#*)"')
_RUST_IDENT = re.compile(r"[A-Za-z_]\w*")
_RUST_BYTE_CHAR = re.compile(r"b'(?:\\[^']{1,10}|[^'\\])'")


def _rust_use_tokens(source: str) -> int | None:
    """Whole-word ``use`` tokens on *source* outside literals and comments.

    A deliberately dumb, conservative count (``use`` in any position: after
    ``{``, ``}``, an attribute, a ``mod`` header ...). ``None`` when the line
    is not understood: an unterminated string or block comment, a nested
    ``/*`` or a ``macro_rules!`` definition.
    """
    # Any literal, comment or macro on the line: do not trust a hand lexer (a
    # C-string ``cr#"..."#`` slipped past one and turned a base not_computed into
    # a pass). The exception is for plain ``use a; use b;`` lines only.
    if any(t in source for t in ('"', "'", "//", "/*", "!", "#")):
        return None
    count = 0
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        prev_ident = i > 0 and (source[i - 1].isalnum() or source[i - 1] == "_")
        if source.startswith("//", i):
            break
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0 or "/*" in source[i + 2 : end]:
                return None
            i = end + 2
            continue
        if not prev_ident and (m := _RUST_RAW_OPEN.match(source, i)):
            close = '"' + m.group(1)
            end = source.find(close, m.end())
            if end < 0:
                return None
            i = end + len(close)
            continue
        if not prev_ident and (m := _RUST_BYTE_CHAR.match(source, i)):
            i = m.end()
            continue
        if ch == "'":
            m = _CHAR_LITERAL.match(source, i)
            i = m.end() if m else i + 1
            continue
        if ch == '"':
            j = i + 1
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            if j >= n:
                return None
            i = j + 1
            continue
        if not prev_ident and (m := _RUST_IDENT.match(source, i)):
            word = m.group(0)
            if word == "macro_rules":
                return None
            if word == "r" and source.startswith("#", m.end()):
                # A raw identifier ``r#use`` is a name, not the keyword.
                ident = _RUST_IDENT.match(source, m.end() + 1)
                i = ident.end() if ident else m.end() + 1
                continue
            count += word == "use"
            i = m.end()
            continue
        i += 1
    return count


def _rust_expand(tree: str) -> list[str]:
    """``a::{b, c::{d, self}}`` -> ``["a::b", "a::c::d", "a::c"]`` (``as`` dropped)."""
    tree = tree.strip()
    brace = tree.find("{")
    if brace < 0:
        path = tree.split(" as ")[0].strip()
        return [path] if path else []
    prefix = tree[:brace].strip().removesuffix("::")
    inner = tree[brace + 1 : tree.rfind("}")]
    items, depth, start = [], 0, 0
    for i, ch in enumerate(inner):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            items.append(inner[start:i])
            start = i + 1
    items.append(inner[start:])
    out: list[str] = []
    for item in items:
        for path in _rust_expand(item):
            if path == "self":
                out.append(prefix)
            elif path:
                out.append(f"{prefix}::{path}" if prefix else path)
    return out


def _rust_crate_root(file: str) -> str:
    parts = file.split("/")[:-1]
    if "src" in parts:
        idx = len(parts) - 1 - parts[::-1].index("src")
        return "/".join(parts[: idx + 1])
    return "/".join(parts)


def _rust_self_dir(file: str) -> str:
    """The directory holding the children of *file*'s module."""
    directory, base = _dir_of(file), posixpath.basename(file)
    if base in ("mod.rs", "lib.rs", "main.rs"):
        return directory
    return _join(directory, base[: -len(".rs")])


def _rust_module_file(directory: str, crate_root: str, files: frozenset[str]) -> str | None:
    candidates = [f"{directory}.rs", f"{directory}/mod.rs"] if directory else []
    if directory == crate_root:
        candidates += [_join(directory, "lib.rs"), _join(directory, "main.rs")]
    return _exists(files, candidates)


def rust_crates(files: frozenset[str], declared: dict[str, str]) -> dict[str, str]:
    """Crate name -> its ``src/`` dir: *declared* (Cargo.toml) first, then the
    directory name of every ``<dir>/src/lib.rs`` / ``main.rs`` (``-`` -> ``_``)."""
    crates = dict(declared)
    known = set(crates.values())
    for f in sorted(files):
        if posixpath.basename(f) in ("lib.rs", "main.rs") and f.count("/") >= 2:
            src = _dir_of(f)
            crate_dir = _dir_of(src)
            if posixpath.basename(src) == "src" and src not in known:
                crates.setdefault(posixpath.basename(crate_dir).replace("-", "_"), src)
                known.add(src)
    return crates


def _rust_resolve(
    file: str, path: str, files: frozenset[str], crates: dict[str, str] | None = None
) -> str | None:
    parts = [p for p in path.split("::") if p]
    if not parts:
        return None
    crate_root = _rust_crate_root(file)
    head = parts[0]
    if head == "crate":
        base, rest = crate_root, parts[1:]
    elif head in ("self", "super"):
        base, rest = _rust_self_dir(file), parts
        while rest and rest[0] in ("self", "super"):
            if rest[0] == "super":
                if not _under(base, crate_root) or base == crate_root:
                    return None
                base = _dir_of(base)
            rest = rest[1:]
    else:
        # A path starting with a child module of the current module (2018 paths).
        base, rest = _rust_self_dir(file), parts
        child = _join(base, head)
        if not _exists(files, [f"{child}.rs", f"{child}/mod.rs"]):
            if not crates or head not in crates:
                return None
            # Another crate of the workspace: like ``crate::`` inside that crate.
            crate_root = crates[head]
            base, rest = crate_root, parts[1:]
    for k in range(len(rest), 0, -1):
        rel = _join(base, *rest[:k])
        if hit := _exists(files, [f"{rel}.rs", f"{rel}/mod.rs"]):
            return hit
    return _rust_module_file(base, crate_root, files)


def _rust_is_internal(file: str, path: str, files: frozenset[str], crates: dict[str, str]) -> bool:
    head = next((p for p in path.split("::") if p), "")
    if head in ("crate", "self", "super") or head in crates:
        return True
    child = _join(_rust_self_dir(file), head)
    return _exists(files, [f"{child}.rs", f"{child}/mod.rs"]) is not None


def _rust_edges(
    file: str, line: int, source: str, files: frozenset[str], crates: dict[str, str]
) -> list[Edge]:
    m = _RUST.match(source)
    if not m:
        return []
    tree = m.group(1).split("//", 1)[0].strip()
    if tree.count("{") != tree.count("}") or not tree.endswith(";"):
        raw = tree.rstrip(";").strip()
        return [Edge(file, line, raw, None, unresolved=_unclosed(file, line, raw))]
    edges = []
    for p in _rust_expand(tree.rstrip(";")):
        target = _rust_resolve(file, p, files, crates)
        unresolved = None
        if target is None and _rust_is_internal(file, p, files, crates):
            unresolved = _unresolved_internal(p, file)
        edges.append(Edge(file, line, p, target, unresolved=unresolved))
    return edges


# ---------------------------------------------------------------------------
# Java, C#, C/C++
# ---------------------------------------------------------------------------

_JAVA = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*;")
# ``using X;`` / ``using static X.Y;`` / ``global using X;`` / ``using A = X.Y;``
# (a generic alias target ``X.Y<T>`` keeps ``X.Y``). ``using (...)`` statements
# and ``using var x = ...;`` declarations do not match.
_CS = re.compile(
    r"^\s*(?:global\s+)?using\s+(static\s+)?(?:(\w+)\s*=\s*)?"
    r"(?:global::)?([A-Za-z_][\w.]*)\s*(?:<[^;]*>)?\s*;"
)
_CPP = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]")


def _java_files_by_suffix(suffix: str, files: frozenset[str]) -> list[str]:
    return sorted(f for f in files if f == suffix or f.endswith("/" + suffix))


def _resolve_java(
    module: str, files: frozenset[str], ctx: ResolveContext | None = None
) -> str | None:
    """The one file of the class *module* names: the longest prefix naming a class
    (so a static member or a nested class resolves to the outer class file)."""
    packages = ctx.java_packages if ctx else {}
    parts = module.split(".")
    for k in range(len(parts), 0, -1):
        package, cls = ".".join(parts[: k - 1]), parts[k - 1]
        declared = [f for f in packages.get(package, []) if posixpath.basename(f) == f"{cls}.java"]
        hits = declared or _java_files_by_suffix("/".join(parts[:k]) + ".java", files)
        if len(hits) == 1:
            return hits[0]
    return None


def _java_package_files(package: str, files: frozenset[str], ctx: ResolveContext) -> list[str]:
    if declared := ctx.java_packages.get(package):
        return sorted(declared)
    rel = package.replace(".", "/")
    return sorted(
        f
        for f in files
        if f.endswith(".java") and (_dir_of(f) == rel or _dir_of(f).endswith("/" + rel))
    )


def _java_is_internal(module: str, ctx: ResolveContext) -> bool:
    """*module* shares its first two segments with a package an internal file declares."""
    head = ".".join(module.split(".")[:2])
    return any(p == head or p.startswith(head + ".") for p in ctx.java_packages)


def _java_edges(
    file: str, line: int, source: str, files: frozenset[str], ctx: ResolveContext
) -> list[Edge]:
    m = _JAVA.match(source)
    if not m:
        return []
    static, module, star = bool(m.group(1)), m.group(2), bool(m.group(3))
    raw = module + (".*" if star else "")
    if star and not static:
        if targets := _java_package_files(module, files, ctx):
            return [Edge(file, line, raw, t) for t in targets]
    # A static import names a member (or, with `.*`, every member) of a class.
    if target := _resolve_java(module, files, ctx):
        return [Edge(file, line, raw, target)]
    unresolved = _unresolved_internal(raw, file) if _java_is_internal(module, ctx) else None
    return [Edge(file, line, raw, None, unresolved=unresolved)]


def cs_namespaces(hits: list[dict[str, Any]]) -> dict[str, list[str]]:
    """namespace -> declaring files, from ``namespace X`` grep hits (in file order).

    An INDENTED declaration after an unindented block one in the same file is
    taken as nested (``namespace A { namespace B`` -> ``A.B`` as well as ``B``):
    a line-based approximation, since braces are not tracked.
    """
    out: dict[str, list[str]] = {}
    outer: dict[str, str] = {}
    for hit in sorted(hits, key=lambda h: (h["file"], h["line"])):
        m = _CS_NAMESPACE.match(hit["source"])
        if not m:
            continue
        file, name = hit["file"], m.group(2)
        names = [name]
        if m.group(1) and file in outer:
            names.append(f"{outer[file]}.{name}")
        elif not m.group(1):
            outer[file] = name
        for ns in names:
            files = out.setdefault(ns, [])
            if file not in files:
                files.append(file)
    return out


def cs_declarations(hits: list[dict[str, Any]]) -> dict[str, list[tuple[int, str]]]:
    """file -> its ``namespace X`` declarations ``(line, X)``, in file order."""
    out: dict[str, list[tuple[int, str]]] = {}
    for hit in sorted(hits, key=lambda h: (h["file"], h["line"])):
        if m := _CS_NAMESPACE.match(hit["source"]):
            out.setdefault(hit["file"], []).append((int(hit["line"]), m.group(2)))
    return out


CS_MULTI_NAMESPACE = "declares more than one C# namespace"


def cs_multi_namespace_files(ctx: ResolveContext) -> list[str]:
    """Files declaring more than one namespace: nesting is not tracked (by braces)."""
    return sorted(f for f, decls in ctx.cs_declarations.items() if len(decls) > 1)


def cs_nested_names(declarations: dict[str, list[tuple[int, str]]]) -> frozenset[str]:
    """Every name a multi-namespace file may declare by nesting (``A`` then ``B`` -> ``A.B``)."""
    out: set[str] = set()
    for decls in declarations.values():
        if len(decls) < 2:
            continue
        names = [n for _, n in decls]
        for i in range(len(names)):
            for j in range(i + 1, len(names) + 1):
                out.add(".".join(names[i:j]))
    return frozenset(out)


def _cs_is_internal(namespace: str, ctx: ResolveContext) -> bool:
    """*namespace* shares its first two segments with an internal namespace (like Java),
    or a multi-namespace file may declare it (or a namespace under it) by nesting."""
    head = ".".join(namespace.split(".")[:2])
    if any(n == head or n.startswith(head + ".") for n in ctx.cs_namespaces):
        return True
    return any(n == namespace or n.startswith(namespace + ".") for n in ctx.cs_nested)


def _cs_relative_edges(file: str, line: int, name: str, ctx: ResolveContext) -> list[Edge] | None:
    """``using X;`` placed after a ``namespace N`` declaration of *file*: X relative to N.

    C# resolves the namespace name of a using directive inside a namespace body
    against that namespace and its enclosing ones first. Innermost declared
    ``P.X`` -> its files; an internal-looking one -> unresolved; ``None`` when no
    relative reading names anything internal (the absolute reading applies).
    """
    enclosing = [n for ln, n in ctx.cs_declarations.get(file, []) if ln < line]
    if not enclosing:
        return None
    namespace = enclosing[-1]
    parts = namespace.split(".")
    for k in range(len(parts), 0, -1):
        candidate = ".".join([*parts[:k], name])
        if candidate in ctx.cs_namespaces:
            return [Edge(file, line, candidate, t) for t in sorted(ctx.cs_namespaces[candidate])]
        if any(n.startswith(candidate + ".") for n in ctx.cs_namespaces) or (
            candidate in ctx.cs_nested
        ):
            return [
                Edge(
                    file,
                    line,
                    name,
                    None,
                    unresolved=_unresolved_internal(f"{name} (relative: {candidate})", file),
                )
            ]
    return None


CS_GLOBAL_USING = "global using of internal namespace"


def _cs_global_using(name: str, file: str, line: int) -> str:
    return (
        f"{CS_GLOBAL_USING} '{name}' in {file}:{line}: it applies to every file of its "
        "project, which is not modelled"
    )


def _cs_edges(file: str, line: int, source: str, ctx: ResolveContext) -> list[Edge]:
    """``using`` -> every file declaring the named internal namespace.

    ``using X;`` names namespace X, so it is an edge to EVERY file declaring X
    (like a Java wildcard). ``using static X.T;`` / ``using A = X.T;`` name a
    type (or a namespace): the declaring files of X.T when X.T is a namespace,
    else of the longest declared prefix X, narrowed to ``T.cs`` when exactly
    one such file exists. A namespace that is only a PARENT of internal ones
    (``using Acme;`` with ``Acme.Core`` declared) imports no internal type: no
    edge, and not unresolved. Not declared but sharing its first two segments
    with an internal namespace: unresolved. Anything else is external.
    """
    m = _CS.match(source)
    if not m:
        return []
    static, alias, name = bool(m.group(1)), m.group(2), m.group(3)
    declared = ctx.cs_namespaces
    if re.match(r"\s*global\s", source) and (
        name in declared
        or _cs_is_internal(name, ctx)
        or any(n.startswith(name + ".") for n in declared)
    ):
        return [Edge(file, line, name, None, unresolved=_cs_global_using(name, file, line))]
    if not static and not alias and (relative := _cs_relative_edges(file, line, name, ctx)):
        return relative
    if name in declared:
        return [Edge(file, line, name, t) for t in sorted(declared[name])]
    if static or alias:
        parts = name.split(".")
        for k in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:k])
            if prefix in declared:
                files = sorted(declared[prefix])
                named = [f for f in files if posixpath.basename(f) == f"{parts[k]}.cs"]
                return [Edge(file, line, name, t) for t in (named if len(named) == 1 else files)]
    if any(n.startswith(name + ".") for n in declared):
        return [Edge(file, line, name, None)]
    unresolved = _unresolved_internal(name, file) if _cs_is_internal(name, ctx) else None
    return [Edge(file, line, name, None, unresolved=unresolved)]


# ---------------------------------------------------------------------------
# Fully-qualified references (Java, C#): a dependency with no import line
# ---------------------------------------------------------------------------

FQ_PREFIX = "fq:"
# Above this many internal names, the grep alternation uses their distinct
# first-two-segment roots instead and the hits are filtered here.
FQ_ALTERNATION_CAP = 200
# String / char literal contents and comments are blanked before matching, so
# a qualified name inside "..." or after // is not an edge.
_FQ_NOISE = re.compile(r'@"(?:""|[^"])*"|"(?:\\.|[^"\\])*"|' r"'(?:\\.|[^'\\])+'|/\*.*?\*/|//.*$")
# Whitespace may surround the dots (``com.acme.infra . Db``); it is removed.
_FQ_NAME = re.compile(r"(?<![\w.])(?:global::)?([A-Za-z_]\w*(?:\s*\.\s*[A-Za-z_]\w*)+)")
# Declarations whose names are NOT references (the import / using parsers own
# them). Blanked statement by statement, so code after one on the same line
# (``namespace A.Domain { class O { A.Infra.Db d; } }``) is still scanned. A
# generic alias target's type arguments (``using L = List<A.Infra.Db>;``) stay.
_FQ_DECLARATIONS = {
    "java": re.compile(
        r"(?<![\w.])(?:package|import)\s+(?:static\s+)?[A-Za-z_][\w.]*(?:\s*\.\s*\*)?(?=\s*;)"
    ),
    "c_sharp": re.compile(
        r"(?<![\w.])(?:(?:global\s+)?using\s+(?:static\s+)?(?:\w+\s*=\s*)?(?:global::)?"
        r"[A-Za-z_][\w.]*(?=\s*[<;])|namespace\s+[A-Za-z_][\w.]*)"
    ),
}
_GREP_DOT = r"[[:space:]]*\.[[:space:]]*"


def fq_grep_pattern(names: list[str]) -> str | None:
    """ERE prefilter: an internal Java package followed by ``.<identifier>``
    (a class name may be lower-case), whitespace allowed around every dot."""
    if not names:
        return None
    if len(names) > FQ_ALTERNATION_CAP:
        # A root continues with a sub-package or a class (``com.acme`` ->
        # ``com.acme.infra.Db``): any segment follows, ``fq_edges`` filters.
        names = sorted({".".join(n.split(".")[:2]) for n in names})
    alternation = "|".join(n.replace(".", _GREP_DOT) for n in sorted(names))
    return rf"({alternation}){_GREP_DOT}[A-Za-z_]"


def cs_fq_grep_pattern(names: list[str]) -> str | None:
    """ERE prefilter for C#: a declared namespace OR a partially qualified tail of
    one (``Infra`` / ``Domain.Infra`` of ``Acme.Domain.Infra``) followed by ``.<Upper>``.

    Above ``FQ_ALTERNATION_CAP`` alternatives it uses every namespace SEGMENT
    followed by any segment, above that again any dotted name followed by
    ``.<Upper>``; ``fq_edges`` filters the hits either way.
    """
    if not names:
        return None
    tails = set(names)
    for name in names:
        parts = name.split(".")
        tails.update(".".join(parts[k:]) for k in range(1, len(parts)))
    if len(tails) <= FQ_ALTERNATION_CAP:
        alternation = "|".join(t.replace(".", r"\.") for t in sorted(tails))
        return rf"({alternation})\.[A-Z]"
    segments = sorted({seg for name in names for seg in name.split(".")})
    if len(segments) <= FQ_ALTERNATION_CAP:
        return rf"({'|'.join(segments)})\.[A-Za-z_]"
    return r"[A-Za-z_][A-Za-z0-9_]*\.[A-Z]"


def cs_usings(hits: list[dict[str, Any]]) -> dict[str, list[tuple[int, str]]]:
    """file -> its plain ``using X;`` directives from the C# import grep hits."""
    out: dict[str, list[tuple[int, str]]] = {}
    for hit in hits:
        # The whole line too, so a lexical mis-split can never drop a directive.
        found: list[tuple[int, str]] = []
        for statement in [hit["source"], *split_statements("c_sharp", hit["source"])]:
            m = _CS.match(statement)
            if m and not m.group(1) and not m.group(2) and not re.match(r"\s*global\s", statement):
                if (entry := (int(hit["line"]), m.group(3))) not in found:
                    found.append(entry)
        if found:
            out.setdefault(hit["file"], []).extend(found)
    return out


def _qualify(prefix: str, name: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _cs_levels(file: str, line: int, ctx: ResolveContext) -> list[str]:
    """The namespaces enclosing *file*:*line*, innermost first, then the global one."""
    # ``<=``: a name on the declaring line itself (``namespace A { ... }``) is inside it.
    enclosing = [n for ln, n in ctx.cs_declarations.get(file, []) if ln <= line]
    parts = enclosing[-1].split(".") if enclosing else []
    return [".".join(parts[:k]) for k in range(len(parts), 0, -1)] + [""]


def _cs_bindings(
    file: str, line: int, name: str, absolute: bool, ctx: ResolveContext, spaces: frozenset[str]
) -> list[str]:
    """The full name(s) a (partially) qualified C# *name* may denote, C#-style.

    Its first segment is looked up as a namespace in the enclosing namespaces,
    innermost to outermost and then the global one: the first that has it
    binds (one candidate). Else through the file's ``using`` namespaces: every
    one that has it is a candidate (several = ambiguous). *spaces* is every
    declared namespace and every prefix of one. ``global::`` is absolute.
    """
    first = name.split(".")[0]
    levels = [""] if absolute else _cs_levels(file, line, ctx)
    for level in levels:
        if _qualify(level, first) in spaces:
            return [_qualify(level, name)]
    if absolute:
        return []
    found: list[str] = []
    for uline, using in ctx.cs_usings.get(file, []):
        # A using inside a namespace body is relative to it (``namespace Acme {
        # using Infra;`` -> ``Acme.Infra``) when that names a namespace.
        resolved = next(
            (
                _qualify(lv, using)
                for lv in _cs_levels(file, uline, ctx)
                if lv and _qualify(lv, using) in spaces
            ),
            using,
        )
        candidate = _qualify(resolved, name)
        if _qualify(resolved, first) in spaces and candidate not in found:
            found.append(candidate)
    return found


def _fq_declared_prefix(
    name: str, declared: dict[str, list[str]], any_case: bool = False
) -> str | None:
    """The longest declared package/namespace that *name* continues with
    ``.<Upper>`` (with *any_case*, ``.<identifier>``: a Java class may be lower-case;
    the declared package already makes it internal)."""
    parts = name.split(".")
    for k in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:k])
        if prefix in declared and (any_case or parts[k][:1].isupper()):
            return prefix
    return None


def fq_edges(
    family: str,
    hits: list[dict[str, Any]],
    files: frozenset[str],
    ctx: ResolveContext,
) -> list[Edge]:
    """Edges from fully-qualified references to INTERNAL types (no import needed).

    Java ``a.b.C`` resolves like ``import a.b.C;`` (the class file of the
    longest prefix naming a class); C# ``X.Y.T`` like ``using static X.Y.T;``
    (the files of the longest declared namespace, narrowed to ``T.cs`` when
    exactly one). A reference into the file's OWN package / namespace is no
    edge (same-package is not an import). The raw module is ``fq:<name>``.
    """
    declared = ctx.java_packages if family == "java" else ctx.cs_namespaces
    spaces = frozenset(
        ".".join(n.split(".")[:k]) for n in declared for k in range(1, n.count(".") + 2)
    )
    own: dict[str, set[str]] = {}
    for name, declaring in declared.items():
        for f in declaring:
            own.setdefault(f, set()).add(name)
    edges: list[Edge] = []
    for hit in hits:
        file, line, source = hit["file"], int(hit["line"]), hit["source"]
        clean = _FQ_NOISE.sub(" ", source)
        code = _FQ_DECLARATIONS[family].sub(" ", clean)
        seen: set[str] = set()  # one edge per target per line (``Db()`` and ``Db.Open()``)
        for d in _FQ_DECLARATIONS[family].finditer(clean):
            # An import / using AFTER another statement on its line that the
            # import grep does not see (``namespace A { using B;``): parsed here.
            # At the start of a line or after a ``;`` the import parser owns it.
            text = d.group(0)
            before = clean[: d.start()].rstrip()
            if not before or before.endswith(";") or text.startswith(("package", "namespace")):
                continue
            stmt = text + ";"
            for edge in (
                _java_edges(file, line, stmt, files, ctx)
                if family == "java"
                else _cs_edges(file, line, stmt, ctx)
            ):
                if (key := edge.target or edge.module) not in seen:
                    seen.add(key)
                    edges.append(edge)
        for m in _FQ_NAME.finditer(code):
            found: list[Edge]
            name = re.sub(r"\s+", "", m.group(1))
            if family == "java":
                prefix = _fq_declared_prefix(name, declared, any_case=True)
                if prefix is None or prefix in own.get(file, set()):
                    continue
                raw = FQ_PREFIX + name
                target = _resolve_java(name, files, ctx)
                unresolved = (
                    None if target else _unresolved_internal(f"{raw} ({file}:{line})", file)
                )
                found = [Edge(file, line, raw, target, unresolved=unresolved)]
            else:
                absolute = m.group(0).startswith("global::")
                found = _cs_fq_edges(file, line, name, absolute, ctx, spaces, own)
            for edge in found:
                key = edge.target or edge.module
                if key not in seen:
                    seen.add(key)
                    edges.append(edge)
    return edges


def _cs_fq_edges(
    file: str,
    line: int,
    name: str,
    absolute: bool,
    ctx: ResolveContext,
    spaces: frozenset[str],
    own: dict[str, set[str]],
) -> list[Edge]:
    """A C# (partially) qualified *name* -> edges, like ``using static <full>;``.

    Bound (``_cs_bindings``) to one full name with a declared namespace prefix
    followed by ``.<Upper>``: the files of that namespace, narrowed to
    ``<Type>.cs`` when exactly one; into the file's own namespace: no edge.
    Several using-based candidates that name an internal type: unresolved.
    """
    declared = ctx.cs_namespaces
    resolved: list[tuple[str, str]] = []
    for full in _cs_bindings(file, line, name, absolute, ctx, spaces):
        prefix = _fq_declared_prefix(full, declared)
        if prefix is not None:
            resolved.append((full, prefix))
    if len(resolved) > 1:
        names = ", ".join(full for full, _ in resolved)
        raw = FQ_PREFIX + name
        why = f"{raw} ({file}:{line}) is ambiguous through the file's usings: {names}"
        return [Edge(file, line, raw, None, unresolved=_unresolved_internal(why, file))]
    if not resolved or resolved[0][1] in own.get(file, set()):
        return []
    full, prefix = resolved[0]
    targets = sorted(declared[prefix])
    cls = full[len(prefix) + 1 :].split(".")[0]
    named = [f for f in targets if posixpath.basename(f) == f"{cls}.cs"]
    raw = FQ_PREFIX + full
    return [Edge(file, line, raw, t) for t in (named if len(named) == 1 else targets)]


def parse_edges(
    family: str,
    hits: list[dict[str, Any]],
    files: frozenset[str],
    context: ResolveContext | None = None,
    continued: Continuations | None = None,
) -> list[Edge]:
    """Import edges from ``grep -rn`` hits of one language family.

    *continued* replaces the source of a hit that opens a multi-line statement
    with the joined statement (``None``: not closed, so the import is unread).
    """
    ctx = context or ResolveContext()
    continued = continued or {}
    py_idx = python_index(files) if family == "python" else None
    crates = rust_crates(files, ctx.rust_crates) if family == "rust" else {}
    edges: list[Edge] = []
    for hit in hits:
        file, line, source = hit["file"], hit["line"], hit["source"]
        if (file, line) in continued:
            joined = continued[(file, line)]
            if joined is None:
                raw = source.strip()
                edges.append(Edge(file, line, raw, None, unresolved=_unclosed(file, line, raw)))
                continue
            source = joined
        if family == "python" and py_idx is not None:
            edges += _python_edges(file, line, source, files, py_idx)
        elif family == "javascript_typescript":
            edges += union_edges(
                [_js_edge(file, line, m.group(2), files, ctx) for m in _JS_SPEC.finditer(source)],
                [
                    _js_edge(file, line, m.group(2), files, ctx)
                    for statement in split_statements(family, source)
                    for m in _JS_SPEC.finditer(statement)
                ],
            )
            for m in _JS_CALL.finditer(source):
                if m.group(3) is None:
                    edges.append(_non_literal(file, line))
                else:
                    edges.append(_js_edge(file, line, m.group(3), files, ctx))
        elif family == "java":
            per_statement: list[Edge] = []
            for statement in split_statements(family, source):
                per_statement += _java_edges(file, line, statement, files, ctx)
            edges += union_edges(_java_edges(file, line, source, files, ctx), per_statement)
        elif family == "rust":
            per_statement: list[Edge] = []
            uses = 0
            for statement in split_statements(family, source):
                uses += bool(_RUST.match(statement))
                per_statement += _rust_edges(file, line, statement, files, crates)
            whole_rust = _rust_edges(file, line, source, files, crates)
            if uses and _rust_use_tokens(source) == uses:
                # Every `use` token on the line was read statement by statement, so
                # the whole-line reading of `use a; use b;` (one unread tree) adds
                # nothing. Any `use` the split did not read (after `{`/`}`, in a
                # line not understood) keeps the whole-line unread marker.
                whole_rust = [e for e in whole_rust if not (e.unresolved and ";" in e.module)]
            edges += union_edges(whole_rust, per_statement)
        elif family == "c_sharp":
            per_statement: list[Edge] = []
            for statement in split_statements(family, source):
                per_statement += _cs_edges(file, line, statement, ctx)
            edges += union_edges(_cs_edges(file, line, source, ctx), per_statement)
        elif family == "cpp":
            if m := _CPP.match(source):
                spec = m.group(2)
                target = None
                if m.group(1) == '"':
                    local = _join(_dir_of(file), spec)
                    target = _exists(files, [local, posixpath.normpath(spec)])
                edges.append(Edge(file, line, spec, target))
        elif family == "go":
            whole_go = (
                _go_edges(file, line, m.group(1), files, ctx) if (m := _GO.match(source)) else []
            )
            per_statement = []
            for k, statement in enumerate(split_statements(family, source)):
                statement = statement.rstrip().removesuffix(";")
                # After a `;` only an `import` statement is an import (not `x; "s"`).
                if k and not re.match(r"\s*import\s", statement):
                    continue
                if m := _GO.match(statement):
                    per_statement += _go_edges(file, line, m.group(1), files, ctx)
            edges += union_edges(whole_go, per_statement)
    return edges


# ---------------------------------------------------------------------------
# Matching and rules (pure)
# ---------------------------------------------------------------------------


def path_matches(path: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if fnmatch.fnmatchcase(path, pattern):
        return True
    prefix = pattern.rstrip("/")
    return bool(prefix) and path.startswith(prefix + "/")


def _raw_as_path(module: str) -> str:
    module = module.removeprefix(FQ_PREFIX)
    return module if "/" in module else module.replace("::", "/").replace(".", "/")


def target_matches(edge: Edge, pattern: str) -> bool:
    if edge.target is not None and path_matches(edge.target, pattern):
        return True
    return path_matches(_raw_as_path(edge.module), pattern)


def _violation(edge: Edge) -> dict[str, Any]:
    return {
        "file": edge.file,
        "line": edge.line,
        "imports": edge.module,
        "target": edge.target or _raw_as_path(edge.module),
    }


def _result(rule: str, violations: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "rule": rule,
        "status": "fail" if violations else "pass",
        "violations": violations[:VIOLATION_CAP],
        "violation_count": len(violations),
        "truncated": len(violations) > VIOLATION_CAP,
    }


def _not_computed(rule: str, reason: str) -> dict[str, Any]:
    return {"rule": rule, "status": "not_computed", "violations": [], "reason": reason}


def _unread(rule: str, edges: list[Edge]) -> dict[str, Any] | None:
    """``not_computed`` when an import of an in-scope file could not be read completely."""
    unread = [e.unresolved for e in edges if e.unresolved]
    if not unread:
        return None
    more = f" (+{len(unread) - 1} more)" if len(unread) > 1 else ""
    return _not_computed(rule, f"{unread[0]}{more}")


def _unsupported_importers(edges: list[Edge], ctx_go_files: set[str]) -> str | None:
    """Why the graph cannot see some importer's internal edges, or ``None``."""
    languages: dict[str, set[str]] = {}
    for edge in edges:
        family = family_of(edge.file)
        if family in UNRESOLVED_FAMILIES:
            languages.setdefault(family, set()).add(edge.file)
        elif family == "go" and edge.file not in ctx_go_files:
            languages.setdefault("go (outside any go.mod)", set()).add(edge.file)
    if not languages:
        return None
    parts = [f"{lang} ({len(fs)} importing file(s))" for lang, fs in sorted(languages.items())]
    return (
        "internal import resolution is not supported for "
        + ", ".join(parts)
        + " in scope, so their edges are invisible to the graph"
    )


def _unresolvable_scope(rule: str, in_scope: set[str], go_files: set[str]) -> dict[str, Any] | None:
    """``not_computed`` when a file in scope is in a language whose imports are not
    resolved to files: an import of an internal file there cannot be told from an
    external one, so the absence of a violation proves nothing."""
    unsupported = sorted(f for f in in_scope if family_of(f) in UNRESOLVED_FAMILIES)
    if unsupported:
        return _not_computed(
            rule,
            f"import resolution not supported ({len(unsupported)} file(s) in scope: "
            f"{unsupported[:3]})",
        )
    go = sorted(f for f in in_scope if f.endswith(".go") and f not in go_files)
    if go:
        return _not_computed(
            rule,
            f"Go import resolution needs a go.mod ({len(go)} Go file(s) in scope outside any "
            f"go.mod: {go[:3]})",
        )
    return None


def _no_import(
    rule: str, spec: str, edges: list[Edge], files: list[str], go_files: set[str]
) -> dict[str, Any]:
    if "->" not in spec:
        return _not_computed(rule, f"unparsed rule: {rule}")
    src, dst = (part.strip() for part in spec.split("->", 1))
    if not src or not dst:
        return _not_computed(rule, f"unparsed rule: {rule}")
    if not any(path_matches(f, src) for f in files):
        return _not_computed(rule, f"'{src}' matches no scanned source file")
    if not any(path_matches(f, dst) for f in files) and not any(
        target_matches(e, dst) for e in edges
    ):
        return _not_computed(
            rule, f"to_glob matches nothing: '{dst}' matches no scanned file and no import"
        )
    scoped = [e for e in edges if path_matches(e.file, src)]
    in_scope = {f for f in files if path_matches(f, src)} | {e.file for e in scoped}
    if refused := _unresolvable_scope(rule, in_scope, go_files):
        return refused
    if unread := _unread(rule, scoped):
        return unread
    return _result(rule, [_violation(e) for e in scoped if target_matches(e, dst)])


def _layer_of(path: str, layers: list[str]) -> int | None:
    return next((i for i, layer in enumerate(layers) if path_matches(path, layer)), None)


def _layers(
    rule: str, spec: str, edges: list[Edge], files: list[str], go_files: set[str]
) -> dict[str, Any]:
    layers = [part.strip() for part in spec.split(">")]
    if len(layers) < 2 or not all(layers):
        return _not_computed(rule, f"unparsed rule: {rule}")
    empty = [layer for layer in layers if not any(path_matches(f, layer) for f in files)]
    if empty:
        return _not_computed(rule, f"layer glob {empty} matches nothing (no scanned file)")
    in_scope = {f for f in files if _layer_of(f, layers) is not None} | {
        e.file for e in edges if _layer_of(e.file, layers) is not None
    }
    if refused := _unresolvable_scope(rule, in_scope, go_files):
        return refused
    if unread := _unread(rule, [e for e in edges if _layer_of(e.file, layers) is not None]):
        return unread
    violations = []
    for edge in edges:
        low = _layer_of(edge.file, layers)
        if low is None:
            continue
        high = None
        if edge.target is not None:
            high = _layer_of(edge.target, layers)
        if high is None:
            high = _layer_of(_raw_as_path(edge.module), layers)
        if high is not None and high < low:
            violations.append(_violation(edge))
    return _result(rule, violations)


def _internal_graph(edges: list[Edge], glob: str | None) -> dict[str, dict[str, Edge]]:
    graph: dict[str, dict[str, Edge]] = {}
    for edge in edges:
        # A leaf (asset, namespace dir) runs no code; a self-import is not a cycle.
        if edge.target is None or edge.leaf or edge.target == edge.file:
            continue
        if glob and not (path_matches(edge.file, glob) and path_matches(edge.target, glob)):
            continue
        graph.setdefault(edge.file, {}).setdefault(edge.target, edge)
    return graph


def _strongly_connected(graph: dict[str, dict[str, Edge]]) -> list[list[str]]:
    """Tarjan, iterative. Components of size > 1, or with a self-loop."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    nodes = set(graph) | {t for targets in graph.values() for t in targets}
    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(graph.get(root, {})))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            if children:
                child = children.pop(0)
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(graph.get(child, {}))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in graph.get(node, {}):
                    out.append(sorted(component))
    return out


def _graph_evidence(
    rule: str, glob: str | None, edges: list[Edge], go_files: set[str], has_internal: bool
) -> dict[str, Any] | None:
    """``not_computed`` for a graph rule that has no evidence or cannot see some edges."""
    scoped = [e for e in edges if not glob or path_matches(e.file, glob)]
    if reason := _unsupported_importers(scoped, go_files):
        return _not_computed(rule, reason)
    if unread := _unread(rule, scoped):
        return unread
    if not has_internal:
        return _not_computed(
            rule, "no resolved internal import edge in scope, so there is nothing to check"
        )
    return None


def _no_cycles(
    rule: str, glob: str | None, edges: list[Edge], files: list[str], go_files: set[str]
) -> dict[str, Any]:
    if glob is not None and not glob:
        return _not_computed(rule, f"unparsed rule: {rule}")
    if glob and not any(path_matches(f, glob) for f in files):
        return _not_computed(rule, f"'{glob}' matches no scanned source file")
    graph = _internal_graph(edges, glob)
    if refused := _graph_evidence(rule, glob, edges, go_files, bool(graph)):
        return refused
    violations = []
    for component in _strongly_connected(graph):
        members = set(component)
        edge = next(
            e for f in component for t, e in sorted(graph.get(f, {}).items()) if t in members
        )
        violations.append({**_violation(edge), "cycle": component})
    return _result(rule, violations)


def _max_fan_out(
    rule: str, spec: str, edges: list[Edge], files: list[str], go_files: set[str]
) -> dict[str, Any]:
    limit_text, has_glob, glob = spec.partition(":")
    if not limit_text.strip().isdigit() or (has_glob and not glob.strip()):
        return _not_computed(rule, f"unparsed rule: {rule}")
    limit = int(limit_text.strip())
    glob = glob.strip() or None
    if glob and not any(path_matches(f, glob) for f in files):
        return _not_computed(rule, f"'{glob}' matches no scanned source file")
    fan: dict[str, set[str]] = {}
    for edge in edges:
        if edge.target is None or edge.leaf or edge.target == edge.file:
            continue
        if glob and not path_matches(edge.file, glob):
            continue
        fan.setdefault(edge.file, set()).add(edge.target)
    if refused := _graph_evidence(rule, glob, edges, go_files, bool(fan)):
        return refused
    violations = [
        {"file": f, "line": None, "imports": len(t), "target": sorted(t)}
        for f, t in sorted(fan.items())
        if len(t) > limit
    ]
    return _result(rule, violations)


def evaluate_rule(
    rule: str, edges: list[Edge], files: list[str], go_files: set[str] | None = None
) -> dict[str, Any]:
    """One rule's result. *go_files*: the ``.go`` files under some ``go.mod``.

    When *go_files* is ``None`` every Go file counts as under a ``go.mod``.
    """
    if go_files is None:
        go_files = {f for f in files if f.endswith(".go")} | {
            e.file for e in edges if e.file.endswith(".go")
        }
    text = rule.strip()
    head, sep, rest = text.partition(":")
    head = head.strip()
    if head == "no_import" and sep:
        return _no_import(rule, rest, edges, files, go_files)
    if head == "layers" and sep:
        return _layers(rule, rest, edges, files, go_files)
    if head == "no_cycles":
        return _no_cycles(rule, rest.strip() if sep else None, edges, files, go_files)
    if head == "max_fan_out" and sep:
        return _max_fan_out(rule, rest, edges, files, go_files)
    return _not_computed(rule, f"unparsed rule: {rule}")


# ---------------------------------------------------------------------------
# The workspace part
# ---------------------------------------------------------------------------


async def _read(
    container_name: str, path: str, tenant_id: str | None, stats: ScanStats | None = None
) -> str | None:
    if stats is not None:
        stats.exec_calls += 1
    try:
        return await read_workspace_file_checked(container_name, path, tenant_id)
    except (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError) as e:
        raise GraphNotComputedError(f"'{path}' could not be read: {e}") from e


async def _load_ts_config(
    container_name: str, path: str, tenant_id: str | None, stats: ScanStats | None = None
) -> TsConfig | None:
    """The resolution options of the config at *path*, following a relative ``extends``."""
    chain: list[tuple[str, dict[str, Any]]] = []
    current: str | None = path
    while current is not None and len(chain) < EXTENDS_DEPTH:
        text = await _read(container_name, current, tenant_id, stats)
        if text is None:
            if current == path:
                return None
            raise GraphNotComputedError(f"'{current}' (extended by '{path}') does not exist")
        try:
            data = parse_jsonc(text)
        except ValueError as e:
            raise GraphNotComputedError(f"'{current}' is not parseable JSON: {e}") from e
        if not isinstance(data, dict):
            raise GraphNotComputedError(f"'{current}' is not a JSON object")
        chain.append((current, data))
        extends = data.get("extends")
        current = None
        if isinstance(extends, str) and extends.startswith("."):
            target = _join(_dir_of(chain[-1][0]), extends)
            current = target if target.endswith(".json") else f"{target}.json"
    config = TsConfig(paths_base=_dir_of(path))
    for config_path, data in reversed(chain):  # base first, the extending file wins
        options = data.get("compilerOptions")
        if not isinstance(options, dict):
            continue
        directory = _dir_of(config_path)
        base_url = options.get("baseUrl")
        if isinstance(base_url, str):
            config.base_url = _join(directory, base_url)
            config.paths_base = config.base_url
        paths = options.get("paths")
        if isinstance(paths, dict):
            config.paths = {
                str(k): [str(t) for t in v if isinstance(t, str)]
                for k, v in paths.items()
                if isinstance(v, list)
            }
            if not isinstance(base_url, str):
                config.paths_base = config.base_url or directory
    return config


async def _resolve_context(
    container_name: str, configs: list[str], tenant_id: str | None, stats: ScanStats | None = None
) -> ResolveContext:
    cargo = [c for c in configs if posixpath.basename(c) == CARGO_TOML]
    others = [c for c in configs if posixpath.basename(c) != CARGO_TOML]
    if len(others) > CONFIG_CAP:
        raise GraphNotComputedError(
            f"{len(others)} go.mod/tsconfig/jsconfig files exceed the {CONFIG_CAP}-file cap"
        )
    if len(cargo) > CONFIG_CAP:
        raise GraphNotComputedError(
            f"{len(cargo)} Cargo.toml files exceed the {CONFIG_CAP}-file cap"
        )
    ctx = ResolveContext()
    for path in cargo:
        text = await _read(container_name, path, tenant_id, stats)
        if text is None:
            continue
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            raise GraphNotComputedError(f"'{path}' is not parseable TOML: {e}") from e
        src = _join(_dir_of(path), "src")
        for section in ("package", "lib"):
            table = data.get(section)
            name = table.get("name") if isinstance(table, dict) else None
            if isinstance(name, str) and name:
                ctx.rust_crates[name.replace("-", "_")] = src
    for path in others:
        directory = _dir_of(path)
        if posixpath.basename(path) == GO_MOD:
            text = await _read(container_name, path, tenant_id, stats)
            if text is None:
                continue
            if m := _GO_MODULE.search(text):
                ctx.go_modules[directory] = m.group(1)
            else:
                raise GraphNotComputedError(f"'{path}' has no module directive")
        elif (config := await _load_ts_config(container_name, path, tenant_id, stats)) is not None:
            # tsconfig.json wins over jsconfig.json in the same directory.
            if directory not in ctx.ts_configs or path.endswith("tsconfig.json"):
                ctx.ts_configs[directory] = config
    return ctx


async def _workspace_packages(
    container_name: str,
    manifests: list[str],
    files: list[str],
    tenant_id: str | None,
    stats: ScanStats | None = None,
) -> dict[str, tuple[str, dict[str, Any]]]:
    """``name`` -> (dir, package.json) of every listed package.json (cap ``CONFIG_CAP``).

    The root package.json's name is skipped when the root has no JS/TS source of
    its own (every JS/TS file belongs to a nested package): it names the
    monorepo, not an importable package.
    """
    if len(manifests) > CONFIG_CAP:
        raise GraphNotComputedError(
            f"{len(manifests)} package.json files exceed the {CONFIG_CAP}-file cap"
        )
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in manifests:
        text = await _read(container_name, path, tenant_id, stats)
        if text is None:
            continue
        try:
            data = json.loads(text)
        except ValueError as e:
            raise GraphNotComputedError(f"'{path}' is not parseable JSON: {e}") from e
        name = data.get("name") if isinstance(data, dict) else None
        if isinstance(name, str) and name and isinstance(data, dict):
            found.setdefault(name, (_dir_of(path), data))
    package_dirs = [d for d, _ in found.values() if d]
    root_owns_source = any(
        family_of(f) == "javascript_typescript" and not any(_under(f, d) for d in package_dirs)
        for f in files
    )
    return {name: entry for name, entry in found.items() if entry[0] or root_owns_source}


class ScanBudgetExceededError(GraphNotComputedError):
    """The sharded scan ran out of exec calls or wall-clock time."""


@dataclass
class ScanStats:
    """What the scan cost and whether it finished (``scan`` in the response)."""

    files: int = 0
    exec_calls: int = 0  # every exec the scan made (searches, splits and config reads)
    shards: int = 0  # search units that completed (a directory or an explicit file chunk)
    complete: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "exec_calls": self.exec_calls,
            "shards": self.shards,
            "complete": self.complete,
        }


def scope_root(scope: str | None) -> str:
    """The literal directory prefix of a *scope* glob (``""``: the workspace root).

    ``src/api/**/*.py`` -> ``src/api``; ``src/*`` -> ``src``; ``*.py`` -> ``""``; a
    pattern without glob characters is its own prefix (a directory or a file).
    """
    if not scope:
        return ""
    text = scope.strip().removeprefix("./").strip("/")
    cut = min((i for i in (text.find(c) for c in "*?[") if i >= 0), default=-1)
    if cut < 0:
        literal = text
    else:
        head = text[:cut]
        literal = head.rsplit("/", 1)[0] if "/" in head else ""
    try:
        return validate_relative_path(literal) if literal else ""
    except ValueError:
        return ""


def _at_or_under(path: str, root: str) -> bool:
    return not root or path == root or path.startswith(root + "/")


class _Scanner:
    """Directory-sharded ``find`` / ``grep`` with an exec-call and wall-clock budget.

    Every search first runs over its whole directory. Only when that output is
    CLIPPED (the containers' exec output budget) is the directory split: one
    ``find -maxdepth 1 -type d`` for its children (vendored dirs dropped), the
    files directly in it searched on their own (``find -maxdepth 1`` for the
    listing; explicit-file ``grep`` in chunks of ``GREP_FILE_CHUNK``, halved
    again while a chunk is clipped), and each child searched recursively. A
    clipped single file, a clipped child listing, a failed call or an
    exhausted budget raises ``GraphNotComputedError``: a truncated shard never
    counts as complete. A non-clip failure (non-zero exit, 124 timeout) is not
    split -- it is not computed, as before.
    """

    def __init__(self, container_name: str, tenant_id: str | None, stats: ScanStats) -> None:
        self.container_name = container_name
        self.tenant_id = tenant_id
        self.stats = stats
        self.search_calls = 0
        self.started = time.monotonic()
        self.children: dict[str, list[str]] = {}
        self.files: list[str] = []  # the listed source files (what a grep split works from)

    def _spend(self, what: str) -> None:
        if self.search_calls >= EXEC_CALL_BUDGET:
            raise ScanBudgetExceededError(
                f"the scan used its {EXEC_CALL_BUDGET}-exec-call budget before {what} finished "
                "(the workspace is too large to shard within it)"
            )
        if time.monotonic() - self.started > SCAN_SECONDS_BUDGET:
            raise ScanBudgetExceededError(
                f"the scan exceeded its {SCAN_SECONDS_BUDGET:.0f}s wall-clock budget before "
                f"{what} finished"
            )
        self.search_calls += 1
        self.stats.exec_calls += 1

    async def _call(self, what: str, factory: Any) -> Any:
        """Run one search; ``None`` when its output was clipped (split it)."""
        self._spend(what)
        try:
            result = await factory()
        except WorkspaceSearchIncompleteError:
            return None
        except WorkspaceBackendUnavailableError as e:
            raise GraphNotComputedError(f"{what} could not finish: {e}") from e
        self.stats.shards += 1
        return result

    async def _children(self, root: str, what: str) -> list[str]:
        if root not in self.children:
            dirs = await self._call(
                what,
                lambda: list_child_dirs(self.container_name, root, self.tenant_id),
            )
            if dirs is None:
                raise GraphNotComputedError(
                    f"{what} could not finish: the directory listing of '{root or '.'}' was "
                    "clipped at the exec output budget, so it cannot be split"
                )
            self.children[root] = dirs
        return self.children[root]

    async def list_files(self, extensions: tuple[str, ...], what: str, root: str = "") -> list[str]:
        found = await self._call(
            what,
            lambda: list_source_files(self.container_name, extensions, self.tenant_id, root),
        )
        if found is not None:
            return found
        here = await self._call(
            what,
            lambda: list_source_files(
                self.container_name, extensions, self.tenant_id, root, maxdepth=1
            ),
        )
        if here is None:
            raise GraphNotComputedError(
                f"{what} could not finish: the files directly in '{root or '.'}' alone exceed "
                "the exec output budget"
            )
        out = set(here)
        for child in await self._children(root, what):
            out.update(await self.list_files(extensions, what, child))
        return sorted(out)

    async def grep(
        self,
        pattern: str,
        extensions: tuple[str, ...],
        what: str,
        root: str = "",
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        relevant = [f for f in self.files if f.lower().endswith(extensions)]
        if not any(_at_or_under(f, root) for f in relevant):
            return []
        hits = await self._call(what, lambda: self._grep(pattern, extensions, root, None, after))
        if hits is not None:
            return hits
        if root in relevant:  # the shard IS one file (a scope naming a file)
            raise GraphNotComputedError(
                f"{what} could not finish: the matches in '{root}' alone exceed the exec "
                "output budget"
            )
        here = [f for f in relevant if _dir_of(f) == root]
        out: list[dict[str, Any]] = []
        for i in range(0, len(here), GREP_FILE_CHUNK):
            out += await self._grep_chunk(
                pattern, extensions, here[i : i + GREP_FILE_CHUNK], what, after
            )
        for child in await self._children(root, what):
            out += await self.grep(pattern, extensions, what, child, after)
        return out

    async def _grep_chunk(
        self,
        pattern: str,
        extensions: tuple[str, ...],
        files: list[str],
        what: str,
        after: int | None,
    ) -> list[dict[str, Any]]:
        hits = await self._call(what, lambda: self._grep(pattern, extensions, "", files, after))
        if hits is not None:
            return hits
        if len(files) == 1:
            raise GraphNotComputedError(
                f"{what} could not finish: the matches in '{files[0]}' alone exceed the exec "
                "output budget"
            )
        mid = len(files) // 2
        return await self._grep_chunk(pattern, extensions, files[:mid], what, after) + (
            await self._grep_chunk(pattern, extensions, files[mid:], what, after)
        )

    async def _grep(
        self,
        pattern: str,
        extensions: tuple[str, ...],
        root: str,
        files: list[str] | None,
        after: int | None,
    ) -> list[dict[str, Any]]:
        if after is None:
            return await grep_lines(
                self.container_name, pattern, extensions, self.tenant_id, root, files=files
            )
        return await grep_blocks(
            self.container_name, pattern, extensions, self.tenant_id, after, root, files=files
        )


async def build_import_graph(
    container_name: str,
    tenant_id: str | None,
    scope: str | None = None,
    stats: ScanStats | None = None,
) -> tuple[list[str], list[str], list[Edge], set[str]]:
    """``(files, languages_scanned, edges, go_files_under_a_go_mod)`` from the workspace.

    Raises ``GraphNotComputedError`` on any incomplete search, the file cap or
    an exhausted scan budget. ``ContainerNotResolvableError`` propagates (the
    router maps it to 404). *stats* is filled as the scan goes.
    """
    stats = stats if stats is not None else ScanStats()
    scan = _Scanner(container_name, tenant_id, stats)
    listed = await scan.list_files(
        ALL_EXTENSIONS + CONFIG_NAMES + (PACKAGE_JSON,), "the source-file listing"
    )
    configs = [f for f in listed if posixpath.basename(f) in CONFIG_NAMES]
    files = [f for f in listed if f.lower().endswith(ALL_EXTENSIONS)]
    stats.files = len(files)
    scan.files = files
    if len(files) > FILE_CAP:
        raise GraphNotComputedError(
            f"{len(files)} source files exceed the {FILE_CAP}-file import-graph cap"
        )
    ctx = await _resolve_context(container_name, configs, tenant_id, stats)
    if any(family_of(f) == "javascript_typescript" for f in files):
        manifests = [f for f in listed if posixpath.basename(f) == PACKAGE_JSON]
        ctx.packages = await _workspace_packages(container_name, manifests, files, tenant_id, stats)
        try:
            ctx.assets = frozenset(await scan.list_files(ASSET_EXTENSIONS, "the asset listing"))
        except ScanBudgetExceededError:
            raise
        except GraphNotComputedError:
            ctx.assets = None  # asset imports become unresolved, never external
    java_exts = FAMILIES["java"][0]
    if any(f.lower().endswith(java_exts) for f in files):
        declared = await scan.grep(JAVA_PACKAGE_PATTERN, java_exts, "the Java package search")
        for hit in declared:
            if m := _JAVA_PACKAGE.match(hit["source"]):
                ctx.java_packages.setdefault(m.group(1), []).append(hit["file"])
    cs_exts = FAMILIES["c_sharp"][0]
    if any(f.lower().endswith(cs_exts) for f in files):
        declared = await scan.grep(CS_NAMESPACE_PATTERN, cs_exts, "the C# namespace search")
        ctx.cs_namespaces = cs_namespaces(declared)
        ctx.cs_declarations = cs_declarations(declared)
        ctx.cs_nested = cs_nested_names(ctx.cs_declarations)
    # Imports are only needed from the files in scope; resolution (above) needs all.
    root = scope_root(scope)
    file_set = frozenset(files)
    edges: list[Edge] = []
    languages: list[str] = []
    for family, (extensions, pattern) in FAMILIES.items():
        if not any(f.lower().endswith(extensions) for f in files):
            continue
        hits = await scan.grep(pattern, extensions, f"the {family} import search", root)
        continued: Continuations = {}
        if family in CONTINUATIONS:
            blocks = await scan.grep(
                CONTINUATIONS[family][0],
                extensions,
                f"the {family} multi-line import search",
                root,
                after=BLOCK_CONTEXT,
            )
            continued = join_continuations(family, blocks)
        languages.append(family)
        if family == "c_sharp":
            ctx.cs_usings = cs_usings(hits)
        edges += parse_edges(family, hits, file_set, ctx, continued)
    # A fully-qualified reference to an internal type needs no import line: one
    # more sharded grep per family for the internal names followed by `.<Type>`.
    for family, declared_names in (("java", ctx.java_packages), ("c_sharp", ctx.cs_namespaces)):
        extensions = FAMILIES[family][0]
        make = fq_grep_pattern if family == "java" else cs_fq_grep_pattern
        pattern = make(sorted(declared_names))
        if pattern is None or not any(f.lower().endswith(extensions) for f in files):
            continue
        hits = await scan.grep(
            pattern, extensions, f"the {family} fully-qualified reference search", root
        )
        edges += fq_edges(family, hits, file_set, ctx)
    # A C# `global using` of an internal namespace is an import of EVERY file of
    # its project; projects are not modelled, so every C# file's rules abstain.
    global_usings = [e for e in edges if (e.unresolved or "").startswith(CS_GLOBAL_USING)]
    if global_usings:
        first = global_usings[0]
        edges += [
            Edge(f, 0, first.module, None, unresolved=first.unresolved)
            for f in files
            if family_of(f) == "c_sharp" and f != first.file
        ]
    # A file declaring several namespaces may nest them (braces are not tracked):
    # its usings may be relative to a namespace not modelled, so it abstains.
    edges += [
        Edge(f, 0, "namespace", None, unresolved=f"{f} {CS_MULTI_NAMESPACE} (nesting not tracked)")
        for f in cs_multi_namespace_files(ctx)
        if f in files
    ]
    go_files = {f for f in files if f.endswith(".go") and _nearest(ctx.go_modules, f) is not None}
    if scope:
        files = [f for f in files if path_matches(f, scope)]
        edges = [e for e in edges if path_matches(e.file, scope)]
    stats.complete = True
    return files, languages, edges, go_files


RESOLUTION_NOTE = (
    "Imports are read from import lines by pattern. A literal dynamic import (JS/TS "
    "`import('x')` / `require('x')`, Python `import_module('x')` / `__import__('x')`) is "
    "an edge; a non-literal one makes the rules over its file not computed. Python imports "
    "inside multi-line strings are not modelled. A multi-line "
    f"Python `from X import (...)` or Rust `use` is read up to {BLOCK_CONTEXT} lines; one "
    "not closed within that is unread and makes the rules over its file not computed. "
    "Internal resolution: Python (package roots, namespace packages), JS/TS incl. .mts/.cts "
    "and .vue/.svelte/.astro (relative, tsconfig/jsconfig paths and baseUrl, workspace "
    "package.json names, non-code assets as leaves), Go (go.mod module "
    "path), Rust (crate/super/self/child-module and workspace-crate paths), Java (declared "
    "packages; single-type, static and wildcard imports), C# (declared namespaces) and "
    "quoted C/C++ includes. An "
    "import that names an internal package but resolves to no file makes the rules over "
    "its file not computed. C# `using` resolves through the declared namespaces (a using "
    "names every file of that namespace). A Java / C# fully-qualified reference to an "
    "internal type without an import (`new com.acme.infra.Db()`) is an edge too (raw "
    "module `fq:<name>`); one into the file's own package / namespace is not. A C# "
    "partially qualified name binds through the enclosing namespaces (innermost first), then "
    "the file's usings (several readings: not computed). Large workspaces are scanned in directory shards (see `scan`). "
    "The verdict is valid only when EVERY rule passes."
)


async def evaluate_architecture(
    container_name: str, rules: list[str], tenant_id: str | None, scope: str | None = None
) -> dict[str, Any]:
    """The ``validation_results`` body of ``POST /api/validation/architecture``."""
    notes: list[str] = []
    stats = ScanStats()
    try:
        files, languages, edges, go_files = await build_import_graph(
            container_name, tenant_id, scope, stats
        )
    except GraphNotComputedError as e:
        stats.complete = False
        reason = f"Import graph not computed: {e}."
        return {
            "verdict": "not_computed",
            "rules": [_not_computed(rule, reason) for rule in rules],
            "languages_scanned": [],
            "files_scanned": 0,
            "edges": 0,
            "excluded_dirs": list(VENDOR_EXCLUDE_DIRS),
            "scan": stats.as_dict(),
            "notes": [reason],
        }
    results = [evaluate_rule(rule, edges, files, go_files) for rule in rules]
    statuses = [r["status"] for r in results]
    # "valid" is a claim about EVERY rule: one rule not computed is not a pass.
    if "fail" in statuses:
        verdict = "invalid"
    elif all(status == "pass" for status in statuses):
        verdict = "valid"
    else:
        verdict = "not_computed"
    uncomputed = [r["rule"] for r in results if r["status"] == "not_computed"]
    if uncomputed:
        notes.append(f"Not computed: {uncomputed}")
    if not files:
        notes.append("No source file was found in the workspace.")
    notes.append(RESOLUTION_NOTE)
    return {
        "verdict": verdict,
        "rules": results,
        "languages_scanned": languages,
        "files_scanned": len(files),
        "edges": len(edges),
        "excluded_dirs": list(VENDOR_EXCLUDE_DIRS),
        "scan": stats.as_dict(),
        "notes": notes,
    }


__all__ = [
    "ContainerNotResolvableError",
    "evaluate_architecture",
    "evaluate_rule",
    "parse_edges",
    "parse_jsonc",
]
