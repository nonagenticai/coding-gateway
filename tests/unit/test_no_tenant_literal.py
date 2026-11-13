r"""The ``"n8n-dev"`` tenant literal must not come back into ``src/``.

WHAT WAS WRONG
--------------
``src/config/settings.py`` carried ``default_tenant_id: str =
Field(default="n8n-dev")``, read at **four** call sites::

    src/routers/cache.py:62       identity_headers(settings.default_tenant_id)
    src/services/validation.py:92 identity_headers(settings.default_tenant_id)
    src/routers/metrics.py:153    tenant = tenant_id or settings.default_tenant_id
    src/routers/graphs.py:267     tenant_id or get_settings().default_tenant_id

``identity_headers`` sends ``X-Tenant-Id`` **plus this service's own licence**
(``clients/outbound_identity.py``), and ``svc-coding-gateway`` is listed in
coding-analysis's ``CODING_ONBEHALF_SUBJECTS``. So the callee does not merely
receive the asserted tenant — it *honours* it.

``"n8n-dev"`` is not junk and it is not a real tenant_id either, which is exactly
what made it dangerous. It **was** a real tenant, as a text tenant_id, until the
2026-04-29 Keycloak migration moved tenant identity to uuid7 and demoted the
label to a ``tenant_name`` attribute.

Put together: an unset ``DEFAULT_TENANT_ID`` would not have 401'd and would not
have crashed. The gateway would have presented a valid licence while asserting a
tenant nobody chose, and been believed. ``metrics.py`` and ``graphs.py`` were the
worst of the four — their fallback is *per request*, so any call that failed to
thread a tenant silently became the default one.

The ``graphs.py`` site is also why this file exists rather than a grep: it reads
``get_settings().default_tenant_id``, and a ``settings\.default_tenant_id`` census
does not span the parentheses. The suite caught it; the census did not.

The env var is set on all five affected deployments today, so this was latent,
not live. It is one manifest edit away from not being latent.

WHY THE SUITE DID NOT CATCH IT
------------------------------
``tests/unit/test_settings.py`` asserted ``settings.default_tenant_id ==
"n8n-dev"``. It did not merely fail to catch the defect — it **pinned the defect
as correct**, so it would have gone red on the fix and stayed green on the bug.

MUTANTS — each was run against this file
----------------------------------------
    M1  restore the settings field with the literal   -> FAILS
    M2  same literal under another name               -> FAILS (keyed on the
        literal, not the field name)
    M2b field back with a uuid-shaped default         -> FAILS via model_fields
        (the literal rule is blind here — that is why there are two rules)
    M3  literal in a comment or docstring only        -> passes, deliberately
    M4  scan root at an empty directory               -> the NON-VACUITY test
        fails, not the rules
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

# Resolve the scan root through an IMPORTED module, never a path relative to
# this file. A relative path is the classic way a guard ends up scanning an
# empty directory and passing vacuously (M4).
_MODULE_FILE = importlib.import_module("config.settings").__file__
assert _MODULE_FILE is not None, "config.settings has no __file__"
SRC = pathlib.Path(_MODULE_FILE).parent.parent

BANNED_TENANT_LITERAL = "n8n-dev"


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """``id()`` of every docstring Constant — module, class and function.

    Docstrings are prose, not values the code can hand out, so they are exempt.
    Excluded by IDENTITY rather than by "is the first statement", because
    ``ast.walk`` yields nodes flattened and a positional test would also exempt
    a real assignment that happens to be first in its block.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
            and ast.get_docstring(node, clean=False) is not None
        ):
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                out.add(id(first.value))
    return out


@pytest.fixture(scope="module")
def parsed() -> list[tuple[pathlib.Path, ast.Module]]:
    """Every ``src/`` module, parsed once."""
    out = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        out.append((path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))))
    return out


def test_the_scan_is_not_vacuous(parsed):
    """Prove the walk found this service's real source before trusting a pass.

    Without this, deleting or mistyping ``SRC`` turns every rule below into a
    green that measured nothing.
    """
    files = {p for p, _ in parsed}
    assert len(files) >= 40, f"only {len(files)} source files found under {SRC}"
    names = {p.name for p in files}
    for required in ("settings.py", "outbound_identity.py", "metrics.py"):
        assert required in names, f"{required} not in the scanned set under {SRC}"

    # Positive control that the AST walk actually parses string constants. The
    # value is MEASURED (25 occurrences of "coding-gateway" in src/), not
    # guessed: an earlier draft of this control in a sibling repo named a string
    # that the fix itself had just removed, so the control could never hold.
    found_known = any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "coding-gateway" in node.value
        for _, tree in parsed
        for node in ast.walk(tree)
    )
    assert found_known, "the AST walk found no known string constant — it is not seeing code"


def test_the_n8n_dev_tenant_literal_is_absent_from_src(parsed):
    """No ``src/`` module may carry ``"n8n-dev"`` as a string constant.

    Keyed on the LITERAL, not on the field name, so reintroducing it as
    ``fallback_tenant`` or ``tenant_default`` is still caught. Comments and
    docstrings are invisible to the AST walk by design — prose explaining the
    removal must stay legal, and this docstring is the standing false-positive
    control for that.
    """
    offenders = []
    for path, tree in parsed:
        exempt = _docstring_nodes(tree)
        offenders += [
            f"{path.relative_to(SRC)}:{node.lineno}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and BANNED_TENANT_LITERAL in node.value
            and id(node) not in exempt
        ]
    assert not offenders, (
        f"the {BANNED_TENANT_LITERAL!r} tenant literal is back at: {offenders}. "
        "This service sends its own licence alongside X-Tenant-Id and is "
        "allowlisted in coding-analysis's CODING_ONBEHALF_SUBJECTS, so a guessed "
        "tenant is VOUCHED FOR downstream rather than rejected. Use "
        "clients.outbound_identity.service_tenant(), which refuses when unset."
    )


def test_settings_has_no_default_tenant_field():
    """``config.settings.Settings`` must not re-grow a tenant default.

    Complements the literal rule: this catches the field returning with a
    *different* (e.g. uuid-shaped) default, which the literal rule cannot see
    and which is the same defect — a tenant chosen by the code rather than by
    configuration.
    """
    from config.settings import Settings

    assert "default_tenant_id" not in Settings.model_fields, (
        "Settings.default_tenant_id is back. The gateway's own routes must take "
        "their tenant from clients.outbound_identity.service_tenant()."
    )
