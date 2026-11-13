"""Tests for services/container_workspace.py — the workspace reader.

This module is the thing that makes the validation endpoints capable of being
WRONG, which is the property they previously lacked: before it existed, every
reference question was answered from the gateway's own empty filesystem, so
"not found" was a constant rather than a measurement.

The tests below therefore concentrate on the two boundaries where a constant
could creep back in:

1. **Parsing** — a ``find``/``grep`` result must produce a different answer than
   an empty one. Every behavioural test is a PAIR for that reason.
2. **Error mapping** — "I could not look" (503) and "there is no such
   workspace" (404) must never be collapsed into a verdict.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ⚠️ Importing this module at module scope is only safe because
# ``services.container_workspace`` does NOT import ``routers.proxy`` at module
# scope. It used to, and that made merely COLLECTING this file freeze
# ``routers.proxy.PROXY_ROUTES`` from an environment conftest had not populated
# yet -- every backend prefix mapped to "", and
# ``test_e2e_proxy.py::test_proxy_forwards_to_correct_backend`` then failed on a
# bare "/api/tests/run" URL. Do not "tidy" that deferred import back up to the
# top of container_workspace.py, and do not add env-setting side effects here to
# compensate for one.
from services import container_workspace
from services.container_workspace import (
    ContainerNotResolvableError,
    WorkspaceBackendUnavailableError,
    WorkspaceSearchIncompleteError,
    exec_in_workspace,
    find_file,
    grep_definition,
    read_workspace_file_checked,
    validate_identifier,
    validate_relative_path,
)


def _exec_result(
    stdout: str = "",
    exit_code: int = 0,
    stderr: str = "",
    truncated: bool = False,
    bytes_omitted: int = 0,
) -> dict:
    """The shape coding-containers actually returns (``ExecResponse``).

    ``truncated``/``bytes_omitted`` are part of that contract and are modelled
    here because omitting them is exactly how a caller stops looking at them.
    """
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration": 0.01,
        "truncated": truncated,
        "bytes_omitted": bytes_omitted,
    }


def _patch_exec(result):
    if isinstance(result, BaseException):
        return patch(
            "services.container_workspace.exec_in_workspace", new=AsyncMock(side_effect=result)
        )
    return patch(
        "services.container_workspace.exec_in_workspace", new=AsyncMock(return_value=result)
    )


# ---------------------------------------------------------------------------
# validate_relative_path
# ---------------------------------------------------------------------------


class TestValidateRelativePath:
    """Regression tests for the traversal guard.

    ⚠️ The first implementation used ``path.lstrip("./")`` to drop a leading
    "./" and then checked for a ".." component. ``lstrip`` takes a SET OF
    CHARACTERS, not a prefix, so "../../etc/passwd" was rewritten to
    "etc/passwd" and the ".." check then found nothing to reject — the guard
    silently laundered the traversal into a valid-looking path. These cases pin
    the fix.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "../../etc/passwd",
            "..",
            "src/../../../etc/shadow",
            "./../secrets.env",
        ],
    )
    def test_traversal_is_refused(self, path):
        with pytest.raises(ValueError, match="traversal"):
            validate_relative_path(path)

    def test_absolute_path_is_refused(self):
        with pytest.raises(ValueError, match="absolute"):
            validate_relative_path("/etc/passwd")

    def test_null_byte_is_refused(self):
        with pytest.raises(ValueError, match="null"):
            validate_relative_path("src/main\x00.py")

    @pytest.mark.parametrize("path", ["", "   ", ".", "./"])
    def test_empty_is_refused(self, path):
        with pytest.raises(ValueError):
            validate_relative_path(path)

    @pytest.mark.parametrize(
        "given,expected",
        [
            ("src/main.py", "src/main.py"),
            ("./src/main.py", "src/main.py"),
            ("src//main.py", "src/main.py"),
            ("pyproject.toml", "pyproject.toml"),
            ("a/b/c/d.txt", "a/b/c/d.txt"),
        ],
    )
    def test_normal_paths_survive(self, given, expected):
        assert validate_relative_path(given) == expected

    def test_a_filename_merely_containing_dots_is_not_traversal(self):
        """Guard against an over-broad fix that rejects legitimate names."""
        assert validate_relative_path("src/..hidden..py") == "src/..hidden..py"


# ---------------------------------------------------------------------------
# validate_identifier
# ---------------------------------------------------------------------------


class TestValidateIdentifier:
    @pytest.mark.parametrize("name", ["foo", "_private", "CamelCase", "with_123"])
    def test_identifiers_pass(self, name):
        assert validate_identifier(name, "function") == name

    @pytest.mark.parametrize(
        "name",
        ["foo; rm -rf /", "foo bar", "foo.bar", "1foo", "", "foo()", "fo|o", "-rf", ".*"],
    )
    def test_non_identifiers_are_refused(self, name):
        """The name reaches both a regex and an argv, so nothing else is allowed."""
        with pytest.raises(ValueError):
            validate_identifier(name, "function")


# ---------------------------------------------------------------------------
# find_file
# ---------------------------------------------------------------------------


class TestFindFile:
    async def test_present_and_absent_differ(self):
        """THE PAIR — identical code path, opposite answers.

        ⚠️ The absent half is exit **0**, not 1. ``find`` has no "no match" exit
        code the way ``grep`` does: absence is a successful traversal that
        printed nothing. This fixture used to say ``exit_code=1``, which is a
        traversal FAILURE — and reading that as a verdict is the very defect
        ``test_find_traversal_error_is_a_refusal`` now pins.
        """
        with _patch_exec(_exec_result(stdout="/workspace/src/main.py\n")):
            present, _ = await find_file("c1", "src/main.py", "t1")
        with _patch_exec(_exec_result(stdout="", exit_code=0)):
            absent, _ = await find_file("c1", "src/main.py", "t1")
        assert present is True
        assert absent is False

    async def test_find_timeout_124_is_a_refusal_not_an_absence(self):
        """⭐ The case the guard exists for.

        coding-containers runs every exec as ``timeout <N> <argv>``, so a
        ``find`` that outruns its 30s budget returns 124 with EMPTY stdout —
        byte-identical, on the wire, to "the file is not there". Publishing that
        as ``valid=false`` is a false negative that PASSES a verification. It
        must raise, so the router answers 503 ("I could not look").
        """
        with _patch_exec(_exec_result(stdout="", exit_code=124, stderr="")):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await find_file("c1", "src/main.py", "t1")

    async def test_find_traversal_error_is_a_refusal(self):
        """Any non-zero exit means the search did not complete — not an answer."""
        with _patch_exec(
            _exec_result(stdout="", exit_code=1, stderr="find: '/workspace/x': Permission denied")
        ):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await find_file("c1", "src/main.py", "t1")

    async def test_a_partial_result_is_refused_rather_than_answered(self):
        """A non-zero exit that DID print hits is still refused.

        This is the direction that decides the design: the file could be
        somewhere under the subtree the traversal could not read, so "found what
        I could" is not "I looked everywhere". Fail closed.
        """
        with _patch_exec(
            _exec_result(stdout="/workspace/tests/main.py\n", exit_code=1, stderr="find: ...")
        ):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await find_file("c1", "src/main.py", "t1")

    async def test_removing_the_guard_reintroduces_the_false_negative(self):
        """MUTANT — delete the guard, and the 124 timeout reads as "not there".

        Proves the tests above discriminate rather than passing incidentally:
        the module is re-executed with the ``if exit_code != 0:`` block cut out,
        and the SAME timeout fixture that raises on the real module returns a
        confident, wrong ``False``.
        """
        src = Path(container_workspace.__file__).read_text(encoding="utf-8")
        guard = re.search(
            r"\n    exit_code = result\.get\(\"exit_code\", 1\)\n"
            r"    if exit_code != 0:\n"
            r"(?:        .*\n|\n)*?        \)\n",
            src,
        )
        assert guard is not None, "the guard's anchor no longer matches — update this mutant"
        mutated = src.replace(guard.group(0), "\n")
        assert mutated != src

        ns: dict = {
            "__name__": "container_workspace_mutant",
            "__file__": container_workspace.__file__,
        }
        exec(compile(mutated, "<mutant>", "exec"), ns)  # noqa: S102 - deliberate mutation control
        ns["exec_in_workspace"] = AsyncMock(
            return_value=_exec_result(stdout="", exit_code=124, stderr="")
        )
        found, candidates = await ns["find_file"]("c1", "src/main.py", "t1")
        assert found is False, "the mutant should answer, wrongly, that the file is absent"
        assert candidates == []

    async def test_a_clipped_traversal_is_refused_not_published_as_absence(self):
        """⭐ THE FOURTH OUTCOME. Exit 0, ``truncated: true``, and the file is NOT in stdout.

        The exit-code guard above this one cannot see this case: coding-containers
        clips each stream at ``exec_max_output_bytes`` (1 MiB by default,
        ``coding-containers/src/config/settings.py:86``) and the command itself still
        exits **0**. So the wire looks exactly like a complete traversal that happened
        not to find the file, and ``find_file`` would answer a confident ``False``.

        The fixture is the measured one: a ``.venv`` hit survived the clip, the
        caller's real ``src/main.py`` did not, 1 MiB omitted. ``find`` emits in readdir
        order, so which of the two survives is directory-order luck — that is what
        makes this reachable rather than theoretical.
        """
        with _patch_exec(
            _exec_result(
                stdout="/workspace/.venv/x/main.py\n",
                exit_code=0,
                truncated=True,
                bytes_omitted=1048576,
            )
        ):
            with pytest.raises(WorkspaceSearchIncompleteError) as excinfo:
                await find_file("c1", "src/main.py", "t1")
        # The refusal must carry what a caller can act on, not just prose.
        assert excinfo.value.tool == "find"
        assert excinfo.value.bytes_omitted == 1048576
        assert excinfo.value.container_name == "c1"

    async def test_a_clipped_traversal_that_did_find_it_is_still_refused(self):
        """The partial-result direction, same as the non-zero-exit case.

        A clipped run that happens to contain the asked-for path is still a partial
        view: the ``candidates`` list published alongside the verdict is short, so the
        "did you mean" suggestions would silently omit real matches. Fail closed both
        ways, or the guard is only half a guard.
        """
        with _patch_exec(
            _exec_result(stdout="/workspace/src/main.py\n", truncated=True, bytes_omitted=99)
        ):
            with pytest.raises(WorkspaceSearchIncompleteError):
                await find_file("c1", "src/main.py", "t1")

    async def test_a_missing_exit_code_fails_closed(self):
        """A body without ``exit_code`` is absent evidence, not a successful traversal.

        ``exit_code`` is required on ``ExecResponse``, so this is latent — but the
        default is the whole argument: ``.get("exit_code", 0)`` reads a malformed body
        as a clean traversal and publishes a verdict from it.
        """
        with pytest.raises(WorkspaceBackendUnavailableError):
            with _patch_exec({"stdout": "", "stderr": "", "duration": 0.01}):
                await find_file("c1", "src/main.py", "t1")

    async def test_removing_the_truncation_guard_republishes_the_absence(self):
        """MUTANT — cut the truncation guard, and the clipped search answers ``False``.

        This is the control that makes the three tests above measurements rather than
        decoration: the module is re-executed with ONLY the ``truncated`` block removed
        (the exit-code guard stays), and the SAME exit-0 fixture that refuses on the
        real module returns a confident, wrong verdict — plus a candidate list that is
        missing whatever the clip ate.
        """
        src = Path(container_workspace.__file__).read_text(encoding="utf-8")
        guard = (
            '    if result.get("truncated"):\n'
            "        raise WorkspaceSearchIncompleteError(\n"
            "            container_name=container_name,\n"
            '            tool="find",\n'
            '            bytes_omitted=int(result.get("bytes_omitted") or 0),\n'
            "        )\n"
        )
        assert src.count(guard) == 1, (
            "the find truncation guard no longer matches — update this mutant"
        )
        mutated = src.replace(guard, "")

        ns: dict = {
            "__name__": "container_workspace_mutant_truncation",
            "__file__": container_workspace.__file__,
        }
        exec(compile(mutated, "<mutant>", "exec"), ns)  # noqa: S102 - deliberate mutation control
        ns["exec_in_workspace"] = AsyncMock(
            return_value=_exec_result(
                stdout="/workspace/.venv/x/main.py\n",
                exit_code=0,
                truncated=True,
                bytes_omitted=1048576,
            )
        )
        found, candidates = await ns["find_file"]("c1", "src/main.py", "t1")
        assert found is False, "the mutant should answer, wrongly, that the file is absent"
        assert candidates == [".venv/x/main.py"]

    async def test_same_basename_at_a_different_path_is_not_a_hit(self):
        found, candidates = await _find("/workspace/tests/main.py\n", "src/main.py")
        assert found is False
        assert candidates == ["tests/main.py"]

    async def test_exact_match_among_several_candidates(self):
        found, candidates = await _find(
            "/workspace/tests/main.py\n/workspace/src/main.py\n", "src/main.py"
        )
        assert found is True
        assert "src/main.py" in candidates

    async def test_leading_dot_slash_request_still_matches(self):
        found, _ = await _find("/workspace/src/main.py\n", "./src/main.py")
        assert found is True

    async def test_container_not_resolvable_propagates(self):
        with _patch_exec(ContainerNotResolvableError("ghost")):
            with pytest.raises(ContainerNotResolvableError):
                await find_file("ghost", "src/main.py", "t1")


async def _find(stdout: str, requested: str):
    with _patch_exec(_exec_result(stdout=stdout)):
        return await find_file("c1", requested, "t1")


# ---------------------------------------------------------------------------
# grep_definition
# ---------------------------------------------------------------------------


class TestGrepDefinition:
    async def test_found_and_not_found_differ(self):
        """THE PAIR — this is the measured defect in miniature."""
        with _patch_exec(_exec_result(stdout="/workspace/src/a.py:10:def do_stuff(a):\n")):
            found = await grep_definition("c1", "do_stuff", "t1", kind="function")
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            missing = await grep_definition("c1", "zz_nonexistent_fn_4711", "t1", kind="function")
        assert found and found[0]["file"] == "src/a.py" and found[0]["line"] == 10
        assert missing == []

    async def test_multiple_definition_sites_are_all_returned(self):
        stdout = "/workspace/a.py:3:def f():\n/workspace/b.py:9:    def f(self):\n"
        with _patch_exec(_exec_result(stdout=stdout)):
            matches = await grep_definition("c1", "f", "t1", kind="function")
        assert len(matches) == 2
        assert [m["line"] for m in matches] == [3, 9]

    async def test_grep_exit_1_is_an_answer_not_an_error(self):
        """grep exits 1 for 'no match'. That is a computed result, not a failure."""
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            assert await grep_definition("c1", "nope", "t1", kind="class") == []

    async def test_grep_exit_2_is_an_error_not_an_answer(self):
        """A real grep failure must not be reported as 'symbol absent'."""
        with _patch_exec(_exec_result(stdout="", exit_code=2, stderr="grep: bad regex")):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await grep_definition("c1", "nope", "t1", kind="class")

    async def test_unparseable_grep_line_is_skipped_not_counted(self):
        with _patch_exec(_exec_result(stdout="garbage without colons\n")):
            assert await grep_definition("c1", "f", "t1", kind="function") == []

    async def test_a_clipped_grep_is_refused_not_published_as_absence(self):
        """⭐ THE FOURTH OUTCOME on the OTHER helper.

        Guarding only ``find_file`` would recreate, one field over, exactly the
        asymmetry this PR exists to remove. A clipped grep exits 0 or 1 with short
        stdout: the ``> 1`` guard sees nothing wrong and the parser yields a short — or
        empty — match list, which the endpoint publishes as ``valid: false``.
        """
        with _patch_exec(
            _exec_result(stdout="", exit_code=1, truncated=True, bytes_omitted=1048576)
        ):
            with pytest.raises(WorkspaceSearchIncompleteError) as excinfo:
                await grep_definition("c1", "do_stuff", "t1", kind="function")
        assert excinfo.value.tool == "grep"
        assert excinfo.value.bytes_omitted == 1048576

    async def test_a_clipped_grep_with_hits_is_refused_rather_than_undercounted(self):
        """An under-counted definition-site list is a wrong answer, not a partial one.

        The endpoint reports ``matches[0]`` as THE definition site. If the clip ate the
        other sites, a caller is told a symbol is defined once when it is defined five
        times — a confident answer built from a search that did not finish.
        """
        with _patch_exec(
            _exec_result(stdout="/workspace/a.py:3:def f():\n", truncated=True, bytes_omitted=1)
        ):
            with pytest.raises(WorkspaceSearchIncompleteError):
                await grep_definition("c1", "f", "t1", kind="function")

    async def test_a_missing_exit_code_fails_closed(self):
        """Absent evidence must land on the REFUSING side of this guard, which is > 1.

        So the fail-closed default here is 2, not the 1 ``find_file`` uses — the same
        reason the thresholds themselves differ. A default of 1 would be read as grep's
        "no match" and published as a confident absence.
        """
        with pytest.raises(WorkspaceBackendUnavailableError):
            with _patch_exec({"stdout": "", "stderr": "", "duration": 0.01}):
                await grep_definition("c1", "f", "t1", kind="function")

    async def test_removing_the_truncation_guard_republishes_the_absence(self):
        """MUTANT — cut the grep truncation guard and the clipped search answers ``[]``."""
        src = Path(container_workspace.__file__).read_text(encoding="utf-8")
        guard = (
            '    if result.get("truncated"):\n'
            "        raise WorkspaceSearchIncompleteError(\n"
            "            container_name=container_name,\n"
            '            tool="grep",\n'
            '            bytes_omitted=int(result.get("bytes_omitted") or 0),\n'
            "        )\n"
        )
        assert src.count(guard) == 1, (
            "the grep truncation guard no longer matches — update this mutant"
        )
        mutated = src.replace(guard, "")

        ns: dict = {
            "__name__": "container_workspace_mutant_truncation_grep",
            "__file__": container_workspace.__file__,
        }
        exec(compile(mutated, "<mutant>", "exec"), ns)  # noqa: S102 - deliberate mutation control
        ns["exec_in_workspace"] = AsyncMock(
            return_value=_exec_result(stdout="", exit_code=1, truncated=True, bytes_omitted=1048576)
        )
        assert await ns["grep_definition"]("c1", "do_stuff", "t1", kind="function") == [], (
            "the mutant should answer, wrongly, that the symbol is absent"
        )

    async def test_injection_attempt_is_refused_before_any_call(self):
        mock = AsyncMock()
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            with pytest.raises(ValueError):
                await grep_definition("c1", "f; cat /etc/passwd", "t1", kind="function")
        mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# G25 — vendored directories are never searched
# ---------------------------------------------------------------------------

# ⚠️ Spelled out LITERALLY, not read from ``container_workspace.VENDOR_EXCLUDE_DIRS``.
# Asserting the argv against the constant would pass with the constant emptied — a
# test that cannot fail. This list is the requirement; the constant is the implementation.
_REQUIRED_EXCLUDES = [
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
]


def _argv_of(mock: AsyncMock) -> list[str]:
    mock.assert_awaited_once()
    return list(mock.await_args.args[1])


class TestVendorDirsAreExcluded:
    """A name that exists ONLY in a virtualenv/site-packages must not read as "in the repo"."""

    @pytest.mark.parametrize("d", _REQUIRED_EXCLUDES)
    async def test_grep_argv_excludes_each_vendored_dir(self, d):
        mock = AsyncMock(return_value=_exec_result(stdout="", exit_code=1))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            await grep_definition("c1", "f", "t1", kind="function")
        assert f"--exclude-dir={d}" in _argv_of(mock)

    @pytest.mark.parametrize("d", _REQUIRED_EXCLUDES)
    async def test_find_argv_prunes_each_vendored_dir(self, d):
        mock = AsyncMock(return_value=_exec_result(stdout="", exit_code=0))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            await find_file("c1", "src/main.py", "t1")
        argv = _argv_of(mock)
        prune = ["-not", "-path", f"*/{d}/*"]
        assert any(argv[i : i + 3] == prune for i in range(len(argv))), argv

    async def test_grep_argv_keeps_pattern_and_root_last(self):
        """The excludes must not displace the pattern/root positional tail."""
        mock = AsyncMock(return_value=_exec_result(stdout="", exit_code=1))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            await grep_definition("c1", "f", "t1", kind="function")
        argv = _argv_of(mock)
        assert argv[:3] == ["grep", "-rnE", "--include=*.py"]
        assert argv[-1] == "/workspace"
        assert "def" in argv[-2]

    async def test_dropping_an_entry_from_the_constant_is_caught(self):
        """MUTANT — the argv check above discriminates rather than passing incidentally."""
        mutated = tuple(d for d in container_workspace.VENDOR_EXCLUDE_DIRS if d != ".venv")
        assert mutated != container_workspace.VENDOR_EXCLUDE_DIRS
        mock = AsyncMock(return_value=_exec_result(stdout="", exit_code=1))
        with (
            patch.object(container_workspace, "VENDOR_EXCLUDE_DIRS", mutated),
            patch("services.container_workspace.exec_in_workspace", new=mock),
        ):
            await grep_definition("c1", "f", "t1", kind="function")
        assert "--exclude-dir=.venv" not in _argv_of(mock)


# ---------------------------------------------------------------------------
# read_workspace_file
# ---------------------------------------------------------------------------


class TestReadWorkspaceFileChecked:
    """GAP-2: ``None`` is a CONFIRMED absence; every incomplete read raises."""

    async def test_present_and_absent_differ(self):
        with _patch_exec(_exec_result(stdout="x = 1\n")):
            present = await read_workspace_file_checked("c1", "a.py", "t1")
        with _patch_exec(
            _exec_result(stdout="", exit_code=1, stderr="cat: a.py: No such file or directory")
        ):
            absent = await read_workspace_file_checked("c1", "a.py", "t1")
        assert present == "x = 1\n"
        assert absent is None

    @pytest.mark.parametrize(
        "result",
        [
            {"exit_code": 124, "stdout": "", "stderr": "", "truncated": False},
            {"exit_code": 0, "stdout": "def f(", "stderr": "", "truncated": True},
            {"stdout": "", "stderr": "", "truncated": False},
            {"exit_code": 1, "stdout": "", "stderr": "cat: a: Is a directory"},
            {"exit_code": 2, "stdout": "", "stderr": "Permission denied"},
        ],
        ids=["timeout-124", "clipped", "no-exit-code", "is-a-directory", "other-exit"],
    )
    async def test_incomplete_reads_raise_never_none_or_text(self, result):
        with _patch_exec(result):
            with pytest.raises(WorkspaceSearchIncompleteError) as info:
                await read_workspace_file_checked("c1", "a.py", "t1")
        assert info.value.tool == "cat"

    async def test_unreachable_backend_raises_rather_than_returning_none(self):
        """``None`` means 'confirmed absent'. It must never mean 'I could not look'."""
        with _patch_exec(WorkspaceBackendUnavailableError("down")):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await read_workspace_file_checked("c1", "a.py", "t1")


# ---------------------------------------------------------------------------
# exec_in_workspace — status mapping
# ---------------------------------------------------------------------------


class TestExecStatusMapping:
    async def test_unconfigured_backend_refuses_loudly(self):
        """An unset CONTAINERS_URL must refuse, never fall back to local disk."""
        with patch("services.container_workspace.get_settings") as mock_settings:
            mock_settings.return_value.containers_url = ""
            with pytest.raises(WorkspaceBackendUnavailableError, match="CONTAINERS_URL"):
                await exec_in_workspace("c1", ["ls"], "t1")

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (404, ContainerNotResolvableError),
            (403, WorkspaceBackendUnavailableError),
            (500, WorkspaceBackendUnavailableError),
        ],
    )
    async def test_status_codes_map_to_the_right_claim(self, status_code, expected):
        class _Resp:
            def __init__(self) -> None:
                self.status_code = status_code
                self.text = "boom"

            def json(self):
                return {}

        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp())

        with (
            patch("services.container_workspace.get_settings") as mock_settings,
            patch(
                # Patched where it is DEFINED, because container_workspace
                # imports it inside the function (see the note at the top of
                # this file) -- there is no module-level name to patch.
                "routers.proxy.get_proxy_client",
                new=AsyncMock(return_value=client),
            ),
            patch("services.container_workspace.identity_headers", return_value={}),
        ):
            mock_settings.return_value.containers_url = "http://coding-containers:8691"
            with pytest.raises(expected):
                await exec_in_workspace("c1", ["ls"], "t1")


# ---------------------------------------------------------------------------
# New helpers: snippet delegation, directories, line counts, lint
# ---------------------------------------------------------------------------


class TestContainersPostMapping:
    """The snippet delegation shares exec's status mapping (one HTTP helper)."""

    @pytest.mark.parametrize(
        "status_code,expected",
        [(404, ContainerNotResolvableError), (422, WorkspaceBackendUnavailableError)],
    )
    async def test_snippet_status_maps_to_the_right_claim(self, status_code, expected):
        class _Resp:
            def __init__(self) -> None:
                self.status_code = status_code
                self.text = "boom"

            def json(self):
                return {}

        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp())
        with (
            patch("services.container_workspace.get_settings") as mock_settings,
            patch("routers.proxy.get_proxy_client", new=AsyncMock(return_value=client)),
            patch("services.container_workspace.identity_headers", return_value={"x": "y"}),
        ):
            mock_settings.return_value.containers_url = "http://coding-containers:8691"
            with pytest.raises(expected):
                await container_workspace.validate_snippet_in_workspace("c1", "import os", "t1")
        url = client.post.await_args.args[0]
        assert url == "http://coding-containers:8691/api/validation/multi-line-code-snippet"
        assert client.post.await_args.kwargs["json"]["include_execution_validation"] is False
        assert client.post.await_args.kwargs["headers"] == {"x": "y"}

    async def test_exec_still_posts_to_the_exec_route(self):
        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"exit_code": 0}

        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp())
        with (
            patch("services.container_workspace.get_settings") as mock_settings,
            patch("routers.proxy.get_proxy_client", new=AsyncMock(return_value=client)),
            patch("services.container_workspace.identity_headers", return_value={}),
        ):
            mock_settings.return_value.containers_url = "http://cc:8691"
            assert await exec_in_workspace("c1", ["ls"], "t1") == {"exit_code": 0}
        assert client.post.await_args.args[0] == "http://cc:8691/api/workspaces/c1/exec"
        assert client.post.await_args.kwargs["json"]["command"] == ["ls"]


class TestFindDirectory:
    async def test_present_and_absent_differ(self):
        with _patch_exec(_exec_result(stdout="/workspace/src/pkg\n")):
            assert await container_workspace.find_directory("c1", "src/pkg", "t1") is True
        with _patch_exec(_exec_result(stdout="")):
            assert await container_workspace.find_directory("c1", "src/pkg", "t1") is False

    async def test_incomplete_traversal_is_refused(self):
        with _patch_exec(_exec_result(exit_code=124)):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await container_workspace.find_directory("c1", "src", "t1")

    async def test_glob_characters_are_escaped(self):
        mock = AsyncMock(return_value=_exec_result())
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            await container_workspace.find_directory("c1", "a*/b?", "t1")
        argv = mock.await_args.args[1]
        assert argv[argv.index("-path") + 1] == r"/workspace/a\*/b\?"


class TestCountLines:
    async def test_count_and_failure_differ(self):
        with _patch_exec(_exec_result(stdout="7\n")):
            assert await container_workspace.count_lines("c1", "a.py", "t1") == 7
        with _patch_exec(_exec_result(exit_code=2)):
            assert await container_workspace.count_lines("c1", "a.py", "t1") is None

    async def test_missing_exit_code_fails_closed(self):
        with _patch_exec({"stdout": "7\n"}):
            assert await container_workspace.count_lines("c1", "a.py", "t1") is None


class TestLintPythonFile:
    async def test_clean_vs_findings(self):
        with _patch_exec(_exec_result(exit_code=0)):
            clean = await container_workspace.lint_python_file("c1", "a.py", "t1")
        with _patch_exec(_exec_result(stdout="a.py:1:1: undefined name 'x'\n", exit_code=1)):
            dirty = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert clean["status"] == "valid"
        assert dirty["status"] == "invalid"

    async def test_syntax_error_on_stderr_is_a_finding(self):
        with _patch_exec(_exec_result(stderr="a.py:1:7: invalid syntax\n", exit_code=1)):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert result["status"] == "invalid"

    async def test_pyflakes_crash_is_not_computed(self):
        with _patch_exec(_exec_result(stderr="Traceback (most recent call last):\n", exit_code=1)):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert result["status"] == "not_computed"

    async def test_missing_exit_code_is_not_a_pass(self):
        with _patch_exec({"stdout": ""}):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert result["status"] != "valid"

    async def test_ruff_clean_and_bad_exit(self):
        missing = _exec_result(exit_code=1, stderr="No module named pyflakes")
        mock = AsyncMock(side_effect=[missing, missing, _exec_result(stdout="[]", exit_code=0)])
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            clean = await container_workspace.lint_python_file("c1", "a.py", "t1")
        mock = AsyncMock(side_effect=[missing, missing, _exec_result(stdout="", exit_code=2)])
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            broken = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert clean == {"status": "valid", "tool": "ruff", "issues": []}
        assert broken["status"] == "not_computed"

    async def test_traversal_refused_before_any_exec(self):
        mock = AsyncMock()
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            with pytest.raises(ValueError):
                await container_workspace.lint_python_file("c1", "../x.py", "t1")
        mock.assert_not_awaited()


class TestRequestRefusal:
    """coding-containers answers 400 when its command policy refuses an argv
    (allowlist, or the dangerous-pattern regex that matches `shutdown`/`reboot`/
    `mkfs` ANYWHERE in the joined argv -- including a file path)."""

    @pytest.mark.parametrize(
        "status_code,expected",
        [
            (400, container_workspace.WorkspaceRequestRefusedError),
            (500, WorkspaceBackendUnavailableError),
        ],
    )
    async def test_400_is_a_typed_refusal(self, status_code, expected):
        class _Resp:
            def __init__(self) -> None:
                self.status_code = status_code
                self.text = "Command contains dangerous patterns and is not allowed"

            def json(self):
                return {}

        client = AsyncMock()
        client.post = AsyncMock(return_value=_Resp())
        with (
            patch("services.container_workspace.get_settings") as mock_settings,
            patch("routers.proxy.get_proxy_client", new=AsyncMock(return_value=client)),
            patch("services.container_workspace.identity_headers", return_value={}),
        ):
            mock_settings.return_value.containers_url = "http://cc:8691"
            with pytest.raises(expected) as info:
                await exec_in_workspace("c1", ["cat", "/workspace/shutdown.py"], "t1")
        # Still a BackendUnavailable for every pre-existing caller (-> 503 there).
        assert isinstance(info.value, WorkspaceBackendUnavailableError)
        if status_code == 500:
            assert not isinstance(info.value, container_workspace.WorkspaceRequestRefusedError)

    async def test_new_execs_report_refusal_as_not_computed(self):
        refused = container_workspace.WorkspaceRequestRefusedError("c1", "dangerous pattern")
        with _patch_exec(refused):
            assert await container_workspace.find_directory("c1", "src/shutdown", "t1") is None
        with _patch_exec(refused):
            assert await container_workspace.count_lines("c1", "src/shutdown.py", "t1") is None
        mock = AsyncMock(side_effect=refused)
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            lint = await container_workspace.lint_python_file("c1", "src/shutdown.py", "t1")
        assert lint["status"] == "not_computed"
        assert "refused by workspace command policy" in lint["reason"]

    async def test_pre_existing_find_file_still_raises(self):
        """Scope: only the NEW execs abstain; the basename find keeps its 503."""
        refused = container_workspace.WorkspaceRequestRefusedError("c1", "dangerous pattern")
        with _patch_exec(refused):
            with pytest.raises(WorkspaceBackendUnavailableError):
                await find_file("c1", "src/shutdown.py", "t1")


class TestLintInterpreter:
    async def test_repo_venv_python_is_tried_first(self):
        mock = AsyncMock(return_value=_exec_result(exit_code=0))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert mock.await_args_list[0].args[1] == [
            "/workspace/.venv/bin/python",
            "-m",
            "pyflakes",
            "/workspace/a.py",
        ]
        assert (
            result["status"] == "valid" and result["interpreter"] == "/workspace/.venv/bin/python"
        )

    async def test_falls_back_to_system_python3_without_venv_pyflakes(self):
        missing = _exec_result(exit_code=1, stderr="No module named pyflakes")
        mock = AsyncMock(side_effect=[missing, _exec_result(exit_code=0)])
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert mock.await_args_list[1].args[1][0] == "python3"
        assert result["status"] == "valid" and result["interpreter"] == "python3"

    async def test_venv_exec_backend_error_falls_back_not_503(self):
        """A missing venv binary may surface as a backend error, not exit 127."""
        boom = WorkspaceBackendUnavailableError("exec failed: no such file")
        mock = AsyncMock(side_effect=[boom, _exec_result(exit_code=0)])
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            result = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert result["status"] == "valid"

    async def test_skewed_syntax_is_not_computed_real_syntax_error_is_invalid(self):
        """pyflakes on an older python rejects `except A, B:` (valid 3.14)."""
        stderr = "/workspace/a.py:3:8: multiple exception types must be parenthesized\n"
        flakes = _exec_result(stderr=stderr, exit_code=1)
        good = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"
        missing = _exec_result(exit_code=1, stderr="No module named pyflakes")

        def _system_python3():
            # venv has no pyflakes -> the (possibly older) system python3 lints.
            return patch(
                "services.container_workspace.exec_in_workspace",
                new=AsyncMock(side_effect=[missing, flakes]),
            )

        with _system_python3():
            skew = await container_workspace.lint_python_file("c1", "a.py", "t1", content=good)
        with _system_python3():
            real = await container_workspace.lint_python_file(
                "c1", "a.py", "t1", content="def f(:\n"
            )
        with _system_python3():
            unknown = await container_workspace.lint_python_file("c1", "a.py", "t1")
        assert skew["status"] == "not_computed"
        assert "linter python older than the code" in skew["reason"]
        assert real["status"] == "invalid"
        # No content to cross-check -> keep pyflakes' verdict (never a pass).
        assert unknown["status"] == "invalid"

    async def test_venv_python_syntax_error_is_never_excused_as_skew(self):
        """The repo's own venv python IS the target interpreter: its syntax error stands."""
        stderr = "/workspace/a.py:3:8: multiple exception types must be parenthesized\n"
        flakes = _exec_result(stderr=stderr, exit_code=1)
        good = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"
        with _patch_exec(flakes):
            venv = await container_workspace.lint_python_file("c1", "a.py", "t1", content=good)
        missing = _exec_result(exit_code=1, stderr="No module named pyflakes")
        system = AsyncMock(side_effect=[missing, flakes])
        with patch("services.container_workspace.exec_in_workspace", new=system):
            sys3 = await container_workspace.lint_python_file("c1", "a.py", "t1", content=good)
        assert venv["interpreter"] == "/workspace/.venv/bin/python"
        assert venv["status"] == "invalid"
        # Control: the same output under system python3 is still excused as skew.
        assert sys3["interpreter"] == "python3" and sys3["status"] == "not_computed"

    async def test_stdout_findings_are_kept_even_if_content_parses(self):
        out = _exec_result(stdout="/workspace/a.py:1:1: 'os' imported but unused\n", exit_code=1)
        with _patch_exec(out):
            result = await container_workspace.lint_python_file(
                "c1", "a.py", "t1", content="import os\n"
            )
        assert result["status"] == "invalid"
