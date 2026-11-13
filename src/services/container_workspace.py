"""Read a caller's workspace through coding-containers.

WHY THIS MODULE EXISTS — measured live 2026-09-17 against the deployed gateway
pod, not inferred:

    POST /api/validation/function-reference  {"function_name": "<a real symbol>",
                                              "container_name": "<a real container>"}
        -> 200 {"valid": false}                              in 13 ms
    ... same call with "zz_nonexistent_fn_4711"              -> 200 {"valid": false}
    ... same call with a nonexistent container               -> 200 {"valid": false}
    ... same call with NO container at all                   -> 200 {"valid": false}

Four different questions, one answer, and 3-13 ms of latency proving no
container was ever consulted.

The cause was ``_resolve_codebase_path``. Describing it precisely matters,
because the obvious description is wrong: it did NOT drop ``container_name``.
It used the name as a **flag** — "a container was named, so probe the workspace
directories" — and then chose between three hard-coded paths on **the gateway's
own filesystem** (``/app/analysis_workspace``, ``/app/data/repo``,
``_CODEBASE_PATH``), never once consulting the service that owns containers. Two
containers with different contents resolved to the same directory, and a
container name that existed nowhere resolved to that directory too.

None of those three directories exists in the gateway pod, so every request fell
through to the default, ``ReferenceValidator`` indexed an empty tree, and every
symbol on earth was absent from it. That is a gate that cannot fail: a caller
asking "does this class exist in my workspace?" was told "no" regardless of
truth.

WHY IT GOES OVER HTTP RATHER THAN OPENING A PATH. The workspace a container
名 refers to is a Kubernetes ``emptyDir`` mounted at ``/workspace`` **inside the
workspace pod** (coding-containers ``src/core/kubernetes_container_provisioner.py``
:1246-1249 declares the volume, :1220-1223 mounts it, :736 records
``codebase_path="/workspace"``). An emptyDir is pod-local and node-local: it is
not a PVC, not a hostPath, not a share. No response from coding-containers
returns a path the gateway could ``open()``, and none could. So the only honest
way for the gateway to answer a question about a caller's workspace is to ask
the service that owns it.

WHY ``exec`` IS THE INSTRUMENT. ``POST /api/workspaces/{name}/exec`` is
BOLA-scoped: a container that does not exist, or belongs to another tenant,
returns **404** rather than its contents. That 404 is what lets this module keep
two claims apart that the old code merged into one ``valid=false``:

    "your container is unknown to me"   -> refusal (this module raises)
    "your container does not contain X" -> a computed, falsifiable answer

Those are different claims and they must not share a response shape.

The command allowlist (coding-containers ``src/runtime/policy.py:58-105``)
permits ``find`` and ``grep``, so existence questions are answerable today with
no new endpoint on that service.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import re
import sys
from typing import Any

import httpx

from clients.outbound_identity import identity_headers
from config.settings import get_settings

logger = logging.getLogger(__name__)

# The stable path the workspace is always mounted at inside the container.
# coding-containers treats this as the canonical API alias for the repo root
# (``src/routers/containers/_common.py``), on both the Kubernetes and Docker
# backends, so the gateway does not need to know which backend is in play.
WORKSPACE_ROOT = "/workspace"

# A symbol we are willing to interpolate into a grep pattern. Deliberately
# strict: this string becomes part of a regular expression AND part of an argv
# element, so anything outside this shape is refused at the router rather than
# escaped and hoped for.
# ⛔ Always ``fullmatch``: ``^…$`` with ``match`` accepts a trailing ``\n``
# (Python's ``$`` matches before it), and a newline inside a ``grep -E`` pattern
# is an OR -- ``Ghost\n`` came back valid=true (coding-gateway#78).
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Source extensions per language, as coding-analysis ``POST /api/analysis/symbols``
# resolves them (hunt_agent ``SOURCE_EXTENSIONS`` + TSX). ``.d.ts`` is covered by
# ``.ts``. C and C++ share the ``cpp`` grammar there, so they share a row here.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyw"),
    "javascript": (".js", ".jsx", ".mjs", ".cjs"),
    "typescript": (".ts", ".tsx"),
    "go": (".go",),
    "java": (".java",),
    "rust": (".rs",),
    "cpp": (".c", ".h", ".cc", ".cpp", ".cxx", ".c++", ".hh", ".hpp", ".hxx", ".h++"),
    "c_sharp": (".cs",),
}

# Languages the CONTAINER-mode reference endpoints search: Python by grepping
# ``def``/``class`` syntax, every other language by grepping candidate files for
# the name and confirming each through coding-analysis' tree-sitter symbol
# extraction (``services.symbol_index``). Published as ``searched_languages`` on
# every response so a caller can see the scope of a negative answer.
SEARCHED_LANGUAGES: tuple[str, ...] = tuple(LANGUAGE_EXTENSIONS)

# What the explicit-``codebase_path`` mode and ``validate_code`` cover: Python only.
PYTHON_ONLY: tuple[str, ...] = ("python",)

# Every non-Python source extension, for the candidate grep.
NON_PYTHON_EXTENSIONS: tuple[str, ...] = tuple(
    ext for lang, exts in LANGUAGE_EXTENSIONS.items() if lang != "python" for ext in exts
)

# Source files of languages coding-analysis does NOT extract symbols from. The
# unlocated candidate grep lists them too: a definition there is invisible to
# the symbol search, so a mention in one makes an absence ``None``, never
# ``False``.
UNSEARCHED_MENTION_EXTENSIONS: tuple[str, ...] = (
    ".vue",
    ".svelte",
    ".mts",
    ".cts",
    ".kt",
    ".kts",
    ".scala",
    ".swift",
    ".php",
    ".rb",
)

_FILE_GLOB = "--include=*.py"

# Directories the definition/file search never descends into (G25). These hold
# vendored or generated code — a virtualenv, site-packages, node_modules, tool
# caches, build metadata — so a name found ONLY there is a dependency's name,
# not the repo's. Before this list existed only ``.git`` was pruned, and the
# conveyor's QA reported a symbol that lived solely in ``.venv/`` as "exists in
# repo": a false PASS. Each entry is a basename glob; ``grep_definition`` feeds
# them to ``--exclude-dir=`` and ``find_file`` to ``-not -path */<dir>/*``, from
# this ONE constant so the two searches cannot drift apart. The routers publish
# it as ``excluded_dirs`` so a caller can see what was not searched.
VENDOR_EXCLUDE_DIRS: tuple[str, ...] = (
    ".git",
    ".venv",
    "venv",
    "site-packages",
    "node_modules",
    ".tox",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".eggs",
    "*.egg-info",
)


class WorkspaceError(Exception):
    """Base for every reason the gateway cannot answer from a workspace."""


class ContainerNotResolvableError(WorkspaceError):
    """coding-containers will not show us this container.

    Either it does not exist or it is not owned by the caller's tenant — the
    service answers 404 for both on purpose (BOLA), and the gateway must not
    guess which. Callers turn this into a refusal, never into ``valid=false``.
    """

    def __init__(self, container_name: str) -> None:
        self.container_name = container_name
        super().__init__(
            f"Workspace '{container_name}' is not available to this tenant "
            f"(unknown container, or owned by another tenant). The gateway "
            f"cannot check references against a workspace it cannot read."
        )


class WorkspaceBackendUnavailableError(WorkspaceError):
    """coding-containers is unreachable, unconfigured, or errored.

    Distinct from :class:`ContainerNotResolvableError`: there, the answer is
    "no such workspace"; here, we do not know, and saying ``valid=false``
    would be inventing one.
    """


class WorkspaceRequestRefusedError(WorkspaceBackendUnavailableError):
    """coding-containers REFUSED the request (HTTP 400) -- e.g. its command policy.

    On the exec route a 400 means the argv failed the allowlist or the
    dangerous-pattern regex, which matches ``shutdown`` / ``reboot`` / ``mkfs`` /
    ``poweroff`` ANYWHERE in the joined argv -- so a perfectly ordinary file path
    such as ``src/shutdown.py`` is enough. Nothing was looked at: "not computed".

    Subclasses :class:`WorkspaceBackendUnavailableError` on purpose, so every
    pre-existing caller keeps its 503. Only the helpers that can honestly
    abstain (``find_directory``, ``count_lines``, ``lint_python_file``) catch it.
    """

    def __init__(self, container_name: str, detail: str) -> None:
        self.container_name = container_name
        self.detail = detail
        super().__init__(
            f"coding-containers refused the request for workspace '{container_name}' "
            f"(refused by workspace command policy): {detail}"
        )


POLICY_REFUSED_REASON = "refused by workspace command policy"


class WorkspaceSearchIncompleteError(WorkspaceError):
    """The search RAN, exited cleanly, and its output was CLIPPED on the way back.

    ⭐ THE FOURTH OUTCOME, and the one that hides best. The other three announce
    themselves: a 404 says "no such workspace", a non-zero exit says "the
    traversal failed", and a clean exit with parsed hits says "here they are".
    This one arrives as **exit 0, short stdout, and nothing else wrong** —
    ``truncated: true`` is the ONLY thing that distinguishes "I looked
    everywhere and this is all there was" from "I stopped reading your answer
    halfway". Both an exit-code guard and a stdout parser sail straight past it.

    coding-containers caps each stream at ``exec_max_output_bytes``
    (``src/config/settings.py:86``, 1 MiB by default) and reports the clip on
    every response (``src/routers/container_exec.py:408-415``, computed on all
    three runtime adapters). Its own ``find`` over ``/workspace`` already
    refuses to trust a clipped tree, in these words
    (``core/kubernetes_container_provisioner.py:2092-2096``): *"a repository
    large enough to hit it would otherwise return a subtree that looks like the
    whole thing — the same silent-success shape as a listing that reports 'no
    files' for a directory it was never able to read."*

    This was NOT theoretical on our two searches. Before G25 ``find_file`` pruned
    only ``.git`` (it now prunes ``VENDOR_EXCLUDE_DIRS``, which narrows but does
    not remove the risk), so after a ``uv sync`` — the normal state of a workspace, which
    coding-containers wires ``UV_EXTRA_INDEX_URL`` precisely to enable — a
    189-package ``.venv`` contributes thousands of ``__init__.py`` hits at
    ~80 bytes a line. ``find`` emits in readdir order, so whether the caller's
    real ``src/__init__.py`` survives the 1 MiB clip is a coin flip on
    directory order. When it does not, the exit code is 0 and the honest answer
    is "I could not finish", never "it is not there".

    Kept SEPARATE from :class:`WorkspaceBackendUnavailableError` on purpose: the
    backend was available and did its job.

    ⚠️ The router raises this one with a structured ``detail``, but read
    ``routers.validation._workspace_error`` before relying on the field names: the
    service's response envelope DROPS ``error_code``, so what a production caller
    actually receives is ``503 {"error": {"code": "HTTP_503", "message": ...,
    "details": {"container_name", "tool", "bytes_omitted"}}}``. A clipped look is
    separable from a timeout or a dead backend only by the PRESENCE of
    ``error.details`` — incidental, not structural.
    """

    def __init__(
        self,
        container_name: str,
        tool: str,
        bytes_omitted: int,
        detail: str | None = None,
    ) -> None:
        self.container_name = container_name
        self.tool = tool
        self.bytes_omitted = bytes_omitted
        super().__init__(
            detail
            or (
                f"The {tool} in workspace '{container_name}' exited cleanly but its output "
                f"was clipped at the exec output budget ({bytes_omitted} bytes omitted), so "
                f"the result is a partial view of the workspace. Refusing to publish it as "
                f"an answer: an absence computed from a truncated search is not an absence."
            )
        )


def validate_identifier(name: str, kind: str) -> str:
    """Return *name* if it is a bare identifier, else raise ``ValueError``.

    Applied before *any* interpolation. ``Class.method`` is handled by the
    caller splitting on the dot and validating each half.
    """
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(
            f"Invalid {kind} '{name}': expected a bare identifier "
            f"(letters, digits, underscore; not starting with a digit)."
        )
    return name


def validate_relative_path(path: str) -> str:
    """Return *path* if it is a safe workspace-relative path, else raise.

    Rejects absolute paths and any traversal: the caller is naming something
    inside their own workspace, and ``..`` would be a request to leave it.
    """
    if "\x00" in path:
        raise ValueError("Invalid file path: embedded null character.")

    cleaned = path.strip()
    if not cleaned:
        raise ValueError("Invalid file path: empty.")
    if cleaned.startswith("/"):
        raise ValueError(
            f"Invalid file path '{path}': must be relative to the workspace root, not absolute."
        )

    # ⚠️ Split into components and inspect them. The obvious one-liner,
    # ``path.lstrip("./")``, is WRONG and was caught by
    # ``test_path_traversal_refused``: ``lstrip`` takes a SET OF CHARACTERS, not
    # a prefix, so "../../etc/passwd" -> "etc/passwd" -- the traversal is
    # silently rewritten into a innocuous-looking relative path and the
    # subsequent ".." check finds nothing to reject.
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError(f"Invalid file path '{path}': path traversal is not permitted.")
    if not parts:
        raise ValueError("Invalid file path: empty.")
    return "/".join(parts)


async def exec_in_workspace(
    container_name: str,
    command: list[str],
    tenant_id: str | None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Run an allowlisted command in *container_name* and return the result.

    Raises:
        ContainerNotResolvableError: coding-containers answered 404.
        WorkspaceBackendUnavailableError: anything else went wrong, including the
            service being unconfigured or unreachable.
    """
    return await containers_post(
        f"/api/workspaces/{container_name}/exec",
        {"command": command, "workdir": WORKSPACE_ROOT, "timeout": timeout},
        container_name=container_name,
        tenant_id=tenant_id,
        read_timeout=float(timeout) + 15.0,
    )


async def containers_post(
    path: str,
    payload: dict[str, Any],
    *,
    container_name: str,
    tenant_id: str | None,
    read_timeout: float,
) -> dict[str, Any]:
    """POST *payload* to coding-containers at *path* as the gateway itself.

    The one outbound HTTP call this module makes: same shared client as the
    proxy (``routers.proxy.get_proxy_client``), same identity headers as every
    gateway-owned route (``identity_headers`` -- the customer tenant in
    ``X-Tenant-Id``, the gateway's own licence in ``Authorization``), and the
    same status mapping for every caller:

        404        -> ContainerNotResolvableError (unknown / other tenant's workspace)
        other 4xx/5xx, transport error, timeout, non-JSON
                   -> WorkspaceBackendUnavailableError ("I could not look")

    Raises:
        ContainerNotResolvableError: coding-containers answered 404.
        WorkspaceBackendUnavailableError: anything else went wrong, including the
            service being unconfigured or unreachable.
    """
    # ⛔ Imported HERE, not at module scope, and not as a style preference.
    # ``routers.proxy`` executes ``settings = get_settings()`` at IMPORT time
    # and freezes the resolved backend URLs into the module-level
    # ``PROXY_ROUTES`` dict (proxy.py:51-81). That dict is never rebuilt, so
    # whoever imports ``routers.proxy`` FIRST decides its contents for the whole
    # process. A module-level import here made importing this service module --
    # something a test collector does before any fixture runs -- enough to
    # freeze PROXY_ROUTES from an unset environment, leaving every prefix
    # mapped to "" and the proxy silently forwarding to a bare path.
    # That is exactly how it failed: `test_proxy_forwards_to_correct_backend`
    # saw url="/api/tests/run" instead of "http://coding-testing:8689/...".
    # ``services/validation.py:88`` already imports it this way for the same
    # reason; a service importing a router at module scope is also a layering
    # inversion. Keep it inside the function.
    from routers.proxy import get_proxy_client

    settings = get_settings()
    base = (settings.containers_url or "").rstrip("/")
    if not base:
        raise WorkspaceBackendUnavailableError(
            "CONTAINERS_URL is not configured on this gateway, so no workspace "
            "can be read. Refusing rather than answering from the gateway's own "
            "filesystem."
        )

    client = await get_proxy_client()
    try:
        response = await client.post(
            f"{base}{path}",
            json=payload,
            headers=identity_headers(tenant_id),
            timeout=httpx.Timeout(connect=5.0, read=read_timeout, write=30.0, pool=10.0),
        )
    except httpx.TimeoutException as exc:
        raise WorkspaceBackendUnavailableError(
            f"Timed out reading workspace '{container_name}' from coding-containers."
        ) from exc
    except httpx.HTTPError as exc:
        raise WorkspaceBackendUnavailableError(
            f"coding-containers is unreachable, so workspace '{container_name}' "
            f"could not be read: {exc}"
        ) from exc

    if response.status_code == 404:
        raise ContainerNotResolvableError(container_name)
    if response.status_code == 400:
        raise WorkspaceRequestRefusedError(container_name, response.text[:300])
    if response.status_code >= 400:
        raise WorkspaceBackendUnavailableError(
            f"coding-containers returned {response.status_code} for workspace "
            f"'{container_name}': {response.text[:300]}"
        )

    try:
        return response.json()
    except ValueError as exc:
        raise WorkspaceBackendUnavailableError(
            f"coding-containers returned a non-JSON body for workspace '{container_name}'."
        ) from exc


def _stdout_lines(result: dict[str, Any]) -> list[str]:
    """Non-empty stdout lines from an exec result."""
    return [line for line in (result.get("stdout") or "").splitlines() if line.strip()]


def _relative(path: str) -> str:
    """Strip the workspace root prefix from an absolute in-container path."""
    prefix = WORKSPACE_ROOT.rstrip("/") + "/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _find_prune_args() -> list[str]:
    """``find`` predicates that skip every path under a ``VENDOR_EXCLUDE_DIRS`` entry."""
    args: list[str] = []
    for d in VENDOR_EXCLUDE_DIRS:
        args += ["-not", "-path", f"*/{d}/*"]
    return args


def _refuse_incomplete_find(result: dict[str, Any], container_name: str) -> None:
    """Raise unless a ``find`` exec result is a COMPLETE traversal.

    Shared by ``find_file`` and ``find_module_files``; the reasoning for both
    guards (every non-zero ``find`` exit is an incomplete answer; a clipped
    exit-0 is the fourth outcome) is written out in ``find_file``.
    """
    exit_code = result.get("exit_code", 1)
    if exit_code != 0:
        raise WorkspaceBackendUnavailableError(
            f"find failed in workspace '{container_name}' "
            f"(exit {exit_code}): {(result.get('stderr') or '')[:300]}"
        )
    if result.get("truncated"):
        raise WorkspaceSearchIncompleteError(
            container_name=container_name,
            tool="find",
            bytes_omitted=int(result.get("bytes_omitted") or 0),
        )


async def find_file(
    container_name: str, file_path: str, tenant_id: str | None
) -> tuple[bool, list[str]]:
    """Does *file_path* exist in the workspace? Also return basename matches.

    One round trip answers both questions: ``find -name <basename>`` yields every
    candidate, the exact relative path decides existence, and the remainder are
    the "did you mean" suggestions.

    Language-agnostic — this is a filesystem question, not an AST one.
    """
    relative = validate_relative_path(file_path)
    basename = relative.rsplit("/", 1)[-1]

    result = await exec_in_workspace(
        container_name,
        [
            "find",
            WORKSPACE_ROOT,
            "-type",
            "f",
            "-name",
            basename,
            *_find_prune_args(),
        ],
        tenant_id,
        timeout=30,
    )

    # ⭐ THE SAME GUARD ``grep_definition`` CARRIES, AND FOR THE SAME REASON — a search that
    # could not finish must never be published as a confident absence. coding-containers wraps
    # every exec as `cd <workdir> && timeout <N> <argv>`
    # (core/kubernetes_container_provisioner.py:1912), so a `find` that outruns its 30s budget on
    # a large workspace returns **124 with EMPTY stdout**, which is byte-identical to "the file is
    # not there". Without this, that timeout is reported as ``valid=false``: a false negative that
    # PASSES a verification, which is the worst direction available here.
    #
    # ⚠️ The threshold is NOT `> 1` as it is on the grep path, and the difference is not an
    # oversight. grep spends exit 1 on "no match" — a computed answer — so only >1 can be an
    # error. `find` has no such code: absence is exit 0 with empty stdout, and POSIX gives it a
    # non-zero exit ONLY when the traversal itself failed (an unreadable directory, a vanished
    # path, the timeout). Every non-zero exit from `find` therefore means the answer is
    # incomplete, so every one of them is a refusal — 503 at the router (`_workspace_error`),
    # which the caller reads as "I could not look", never as a verdict.
    #
    # ⚠️ The default is **1**, not 0. ``exit_code`` is REQUIRED on
    # ``ExecResponse`` (container_exec.py:149-152) so its absence is latent today, but a
    # default of 0 is the wrong side of the argument: a body that lost the field would be read
    # as a successful traversal and published as a verdict. ~20 coding-containers call sites
    # default to 1 for exactly this reason. Absent evidence is a refusal, not a pass.
    # (Both guards run in ``_refuse_incomplete_find``, called after the next comment.)

    # ⭐ THE FOURTH OUTCOME — and the one the exit-code guard above CANNOT see. A clipped
    # response is exit **0** with short stdout: indistinguishable from a complete traversal by
    # every signal except this flag. ``ExecResponse`` exists in its current shape to make that
    # detectable ("a clipped result must be distinguishable from a complete one — otherwise the
    # cap trades an availability bug for a correctness one", container_exec.py:130-138), and the
    # flag is only useful to a caller that LOOKS at it. This handler used to not look.
    #
    # Reachable, not theoretical: before G25 this `find` pruned only `.git`, so a synced
    # `.venv` alone could fill the 1 MiB budget; vendored dirs are pruned now, but a large
    # first-party tree still can, and `find` emits in readdir order — whether the
    # caller's real file survives the clip is directory-order luck. See
    # ``WorkspaceSearchIncompleteError`` for the full measurement.
    _refuse_incomplete_find(result, container_name)

    candidates = [_relative(line.strip()) for line in _stdout_lines(result)]
    return (relative in candidates, candidates)


def _definition_pattern(name: str, kind: str) -> str:
    """The grep ERE for a Python ``class``/``def`` of *name* (already validated).

    Defence in depth: refuses a line break or NUL even though every caller has
    already validated *name*. ``grep`` reads a newline in its pattern as a
    SEPARATOR between patterns (logical OR), so one slipping past a validator
    turns "is this class defined?" into "does any line look like ``Foo(``?".
    """
    if any(ch in name for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"Invalid {kind} {name!r}: line breaks and NUL are not permitted.")
    if kind == "class":
        return rf"^[[:space:]]*class[[:space:]]+{name}[[:space:]]*[(:]"
    return rf"^[[:space:]]*(async[[:space:]]+)?def[[:space:]]+{name}[[:space:]]*\("


async def grep_definition(
    container_name: str,
    name: str,
    tenant_id: str | None,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    """Locate Python ``def``/``class`` definitions of *name* in the workspace.

    ``kind`` is ``"function"`` or ``"class"``. Returns one entry per definition
    site: ``{"file": <relative path>, "line": <int>, "source": <matched line>}``.

    ⚠️ This searches Python definition syntax in ``*.py`` files only. An empty
    result therefore means "no Python definition of this name", NOT "this symbol
    does not exist" — every caller must publish ``searched_languages`` alongside
    the verdict so the two cannot be confused.
    """
    validate_identifier(name, kind)

    pattern = _definition_pattern(name, kind)

    result = await exec_in_workspace(
        container_name,
        [
            "grep",
            "-rnE",
            _FILE_GLOB,
            *(f"--exclude-dir={d}" for d in VENDOR_EXCLUDE_DIRS),
            pattern,
            WORKSPACE_ROOT,
        ],
        tenant_id,
        timeout=60,
    )

    return _grep_matches(result, container_name)


def _grep_matches(result: dict[str, Any], container_name: str) -> list[dict[str, Any]]:
    """Parse a ``grep -n`` exec result into definition sites, refusing incomplete searches."""
    # grep exits 1 for "no matches" — a legitimate, computed answer, not a
    # failure. Only exit codes above 1 mean grep itself could not run, and the
    # exec call above has already raised for anything that is a transport or
    # authorisation problem.
    #
    # ⭐ The case that matters most here is **124**. coding-containers wraps
    # every exec as `cd <workdir> && timeout <N> <argv>`
    # (core/kubernetes_container_provisioner.py:1912), so a grep that outruns
    # its budget on a large repo comes back 124 with EMPTY stdout — which is
    # byte-identical to "this symbol does not exist". Treating >1 as an error
    # is what stops a timed-out search from being published as a confident
    # absence. That would be the original defect reintroduced by the back door.
    #
    # ⚠️ The default is **2**, not 0 and not the 1 that ``find_file`` uses. The rule is
    # "absent evidence must land on the refusing side of THIS guard", and this guard refuses
    # above 1 — so the fail-closed default here is 2, exactly as the fail-closed default there
    # is 1. Defaulting to 1 would mean a body that lost ``exit_code`` was read as grep's "no
    # match", i.e. published as a confident absence. Same argument as the differing thresholds.
    #
    # ⭐ THE FOURTH OUTCOME, identically to ``find_file`` — and it must be guarded on BOTH
    # helpers or the asymmetry this function's own guard exists to remove is simply recreated
    # one field over. A clipped grep is exit 0 (or 1) with short stdout: the exit-code guard
    # above sees nothing wrong, the parser below yields a SHORT match list, and the caller gets
    # a confident "no definition" or an under-counted set of definition sites. ``--exclude-dir``
    # now prunes ``VENDOR_EXCLUDE_DIRS``, but a large first-party tree can still fill the 1 MiB
    # budget with a common symbol name.
    _refuse_incomplete_grep(result, container_name)

    matches: list[dict[str, Any]] = []
    for line in _stdout_lines(result):
        # grep -n output: <path>:<lineno>:<text>
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        matches.append(
            {
                "file": _relative(parts[0]),
                "line": int(parts[1]),
                "source": parts[2].strip(),
            }
        )
    return matches


async def find_module_files(
    container_name: str, candidates: list[str], tenant_id: str | None
) -> set[str]:
    """Which of the repo-relative *candidates* exist as files in the workspace.

    *candidates* come from ``services.dotted_names`` and are built only from
    segments that passed the identifier check, so they carry no glob or shell
    metacharacters. One ``find`` answers all of them; ``-maxdepth`` bounds the
    walk to the deepest candidate so a large tree is not traversed for a
    question about at most a handful of fixed paths.

    Refuses exactly like ``find_file``: any non-zero exit (including the 124
    timeout) and a clipped response raise, because an absence computed from an
    incomplete traversal would be published as "external" -- a verdict the
    caller may act on.
    """
    if not candidates:
        return set()
    depth = max(c.count("/") for c in candidates) + 1
    expr: list[str] = []
    for i, rel in enumerate(candidates):
        if i:
            expr.append("-o")
        expr += ["-path", f"{WORKSPACE_ROOT}/{validate_relative_path(rel)}"]

    result = await exec_in_workspace(
        container_name,
        ["find", WORKSPACE_ROOT, "-maxdepth", str(depth), "-type", "f", "(", *expr, ")"],
        tenant_id,
        timeout=30,
    )
    _refuse_incomplete_find(result, container_name)
    wanted = set(candidates)
    return {
        rel for rel in (_relative(line.strip()) for line in _stdout_lines(result)) if rel in wanted
    }


async def grep_class_in_files(
    container_name: str, class_name: str, files: list[str], tenant_id: str | None
) -> list[dict[str, Any]]:
    """Python ``class`` definitions of *class_name* inside *files* ONLY.

    The dotted-name counterpart of ``grep_definition``: the search is confined to
    the resolved module's file(s), never the whole repo. ``-H`` forces the
    ``<path>:`` prefix even for a single file so the shared parser applies.
    """
    validate_identifier(class_name, "class")
    if not files:
        return []
    result = await exec_in_workspace(
        container_name,
        [
            "grep",
            "-nHE",
            _definition_pattern(class_name, "class"),
            *(f"{WORKSPACE_ROOT}/{validate_relative_path(f)}" for f in files),
        ],
        tenant_id,
        timeout=30,
    )
    return _grep_matches(result, container_name)


_NO_SUCH_FILE = "no such file or directory"


async def read_workspace_file_checked(
    container_name: str, file_path: str, tenant_id: str | None
) -> str | None:
    """Return the WHOLE text of *file_path*, ``None`` only for a confirmed absence.

    GAP-2, closed. The unchecked predecessor (``read_workspace_file``) mapped
    every non-zero ``cat`` exit to "absent", used clipped output as the whole
    file and defaulted a missing ``exit_code`` to success, so a timed-out read
    was published as "does not exist", a syntax verdict was computed from half a
    file, and a malformed body certified an empty file as valid. Now:

    ===============================================  ==================================
    ``cat`` exit 1 + "No such file or directory"     ``None`` -- the workspace confirmed
                                                     the file is not there
    exit 0, not ``truncated``                        the file's text
    exit 124 (the exec ``timeout`` wrapper)          raises ``WorkspaceSearchIncompleteError``
    exit 0 with ``truncated: true``                  raises ``WorkspaceSearchIncompleteError``
    ``exit_code`` missing                            raises ``WorkspaceSearchIncompleteError``
    any other non-zero exit (a directory, EACCES..)  raises ``WorkspaceSearchIncompleteError``
    ===============================================  ==================================

    Transport / unknown-workspace failures raise as :func:`exec_in_workspace`
    does. Every validation caller turns the incomplete case into
    ``not_computed`` / ``valid=null`` with a note -- never a verdict.
    """
    relative = validate_relative_path(file_path)
    result = await exec_in_workspace(
        container_name,
        ["cat", f"{WORKSPACE_ROOT}/{relative}"],
        tenant_id,
        timeout=30,
    )
    exit_code = result.get("exit_code")
    stderr = (result.get("stderr") or "").strip()

    def _incomplete(why: str) -> WorkspaceSearchIncompleteError:
        return WorkspaceSearchIncompleteError(
            container_name=container_name,
            tool="cat",
            bytes_omitted=int(result.get("bytes_omitted") or 0),
            detail=f"Reading '{relative}' in workspace '{container_name}' {why}, so the "
            "file's content is NOT known (not computed) -- neither present nor absent.",
        )

    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        raise _incomplete("returned no exit_code")
    if exit_code == 124:
        raise _incomplete("timed out (exit 124)")
    if exit_code == 1 and _NO_SUCH_FILE in stderr.lower():
        return None
    if exit_code != 0:
        raise _incomplete(f"failed (exit {exit_code}: {stderr[:200]})")
    if result.get("truncated"):
        raise _incomplete(
            f"was clipped at the exec output budget ({int(result.get('bytes_omitted') or 0)} "
            "bytes omitted)"
        )
    return result.get("stdout") or ""


async def grep_files_mentioning(container_name: str, name: str, tenant_id: str | None) -> list[str]:
    """Workspace-relative non-Python source files containing the WORD *name*.

    Also lists files of ``UNSEARCHED_MENTION_EXTENSIONS`` (no symbol extraction):
    the caller turns a mention there into "not computed", never "absent".

    Candidate discovery for the multi-language reference search: a file that
    does not contain the word cannot define it, so a file NOT listed here is
    refuted cheaply; a listed file is only a candidate and is confirmed through
    coding-analysis' symbol extraction. Vendored dirs are excluded exactly as in
    ``grep_definition``. Refuses an incomplete search (exit >1, 124, clipped)
    by raising, like every other search helper.
    """
    validate_identifier(name, "name")
    result = await exec_in_workspace(
        container_name,
        [
            "grep",
            "-rlw",
            *(f"--include=*{ext}" for ext in NON_PYTHON_EXTENSIONS),
            *(f"--include=*{ext}" for ext in UNSEARCHED_MENTION_EXTENSIONS),
            *(f"--exclude-dir={d}" for d in VENDOR_EXCLUDE_DIRS),
            "-e",
            name,
            WORKSPACE_ROOT,
        ],
        tenant_id,
        timeout=60,
    )
    _refuse_incomplete_grep(result, container_name)
    return sorted({_relative(line.strip()) for line in _stdout_lines(result)})


def _refuse_incomplete_grep(result: dict[str, Any], container_name: str) -> None:
    """Raise unless a ``grep`` exec result is a COMPLETE search.

    The guards every grep helper shares (exit >1 incl. 124, a missing exit code
    read as 2, clipped output); the reasoning is written out in ``_grep_matches``.
    """
    exit_code = result.get("exit_code", 2)
    if exit_code > 1:
        raise WorkspaceBackendUnavailableError(
            f"grep failed in workspace '{container_name}' "
            f"(exit {exit_code}): {(result.get('stderr') or '')[:300]}"
        )
    if result.get("truncated"):
        raise WorkspaceSearchIncompleteError(
            container_name=container_name,
            tool="grep",
            bytes_omitted=int(result.get("bytes_omitted") or 0),
        )


def _search_root(root: str) -> str:
    """``/workspace`` or ``/workspace/<root>`` (*root* validated, workspace-relative)."""
    return f"{WORKSPACE_ROOT}/{validate_relative_path(root)}" if root else WORKSPACE_ROOT


async def list_source_files(
    container_name: str,
    extensions: tuple[str, ...],
    tenant_id: str | None,
    root: str = "",
    *,
    maxdepth: int | None = None,
) -> list[str]:
    """Every workspace-relative file with one of *extensions*, vendored dirs pruned.

    ONE ``find`` for the tree under *root* (the whole workspace by default);
    ``maxdepth=1`` lists only the files directly in *root* (a shard split).
    Refuses an incomplete traversal like ``find_file``.
    """
    names: list[str] = []
    for i, ext in enumerate(extensions):
        if i:
            names.append("-o")
        names += ["-name", f"*{ext}"]
    depth = ["-maxdepth", str(maxdepth)] if maxdepth is not None else []
    result = await exec_in_workspace(
        container_name,
        ["find", _search_root(root), *depth, "-type", "f", "(", *names, ")", *_find_prune_args()],
        tenant_id,
        timeout=60,
    )
    _refuse_incomplete_find(result, container_name)
    return sorted({_relative(line.strip()) for line in _stdout_lines(result)})


def _is_vendor_dir(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in VENDOR_EXCLUDE_DIRS)


async def list_child_dirs(container_name: str, root: str, tenant_id: str | None) -> list[str]:
    """The workspace-relative subdirectories directly under *root*, vendored dirs dropped.

    Used to split a search shard whose output was clipped. Refuses an
    incomplete traversal like ``find_file``.
    """
    result = await exec_in_workspace(
        container_name,
        ["find", _search_root(root), "-mindepth", "1", "-maxdepth", "1", "-type", "d"],
        tenant_id,
        timeout=60,
    )
    _refuse_incomplete_find(result, container_name)
    out = {_relative(line.strip().rstrip("/")) for line in _stdout_lines(result)}
    return sorted(d for d in out if d and not _is_vendor_dir(d.rsplit("/", 1)[-1]))


def _grep_argv(
    flags: list[str],
    pattern: str,
    extensions: tuple[str, ...],
    root: str,
    files: list[str] | None,
) -> list[str]:
    """``grep -r`` over *root*, or (``files`` given) over exactly those files."""
    if files is not None:
        targets = [f"{WORKSPACE_ROOT}/{validate_relative_path(f)}" for f in files]
        return ["grep", "-nHE", *flags, "-e", pattern, *targets]
    return [
        "grep",
        "-rnE",
        *flags,
        *(f"--include=*{ext}" for ext in extensions),
        *(f"--exclude-dir={d}" for d in VENDOR_EXCLUDE_DIRS),
        "-e",
        pattern,
        _search_root(root),
    ]


async def grep_lines(
    container_name: str,
    pattern: str,
    extensions: tuple[str, ...],
    tenant_id: str | None,
    root: str = "",
    *,
    files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """``grep -rnE <pattern>`` over files with *extensions*: ``[{file, line, source}]``.

    Searches the tree under *root* (the whole workspace by default), or only
    the explicit *files* (a shard split). Refuses an incomplete search (exit
    >1, 124, clipped) by raising.
    """
    if files is not None and not files:
        return []
    result = await exec_in_workspace(
        container_name,
        _grep_argv([], pattern, extensions, root, files),
        tenant_id,
        timeout=60,
    )
    _refuse_incomplete_grep(result, container_name)
    matches: list[dict[str, Any]] = []
    for line in _stdout_lines(result):
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        matches.append({"file": _relative(parts[0]), "line": int(parts[1]), "source": parts[2]})
    return matches


async def grep_blocks(
    container_name: str,
    pattern: str,
    extensions: tuple[str, ...],
    tenant_id: str | None,
    after: int,
    root: str = "",
    *,
    files: list[str] | None = None,
) -> list[dict[str, Any]]:
    """``grep -rnE -A <after> <pattern>``: ``[{file, line, source, after: [lines]}]``.

    ``after`` holds the lines that FOLLOW the match, consecutively, as far as
    grep printed them (at most *after*; fewer at end of file). A context line
    that cannot be attributed is dropped, which only shortens ``after`` -- the
    caller treats a block it cannot close as unread, never as complete.
    *root* / *files* scope the search as in ``grep_lines``.
    Refuses an incomplete search (exit >1, 124, clipped) by raising.
    """
    if files is not None and not files:
        return []
    result = await exec_in_workspace(
        container_name,
        _grep_argv(["-A", str(after)], pattern, extensions, root, files),
        tenant_id,
        timeout=60,
    )
    _refuse_incomplete_grep(result, container_name)
    text: dict[str, dict[int, str]] = {}
    matches: list[tuple[str, int]] = []
    current: str | None = None
    for raw in (result.get("stdout") or "").splitlines():
        if raw == "--":
            continue
        if current is not None and raw.startswith(current + "-"):
            number, sep, rest = raw[len(current) + 1 :].partition("-")
            if sep and number.isdigit():
                text.setdefault(current, {})[int(number)] = rest
                continue
        if current is not None and raw.startswith(current + ":"):
            number, sep, rest = raw[len(current) + 1 :].partition(":")
            if sep and number.isdigit():
                text.setdefault(current, {})[int(number)] = rest
                matches.append((current, int(number)))
                continue
        parts = raw.split(":", 2)
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        current = parts[0]
        text.setdefault(current, {})[int(parts[1])] = parts[2]
        matches.append((current, int(parts[1])))
    blocks: list[dict[str, Any]] = []
    for path, number in matches:
        lines = text[path]
        following: list[str] = []
        n = number + 1
        while n in lines and len(following) < after:
            following.append(lines[n])
            n += 1
        blocks.append(
            {"file": _relative(path), "line": number, "source": lines[number], "after": following}
        )
    return blocks


# ---------------------------------------------------------------------------
# Snippet / import resolution — DELEGATED to coding-containers, never redone here
# ---------------------------------------------------------------------------

# coding-containers' snippet validator. It resolves every import of the snippet
# INSIDE the named workspace (repo modules, stdlib, the repo venv's installed and
# declared packages) and returns a top-level ``verdict`` (coding-containers#171).
# The gateway forwards to it rather than growing a second resolver: one resolver
# cannot disagree with itself, two eventually will.
MULTI_LINE_SNIPPET_PATH = "/api/validation/multi-line-code-snippet"

# The resolver re-execs under the repo venv and imports nothing, but on a cold
# workspace it is a few execs; 30s each upstream, so allow a margin above that.
_SNIPPET_READ_TIMEOUT = 90.0


async def validate_snippet_in_workspace(
    container_name: str, code: str, tenant_id: str | None
) -> dict[str, Any]:
    """Validate *code* (syntax + import resolution) in *container_name*.

    Execution is deliberately OFF (``include_execution_validation=false``): this
    is a "does it compile and do its imports exist" question, and running a
    caller's snippet is a different, side-effecting one.

    Returns coding-containers' ``MultiLineValidationResponse`` body verbatim.
    Raises exactly as :func:`containers_post` does.
    """
    return await containers_post(
        MULTI_LINE_SNIPPET_PATH,
        {
            "code_snippet": code,
            "container_name": container_name,
            "include_execution_validation": False,
        },
        container_name=container_name,
        tenant_id=tenant_id,
        read_timeout=_SNIPPET_READ_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Located references: directories, line counts
# ---------------------------------------------------------------------------

_FIND_GLOB_CHARS = re.compile(r"([\\*?\[\]])")


def _escape_find_pattern(value: str) -> str:
    """Escape ``find -path`` glob metacharacters so *value* matches literally."""
    return _FIND_GLOB_CHARS.sub(r"\\\1", value)


async def find_directory(container_name: str, dir_path: str, tenant_id: str | None) -> bool | None:
    """Is *dir_path* a directory in the workspace? ``None`` = not computed.

    ``find_file`` searches ``-type f`` only, so a directory reads as absent
    there. This asks the directory question separately, bounded by
    ``-maxdepth`` to the path's own depth and stopped at the first hit. Refuses
    (raises) on an incomplete traversal exactly like ``find_file``. Returns
    ``None`` when coding-containers' command policy refuses the argv
    (:class:`WorkspaceRequestRefusedError`): nothing was looked at.
    """
    relative = validate_relative_path(dir_path)
    depth = relative.count("/") + 1
    try:
        result = await exec_in_workspace(
            container_name,
            [
                "find",
                WORKSPACE_ROOT,
                "-maxdepth",
                str(depth),
                "-type",
                "d",
                "-path",
                f"{WORKSPACE_ROOT}/{_escape_find_pattern(relative)}",
                "-print",
                "-quit",
            ],
            tenant_id,
            timeout=30,
        )
    except WorkspaceRequestRefusedError:
        return None
    _refuse_incomplete_find(result, container_name)
    wanted = f"{WORKSPACE_ROOT}/{relative}"
    return any(line.strip() == wanted for line in _stdout_lines(result))


async def count_lines(container_name: str, file_path: str, tenant_id: str | None) -> int | None:
    """Number of lines in *file_path* (a final unterminated line counts), or ``None``.

    ``awk 'END{print NR}'`` rather than ``wc -l``: ``wc`` counts newlines, so a
    file whose last line has no trailing newline would be one short and a
    reference to that last line would be called out of range. ``None`` means the
    count could not be computed (the command failed or its output was not a
    number) -- never "zero lines".
    """
    relative = validate_relative_path(file_path)
    try:
        result = await exec_in_workspace(
            container_name,
            ["awk", "END{print NR}", f"{WORKSPACE_ROOT}/{relative}"],
            tenant_id,
            timeout=30,
        )
    except WorkspaceRequestRefusedError:
        return None  # refused by workspace command policy: not computed
    if result.get("exit_code", 1) != 0 or result.get("truncated"):
        return None
    out = (result.get("stdout") or "").strip()
    return int(out) if out.isdigit() else None


# ---------------------------------------------------------------------------
# Lint — pyflakes, else ruff, else an explicit not_computed. Never a silent pass.
# ---------------------------------------------------------------------------

_MISSING_TOOL_MARKERS = ("no module named", "command not found", "not found", "no such file")


def _tool_missing(result: dict[str, Any]) -> bool:
    """Did the exec fail because the TOOL is absent (not because the file is bad)?"""
    exit_code = result.get("exit_code", 1)
    if exit_code in (126, 127):
        return True
    stderr = (result.get("stderr") or "").lower()
    return (
        exit_code != 0
        and not (result.get("stdout") or "").strip()
        and any(marker in stderr for marker in _MISSING_TOOL_MARKERS)
    )


_VENV_PYTHON = f"{WORKSPACE_ROOT}/.venv/bin/python"


def _not_computed(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "not_computed", "reason": reason, **extra}


def _parses_here(content: str | None) -> bool:
    """Does the GATEWAY's own interpreter parse *content*? False when unknown."""
    if content is None:
        return False
    try:
        ast.parse(content)
    except SyntaxError, ValueError:
        return False
    return True


def _pyflakes_verdict(
    result: dict[str, Any], content: str | None = None, interpreter: str | None = None
) -> dict[str, Any]:
    """pyflakes: exit 0 = clean, exit 1 WITH findings on stdout = invalid.

    ⚠️ INTERPRETER SKEW. pyflakes parses with the interpreter it runs under,
    which may be OLDER than the code (the container's system python vs a repo on
    3.14). Then correct code -- ``except A, B:`` (PEP 758) -- comes back as a
    syntax error, an ``invalid`` verdict on a correct file. pyflakes reports a
    file it cannot parse on STDERR with empty stdout (and reports nothing else
    for it), so when that is the ONLY output and the gateway's own ``ast.parse``
    of the same *content* succeeds, the two interpreters disagree about the
    language and the lint is ``not_computed``. A file the gateway cannot parse
    either keeps the ``invalid`` verdict; so does unknown *content*. So does a
    run under the repo's own venv python (``_VENV_PYTHON``): that interpreter IS
    the target, so its syntax error stands -- there is no skew to excuse.
    """
    exit_code = result.get("exit_code", 2)
    lines = _stdout_lines(result)
    if exit_code == 0 and not lines:
        return {"status": "valid", "tool": "pyflakes", "issues": []}
    if exit_code == 1 and lines:
        return {
            "status": "invalid",
            "tool": "pyflakes",
            "issues": [line.strip() for line in lines],
            "truncated": bool(result.get("truncated")),
        }
    # pyflakes reports a file it cannot PARSE (syntax error, undecodable source)
    # on stderr, exit 1, empty stdout. That is a finding about the file. A
    # traceback on stderr is pyflakes itself crashing -- not a finding.
    stderr = (result.get("stderr") or "").strip()
    if exit_code == 1 and stderr and "Traceback" not in stderr:
        if interpreter != _VENV_PYTHON and _parses_here(content):
            return _not_computed(
                "linter python older than the code: pyflakes reported only a syntax "
                "error, but the file parses under the gateway's Python "
                f"{sys.version_info.major}.{sys.version_info.minor}. Lint was NOT run; "
                f"pyflakes said: {stderr[:300]}",
                tool="pyflakes",
            )
        return {
            "status": "invalid",
            "tool": "pyflakes",
            "issues": [line.strip() for line in stderr.splitlines() if line.strip()],
            "truncated": bool(result.get("truncated")),
        }
    return _not_computed(
        f"pyflakes exited {exit_code} without a usable result: "
        f"{(result.get('stderr') or '')[:300]}",
        tool="pyflakes",
    )


def _ruff_verdict(result: dict[str, Any]) -> dict[str, Any]:
    """ruff --output-format=json: exit 0 = clean, exit 1 = violations, else error."""
    import json

    exit_code = result.get("exit_code", 2)
    if exit_code not in (0, 1) or result.get("truncated"):
        return _not_computed(
            f"ruff exited {exit_code}"
            + (" with clipped output" if result.get("truncated") else "")
            + f": {(result.get('stderr') or '')[:300]}",
            tool="ruff",
        )
    try:
        findings = json.loads(result.get("stdout") or "[]")
    except ValueError:
        return _not_computed("ruff output was not JSON", tool="ruff")
    if not isinstance(findings, list):
        return _not_computed("ruff output was not a JSON list", tool="ruff")
    if exit_code == 0 and not findings:
        return {"status": "valid", "tool": "ruff", "issues": []}
    if exit_code == 1 and findings:
        return {
            "status": "invalid",
            "tool": "ruff",
            "issues": [
                f"{(f.get('location') or {}).get('row')}: {f.get('code')} {f.get('message')}"
                for f in findings
                if isinstance(f, dict)
            ],
        }
    return _not_computed(
        f"ruff exit {exit_code} disagrees with its {len(findings)} findings", tool="ruff"
    )


# The repo's own interpreter first: it is the Python the code is written for, so
# it cannot reject the code's syntax as "too new". coding-containers' allowlist
# matches argv[0] by BASENAME (``src/runtime/policy.py`` validate_command: a
# path is reduced to its last segment), so ``/workspace/.venv/bin/python`` is
# admitted as ``python``. Most repo venvs do not carry pyflakes; then the
# system ``python3`` is used and the skew check in ``_pyflakes_verdict`` applies.
_PYFLAKES_INTERPRETERS = (_VENV_PYTHON, "python3")


async def lint_python_file(
    container_name: str,
    file_path: str,
    tenant_id: str | None,
    content: str | None = None,
) -> dict[str, Any]:
    """Lint a Python file in the workspace with the first linter that is present.

    Order: ``/workspace/.venv/bin/python -m pyflakes``, then ``python3 -m
    pyflakes`` (pure undefined-name / unused-import checks, no project config),
    then ``ruff check --no-fix`` (the repo's own config). *content* (the file's
    text, when the caller already has it) lets a pyflakes syntax error be
    cross-checked against the gateway's parser -- see ``_pyflakes_verdict``.
    A command-policy refusal (400) is ``not_computed``, not an error.
    If neither is installed the result is ``status: not_computed`` with the
    reason -- an absent linter is never reported as a clean file.

    Returns ``{"status": "valid"|"invalid"|"not_computed", "tool", "issues"|"reason"}``.
    Transport / unknown-workspace failures raise as :func:`exec_in_workspace` does.
    """
    relative = validate_relative_path(file_path)
    target = f"{WORKSPACE_ROOT}/{relative}"

    try:
        for interpreter in _PYFLAKES_INTERPRETERS:
            try:
                pyflakes = await exec_in_workspace(
                    container_name, [interpreter, "-m", "pyflakes", target], tenant_id, timeout=60
                )
            except WorkspaceRequestRefusedError:
                raise
            except WorkspaceBackendUnavailableError:
                # A missing venv binary can surface as an exec error rather than
                # exit 127. Only the venv attempt may fall through; the system
                # python3 attempt re-raises (that is a genuinely dead backend).
                if interpreter == _PYFLAKES_INTERPRETERS[-1]:
                    raise
                continue
            if not _tool_missing(pyflakes):
                return {
                    **_pyflakes_verdict(pyflakes, content, interpreter),
                    "interpreter": interpreter,
                }

        ruff = await exec_in_workspace(
            container_name,
            ["ruff", "check", "--no-fix", "--output-format=json", target],
            tenant_id,
            timeout=60,
        )
    except WorkspaceRequestRefusedError as exc:
        return _not_computed(
            f"{POLICY_REFUSED_REASON}: coding-containers would not run the linter "
            f"on this path, so lint was NOT run ({exc.detail[:200]}).",
            tool=None,
        )
    if not _tool_missing(ruff):
        return _ruff_verdict(ruff)

    return _not_computed(
        "No linter is available in this workspace: neither the repo venv's python nor "
        "`python3` has pyflakes installed and `ruff` is not on PATH. Lint was NOT run; "
        "this is not a clean result.",
        tool=None,
    )
