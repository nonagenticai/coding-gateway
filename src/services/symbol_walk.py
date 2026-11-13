"""Resolve a ``FileMatch.pending`` follow-up: inherited members and re-exports.

``services.symbol_index.match_symbol`` answers from ONE file. When that answer
depends on another file of the same workspace -- the owner type's bases, a
Rust trait's default methods, a barrel's re-export -- it returns
``valid=None`` with ``pending`` set, and this module follows it. The verdict
discipline is the one in ``symbol_index``'s docstring:

- ``True``  only when a definition of the claimed kind is actually found on a
  base / at the end of the re-export chain;
- ``False`` only when EVERY hop was resolved inside the workspace, every file
  was parsed cleanly, the member / name is absent everywhere, and no
  ``blocker`` (a language or file reason absence cannot be proven) applies;
- ``None``  otherwise, with a note naming what could not be resolved.

Bounds: ``MAX_BASE_HOPS`` levels of bases, ``MAX_REEXPORT_HOPS`` re-export
hops, ``MAX_WALK_FILES`` files read per walk, ``MAX_BASE_CANDIDATES`` candidate
files per base-name search. Reaching a bound is ``None``, never ``False``.

LOCATING A BASE by name -- only with PROOF that the name as written in the
origin file binds to that file; never "the one workspace file that defines a
type so named". Same-file definition is proof. Otherwise, per language:

- JS/TS: the relative module it is imported from (by its own name, not ``as``;
  a default import only of ``export default class Name``), re-exports
  followed. Not imported -> ``None``.
- Java: a single-type import ``a.b.Base`` (a file declaring ``package a.b``);
  else the same package; else -- unless the name is a ``java.lang`` type --
  the ONE wildcard import's internal package. Two wildcards -> ``None``.
- C#: C# lookup order -- a file of the same namespace, else of each ENCLOSING
  namespace outward (``App.Web`` -> ``App`` -> global; a level under a platform
  root such as ``System`` / ``Microsoft`` may hold an external namesake, so it
  is never skipped), and only then the namespaces named by ``using`` when EVERY
  using is internal, none is ``static`` / an alias / inside a namespace, and
  no ``global using`` / implicit usings exist in the workspace. Two
  definitions at one level, or a file declaring several namespaces -> ``None``.

IMPLICIT ROOT TYPES: a member no workspace type declares is ``True`` when the
language gives it to every such type (``java.lang.Object`` / ``Enum`` / record
accessors, ``System.Object``, JS ``Object.prototype`` once every base resolved);
see ``_implicit_root_member``. Python's ``object`` is handled by the router.
- Go: a non-test file of the same package directory; a dot import -> ``None``.
- Rust: a ``use`` of the name (no alias) or a written path, whose head is
  ``crate`` / ``self`` / ``super`` or a ``mod`` declared in the file;
  external crates -> ``None``.
- C/C++: a header included with quotes next to the file, defining the class
  (not only forward-declared) outside any namespace; any ``#include <...>``
  -> ``None``.

Other qualified bases (``pkg.Base``, ``ns::Base``) are not resolved; a name
bound in more than one candidate file is ambiguous. Each is ``None``.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from services.architecture_rules import (
    _join,
    _python_package_dir,
    _rust_crate_root,
    _rust_self_dir,
    js_path_candidates,
)
from services.container_workspace import (
    WorkspaceBackendUnavailableError,
    WorkspaceSearchIncompleteError,
)
from services.dotted_names import module_file_candidates
from services.symbol_index import (
    CLASS_KINDS,
    FileMatch,
    Inherit,
    Reexport,
    SymbolsFetch,
    match_symbol,
    simple_type_name,
    symbol_language,
)

MAX_BASE_HOPS = 5
MAX_REEXPORT_HOPS = 3
MAX_WALK_FILES = 30
MAX_BASE_CANDIDATES = 20

_JS = frozenset({"javascript", "typescript"})


@dataclass
class WalkOps:
    """The workspace operations a walk needs (bound to one container + tenant)."""

    # Whole file text; ``None`` only for a confirmed absence. Raises the
    # container_workspace errors for an incomplete read.
    read: Callable[[str], Awaitable[str | None]]
    symbols: Callable[[str, str], Awaitable[SymbolsFetch]]  # (code, path)
    mentions: Callable[[str], Awaitable[list[str]]]  # files containing the word
    existing: Callable[[list[str]], Awaitable[set[str]]]  # which candidate paths exist
    # ``grep -rnE <pattern>`` over files with the extensions: ``[{file, line, source}]``.
    # ``None``: not available -- whatever needs it stays undecided.
    lines: Callable[[str, tuple[str, ...]], Awaitable[list[dict[str, Any]]]] | None = None
    budget: int = MAX_WALK_FILES
    loaded: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)


class UnresolvedHopError(Exception):
    """One hop of a walk could not be resolved inside the workspace."""


_WORKSPACE_ERRORS = (WorkspaceBackendUnavailableError, WorkspaceSearchIncompleteError)


async def load(ops: WalkOps, path: str) -> tuple[str, dict[str, Any]]:
    """``(content, symbols body)`` of *path*, or ``UnresolvedHopError``."""
    if path in ops.loaded:
        return ops.loaded[path]
    if len(ops.loaded) >= ops.budget:
        raise UnresolvedHopError(
            f"the walk read its {ops.budget}-file budget before reaching {path}"
        )
    try:
        content = await ops.read(path)
    except (*_WORKSPACE_ERRORS, ValueError) as e:
        raise UnresolvedHopError(f"{path} could not be read completely ({e})") from e
    if content is None:
        raise UnresolvedHopError(f"{path} does not exist")
    fetched = await ops.symbols(content, path)
    if fetched.body is None:
        raise UnresolvedHopError(f"symbol extraction for {path} failed ({fetched.error})")
    ops.loaded[path] = (content, fetched.body)
    return content, fetched.body


async def _first_existing(ops: WalkOps, candidates: list[str], why: str) -> str:
    if not candidates:
        raise UnresolvedHopError(why)
    try:
        present = await ops.existing(candidates)
    except (*_WORKSPACE_ERRORS, ValueError) as e:
        raise UnresolvedHopError(f"the file lookup for {why} could not finish ({e})") from e
    hit = next((c for c in candidates if c in present), None)
    if hit is None:
        raise UnresolvedHopError(why)
    return hit


def python_module_candidates(importer: str, module: str) -> list[str] | None:
    """Files a Python ``from <module> import`` in *importer* may name (``None``: above root)."""
    rest = module.lstrip(".")
    level = len(module) - len(rest)
    rel = rest.replace(".", "/")
    if not level:
        return module_file_candidates(rest) if rest else []
    base = _python_package_dir(importer, level)
    if base is None:
        return None
    if not rel:
        return [_join(base, "__init__.py")]
    return [_join(base, f"{rel}.py"), _join(base, rel, "__init__.py")]


def _rust_module_candidates(importer: str, module: str) -> list[str]:
    parts = [p for p in module.split("::") if p]
    if not parts:
        return []
    crate_root = _rust_crate_root(importer)
    if parts[0] == "crate":
        base, rest = crate_root, parts[1:]
    else:
        base, rest = _rust_self_dir(importer), parts
        while rest and rest[0] in ("self", "super"):
            if rest[0] == "super":
                if base == crate_root or not base.startswith(crate_root):
                    return []
                base = posixpath.dirname(base)
            rest = rest[1:]
    if rest:
        rel = _join(base, *rest)
        return [f"{rel}.rs", f"{rel}/mod.rs"]
    out = [f"{base}.rs", f"{base}/mod.rs"] if base else []
    if base == crate_root:
        out += [_join(base, "lib.rs"), _join(base, "main.rs")]
    return out


async def resolve_module(ops: WalkOps, importer: str, module: str | None) -> str:
    """The workspace file *module* (as written in *importer*) names, or ``UnresolvedHopError``."""
    language = symbol_language(importer)
    if not module:
        raise UnresolvedHopError(f"a re-export in {importer} names no source module")
    if language in _JS:
        if not module.startswith("."):
            raise UnresolvedHopError(
                f"'{module}' (in {importer}) is a package or path-alias specifier, not followed"
            )
        bare = module.split("?", 1)[0].split("#", 1)[0]
        target = _join(posixpath.dirname(importer), bare)
        return await _first_existing(
            ops, js_path_candidates(target), f"'{module}' (in {importer}) resolves to no file"
        )
    if language == "python":
        candidates = python_module_candidates(importer, module)
        return await _first_existing(
            ops,
            candidates or [],
            f"'{module}' (in {importer}) is not a module of this workspace (external)",
        )
    if language == "rust":
        head = module.split("::", 1)[0]
        return await _first_existing(
            ops,
            _rust_module_candidates(importer, module),
            f"'{module}' (in {importer}) is "
            + (
                "not a module file of this crate"
                if head in ("crate", "self", "super")
                else "an external crate or not a child module"
            ),
        )
    raise UnresolvedHopError(f"re-exports are not followed for {importer}")


def _rename(name: str, word: str, target: str) -> str:
    """*name* with its looked-up segment *word* (the owner, or the name) renamed."""
    *owners, member = name.split(".")
    if owners and owners[-1] == word:
        return ".".join([*owners[:-1], target, member])
    return target if member == word else name


async def resolve(match: FileMatch, name: str, kind: str, ops: WalkOps, hops: int = 0) -> FileMatch:
    """*match* with its ``pending`` follow-up resolved (unchanged when there is none)."""
    pending = match.pending
    if isinstance(pending, Reexport):
        return await _follow_reexport(pending, name, kind, ops, hops)
    if isinstance(pending, Inherit):
        return await _walk_bases(pending, kind, ops)
    return match


async def _follow_reexport(
    rx: Reexport, name: str, kind: str, ops: WalkOps, hops: int
) -> FileMatch:
    if hops >= MAX_REEXPORT_HOPS:
        return FileMatch(
            None,
            f"'{rx.word}' is re-exported by {rx.path}, but the chain is longer than "
            f"{MAX_REEXPORT_HOPS} hops, so it was not followed to the end (valid=null).",
        )
    results: list[tuple[str, FileMatch]] = []
    for entry in rx.entries:
        target_word = rx.word if entry.get("name") == "*" else str(entry.get("name"))
        try:
            if entry.get("name") != "*" and symbol_language(rx.path) in _JS:
                target_word = _js_exported_source_name(rx.content, entry)
        except UnresolvedHopError as e:
            results.append((str(entry.get("from_module")), FileMatch(None, str(e))))
            continue
        renamed = _rename(name, rx.word, target_word)
        try:
            target = await resolve_module(ops, rx.path, entry.get("from_module"))
            content, body = await load(ops, target)
        except UnresolvedHopError as e:
            results.append((str(entry.get("from_module")), FileMatch(None, str(e))))
            continue
        found = match_symbol(body, renamed, kind, target, content)
        results.append((target, await resolve(found, renamed, kind, ops, hops + 1)))
    found_defs = [d for _, m in results if m.verdict is True for d in m.definitions]
    if found_defs or any(m.verdict is True for _, m in results):
        where = sorted({d["file"] for d in found_defs}) or [t for t, m in results if m.verdict]
        return FileMatch(
            True,
            f"'{rx.word}' is re-exported via {rx.path}; defined in {where}.",
            found_defs,
        )
    targets = [t for t, _ in results]
    if all(m.verdict is False for _, m in results) and not rx.blockers:
        return FileMatch(
            False,
            f"'{name}' is not defined in {rx.path}; it is re-exported from {targets} and the "
            "chain was followed to the end: no definition of the claimed kind there.",
        )
    reasons = [m.note or t for t, m in results if m.verdict is not False] + rx.blockers
    return FileMatch(
        None,
        f"'{rx.word}' is re-exported by {rx.path} (from {targets}), but the chain is not "
        f"decided: {'; '.join(reasons)} (valid=null).",
    )


_JS_EXPORT_CLAUSE = re.compile(r"\bexport\s+(?:type\s+)?\{([^}]*)\}\s*(from\b)?")
_JS_IMPORT = re.compile(r"\bimport\s+(?:type\s+)?([^;'\"]*?)\s*\bfrom\s*['\"][^'\"]+['\"]", re.S)


def _js_import_binding(content: str, local: str) -> tuple[str, str] | None:
    """How *local* is bound by an ``import`` of *content*: ``("named", imported)``,
    ``("default", local)`` or ``("namespace", local)``; ``None``: not imported."""
    found: list[tuple[str, str]] = []
    for m in _JS_IMPORT.finditer(content):
        clause = m.group(1).strip()
        named = re.search(r"\{([^}]*)\}", clause)
        rest = re.sub(r"\{[^}]*\}", "", clause)
        if re.search(rf"\*\s*as\s+{re.escape(local)}\b", rest):
            found.append(("namespace", local))
        elif re.match(rf"\s*{re.escape(local)}\s*(?:,|$)", rest):
            found.append(("default", local))
        for spec in named.group(1).split(",") if named else []:
            sm = re.fullmatch(
                r"\s*(?:type\s+)?([\w$]+|'[^']*'|\"[^\"]*\")(?:\s+as\s+([\w$]+))?\s*", spec
            )
            if sm and (sm.group(2) or sm.group(1)) == local:
                found.append(("named", sm.group(1).strip("'\"")))
    return found[0] if len(found) == 1 else (("ambiguous", local) if found else None)


def _js_exported_source_name(content: str, entry: dict[str, Any]) -> str:
    """The name to look up in ``from_module`` for one named JS/TS re-export *entry*.

    ``export { a as b } from './m'`` -> ``a``. A source-less ``export { w as q }``
    re-exports the LOCAL binding ``w``: through ``import { y as w } from './m'``
    it is ``y``; a default or namespace import, or an undetermined statement, is
    not followed (``UnresolvedHopError``) -- never searched for under ``w``.
    """
    name = str(entry.get("name"))
    line = entry.get("line")
    lines = content.splitlines()
    if not isinstance(line, int) or not 1 <= line <= len(lines):
        raise UnresolvedHopError(f"the `export` of '{name}' could not be located in the file")
    m = _JS_EXPORT_CLAUSE.search("\n".join(lines[line - 1 :]))
    if m is None or not re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", m.group(1)):
        raise UnresolvedHopError(f"the `export {{ {name} ... }}` statement could not be read")
    if m.group(2):
        return name  # ``export { name as alias } from '...'``: name is the source's own
    binding = _js_import_binding(content, name)
    if binding is None:
        raise UnresolvedHopError(f"'{name}' is exported but not imported by a static `import`")
    how, imported = binding
    if how != "named":
        raise UnresolvedHopError(
            f"'{name}' is a {how} import re-exported under another name; its source "
            "definition is not named by it, so it is not followed"
        )
    return imported


def _type_symbols(body: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [
        s
        for s in body.get("symbols") or []
        if isinstance(s, dict) and s.get("name") == name and s.get("kind") in CLASS_KINDS
    ]


def _top_level_type(body: dict[str, Any], name: str) -> bool:
    """*name* is a type declared at the top of its file (not nested in another type)."""
    return any(not s.get("parent") for s in _type_symbols(body, name))


# java.lang is imported on demand into every Java file: a bare name among these
# may be the platform type, whatever the workspace defines elsewhere.
JAVA_LANG_TYPES = frozenset(
    {
        "AbstractMethodError", "Appendable", "ArithmeticException",
        "ArrayIndexOutOfBoundsException", "ArrayStoreException", "AssertionError",
        "AutoCloseable", "Boolean", "Byte", "CharSequence", "Character", "Class",
        "ClassCastException", "ClassLoader", "ClassNotFoundException", "ClassValue",
        "CloneNotSupportedException", "Cloneable", "Comparable", "Deprecated", "Double",
        "Enum", "Error", "Exception", "ExceptionInInitializerError", "Float",
        "FunctionalInterface", "IllegalAccessException", "IllegalArgumentException",
        "IllegalMonitorStateException", "IllegalStateException",
        "IndexOutOfBoundsException", "InheritableThreadLocal", "InstantiationException",
        "Integer", "InternalError", "InterruptedException", "Iterable", "LinkageError",
        "Long", "Math", "Module", "NegativeArraySizeException", "NoClassDefFoundError",
        "NoSuchFieldException", "NoSuchMethodException", "NullPointerException", "Number",
        "NumberFormatException", "Object", "OutOfMemoryError", "Override", "Package",
        "Process", "ProcessBuilder", "Readable", "Record", "ReflectiveOperationException",
        "Runnable", "Runtime", "RuntimeException", "SafeVarargs", "SecurityException",
        "Short", "StackOverflowError", "StackWalker", "StrictMath", "String",
        "StringBuffer", "StringBuilder", "StringIndexOutOfBoundsException",
        "SuppressWarnings", "System", "Thread", "ThreadDeath", "ThreadGroup", "ThreadLocal",
        "Throwable", "TypeNotPresentException", "UnsupportedOperationException",
        "VirtualMachineError", "Void",
    }
)  # fmt: skip

_JAVA_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.M)
_CS_NAMESPACE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)\s*(?:[;{]|$)", re.M)
_CS_USING = re.compile(
    r"^\s*(global\s+)?using\s+(static\s+)?(?:(\w+)\s*=\s*)?(?:global::)?"
    r"([A-Za-z_][\w.]*)\s*(?:<[^;]*>)?\s*;",
    re.M,
)
_GO_PACKAGE = re.compile(r"^\s*package\s+(\w+)", re.M)
_GO_DOT_IMPORT = re.compile(r"(?:^|\bimport\s*|\n)\s*\.\s+\"", re.M)
_CPP_INCLUDE = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]", re.M)
# The extra searches a C# using-resolution needs: project-wide usings it cannot see.
CS_GLOBAL_USING_PATTERN = r"^[[:space:]]*global[[:space:]]+using[[:space:]]"
CS_IMPLICIT_USINGS_PATTERN = r"<ImplicitUsings>[[:space:]]*(enable|true)"


def _no_proof(simple: str, why: str) -> UnresolvedHopError:
    return UnresolvedHopError(
        f"'{simple}' {why}, so the name is not proven to bind to a workspace definition"
    )


async def _defining_files(
    ops: WalkOps, origin: Inherit, simple: str, language: str | None
) -> list[tuple[str, str, dict[str, Any]]]:
    """Files of *language* (other than the origin) declaring the top-level type *simple*."""
    try:
        mentioned = await ops.mentions(simple)
    except _WORKSPACE_ERRORS as e:
        raise UnresolvedHopError(f"the search for '{simple}' could not finish ({e})") from e
    candidates = [c for c in mentioned if c != origin.path and symbol_language(c) == language]
    if len(candidates) > MAX_BASE_CANDIDATES:
        raise UnresolvedHopError(
            f"'{simple}' is mentioned in {len(candidates)} files, more than the "
            f"{MAX_BASE_CANDIDATES} checked"
        )
    defining: list[tuple[str, str, dict[str, Any]]] = []
    for path in candidates:
        content, body = await load(ops, path)
        if _top_level_type(body, simple):
            defining.append((path, content, body))
    return defining


def _one(simple: str, defining: list[tuple[str, str, dict[str, Any]]], where: str):
    if not defining:
        raise _no_proof(simple, f"is not defined {where} (external, or not visible here)")
    if len(defining) > 1:
        raise UnresolvedHopError(
            f"'{simple}' is defined {where} in {len(defining)} files "
            f"({[d[0] for d in defining]}): ambiguous"
        )
    return defining[0]


async def _defined_at(
    ops: WalkOps, importer: str, module: str, simple: str
) -> tuple[str, str, dict[str, Any]]:
    """The file *module* (written in *importer*) names, which provably defines *simple*."""
    target = await resolve_module(ops, importer, module)
    content, body = await load(ops, target)
    found = await resolve(
        match_symbol(body, simple, "class", target, content), simple, "class", ops
    )
    if found.verdict is not True or not found.definitions:
        raise UnresolvedHopError(
            f"'{simple}' is imported from '{module}' ({target}), which does not "
            f"provably define it ({found.note})"
        )
    where = found.definitions[0]["file"]
    content, body = await load(ops, where)
    return where, content, body


async def _locate_js(ops: WalkOps, origin: Inherit, simple: str):
    imported = [
        i
        for i in origin.body.get("imports") or []
        if isinstance(i, dict) and simple in (i.get("names") or [])
    ]
    if not imported:
        raise _no_proof(simple, f"is neither defined in nor imported by {origin.path}")
    if len(imported) > 1:
        raise UnresolvedHopError(f"'{simple}' is imported more than once in {origin.path}")
    # ``imports[].names`` are the IMPORTED names: ``{ Base as B }`` lists ``Base``,
    # which is then not bound under that name here.
    if re.search(rf"\b{re.escape(simple)}\s+as\b", origin.content):
        raise _no_proof(simple, f"is imported under another name (`as`) in {origin.path}")
    module = str(imported[0].get("module") or "")
    path, content, body = await _defined_at(ops, origin.path, module, simple)
    default = re.search(
        rf"\bimport\s+(?:type\s+)?{re.escape(simple)}\s*(?:,|from\b)", origin.content
    )
    if default and not re.search(
        rf"\bexport\s+default\s+(?:abstract\s+)?class\s+{re.escape(simple)}\b", content
    ):
        raise _no_proof(
            simple, f"is a default import in {origin.path} whose export is not that class"
        )
    return path, content, body


async def _locate_java(ops: WalkOps, origin: Inherit, simple: str):
    language = "java"
    m = _JAVA_PACKAGE.search(origin.content)
    package = m.group(1) if m else None
    imports = [i for i in origin.body.get("imports") or [] if isinstance(i, dict)]
    single = [
        str(i.get("module") or "")
        for i in imports
        if (i.get("names") or []) == [simple] and not str(i.get("module") or "").endswith("*")
    ]
    wildcards = [
        str(i.get("module") or "").removesuffix(".*")
        for i in imports
        if (i.get("names") or []) == ["*"]
    ]

    def in_package(pkg: str | None, defining):
        out = []
        for d in defining:
            dm = _JAVA_PACKAGE.search(d[1])
            if (dm.group(1) if dm else None) == pkg:
                out.append(d)
        return out

    if single:
        if len(set(single)) > 1:
            raise UnresolvedHopError(f"'{simple}' is imported twice in {origin.path}")
        pkg = single[0].rpartition(".")[0]
        defining = await _defining_files(ops, origin, simple, language)
        return _one(simple, in_package(pkg, defining), f"in package '{pkg}' ({single[0]})")
    defining = await _defining_files(ops, origin, simple, language)
    same = in_package(package, defining)
    if same:
        return _one(simple, same, f"in the same package '{package}'")
    if simple in JAVA_LANG_TYPES:
        raise _no_proof(simple, "is also a java.lang type (imported into every file)")
    if len(wildcards) != 1:
        raise _no_proof(
            simple,
            "is not imported by name nor defined in the same package"
            + (f", and {len(wildcards)} wildcard imports could supply it" if wildcards else ""),
        )
    return _one(simple, in_package(wildcards[0], defining), f"in package '{wildcards[0]}' (`.*`)")


async def _cs_namespace_is_internal(ops: WalkOps, namespace: str, known: set[str]) -> bool:
    """A workspace .cs file declares *namespace* (else it is external, or unknown)."""
    if namespace in known:
        return True
    last = namespace.rsplit(".", 1)[-1]
    try:
        mentioned = [c for c in await ops.mentions(last) if symbol_language(c) == "c_sharp"]
    except _WORKSPACE_ERRORS as e:
        raise UnresolvedHopError(
            f"the search for namespace '{namespace}' could not finish ({e})"
        ) from e
    if len(mentioned) > MAX_BASE_CANDIDATES:
        raise UnresolvedHopError(
            f"namespace '{namespace}' is mentioned in {len(mentioned)} files, more than "
            f"the {MAX_BASE_CANDIDATES} checked"
        )
    for path in mentioned:
        try:
            content = await ops.read(path)
        except (*_WORKSPACE_ERRORS, ValueError) as e:
            raise UnresolvedHopError(f"{path} could not be read completely ({e})") from e
        if content and namespace in _CS_NAMESPACE.findall(content):
            return True
    return False


async def _cs_project_usings(ops: WalkOps) -> str | None:
    """Why usings this file cannot see (``global using``, implicit usings) may apply."""
    if ops.lines is None:
        return "project-wide usings were not searched"
    try:
        if await ops.lines(CS_GLOBAL_USING_PATTERN, (".cs",)):
            return "a `global using` elsewhere in the workspace also applies to this file"
        if await ops.lines(CS_IMPLICIT_USINGS_PATTERN, (".csproj", ".props", ".targets")):
            return "the project enables implicit usings (System, ...)"
    except (*_WORKSPACE_ERRORS, ValueError) as e:
        return f"the search for project-wide usings could not finish ({e})"
    return None


# Root namespaces of platform / third-party assemblies: an enclosing namespace
# under one of these may hold an external type of the name, which C# finds before
# any using directive -- so it is never skipped as "no type of that name here".
_CS_EXTERNAL_ROOTS = frozenset(
    {
        "System", "Microsoft", "Windows", "Mono", "Azure", "Amazon", "Google", "Newtonsoft",
        "NUnit", "Xunit", "Moq", "Serilog", "Grpc", "Unity", "UnityEngine", "Avalonia",
        "AutoMapper", "MediatR", "FluentValidation", "Polly", "Castle", "NLog", "log4net",
    }
)  # fmt: skip


async def _cs_level_skippable(ops: WalkOps, namespace: str, own: str, known: set[str]) -> bool:
    """No workspace type of the name is in *namespace* (an enclosing level of *own*):
    may lookup move outward, i.e. is no EXTERNAL type of the name there either?"""
    if namespace == own or namespace in known:
        return True
    if namespace.split(".", 1)[0] in _CS_EXTERNAL_ROOTS:
        return False
    # An ancestor of the origin's own (workspace) namespace, under a non-platform root.
    return own.startswith(namespace + ".") or await _cs_namespace_is_internal(ops, namespace, known)


async def _locate_cs(ops: WalkOps, origin: Inherit, simple: str):
    """C# simple-name lookup (ECMA-334 12.8.4), from the innermost namespace outward.

    For each namespace level -- the origin's own namespace, then each ENCLOSING one
    (``App.Web`` -> ``App`` -> global) -- a type of that namespace wins; only after
    the global namespace do the compilation unit's ``using`` directives apply. Two
    workspace definitions at one level, or a using step while any using may be
    external / global / implicit, is ambiguous -> ``None``.
    """
    declared = _CS_NAMESPACE.findall(origin.content)
    if len(declared) > 1:
        raise _no_proof(simple, f"is used in {origin.path}, which declares several namespaces")
    namespace = declared[0] if declared else None
    defining = await _defining_files(ops, origin, simple, "c_sharp")
    spaces: list[tuple[str | None, tuple[str, str, dict[str, Any]]]] = []
    for d in defining:
        ns = _CS_NAMESPACE.findall(d[1])
        if len(ns) > 1:
            raise _no_proof(simple, f"is defined in {d[0]}, which declares several namespaces")
        spaces.append((ns[0] if ns else None, d))
    known = {ns for ns, _ in spaces if ns}
    usings = list(_CS_USING.finditer(origin.content))
    decl = re.search(r"^\s*namespace\b", origin.content, re.M)
    inner_usings = bool(decl and any(u.start() > decl.start() for u in usings))
    parts = namespace.split(".") if namespace else []
    levels: list[str | None] = [".".join(parts[:k]) for k in range(len(parts), 0, -1)]
    levels.append(None)  # the global namespace
    for i, level in enumerate(levels):
        here = [d for ns, d in spaces if ns == level]
        if here:
            # Members of an enclosing namespace are found before any using directive.
            return _one(simple, here, f"in the namespace '{level or '(global)'}'")
        if i == 0 and inner_usings:
            # Usings inside the namespace block apply before the enclosing levels.
            raise _no_proof(simple, f"may come from a using inside a namespace of {origin.path}")
        if (
            level is not None
            and namespace
            and not await _cs_level_skippable(ops, level, namespace, known)
        ):
            raise _no_proof(
                simple,
                f"is not defined in the workspace's '{level}', an enclosing namespace that "
                "may hold an external type of that name (found before any using)",
            )
    if any(u.group(3) == simple or u.group(2) for u in usings):
        raise _no_proof(simple, f"may be named by a `using static` / alias in {origin.path}")
    names = [u.group(4) for u in usings if not u.group(3)]
    if not names:
        raise _no_proof(
            simple, "is not in the namespace or an enclosing one, and no `using` names a namespace"
        )
    external = [n for n in names if not await _cs_namespace_is_internal(ops, n, known)]
    if external:
        raise _no_proof(simple, f"may come from the external namespace(s) {external}")
    if why := await _cs_project_usings(ops):
        raise _no_proof(simple, f"may come from elsewhere: {why}")
    return _one(simple, [d for ns, d in spaces if ns in names], f"in a namespace of {names}")


async def _locate_go(ops: WalkOps, origin: Inherit, simple: str):
    if _GO_DOT_IMPORT.search(origin.content):
        raise _no_proof(simple, f"may come from a dot import in {origin.path}")
    directory = posixpath.dirname(origin.path)
    m = _GO_PACKAGE.search(origin.content)
    package = m.group(1) if m else None
    test = origin.path.endswith("_test.go")
    defining = [
        d
        for d in await _defining_files(ops, origin, simple, "go")
        if posixpath.dirname(d[0]) == directory
        and (test or not d[0].endswith("_test.go"))
        and (pm := _GO_PACKAGE.search(d[1])) is not None
        and pm.group(1) == package
    ]
    return _one(simple, defining, f"in package '{package}' ({directory or '.'})")


def _rust_local_head(content: str, head: str) -> bool:
    return head in ("crate", "self", "super") or bool(
        re.search(rf"^\s*(?:pub(?:\([^)]*\))?\s+)?mod\s+{re.escape(head)}\s*;", content, re.M)
    )


async def _locate_rust(ops: WalkOps, origin: Inherit, simple: str, base: str):
    if "::" in base:
        module, _, name = simple.rpartition("::")
        if not _rust_local_head(origin.content, module.split("::", 1)[0]):
            raise _no_proof(base, "names an external crate or an undeclared module")
        return await _defined_at(ops, origin.path, module, name)
    imported = [
        i
        for i in origin.body.get("imports") or []
        if isinstance(i, dict) and simple in (i.get("names") or [])
    ]
    if not imported:
        raise _no_proof(simple, f"is neither defined in nor imported by name in {origin.path}")
    if len(imported) > 1 or re.search(rf"\bas\s+{re.escape(simple)}\b", origin.content):
        raise _no_proof(simple, f"is imported under an alias or twice in {origin.path}")
    module = str(imported[0].get("module") or "")
    if not module or not _rust_local_head(origin.content, module.split("::", 1)[0]):
        raise _no_proof(
            simple, f"is imported from '{module}', an external crate or undeclared module"
        )
    return await _defined_at(ops, origin.path, module, simple)


async def _locate_cpp(ops: WalkOps, origin: Inherit, simple: str):
    includes = _CPP_INCLUDE.findall(origin.content)
    if any(kind == "<" for kind, _ in includes):
        raise _no_proof(
            simple, f"may come from a system/library header `#include <...>` of {origin.path}"
        )
    directory = posixpath.dirname(origin.path)
    defining: list[tuple[str, str, dict[str, Any]]] = []
    for _, header in includes:
        target = await _first_existing(
            ops,
            [_join(directory, header)],
            f"'#include \"{header}\"' (in {origin.path}) is not next to it (include paths unknown)",
        )
        content, body = await load(ops, target)
        if _type_symbols(body, simple):
            defining.append((target, content, body))
    path, content, body = _one(simple, defining, f"in a header {origin.path} includes")
    if re.search(r"\bnamespace\b", content) or not re.search(
        rf"\b(?:class|struct)\s+{re.escape(simple)}\b[^;{{]*\{{", content
    ):
        raise _no_proof(
            simple, f"is declared in {path} inside a namespace or only forward-declared"
        )
    return path, content, body


async def _locate_type(ops: WalkOps, origin: Inherit, base: str) -> tuple[str, str, dict[str, Any]]:
    """``(path, content, body)`` of the workspace file the base name *base* binds to.

    Only with PROOF that the name as written in the origin file binds there
    (see the module docstring); anything else raises ``UnresolvedHopError``.
    """
    simple = simple_type_name(base)
    language = symbol_language(origin.path)
    if language == "rust" and simple and "::" in simple:
        return await _locate_rust(ops, origin, simple, base)
    if not simple or "." in simple or "::" in simple:
        raise UnresolvedHopError(
            f"'{base}' is qualified (its qualifier is a package, namespace or module), so it is "
            "not resolved"
        )
    if _type_symbols(origin.body, simple):
        return origin.path, origin.content, origin.body
    if language in _JS:
        return await _locate_js(ops, origin, simple)
    if language == "java":
        return await _locate_java(ops, origin, simple)
    if language == "c_sharp":
        return await _locate_cs(ops, origin, simple)
    if language == "go":
        return await _locate_go(ops, origin, simple)
    if language == "rust":
        return await _locate_rust(ops, origin, simple, base)
    if language == "cpp":
        return await _locate_cpp(ops, origin, simple)
    raise _no_proof(simple, f"is not defined in {origin.path}")


_JS_EXTENSIONS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")


async def _js_augmentations(ops: WalkOps, names: list[str], member: str) -> list[str]:
    """Why *member* may be added to one of *names* from another file (JS/TS).

    TS declaration merging and module augmentation (``interface Owner`` /
    ``declare module``), and prototype / static assignments anywhere.
    """
    if ops.lines is None:
        return ["declaration merging / prototype assignments elsewhere were not searched"]
    alts = "|".join(re.escape(n) for n in dict.fromkeys(names))
    m = re.escape(member)
    pattern = (
        rf"\binterface[[:space:]]+({alts})\b|\b({alts})\.prototype\.{m}\b"
        rf"|\b({alts})\.{m}[[:space:]]*=([^=]|$)"
    )
    try:
        hits = await ops.lines(pattern, _JS_EXTENSIONS)
    except (*_WORKSPACE_ERRORS, ValueError) as e:
        return [f"the search for declaration merging could not finish ({e})"]
    return [
        f"{h.get('file')}:{h.get('line')} may add '{member}' (declaration merging, module "
        "augmentation or an assignment)"
        for h in hits[:3]
    ]


# Members every type of the language has from its IMPLICIT root type, which no
# workspace file declares. Exact sets only: a member here is certainly present.
_JAVA_OBJECT_PUBLIC = frozenset(
    {"toString", "equals", "hashCode", "getClass", "notify", "notifyAll", "wait"}
)
# ``protected`` on java.lang.Object: members of every class, not of an interface (JLS 9.2).
_JAVA_OBJECT_PROTECTED = frozenset({"clone", "finalize"})
# java.lang.Enum's methods and the compiler-synthesized statics of every enum (JLS 8.9.3).
_JAVA_ENUM_MEMBERS = frozenset(
    {"values", "valueOf", "name", "ordinal", "compareTo", "getDeclaringClass",
     "describeConstable"}
)  # fmt: skip
_JS_OBJECT_PROTOTYPE = frozenset(
    {"constructor", "toString", "toLocaleString", "valueOf", "hasOwnProperty",
     "isPrototypeOf", "propertyIsEnumerable", "__proto__", "__defineGetter__",
     "__defineSetter__", "__lookupGetter__", "__lookupSetter__"}
)  # fmt: skip
_CS_OBJECT_MEMBERS = frozenset(
    {"ToString", "Equals", "GetHashCode", "GetType", "MemberwiseClone", "ReferenceEquals",
     "Finalize"}
)  # fmt: skip


def _java_record_components(content: str, owner: str) -> list[str] | None:
    """Component names of ``record <owner>(...)`` in *content* (its accessors), or
    ``None`` when the header cannot be read exactly."""
    m = re.search(rf"\brecord\s+{re.escape(owner)}\s*(?:<[^()]*>)?\s*\(", content)
    if m is None:
        return None
    depth, start, parts = 0, m.end(), []
    for i in range(m.end(), len(content)):
        c = content[i]
        if c in "(<[":
            depth += 1
        elif c in ")>]":
            if depth == 0:
                parts.append(content[start:i])
                break
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(content[start:i])
            start = i + 1
    else:
        return None
    names: list[str] = []
    for part in parts:
        part = re.sub(r"@[\w.]+(?:\s*\([^()]*\))?", " ", part).strip()
        if not part:
            continue
        ident = re.search(r"([A-Za-z_$][\w$]*)\s*$", part)
        if ident is None or len(part.split()) < 2 and "..." not in part:
            return None
        names.append(ident.group(1))
    return names


def _implicit_root_member(root: Inherit, kind: str, *, resolved: bool) -> str | None:
    """Where *root.member* certainly comes from when no workspace type declares it.

    Java: ``java.lang.Object`` (every class, record, enum; its public methods on
    an interface), ``java.lang.Enum`` + synthesized ``values``/``valueOf`` (an
    enum), a record's component accessors (header read exactly). C#:
    ``System.Object``. JS/TS: ``Object.prototype`` -- only when every base was
    *resolved* (an unresolved base may have a null prototype). Python is
    handled by the router; Go / Rust / C++ have no implicit root type.
    """
    if kind == "class":
        return None
    member, language = root.member, symbol_language(root.path)
    kinds = {t.get("kind") for t in _type_symbols(root.body, root.owner)}
    if language == "java":
        if member in _JAVA_OBJECT_PUBLIC:
            return "java.lang.Object"
        if member in _JAVA_OBJECT_PROTECTED and kinds and kinds <= {"class", "record", "enum"}:
            return "java.lang.Object"
        if member in _JAVA_ENUM_MEMBERS and kinds == {"enum"}:
            return "java.lang.Enum (or the enum's synthesized statics)"
        if kinds == {"record"}:
            components = _java_record_components(root.content, root.owner)
            if components is not None and member in components:
                return f"the accessor of record component '{member}'"
        return None
    if language == "c_sharp" and member in _CS_OBJECT_MEMBERS:
        return "System.Object"
    if (
        language in _JS
        and resolved
        and member in _JS_OBJECT_PROTOTYPE
        and kinds
        and kinds <= {"class", "interface", "enum"}
    ):
        return "Object.prototype"
    return None


def _java_record_unreadable(root: Inherit) -> bool:
    """A Java record whose header cannot be read: an absent member may be an accessor."""
    if symbol_language(root.path) != "java":
        return False
    kinds = {t.get("kind") for t in _type_symbols(root.body, root.owner)}
    return "record" in kinds and _java_record_components(root.content, root.owner) is None


async def _walk_bases(root: Inherit, kind: str, ops: WalkOps) -> FileMatch:
    member = root.member
    queue: list[tuple[str, Inherit, int]] = [(b, root, 1) for b in root.bases + root.traits]
    visited = {(root.path, root.owner)}
    unresolved: list[str] = []
    blockers = list(root.blockers)
    searched: list[str] = []
    names = [root.owner]
    while queue:
        base, origin, hop = queue.pop(0)
        if hop > MAX_BASE_HOPS:
            unresolved.append(f"'{base}' is more than {MAX_BASE_HOPS} inheritance hops away")
            continue
        try:
            path, content, body = await _locate_type(ops, origin, base)
        except UnresolvedHopError as e:
            unresolved.append(f"base '{base}' of '{origin.owner}': {e}")
            continue
        simple = simple_type_name(base).rsplit("::", 1)[-1]
        if (path, simple) in visited:
            continue
        visited.add((path, simple))
        names.append(simple)
        searched.append(f"{simple} ({path})")
        found = match_symbol(body, f"{simple}.{member}", kind, path, content)
        if found.verdict is True:
            line = found.definitions[0].get("line") if found.definitions else None
            return FileMatch(
                True,
                f"'{root.owner}.{member}' is inherited from {simple} ({path}:{line}).",
                found.definitions,
            )
        if isinstance(found.pending, Inherit):
            blockers += found.pending.blockers
            queue += [
                (b, found.pending, hop + 1) for b in found.pending.bases + found.pending.traits
            ]
        else:
            unresolved.append(found.note or f"'{simple}' in {path} could not be checked")
    owner = f"'{root.owner}' ({root.path})"
    if implicit := _implicit_root_member(root, kind, resolved=not unresolved):
        return FileMatch(
            True,
            f"'{root.owner}.{member}' is not declared in the workspace; it is provided "
            f"implicitly by {implicit} (implicit_root, language-defined, certain).",
        )
    if _java_record_unreadable(root):
        blockers.append(f"'{root.owner}' is a record whose components could not be read")
    bases = f"its bases {searched}" if searched else "any resolved base"
    if unresolved:
        return FileMatch(
            None,
            f"'{member}' is not defined on {owner} nor on {bases}, but it may be inherited "
            f"from a base that was not resolved in the workspace: {'; '.join(unresolved)} "
            "(valid=null).",
        )
    if symbol_language(root.path) in _JS:
        blockers += await _js_augmentations(ops, names, member)
    if blockers:
        return FileMatch(
            None,
            f"'{member}' is not defined on {owner} nor on {bases}, but absence cannot be "
            f"proven: {'; '.join(dict.fromkeys(blockers))} (valid=null).",
        )
    return FileMatch(
        False,
        f"'{member}' is not defined on {owner} nor on {bases}; every base was resolved in "
        "the workspace and parsed cleanly.",
    )
