"""Dotted (module-qualified) class names: parsing and in-repo module candidates.

WHY THIS EXISTS (C1-42 / fix_unit QA q6). ``code.symbolise`` emits classes as
``module.Class`` -- ``pathlib.Path``, ``app.models.User``. The class-reference
endpoint used to feed that string straight to ``validate_identifier``, which
accepts only a bare identifier, so every symbolised class came back **400** and
q6 could not ground a single class candidate.

THE ONE RULE THAT MUST NOT BE BROKEN: the last segment is NEVER searched
repo-wide. ``pathlib.Path`` names the ``Path`` defined in the module
``pathlib``; a repo that happens to define an unrelated ``class Path`` in
``app/geometry.py`` does not satisfy it. Stripping the prefix and grepping the
tail would turn that coincidence into ``valid=true`` -- a false PASS, the worst
direction a verification gate can fail in. So the prefix is resolved as a
MODULE first, and the class is looked for only inside that module's file.

HOW A PREFIX IS RESOLVED. Import semantics, approximated with the two layouts a
repo actually uses: the module ``a.b`` lives at ``<root>/a/b.py`` or
``<root>/a/b/__init__.py`` for ``<root>`` in ``MODULE_SEARCH_ROOTS`` (repo root
and ``src/``). A same-named file anywhere else (``app/logging.py``) is NOT the
top-level module ``logging`` and is deliberately not matched -- matching by
path suffix would make ``logging.Logger`` resolve into the repo and come back
``valid=false`` for every repo that has an ``app/logging.py``.

THREE OUTCOMES:
- the full module file is in the repo       -> ``in_repo``; valid iff the class
                                               is defined in that file
- only the top-level package is in the repo -> ``in_repo``, valid=false (a
                                               submodule of OUR package that
                                               does not exist is a hallucination,
                                               not an external reference)
- the top-level package is not in the repo  -> ``external`` (stdlib or a
                                               dependency); not computed here,
                                               the caller decides.
"""

from __future__ import annotations

import re

# Where a top-level import can live in a repository: the repo root (flat
# layout) and ``src/`` (src layout). Published on every dotted response.
MODULE_SEARCH_ROOTS: tuple[str, ...] = ("", "src")

MODULE_RESOLUTION_IN_REPO = "in_repo"
MODULE_RESOLUTION_EXTERNAL = "external"

# ⛔ Always ``fullmatch``, never ``match`` with ``^…$``: Python's ``$`` also
# matches just before a trailing ``\n``, so ``"Path\n"`` passed as a segment.
# The newline then reached a ``grep -E`` pattern, where it is an OR between two
# patterns, and the second half matched almost any line -- a false PASS
# (coding-gateway#78).
_SEGMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_dotted(name: str) -> bool:
    """True when *name* is module-qualified (contains a dot)."""
    return "." in name


def split_dotted_class_name(name: str) -> tuple[str, str]:
    """``'pkg.mod.Cls'`` -> ``('pkg.mod', 'Cls')``, or raise ``ValueError``.

    Every segment must be a bare identifier. That refuses ``a..b``, a leading
    or trailing dot, and every shell/regex metacharacter -- the segments are
    interpolated into ``find -path`` arguments and a grep pattern, so anything
    outside this shape is refused, never escaped.
    """
    parts = name.split(".")
    if len(parts) < 2 or not all(_SEGMENT.fullmatch(p) for p in parts):
        raise ValueError(
            f"Invalid class '{name}': expected a bare identifier or a dotted "
            f"module path whose every segment is a bare identifier "
            f"(letters, digits, underscore; not starting with a digit)."
        )
    return ".".join(parts[:-1]), parts[-1]


def _rooted(root: str, rel: str) -> str:
    return f"{root}/{rel}" if root else rel


def module_file_candidates(module: str) -> list[str]:
    """Repo-relative files that would BE the module *module*."""
    rel = module.replace(".", "/")
    return [
        _rooted(root, path)
        for root in MODULE_SEARCH_ROOTS
        for path in (f"{rel}.py", f"{rel}/__init__.py")
    ]


def top_level_package_candidates(module: str) -> list[str]:
    """Repo-relative files that would make the TOP-LEVEL package of *module* in-repo."""
    top = module.split(".", 1)[0]
    return [
        _rooted(root, path)
        for root in MODULE_SEARCH_ROOTS
        for path in (f"{top}.py", f"{top}/__init__.py")
    ]


def all_candidates(module: str) -> list[str]:
    """Every path worth probing for *module*, de-duplicated, order-stable."""
    seen: dict[str, None] = {}
    for path in module_file_candidates(module) + top_level_package_candidates(module):
        seen.setdefault(path, None)
    return list(seen)
