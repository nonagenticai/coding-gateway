"""Tests for the validation router — file/function/class references,
code snippet validation, consistency checks, code validation, and
architecture validation.

Every test sends real HTTP requests via the ``client`` fixture.

-----------------------------------------------------------------------------
WHY MOST OF THIS FILE WAS REWRITTEN
-----------------------------------------------------------------------------
The previous version of these tests passed against code that could not
compute anything. They asserted, among other things:

    assert vr["valid"] is True      # validate_code, for ANY file in ANY
                                    # container, including ones that did not
                                    # exist -- the handler built the dict as a
                                    # literal
    async def test_unknown_validation_type_still_returns_200(...)
                                    # pinned the certification as intended
    assert body["valid"] is False   # reference endpoints -- and the code
                                    # returned False for EVERY symbol, so this
                                    # assertion could not fail

That is the defect class these tests now guard against: an assertion that the
broken implementation also satisfies. **Every behavioural test below is written
as a PAIR** — the true case and the false case through the same code path with
the same mocks — because only the pair can fail on an implementation that
answers a constant. A test asserting `valid is False` alone would have passed
on the old code; the accompanying `valid is True` case is what makes it a
measurement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.container_workspace import SEARCHED_LANGUAGES as _ALL_LANGUAGES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exec_result(
    stdout: str = "",
    exit_code: int = 0,
    stderr: str = "",
    truncated: bool = False,
    bytes_omitted: int = 0,
) -> dict:
    """Shape of coding-containers' ExecResponse (container_exec.py:149-171).

    ``truncated``/``bytes_omitted`` are settable because they are the ONLY
    signal that separates a complete search from a clipped one — the exit code
    is 0 either way.
    """
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration": 0.01,
        "truncated": truncated,
        "bytes_omitted": bytes_omitted,
    }


def _patch_exec(*results):
    """Patch the ONE HTTP call, leaving all parsing/decision logic real.

    Deliberately patched at ``services.container_workspace.exec_in_workspace``
    rather than at ``routers.validation.find_file`` / ``grep_definition``: the
    discrimination between "found" and "not found" lives in those functions'
    output parsing, so stubbing them would remove the very logic under test and
    give us a test that cannot fail — the defect this file exists to prevent.
    """
    if len(results) == 1 and not isinstance(results[0], BaseException):
        # A single dict must be a return_value: AsyncMock(side_effect=<dict>)
        # treats the dict as an ITERABLE of results and yields its keys, so the
        # handler would receive the string "exit_code" instead of the result.
        mock = AsyncMock(return_value=results[0])
    else:
        mock = AsyncMock(side_effect=list(results))
    return patch("services.container_workspace.exec_in_workspace", new=mock)


def _snippet_body(
    verdict: str | None,
    *,
    unresolved: list[str] | None = None,
    reason: str | None = None,
    imports: list[dict] | None = None,
) -> dict:
    """Shape of coding-containers' MultiLineValidationResponse (#171).

    ``verdict=None`` models a coding-containers that predates import
    resolution: the body carries ``valid`` but no ``verdict``.
    """
    body: dict = {
        "valid": verdict != "invalid",
        "static_analysis": {
            "valid": verdict != "invalid",
            "issues": [],
            "overall_score": 1.0 if verdict == "valid" else 0.5,
            "imports": imports or [],
            "unresolved_imports": unresolved or [],
            "import_resolution": "workspace" if verdict != "not_computed" else "not_computed",
            "not_computed_reason": reason,
        },
        "suggestions": [],
    }
    if verdict is not None:
        body["verdict"] = verdict
    return body


def _patch_containers_post(*results):
    """Patch the ONE coding-containers POST used for snippet / import resolution."""
    if len(results) == 1 and not isinstance(results[0], BaseException):
        mock = AsyncMock(return_value=results[0])
    else:
        mock = AsyncMock(side_effect=list(results))
    return patch("services.container_workspace.containers_post", new=mock)


# ---------------------------------------------------------------------------
# File Reference  POST /api/validation/file-reference
# ---------------------------------------------------------------------------


class TestValidateFileReference:
    """Tests for the file-reference endpoint."""

    async def test_real_and_fake_file_in_same_workspace_differ(self, client):
        """THE PAIR. Same workspace, same mocks — only the answer differs.

        On the pre-fix code both of these returned 200 valid=false, because the
        gateway indexed its own (nonexistent) /app/data/repo. Asserting only
        the second would have passed on the broken build.
        """
        # find(1) located the exact relative path -> it exists
        with _patch_exec(_exec_result(stdout="/workspace/src/main.py\n")):
            found = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        # find(1) matched nothing -> it does not exist. NOTE exit **0**: unlike
        # grep, find has no "no match" exit code — absence is a successful
        # traversal that printed nothing, and a non-zero exit means the search
        # FAILED (see test_a_find_timeout_is_503_not_a_false_verdict below).
        with _patch_exec(_exec_result(stdout="", exit_code=0)):
            missing = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/zz_nonexistent_4711.py", "container_name": "c1"},
            )

        assert found.status_code == 200
        assert found.json()["valid"] is True
        assert found.json()["resolution"] == "container_workspace"

        assert missing.status_code == 200
        assert missing.json()["valid"] is False
        # The pair must actually disagree.
        assert found.json()["valid"] != missing.json()["valid"]

    async def test_a_find_timeout_is_503_not_a_false_verdict(self, client):
        """⭐ A search that could not finish is a REFUSAL, never "the file is not there".

        coding-containers wraps every exec as ``timeout <N> <argv>``, so a find
        that outruns its 30s budget comes back exit 124 with empty stdout —
        indistinguishable on the wire from a clean "no match". Answering
        ``valid=false`` there is a false negative that would PASS a
        verification, which is precisely the ambiguity this endpoint family was
        rebuilt to remove. 503 says "I could not look".
        """
        with _patch_exec(_exec_result(stdout="", exit_code=124)):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        assert resp.status_code == 503
        assert "valid" not in resp.json()

    async def test_a_clipped_find_is_503_not_a_false_verdict(self, client):
        """⭐ THE FOURTH OUTCOME, end to end: exit **0**, ``truncated: true``.

        The exit-code guard above passes this straight through, because nothing
        failed — coding-containers simply stopped copying the answer at
        ``exec_max_output_bytes`` (1 MiB). ``find_file`` prunes only ``.git``, so a
        synced ``.venv`` is inside the searched tree and can fill that budget by
        itself; ``find`` emits in readdir order, so whether the caller's real file
        survives is directory-order luck. Measured at PR head, before this fix:

            {"exit_code":0,"truncated":true,"bytes_omitted":1048576,
             "stdout":"/workspace/.venv/x/main.py\\n"}
              -> 200 {"valid": false}    for a file that exists

        A truncated look is "I could not finish", which is the 503 family — not the
        200 ``valid=false`` family and not a 404.
        """
        with _patch_exec(
            _exec_result(
                stdout="/workspace/.venv/x/main.py\n",
                exit_code=0,
                truncated=True,
                bytes_omitted=1048576,
            )
        ):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        assert resp.status_code == 503
        assert "valid" not in resp.json()

    async def test_a_clipped_look_is_machine_distinguishable_from_a_timeout(self, client):
        """THE PAIR for the RAISED shape: both refuse, and they do not refuse alike.

        ⚠️ SCOPE — this test grades ``_workspace_error``'s output, NOT what a caller
        receives. Every test in this file runs against an app whose error envelope
        ``tests/conftest.py`` replaces with a no-op, so the ``detail`` key asserted
        below exists only here. On the wire, ``coding_client``'s envelope drops
        ``error_code`` entirely and the discriminator that survives is
        ``error.details.tool``. That is measured, and the earlier claim to the
        contrary retracted, in ``test_validation_error_envelope.py`` — read it
        before quoting these field names at a consumer.

        What this pair does establish, and what is worth keeping: (a) a genuine
        absence is 200 ``valid=false``; (b) a 124 timeout raises a free-text
        ``detail``; (d) a clipped search raises a structured one. Asserting (d)
        alone would pass on an implementation that typed EVERY 503 the same way, so
        (b) goes through the same code path in the same test.
        """
        with _patch_exec(
            _exec_result(stdout="", exit_code=0, truncated=True, bytes_omitted=1048576)
        ):
            clipped = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        with _patch_exec(_exec_result(stdout="", exit_code=124)):
            timed_out = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )

        assert clipped.status_code == timed_out.status_code == 503
        detail = clipped.json()["detail"]
        assert detail["error_code"] == "WORKSPACE_SEARCH_INCOMPLETE"
        assert detail["details"] == {
            "container_name": "c1",
            "tool": "find",
            "bytes_omitted": 1048576,
        }
        # ...and the timeout does NOT wear that code, so the two are distinguishable.
        assert not isinstance(timed_out.json()["detail"], dict)

    async def test_container_result_publishes_excluded_dirs(self, client):
        with _patch_exec(_exec_result(stdout="", exit_code=0)):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        assert resp.status_code == 200
        assert ".venv" in resp.json()["excluded_dirs"]
        assert resp.json()["searched_languages"] == ["*"]

    async def test_basename_match_elsewhere_is_a_suggestion_not_a_hit(self, client):
        """A file with the right name at the wrong path is not the file asked for."""
        with _patch_exec(_exec_result(stdout="/workspace/other/main.py\n")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        body = resp.json()
        assert body["valid"] is False
        assert "other/main.py" in body["suggestion"]

    async def test_non_python_file_is_answered_not_refused(self, client):
        """pyproject.toml returned valid=false before, because the index was .py-only."""
        with _patch_exec(_exec_result(stdout="/workspace/pyproject.toml\n")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "pyproject.toml", "container_name": "c1"},
            )
        assert resp.status_code == 200
        assert resp.json()["valid"] is True

    async def test_no_codebase_input_refuses_with_400(self, client):
        """'I cannot check' must not wear the shape of 'it is not there'."""
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "src/main.py"},
        )
        assert resp.status_code == 400
        assert "container_name" in resp.json()["detail"]

    async def test_unknown_container_returns_404_not_a_verdict(self, client):
        from services.container_workspace import ContainerNotResolvableError

        with _patch_exec(ContainerNotResolvableError("ghost-container")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "ghost-container"},
            )
        assert resp.status_code == 404
        assert "valid" not in resp.json()

    async def test_backend_unreachable_returns_503_not_a_verdict(self, client):
        from services.container_workspace import WorkspaceBackendUnavailableError

        with _patch_exec(WorkspaceBackendUnavailableError("coding-containers is down")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "container_name": "c1"},
            )
        assert resp.status_code == 503
        assert "valid" not in resp.json()

    async def test_path_traversal_refused(self, client):
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "../../etc/passwd", "container_name": "c1"},
        )
        assert resp.status_code == 400

    async def test_explicit_path_still_works(self, client, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "main.py", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        assert resp.json()["valid"] is True
        assert resp.json()["resolution"] == "explicit_codebase_path"

    async def test_explicit_path_that_is_not_a_directory_returns_400(self, client):
        resp = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "main.py", "codebase_path": "/no/such/dir/anywhere"},
        )
        assert resp.status_code == 400

    async def test_internal_error_returns_500(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.side_effect = RuntimeError("boom")
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/main.py", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Function Reference  POST /api/validation/function-reference
# ---------------------------------------------------------------------------


class TestValidateFunctionReference:
    async def test_real_and_fake_symbol_in_same_workspace_differ(self, client):
        """THE PAIR — the measured defect was that these were identical."""
        with _patch_exec(_exec_result(stdout="/workspace/src/main.py:10:def do_stuff(a, b):\n")):
            found = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "do_stuff", "container_name": "c1"},
            )
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            missing = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "zz_nonexistent_fn_4711", "container_name": "c1"},
            )

        assert found.json()["valid"] is True
        assert found.json()["details"]["file"] == "src/main.py"
        assert found.json()["details"]["line"] == 10
        assert missing.json()["valid"] is False
        assert found.json()["valid"] != missing.json()["valid"]

    async def test_negative_result_declares_its_language_scope(self, client):
        """A negative names every language it searched, and is false only after all of them."""
        with _patch_exec(_exec_result(stdout="", exit_code=1)) as mock:
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "ServeHTTP", "container_name": "c1"},
            )
        body = resp.json()
        assert body["valid"] is False
        assert body["searched_languages"] == list(_ALL_LANGUAGES)
        assert "python" in body["note"].lower()
        assert "other languages" in body["note"].lower()
        # The Python grep AND the non-Python candidate grep both ran.
        assert [c.args[1][1] for c in mock.await_args_list] == ["-rnE", "-rlw"]

    async def test_container_result_publishes_excluded_dirs(self, client):
        """G25 — a caller must be able to see what the search did NOT look into."""
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "f", "container_name": "c1"},
            )
        body = resp.json()
        assert body["searched_languages"] == list(_ALL_LANGUAGES)
        for d in (".venv", "venv", "site-packages", "node_modules", "*.egg-info"):
            assert d in body["excluded_dirs"]

    async def test_explicit_path_result_claims_no_exclusions(self, client, tmp_path):
        """The explicit-path walker prunes nothing, so it must not claim it did."""
        (tmp_path / "m.py").write_text("def f():\n    pass\n")
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "f", "codebase_path": str(tmp_path)},
        )
        assert resp.status_code == 200
        assert resp.json()["excluded_dirs"] == []

    async def test_positive_result_carries_no_language_caveat(self, client):
        with _patch_exec(_exec_result(stdout="/workspace/a.py:3:def f():\n")):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "f", "container_name": "c1"},
            )
        assert resp.json()["note"] is None

    async def test_a_clipped_grep_is_503_not_a_false_verdict(self, client):
        """The same fourth outcome on the grep path — guarded here too, or not at all.

        Fixing only ``find_file`` would leave ``grep_definition`` publishing
        ``valid: false`` from a clipped search, which is precisely the asymmetry
        (guarded / unguarded) that this PR was opened to remove.
        """
        with _patch_exec(
            _exec_result(stdout="", exit_code=1, truncated=True, bytes_omitted=1048576)
        ):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "do_stuff", "container_name": "c1"},
            )
        assert resp.status_code == 503
        assert "valid" not in resp.json()
        assert resp.json()["detail"]["error_code"] == "WORKSPACE_SEARCH_INCOMPLETE"
        assert resp.json()["detail"]["details"]["tool"] == "grep"

    async def test_no_codebase_input_refuses_with_400(self, client):
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "do_stuff"},
        )
        assert resp.status_code == 400

    async def test_unknown_container_returns_404(self, client):
        from services.container_workspace import ContainerNotResolvableError

        with _patch_exec(ContainerNotResolvableError("ghost")):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "do_stuff", "container_name": "ghost"},
            )
        assert resp.status_code == 404

    async def test_non_identifier_symbol_refused(self, client):
        """The name is interpolated into a regex and an argv; only identifiers allowed."""
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "foo; rm -rf /", "container_name": "c1"},
        )
        assert resp.status_code == 400

    async def test_internal_error_returns_500(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.side_effect = RuntimeError("fail")
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "foo", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Class Reference  POST /api/validation/class-reference
# ---------------------------------------------------------------------------


class TestValidateClassReference:
    async def test_real_and_fake_class_in_same_workspace_differ(self, client):
        """THE PAIR — measured: FastMCP and ZzNonexistentClass4711 both said false."""
        with _patch_exec(_exec_result(stdout="/workspace/src/models.py:5:class FastMCP:\n")):
            found = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "FastMCP", "container_name": "c1"},
            )
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            missing = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "ZzNonexistentClass4711", "container_name": "c1"},
            )

        assert found.json()["valid"] is True
        assert found.json()["details"]["line"] == 5
        assert missing.json()["valid"] is False
        assert found.json()["valid"] != missing.json()["valid"]

    async def test_container_result_publishes_excluded_dirs(self, client):
        with _patch_exec(_exec_result(stdout="", exit_code=1)):
            resp = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "FastMCP", "container_name": "c1"},
            )
        assert "site-packages" in resp.json()["excluded_dirs"]
        assert ".venv" in resp.json()["excluded_dirs"]

    async def test_no_codebase_input_refuses_with_400(self, client):
        resp = await client.post(
            "/api/validation/class-reference",
            json={"class_name": "Ghost"},
        )
        assert resp.status_code == 400

    async def test_internal_error_returns_500(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.side_effect = RuntimeError("boom")
            resp = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "MyModel", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Dotted class names  POST /api/validation/class-reference  (C1-42 q6)
# ---------------------------------------------------------------------------

# A small repo. ``app/geometry.py`` defines an UNRELATED ``class Path`` -- it is
# the decoy that a last-segment, repo-wide search would wrongly accept for
# ``pathlib.Path``.
_DOTTED_REPO = {
    "app/__init__.py": "",
    "app/models.py": "import os\n\nclass User(Base):\n    pass\n",
    "app/geometry.py": "class Path:\n    pass\n",
    "src/svc/__init__.py": "class Engine:\n    pass\n",
}


def _fake_workspace(files: dict[str, str]):
    """An ``exec_in_workspace`` double backed by an in-memory tree.

    It interprets the argv the real helpers send (``find ... -path``,
    ``grep -nHE <pattern> <files>`` and repo-wide ``grep -rnE``) so the
    resolution logic under test -- which paths are probed, which files are
    searched -- runs for real. A canned stdout could not tell a module-scoped
    search from a repo-wide one; this can.
    """
    import re

    root = "/workspace/"

    def _grep(pattern: str, paths: list[str]) -> dict:
        # Real grep semantics: a newline in the pattern SEPARATES patterns and a
        # line matching ANY of them is a hit. Without this the double would hide
        # coding-gateway#78 (``Ghost\n`` -> valid=true).
        rxs = [re.compile(p.replace("[[:space:]]", r"\s")) for p in pattern.split("\n")]
        out = []
        for rel in paths:
            for i, line in enumerate(files.get(rel, "").splitlines(), 1):
                if any(rx.search(line) for rx in rxs):
                    out.append(f"{root}{rel}:{i}:{line}")
        return _exec_result(stdout="\n".join(out), exit_code=0 if out else 1)

    async def _exec(container_name, command, tenant_id, timeout=30):
        if command[0] == "find":
            wanted = [command[i + 1] for i, a in enumerate(command) if a == "-path"]
            hits = [w for w in wanted if w.startswith(root) and w[len(root) :] in files]
            return _exec_result(stdout="\n".join(hits))
        if command[0] == "grep" and command[1] == "-nHE":
            return _grep(command[2], [a[len(root) :] for a in command[3:]])
        if command[0] == "grep" and command[1] == "-rnE":
            return _grep(command[-2], list(files))
        if command[0] == "grep" and command[1] == "-rlw":
            word = command[command.index("-e") + 1]
            exts = tuple(a[len("--include=*") :] for a in command if a.startswith("--include=*"))
            hits = [
                f"{root}{rel}"
                for rel, text in files.items()
                if rel.endswith(exts) and re.search(rf"\b{word}\b", text)
            ]
            return _exec_result(stdout="\n".join(hits), exit_code=0 if hits else 1)
        if command[0] == "cat":
            rel = command[1][len(root) :]
            if rel in files:
                return _exec_result(stdout=files[rel])
            return _exec_result(exit_code=1, stderr="No such file or directory")
        raise AssertionError(f"unexpected argv {command}")

    return patch("services.container_workspace.exec_in_workspace", new=AsyncMock(side_effect=_exec))


async def _class_ref(client, name, **kw):
    body = {"class_name": name, **({"container_name": "c1"} if not kw else kw)}
    return await client.post("/api/validation/class-reference", json=body)


class TestDottedClassReferenceContainer:
    async def test_in_repo_dotted_pair_valid_and_missing(self, client):
        """THE PAIR: same module, real class vs absent class."""
        with _fake_workspace(_DOTTED_REPO):
            found = await _class_ref(client, "app.models.User")
            missing = await _class_ref(client, "app.models.Ghost")
        assert found.status_code == 200, found.text
        fb, mb = found.json(), missing.json()
        assert fb["valid"] is True
        assert fb["module_resolution"] == "in_repo"
        assert (fb["module"], fb["name"]) == ("app.models", "User")
        assert fb["details"]["file"] == "app/models.py"
        assert fb["details"]["line"] == 3
        assert mb["valid"] is False
        assert mb["module_resolution"] == "in_repo"

    async def test_src_layout_package_init(self, client):
        with _fake_workspace(_DOTTED_REPO):
            resp = await _class_ref(client, "svc.Engine")
        assert resp.json()["valid"] is True
        assert resp.json()["details"]["file"] == "src/svc/__init__.py"

    async def test_class_in_other_module_of_same_repo_is_not_accepted(self, client):
        """User lives in app.models; app.geometry.User must NOT be valid."""
        with _fake_workspace(_DOTTED_REPO):
            resp = await _class_ref(client, "app.geometry.User")
        assert resp.json()["valid"] is False
        assert resp.json()["module_resolution"] == "in_repo"

    async def test_missing_submodule_of_in_repo_package_is_invalid_not_external(self, client):
        with _fake_workspace(_DOTTED_REPO):
            resp = await _class_ref(client, "app.nosuchmod.User")
        assert resp.json()["valid"] is False
        assert resp.json()["module_resolution"] == "in_repo"

    @pytest.mark.parametrize(
        ("dotted", "module", "name"),
        [("pathlib.Path", "pathlib", "Path"), ("os.path", "os", "path")],
    )
    async def test_external_module_is_external_with_null_verdict(
        self, client, dotted, module, name
    ):
        with _fake_workspace(_DOTTED_REPO):
            resp = await _class_ref(client, dotted)
        body = resp.json()
        assert resp.status_code == 200, resp.text
        assert body["module_resolution"] == "external"
        assert body["valid"] is None
        assert (body["module"], body["name"]) == (module, name)

    async def test_mutant_repo_class_path_does_not_satisfy_pathlib_path(self, client):
        """⛔ The no-last-segment rule. The repo DOES define ``class Path``
        (app/geometry.py). An implementation that strips ``pathlib.`` and greps
        ``Path`` repo-wide answers valid=true here -- a false pass. The pair
        (``Path`` undotted IS found) proves the decoy is really visible."""
        with _fake_workspace(_DOTTED_REPO):
            dotted = await _class_ref(client, "pathlib.Path")
            bare = await _class_ref(client, "Path")
        assert bare.json()["valid"] is True
        assert dotted.json()["valid"] is not True
        assert dotted.json()["module_resolution"] == "external"

    async def test_undotted_unchanged(self, client):
        with _fake_workspace(_DOTTED_REPO):
            found = await _class_ref(client, "User")
            missing = await _class_ref(client, "Ghost")
        assert found.json()["valid"] is True
        assert found.json()["details"]["file"] == "app/models.py"
        assert found.json()["module_resolution"] is None
        assert missing.json()["valid"] is False

    @pytest.mark.parametrize(
        "bad",
        ["a..b", "a.b.", ".a", "a.b;rm -rf /", "a.b$(id)", "a.1b", "a.b c", "a.*"],
    )
    async def test_malformed_dotted_rejected_with_400_before_any_exec(self, client, bad):
        mock = AsyncMock(side_effect=AssertionError("exec must not run"))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            resp = await _class_ref(client, bad)
        assert resp.status_code == 400, resp.text
        mock.assert_not_called()

    async def test_malformed_undotted_still_rejected(self, client):
        with _patch_exec(_exec_result()):
            resp = await _class_ref(client, "Foo;rm")
        assert resp.status_code == 400

    async def test_find_timeout_refuses_instead_of_external(self, client):
        """A 124 from find must not become 'external' -- that is a verdict."""
        with _patch_exec(_exec_result(exit_code=124)):
            resp = await _class_ref(client, "pathlib.Path")
        assert resp.status_code == 503

    async def test_find_truncated_refuses(self, client):
        with _patch_exec(_exec_result(truncated=True, bytes_omitted=10)):
            resp = await _class_ref(client, "pathlib.Path")
        assert resp.status_code == 503


# coding-gateway#78: Python's ``$`` matches before a trailing ``\n``, so
# ``^…$`` + ``match`` accepted ``Ghost\n``; grep then read the newline as an OR.
_LINE_BREAK_NAMES = [
    "Ghost\n",
    "Ghost\r\n",
    "Gh\nost",
    "Ghost\x00",
    "app.models.Ghost\n",
    "app.models.Ghost\r\n",
    "app.mod\nels.Ghost",
    "app.models.Ghost\x00",
]


class TestLineBreakNamesRefused:
    @pytest.mark.parametrize("bad", _LINE_BREAK_NAMES)
    async def test_class_reference_400_before_any_exec(self, client, bad):
        mock = AsyncMock(side_effect=AssertionError("exec must not run"))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            resp = await _class_ref(client, bad)
        assert resp.status_code == 400, resp.text
        mock.assert_not_called()

    @pytest.mark.parametrize("bad", ["helper\n", "helper\r\n", "hel\nper", "helper\x00"])
    async def test_function_reference_400_before_any_exec(self, client, bad):
        mock = AsyncMock(side_effect=AssertionError("exec must not run"))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": bad, "container_name": "c1"},
            )
        assert resp.status_code == 400, resp.text
        mock.assert_not_called()

    @pytest.mark.parametrize("name", ["app.models.Ghost\n", "Ghost\n"])
    async def test_ghost_newline_is_not_a_false_pass(self, client, name):
        """THE BUG, end to end: ``app/models.py`` exists and has no ``Ghost``.
        Under the old anchors the newline reached grep as an OR and the second
        pattern matched ``class User(Base):`` -> valid=true."""
        with _fake_workspace(_DOTTED_REPO):
            resp = await _class_ref(client, name)
        assert resp.status_code == 400, resp.text
        assert resp.json().get("valid") is not True

    @pytest.mark.parametrize("bad", ["Path\n", "Path\r\n", "Pa\nth", "Path\x00"])
    def test_split_dotted_class_name_refuses(self, bad):
        from services.dotted_names import split_dotted_class_name

        with pytest.raises(ValueError):
            split_dotted_class_name(f"pathlib.{bad}")
        with pytest.raises(ValueError):
            split_dotted_class_name(f"{bad}.Cls")

    @pytest.mark.parametrize("bad", ["Ghost\n", "Ghost\r\n", "Gh\nost", "Ghost\x00"])
    def test_validate_identifier_refuses(self, bad):
        from services.container_workspace import validate_identifier

        with pytest.raises(ValueError):
            validate_identifier(bad, "class")

    @pytest.mark.parametrize("bad", ["Ghost\n", "Ghost\r", "Gh\nost", "Ghost\x00"])
    @pytest.mark.parametrize("kind", ["class", "function"])
    def test_definition_pattern_refuses_even_if_validator_bypassed(self, bad, kind):
        from services.container_workspace import _definition_pattern

        with pytest.raises(ValueError):
            _definition_pattern(bad, kind)

    @pytest.mark.parametrize(
        ("module", "func", "arg"),
        [
            ("routers.cache", "_validate_container_name", "c1\n"),
            ("routers.graphs", "_validate_container_name", "c1\n"),
        ],
    )
    def test_container_name_guards_refuse_trailing_newline(self, module, func, arg):
        import importlib

        fn = getattr(importlib.import_module(module), func)
        assert fn("c1") == "c1"
        with pytest.raises(Exception):  # noqa: B017 -- each router raises its own 400 type
            fn(arg)

    def test_filename_and_param_key_guards_refuse_trailing_newline(self):
        from middleware.input_validator import InputValidator
        from middleware.security_utils import SecurityUtils

        assert SecurityUtils.is_safe_filename("a.txt") is True
        assert SecurityUtils.is_safe_filename("a.txt\n") is False
        assert InputValidator.is_safe_parameter_key("k") is True
        assert InputValidator.is_safe_parameter_key("k\n") is False


class TestDottedClassReferenceExplicitPath:
    @pytest.fixture
    def repo(self, tmp_path):
        base = tmp_path / "work" / "repo"
        for rel, text in _DOTTED_REPO.items():
            f = base / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        return str(base)

    async def test_in_repo_pair(self, client, repo):
        found = await _class_ref(client, "app.models.User", codebase_path=repo)
        missing = await _class_ref(client, "app.models.Ghost", codebase_path=repo)
        assert found.json()["valid"] is True
        assert found.json()["details"]["file"] == "app/models.py"
        assert missing.json()["valid"] is False
        assert missing.json()["module_resolution"] == "in_repo"

    async def test_mutant_explicit_pathlib_path_not_satisfied_by_repo_path(self, client, repo):
        bare = await _class_ref(client, "Path", codebase_path=repo)
        dotted = await _class_ref(client, "pathlib.Path", codebase_path=repo)
        assert bare.json()["valid"] is True
        assert dotted.json()["valid"] is None
        assert dotted.json()["module_resolution"] == "external"

    async def test_malformed_rejected(self, client, repo):
        resp = await _class_ref(client, "a..b", codebase_path=repo)
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Code Snippet  POST /api/validation/code-snippet
# ---------------------------------------------------------------------------


class TestValidateCodeSnippet:
    async def test_missing_path_and_container_returns_400(self, client):
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "import os"},
        )
        assert resp.status_code == 400
        assert "codebase_path" in resp.json().get("detail", "")

    async def test_container_only_is_delegated_not_refused(self, client):
        """Container mode now DELEGATES to coding-containers instead of a 400.

        Negative control: the previous build answered 400 here without calling
        anything, so the call-count assertion fails on it.
        """
        with _patch_containers_post(_snippet_body("valid")) as mock:
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os", "container_name": "c1"},
            )
        assert resp.status_code == 200
        assert mock.await_count == 1

    async def test_empty_snippet_returns_400(self, client):
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "", "codebase_path": "/some/path"},
        )
        assert resp.status_code == 400

    async def test_oversized_snippet_returns_400(self, client):
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "x" * 200_000, "codebase_path": "/some/path"},
        )
        assert resp.status_code == 400
        assert "too large" in resp.json().get("detail", "")

    async def test_valid_snippet_returns_result(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.return_value.validate_code_snippet.return_value = {
                "valid": True,
                "issues": [],
                "suggestion": None,
                "overall_score": 1.0,
            }
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["overall_score"] == 1.0

    async def test_invalid_snippet_returns_issues(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.return_value.validate_code_snippet.return_value = {
                "valid": False,
                "issues": [
                    {"type": "import", "name": "bogus", "valid": False, "suggestion": "Remove it"}
                ],
                "suggestion": "Fix imports",
                "overall_score": 0.3,
            }
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import bogus", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert body["issues"][0]["name"] == "bogus"
        assert body["overall_score"] == pytest.approx(0.3)

    async def test_internal_error_returns_500(self, client, tmp_path):
        with patch("routers.validation.ReferenceValidator") as mock_cls:
            mock_cls.return_value.validate_code_snippet.side_effect = RuntimeError("unexpected")
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os", "codebase_path": str(tmp_path)},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Consistency endpoints
#
# These ABSTAIN. See routers/validation.py `_CONSISTENCY_NOT_COMPUTED_REASON`
# for the investigation: the five rules behind the checker only ask whether the
# DESCRIPTION MENTIONS the code's params/returns/exceptions/IO/name, and none of
# them inspects what the code computes. `return a - b` and `return a + b` are
# byte-identical inputs to all five, so the checker CANNOT discriminate them and
# a confident `consistent=true` was a wrong answer, not a rounding error.
# Fixing that properly means semantic equivalence checking, which is a research
# task — so the endpoint declares its limit instead of faking a verdict.
# ---------------------------------------------------------------------------


class TestConsistencyAbstains:
    _MISMATCHED = {
        "function_code": "def add(a, b):\n    return a - b\n",
        "description": "Adds two numbers and returns the result",
    }
    _CORRECT = {
        "function_code": "def add(a, b):\n    return a + b\n",
        "description": "Adds two numbers and returns the result",
    }

    async def test_mismatched_pair_is_not_certified_consistent(self, client):
        """THE PAIR. Real checker, no mocks — the two bodies contradict each other.

        The acceptance condition the brief allows: *different verdicts, OR an
        explicit abstention on both*. This checker cannot discriminate them (see
        the module comment), so we assert the abstention — and we assert it on
        BOTH, because an abstention that only fired for the wrong implementation
        would itself be a discrimination we do not actually have.

        The negative control is the last assertion: on the pre-fix code this
        endpoint returned `consistent=true` at 0.91 for the MISMATCHED pair, so
        `consistent is None` fails on the old build.
        """
        mismatched = await client.post(
            "/api/validation/function-consistency", json=self._MISMATCHED
        )
        correct = await client.post("/api/validation/function-consistency", json=self._CORRECT)

        assert mismatched.status_code == 200
        assert correct.status_code == 200

        for resp in (mismatched, correct):
            body = resp.json()
            assert body["consistent"] is None, "must not assert a verdict it cannot compute"
            assert body["not_computed"] is True
            assert "semantic equivalence" in body["not_computed_reason"].lower()
            assert body["checks_performed"], "must say what it DID compute"

    async def test_the_computed_part_is_still_reported(self, client):
        """Documentation completeness is genuinely computed; it keeps its value."""
        resp = await client.post("/api/validation/function-consistency", json=self._CORRECT)
        body = resp.json()
        assert isinstance(body["documentation_consistent"], bool)
        assert 0.0 <= body["documentation_score"] <= 1.0

    async def test_class_consistency_abstains(self, client):
        resp = await client.post(
            "/api/validation/class-consistency",
            json={"class_code": "class Foo:\n    pass\n", "description": "Represents a bar"},
        )
        assert resp.status_code == 200
        assert resp.json()["consistent"] is None
        assert resp.json()["not_computed"] is True

    async def test_module_consistency_abstains(self, client):
        resp = await client.post(
            "/api/validation/module-consistency",
            json={"modules": ["import os", "import sys"], "module_paths": ["a.py", "b.py"]},
        )
        assert resp.status_code == 200
        assert resp.json()["consistent"] is None

    async def test_module_mismatched_lengths_returns_422(self, client):
        resp = await client.post(
            "/api/validation/module-consistency",
            json={"modules": ["a", "b"], "module_paths": ["/a"]},
        )
        assert resp.status_code == 422

    async def test_function_checker_exception_returns_500(self, client):
        with patch("routers.validation.CodeConsistencyChecker") as mock_cls:
            mock_cls.side_effect = RuntimeError("broken")
            resp = await client.post(
                "/api/validation/function-consistency",
                json={"function_code": "def x(): pass", "description": "Does nothing"},
            )
        assert resp.status_code == 500

    async def test_class_checker_exception_returns_500(self, client):
        with patch("routers.validation.CodeConsistencyChecker") as mock_cls:
            mock_cls.side_effect = RuntimeError("broken")
            resp = await client.post(
                "/api/validation/class-consistency",
                json={"class_code": "class Foo: pass", "description": "Something"},
            )
        assert resp.status_code == 500

    async def test_module_checker_exception_returns_500(self, client):
        with patch("routers.validation.CodeConsistencyChecker") as mock_cls:
            mock_cls.side_effect = RuntimeError("boom")
            resp = await client.post(
                "/api/validation/module-consistency",
                json={"modules": ["a"], "module_paths": ["/a"]},
            )
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# Code Validation  POST /api/validation/code
# ---------------------------------------------------------------------------


class TestValidateCode:
    async def test_nonexistent_file_is_not_certified_valid(self, client):
        """The headline defect: /etc/passwd in a fake container returned valid=true.

        Negative control: on the pre-fix code this assertion fails, because the
        handler built {"valid": True, "errors": []} as a literal before ever
        looking at the file.
        """
        with _patch_exec(
            _exec_result(stdout="", exit_code=1, stderr="cat: x: No such file or directory")
        ):
            resp = await client.post(
                "/api/validation/code",
                json={
                    "container_name": "c1",
                    "file_path": "zz_nonexistent_4711.py",
                    "validation_type": "syntax",
                },
            )
        assert resp.status_code == 404
        assert "valid" not in resp.text or resp.json().get("validation_results") is None

    async def test_good_and_bad_syntax_differ(self, client):
        """THE PAIR — a real parse, so the two files must disagree."""
        with _patch_exec(_exec_result(stdout="def f():\n    return 1\n")):
            good = await client.post(
                "/api/validation/code",
                json={
                    "container_name": "c1",
                    "file_path": "good.py",
                    "validation_type": "syntax",
                },
            )
        with _patch_exec(_exec_result(stdout="def f(:\n    return\n")):
            bad = await client.post(
                "/api/validation/code",
                json={"container_name": "c1", "file_path": "bad.py", "validation_type": "syntax"},
            )

        assert good.json()["validation_results"]["valid"] is True
        assert bad.json()["validation_results"]["valid"] is False
        assert bad.json()["validation_results"]["errors"], "a syntax error must be reported"

    async def test_unknown_container_returns_404(self, client):
        from services.container_workspace import ContainerNotResolvableError

        with _patch_exec(ContainerNotResolvableError("ghost")):
            resp = await client.post(
                "/api/validation/code",
                json={
                    "container_name": "ghost",
                    "file_path": "a.py",
                    "validation_type": "syntax",
                },
            )
        assert resp.status_code == 404

    @pytest.mark.parametrize("validation_type", ["semantic", "style", "security", "unknown_type"])
    async def test_uncomputable_types_refuse_with_501(self, client, validation_type):
        """Previously each of these returned valid=true with an empty issue list."""
        resp = await client.post(
            "/api/validation/code",
            json={
                "container_name": "c1",
                "file_path": "src/main.py",
                "validation_type": validation_type,
            },
        )
        assert resp.status_code == 501
        assert "not computed" in resp.json()["detail"].lower()

    async def test_non_python_file_abstains_rather_than_certifying(self, client):
        with _patch_exec(_exec_result(stdout="package main\n")):
            resp = await client.post(
                "/api/validation/code",
                json={"container_name": "c1", "file_path": "main.go", "validation_type": "syntax"},
            )
        assert resp.status_code == 200
        vr = resp.json()["validation_results"]
        assert vr["not_computed"] is True
        assert "valid" not in vr

    async def test_missing_fields_returns_422(self, client):
        resp = await client.post("/api/validation/code", json={"container_name": "c"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Architecture Validation  POST /api/validation/architecture
# ---------------------------------------------------------------------------


class TestValidateArchitecture:
    # The evaluation itself (no longer a 501) is covered in test_validation_multilang.py.
    async def test_empty_rules_returns_422(self, client):
        resp = await client.post(
            "/api/validation/architecture",
            json={"container_name": "test-container", "rules": []},
        )
        assert resp.status_code == 422

    async def test_missing_container_returns_422(self, client):
        resp = await client.post("/api/validation/architecture", json={"rules": ["rule1"]})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Explicit-path edge cases (unchanged behaviour, still guarded)
# ---------------------------------------------------------------------------


class TestExplicitPathEdgeCases:
    async def test_code_snippet_null_byte_in_path_returns_400(self, client):
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "x = 1", "codebase_path": "/app/data\x00injected"},
        )
        assert resp.status_code == 400

    async def test_code_snippet_root_path_returns_400(self, client):
        resp = await client.post(
            "/api/validation/code-snippet",
            json={"code_snippet": "x = 1", "codebase_path": "/"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Code snippet, container mode -> delegated to coding-containers (#171)
# ---------------------------------------------------------------------------


class TestCodeSnippetDelegation:
    async def test_valid_and_unresolved_verdicts_differ(self, client):
        """THE PAIR: same route, same mocks; only the upstream verdict differs."""
        with _patch_containers_post(_snippet_body("valid")) as ok_mock:
            ok = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os\n", "container_name": "c1"},
            )
        with _patch_containers_post(_snippet_body("invalid", unresolved=["zz_ghost_4711"])):
            bad = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import zz_ghost_4711\n", "container_name": "c1"},
            )
        assert ok.status_code == bad.status_code == 200
        assert ok.json()["valid"] is True and ok.json()["verdict"] == "valid"
        assert bad.json()["valid"] is False and bad.json()["verdict"] == "invalid"
        assert bad.json()["unresolved_imports"] == ["zz_ghost_4711"]
        assert ok.json()["resolution"] == "container_workspace"

        path, payload = ok_mock.await_args.args
        assert path == "/api/validation/multi-line-code-snippet"
        assert payload["container_name"] == "c1"
        assert payload["include_execution_validation"] is False
        assert ok_mock.await_args.kwargs["tenant_id"]

    async def test_upstream_not_computed_is_not_a_pass(self, client):
        with _patch_containers_post(_snippet_body("not_computed", reason="no venv")):
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import requests\n", "container_name": "c1"},
            )
        body = resp.json()
        assert body["valid"] is False
        assert body["verdict"] == "not_computed"
        assert body["not_computed_reason"] == "no venv"

    async def test_upstream_without_verdict_is_not_computed(self, client):
        """A pre-#171 coding-containers says valid=true meaning only 'it parses'."""
        with _patch_containers_post(_snippet_body(None)):
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import zz_ghost\n", "container_name": "c1"},
            )
        body = resp.json()
        assert body["valid"] is False
        assert body["verdict"] == "not_computed"
        assert "no verdict" in body["not_computed_reason"]

    async def test_unknown_container_is_404_not_a_verdict(self, client):
        from services.container_workspace import ContainerNotResolvableError

        with _patch_containers_post(ContainerNotResolvableError("ghost")):
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os\n", "container_name": "ghost"},
            )
        assert resp.status_code == 404

    async def test_imports_are_passed_through(self, client):
        imports = [{"module": "os", "name": None, "line": 1, "status": "stdlib"}]
        with _patch_containers_post(_snippet_body("valid", imports=imports)):
            resp = await client.post(
                "/api/validation/code-snippet",
                json={"code_snippet": "import os\n", "container_name": "c1"},
            )
        assert resp.json()["imports"][0]["status"] == "stdlib"


# ---------------------------------------------------------------------------
# validateCode: imports + lint
# ---------------------------------------------------------------------------


def _code_req(validation_type: str, file_path: str = "src/a.py") -> dict:
    return {"container_name": "c1", "file_path": file_path, "validation_type": validation_type}


class TestValidateCodeImportsAndLint:
    async def test_imports_resolved_and_unresolved_differ(self, client):
        with (
            _patch_exec(_exec_result(stdout="import os\n")),
            _patch_containers_post(_snippet_body("valid")),
        ):
            ok = await client.post("/api/validation/code", json=_code_req("imports"))
        with (
            _patch_exec(_exec_result(stdout="import zz_ghost\n")),
            _patch_containers_post(_snippet_body("invalid", unresolved=["zz_ghost"])),
        ):
            bad = await client.post("/api/validation/code", json=_code_req("imports"))
        okr, badr = ok.json()["validation_results"], bad.json()["validation_results"]
        assert okr["results"]["imports"]["status"] == "valid" and okr["valid"] is True
        assert badr["results"]["imports"]["status"] == "invalid" and badr["valid"] is False
        assert badr["results"]["imports"]["unresolved_imports"] == ["zz_ghost"]

    async def test_imports_not_computed_upstream_is_not_a_pass(self, client):
        with (
            _patch_exec(_exec_result(stdout="import requests\n")),
            _patch_containers_post(_snippet_body("not_computed", reason="no venv")),
        ):
            resp = await client.post("/api/validation/code", json=_code_req("imports"))
        vr = resp.json()["validation_results"]
        assert vr["status"] == "not_computed" and vr["not_computed"] is True
        assert "valid" not in vr
        assert vr["results"]["imports"]["reason"] == "no venv"

    async def test_file_without_imports_needs_no_upstream_call(self, client):
        with (
            _patch_exec(_exec_result(stdout="X = 1\n")),
            _patch_containers_post(_snippet_body("invalid")) as mock,
        ):
            resp = await client.post("/api/validation/code", json=_code_req("imports"))
        assert mock.await_count == 0
        assert resp.json()["validation_results"]["results"]["imports"]["status"] == "valid"

    async def test_imports_non_python_is_not_computed(self, client):
        with (
            _patch_exec(_exec_result(stdout="package main\n")),
            _patch_containers_post(_snippet_body("valid")) as mock,
        ):
            resp = await client.post(
                "/api/validation/code", json=_code_req("imports", file_path="main.go")
            )
        vr = resp.json()["validation_results"]
        assert mock.await_count == 0
        assert vr["results"]["imports"]["status"] == "not_computed"
        assert "python only" in vr["results"]["imports"]["reason"]
        assert "valid" not in vr

    async def test_lint_clean_and_findings_differ(self, client):
        cat = _exec_result(stdout="import os\n")
        with _patch_exec(cat, _exec_result(stdout="", exit_code=0)):
            clean = await client.post("/api/validation/code", json=_code_req("lint"))
        with _patch_exec(
            cat,
            _exec_result(stdout="/workspace/src/a.py:1:1: 'os' imported but unused\n", exit_code=1),
        ):
            dirty = await client.post("/api/validation/code", json=_code_req("lint"))
        c = clean.json()["validation_results"]["results"]["lint"]
        d = dirty.json()["validation_results"]["results"]["lint"]
        assert c["status"] == "valid" and c["tool"] == "pyflakes"
        assert d["status"] == "invalid" and "imported but unused" in d["issues"][0]

    async def test_lint_falls_back_to_ruff_when_pyflakes_missing(self, client):
        missing = _exec_result(exit_code=1, stderr="/usr/bin/python3: No module named pyflakes")
        ruff = _exec_result(
            exit_code=1,
            stdout='[{"code": "F401", "message": "`os` imported but unused", "location": {"row": 1}}]',
        )
        with _patch_exec(_exec_result(stdout="import os\n"), missing, missing, ruff):
            resp = await client.post("/api/validation/code", json=_code_req("lint"))
        lint = resp.json()["validation_results"]["results"]["lint"]
        assert lint["tool"] == "ruff" and lint["status"] == "invalid"
        assert "F401" in lint["issues"][0]

    async def test_no_linter_is_not_computed_never_a_pass(self, client):
        missing = _exec_result(exit_code=1, stderr="No module named pyflakes")
        no_ruff = _exec_result(exit_code=127, stderr="ruff: command not found")
        with _patch_exec(_exec_result(stdout="import os\n"), missing, missing, no_ruff):
            resp = await client.post("/api/validation/code", json=_code_req("lint"))
        vr = resp.json()["validation_results"]
        assert vr["results"]["lint"]["status"] == "not_computed"
        assert "not a clean result" in vr["results"]["lint"]["reason"]
        assert vr["status"] == "not_computed" and "valid" not in vr

    async def test_lint_argv_is_pyflakes_on_the_workspace_path(self, client):
        mock = AsyncMock(side_effect=[_exec_result(stdout="x = 1\n"), _exec_result()])
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            await client.post("/api/validation/code", json=_code_req("lint"))
        assert mock.await_args_list[1].args[1] == [
            "/workspace/.venv/bin/python",
            "-m",
            "pyflakes",
            "/workspace/src/a.py",
        ]

    async def test_combined_types_any_invalid_is_invalid(self, client):
        with _patch_exec(
            _exec_result(stdout="x = 1\n"),
            _exec_result(stdout="/workspace/src/a.py:1: undefined name 'y'\n", exit_code=1),
        ):
            resp = await client.post("/api/validation/code", json=_code_req("syntax,imports,lint"))
        vr = resp.json()["validation_results"]
        assert vr["results"]["syntax"]["status"] == "valid"
        assert vr["results"]["imports"]["status"] == "valid"
        assert vr["results"]["lint"]["status"] == "invalid"
        assert vr["status"] == "invalid" and vr["valid"] is False

    async def test_unknown_type_501_lists_supported(self, client):
        resp = await client.post("/api/validation/code", json=_code_req("semantic"))
        assert resp.status_code == 501
        for t in ("syntax", "imports", "lint"):
            assert t in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Consistency: the structural verdict
# ---------------------------------------------------------------------------


class TestStructuralConsistency:
    async def test_described_param_present_vs_absent_differ(self, client):
        """THE PAIR: the description names `b`; only one signature has it."""
        desc = "Adds two numbers.\n\nArgs:\n    a (int): first\n    b (int): second\n"
        ok = await client.post(
            "/api/validation/function-consistency",
            json={"function_code": "def add(a, b):\n    return a + b\n", "description": desc},
        )
        bad = await client.post(
            "/api/validation/function-consistency",
            json={"function_code": "def add(a, c):\n    return a + c\n", "description": desc},
        )
        assert ok.json()["consistent"] is True and ok.json()["structural_consistent"] is True
        assert bad.json()["consistent"] is False
        assert bad.json()["missing_parameters"] == ["b"]
        for r in (ok, bad):
            assert r.json()["semantic"] == "not_computed"
            assert r.json()["not_computed"] is True

    async def test_no_named_parameter_keeps_consistent_null(self, client):
        resp = await client.post(
            "/api/validation/function-consistency",
            json={
                "function_code": "def add(a, b):\n    return a - b\n",
                "description": "Adds the parameters together and returns the result",
            },
        )
        body = resp.json()
        assert body["consistent"] is None and body["structural_consistent"] is None
        assert "names no parameter" in body["consistent_reason"]

    async def test_semantic_mismatch_is_not_claimed(self, client):
        """`a - b` described as adding `a` and `b`: structurally fine, semantics abstain."""
        resp = await client.post(
            "/api/validation/function-consistency",
            json={
                "function_code": "def add(a, b):\n    return a - b\n",
                "description": "Adds parameter `a` and parameter `b`.",
            },
        )
        body = resp.json()
        assert body["consistent"] is True
        assert body["semantic"] == "not_computed"
        assert "semantic equivalence" in body["not_computed_reason"].lower()

    @pytest.mark.parametrize(
        "desc",
        [
            ":param x: the value\n:param ghost: nope",
            "@param {int} ghost the value",
            "Uses the argument 'ghost'.",
            "Parameters\n----------\nghost : int\n    the value\n",
        ],
    )
    async def test_each_cue_names_a_parameter(self, client, desc):
        resp = await client.post(
            "/api/validation/function-consistency",
            json={"function_code": "def f(x):\n    return x\n", "description": desc},
        )
        assert resp.json()["consistent"] is False
        assert "ghost" in resp.json()["missing_parameters"]

    async def test_args_continuation_line_is_not_a_parameter(self, client):
        desc = "Args:\n    x (int): the value.\n        Default: 5\n"
        resp = await client.post(
            "/api/validation/function-consistency",
            json={"function_code": "def f(x=5):\n    return x\n", "description": desc},
        )
        assert resp.json()["described_parameters"] == ["x"]
        assert resp.json()["consistent"] is True

    async def test_class_uses_init_parameters(self, client):
        code = "class P:\n    def __init__(self, name):\n        self.name = name\n"
        ok = await client.post(
            "/api/validation/class-consistency",
            json={"class_code": code, "description": "A person.\n:param name: the name"},
        )
        bad = await client.post(
            "/api/validation/class-consistency",
            json={"class_code": code, "description": "A person.\n:param age: the age"},
        )
        assert ok.json()["consistent"] is True
        assert bad.json()["consistent"] is False

    async def test_unparseable_code_is_null_with_reason(self, client):
        resp = await client.post(
            "/api/validation/function-consistency",
            json={"function_code": "def f(:\n", "description": ":param x: v"},
        )
        assert resp.json()["consistent"] is None
        assert "parseable" in resp.json()["consistent_reason"]


# ---------------------------------------------------------------------------
# Located references: file_path / line / directories / language honesty
# ---------------------------------------------------------------------------

_TWO_SITES = "/workspace/src/a.py:3:def target(x):\n/workspace/src/b.py:9:    def target(self):\n"


class TestLocatedReferences:
    @pytest.mark.parametrize("given", ["src/a.py", "./src/a.py", "/workspace/src/a.py"])
    async def test_function_in_named_file_vs_other_file(self, client, given):
        with _patch_exec(_exec_result(stdout=_TWO_SITES)):
            hit = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "target", "container_name": "c1", "file_path": given},
            )
        with _patch_exec(_exec_result(stdout=_TWO_SITES)):
            miss = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "target", "container_name": "c1", "file_path": "src/c.py"},
            )
        assert hit.json()["valid"] is True and hit.json()["in_file"] is True
        assert miss.json()["valid"] is False and miss.json()["in_file"] is False
        assert miss.json()["found_in"] == ["src/a.py", "src/b.py"]
        assert "src/c.py" in miss.json()["note"]

    async def test_without_file_path_verdict_unchanged_and_found_in_listed(self, client):
        with _patch_exec(_exec_result(stdout=_TWO_SITES)):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "target", "container_name": "c1"},
            )
        assert resp.json()["valid"] is True
        assert resp.json()["in_file"] is None
        assert resp.json()["found_in"] == ["src/a.py", "src/b.py"]

    async def test_class_in_named_file_vs_other_file(self, client):
        out = "/workspace/src/m.py:1:class Foo:\n"
        with _patch_exec(_exec_result(stdout=out)):
            hit = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "Foo", "container_name": "c1", "file_path": "src/m.py"},
            )
        with _patch_exec(_exec_result(stdout=out)):
            miss = await client.post(
                "/api/validation/class-reference",
                json={"class_name": "Foo", "container_name": "c1", "file_path": "src/n.py"},
            )
        assert hit.json()["valid"] is True
        assert miss.json()["valid"] is False

    @pytest.mark.parametrize(
        "endpoint,key", [("function", "function_name"), ("class", "class_name")]
    )
    async def test_unsupported_file_path_is_null_not_false(self, client, endpoint, key):
        mock = AsyncMock()
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            resp = await client.post(
                f"/api/validation/{endpoint}-reference",
                json={key: "Target", "container_name": "c1", "file_path": "web/app.rb"},
            )
        body = resp.json()
        assert resp.status_code == 200
        assert body["valid"] is None
        assert body["language_supported"] is False
        assert "not searchable" in body["note"]
        assert ".ts" in body["note"]
        mock.assert_not_awaited()

    async def test_python_result_declares_language_supported(self, client):
        with _patch_exec(_exec_result(stdout=_TWO_SITES)):
            resp = await client.post(
                "/api/validation/function-reference",
                json={"function_name": "target", "container_name": "c1", "file_path": "src/a.py"},
            )
        assert resp.json()["language_supported"] is True

    async def test_file_path_with_explicit_codebase_is_refused(self, client, tmp_path):
        resp = await client.post(
            "/api/validation/function-reference",
            json={
                "function_name": "target",
                "codebase_path": str(tmp_path),
                "file_path": "src/a.py",
            },
        )
        assert resp.status_code == 400

    async def test_file_path_traversal_refused(self, client):
        resp = await client.post(
            "/api/validation/function-reference",
            json={"function_name": "target", "container_name": "c1", "file_path": "../x.py"},
        )
        assert resp.status_code == 400

    async def test_line_in_range_vs_out_of_range(self, client):
        found = _exec_result(stdout="/workspace/src/a.py\n")
        with _patch_exec(found, _exec_result(stdout="10\n")):
            inside = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/a.py", "container_name": "c1", "line": 10},
            )
        with _patch_exec(found, _exec_result(stdout="10\n")):
            outside = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/a.py", "container_name": "c1", "line": 11},
            )
        assert inside.json()["line_exists"] is True
        assert outside.json()["line_exists"] is False
        assert inside.json()["line_count"] == 10

    async def test_line_count_failure_is_null_not_false(self, client):
        found = _exec_result(stdout="/workspace/src/a.py\n")
        with _patch_exec(found, _exec_result(exit_code=2, stderr="awk: boom")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/a.py", "container_name": "c1", "line": 1},
            )
        # The claim was "line 1 of src/a.py"; the line half was not computed, so
        # the claim as a whole is not computed -- never a pass.
        assert resp.json()["valid"] is None
        assert resp.json()["line_exists"] is None
        assert "line" in resp.json()["note"]

    async def test_line_out_of_range_makes_the_reference_invalid(self, client):
        """`line` is part of the claim: an out-of-range line is a false reference."""
        found = _exec_result(stdout="/workspace/src/a.py\n")
        with _patch_exec(found, _exec_result(stdout="10\n")):
            inside = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/a.py", "container_name": "c1", "line": 10},
            )
        with _patch_exec(found, _exec_result(stdout="10\n")):
            outside = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/a.py", "container_name": "c1", "line": 11},
            )
        assert inside.json()["valid"] is True
        assert outside.json()["valid"] is False

    async def test_directory_is_not_a_file_unless_allow_dir(self, client):
        """`src/models` must not satisfy a claim about `src/models.py`."""
        no_file = _exec_result(stdout="")
        is_dir = _exec_result(stdout="/workspace/src/pkg\n")
        with _patch_exec(no_file, is_dir):
            d = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/pkg", "container_name": "c1"},
            )
        with _patch_exec(no_file, is_dir):
            allowed = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/pkg", "container_name": "c1", "allow_dir": True},
            )
        with _patch_exec(no_file, _exec_result(stdout="")):
            m = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/nope", "container_name": "c1", "allow_dir": True},
            )
        assert d.json()["valid"] is False and d.json()["is_dir"] is True
        assert allowed.json()["valid"] is True and allowed.json()["is_dir"] is True
        assert m.json()["valid"] is False and m.json()["is_dir"] is False

    async def test_refused_directory_check_is_null_not_a_503(self, client):
        """coding-containers' dangerous-pattern regex refuses a path containing
        `shutdown` with a 400. For the NEW dir probe that is 'not computed'."""
        from services.container_workspace import WorkspaceRequestRefusedError

        refused = WorkspaceRequestRefusedError("c1", "dangerous pattern")
        with _patch_exec(_exec_result(stdout=""), refused):
            allowed = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/shutdown", "container_name": "c1", "allow_dir": True},
            )
        with _patch_exec(_exec_result(stdout=""), refused):
            strict = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/shutdown", "container_name": "c1"},
            )
        assert allowed.status_code == 200
        assert allowed.json()["valid"] is None and allowed.json()["is_dir"] is None
        assert "refused by workspace command policy" in allowed.json()["note"]
        # Without allow_dir a directory could never make it valid; no file -> False.
        assert strict.json()["valid"] is False

    async def test_refused_line_count_is_null_not_a_503(self, client):
        from services.container_workspace import WorkspaceRequestRefusedError

        found = _exec_result(stdout="/workspace/src/shutdown.py\n")
        with _patch_exec(found, WorkspaceRequestRefusedError("c1", "dangerous pattern")):
            resp = await client.post(
                "/api/validation/file-reference",
                json={"file_path": "src/shutdown.py", "container_name": "c1", "line": 3},
            )
        assert resp.status_code == 200
        assert resp.json()["valid"] is None and resp.json()["line_exists"] is None

    async def test_explicit_path_directory_and_line(self, client, tmp_path):
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "a.py").write_text("x = 1\ny = 2")
        d = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "pkg", "codebase_path": str(repo)},
        )
        f = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "pkg/a.py", "codebase_path": str(repo), "line": 2},
        )
        g = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "pkg", "codebase_path": str(repo), "allow_dir": True},
        )
        out = await client.post(
            "/api/validation/file-reference",
            json={"file_path": "pkg/a.py", "codebase_path": str(repo), "line": 3},
        )
        assert d.json()["valid"] is False and d.json()["is_dir"] is True
        assert g.json()["valid"] is True and g.json()["is_dir"] is True
        assert f.json()["line_exists"] is True and f.json()["line_count"] == 2
        assert f.json()["valid"] is True
        assert out.json()["line_exists"] is False and out.json()["valid"] is False


# ---------------------------------------------------------------------------
# Sound answers: lookalike paths, owner classes, relative imports, lint skew
# ---------------------------------------------------------------------------

_OWNER_SRC = """\
def helper():
    pass


class Other:
    def save(self):
        pass


class Repo:
    class Inner:
        async def flush(self):
            pass

    def load(self):
        pass

    alias = load
"""


async def _fn_ref(client, name, file_path=None):
    body: dict = {"function_name": name, "container_name": "c1"}
    if file_path is not None:
        body["file_path"] = file_path
    return await client.post("/api/validation/function-reference", json=body)


class TestSoundAnswers:
    async def test_lookalike_path_is_not_the_named_file(self, client):
        """`xa/b.py` must not satisfy a claim about `a/b.py` (no suffix matching)."""
        out = "/workspace/xa/b.py:1:def target():\n"
        with _patch_exec(_exec_result(stdout=out)):
            miss = await _fn_ref(client, "target", "a/b.py")
        with _patch_exec(_exec_result(stdout=out)):
            hit = await _fn_ref(client, "target", "xa/b.py")
        assert miss.json()["valid"] is False and miss.json()["in_file"] is False
        assert miss.json()["found_in"] == ["xa/b.py"]
        assert hit.json()["valid"] is True and hit.json()["in_file"] is True

    async def test_class_method_in_file_requires_the_owner_class(self, client):
        files = {"src/repo.py": _OWNER_SRC}
        with _fake_workspace(files):
            on_owner = await _fn_ref(client, "Repo.load", "src/repo.py")
        with _fake_workspace(files):
            on_other_class = await _fn_ref(client, "Repo.save", "src/repo.py")
        with _fake_workspace(files):
            module_level = await _fn_ref(client, "Repo.helper", "src/repo.py")
        with _fake_workspace(files):
            no_such_class = await _fn_ref(client, "Ghost.load", "src/repo.py")
        assert on_owner.json()["valid"] is True
        # `def save` IS in the file -- on Other, not Repo. Grep alone said yes.
        assert on_other_class.json()["in_file"] is True
        assert on_other_class.json()["valid"] is False
        assert "Repo" in on_other_class.json()["note"]
        assert module_level.json()["valid"] is False
        assert no_such_class.json()["valid"] is False

    async def test_nested_owner_and_module_qualifier(self, client):
        files = {"src/pkg/repo.py": _OWNER_SRC}
        with _fake_workspace(files):
            nested = await _fn_ref(client, "Repo.Inner.flush", "src/pkg/repo.py")
        with _fake_workspace(files):
            wrong_nest = await _fn_ref(client, "Repo.flush", "src/pkg/repo.py")
        with _fake_workspace(files):
            qualified = await _fn_ref(client, "pkg.repo.Repo.load", "src/pkg/repo.py")
        with _fake_workspace(files):
            wrong_module = await _fn_ref(client, "zz.repo.Repo.load", "src/pkg/repo.py")
        assert nested.json()["valid"] is True
        assert wrong_nest.json()["valid"] is False
        assert qualified.json()["valid"] is True
        assert wrong_module.json()["valid"] is False

    async def test_owner_check_unparseable_file_is_null(self, client):
        files = {"src/repo.py": "class Repo:\n    def load(self):\n        pass\n  x = (\n"}
        with _fake_workspace(files):
            resp = await _fn_ref(client, "Repo.load", "src/repo.py")
        assert resp.json()["valid"] is None
        assert "not computed" in resp.json()["note"].lower()

    async def test_dotted_without_file_path_says_owner_was_checked(self, client):
        with _fake_workspace({"src/a.py": "class Repo:\n    def load(self):\n        pass\n"}):
            resp = await _fn_ref(client, "Repo.load")
        assert resp.json()["valid"] is True
        assert "owner class" in resp.json()["note"]
        with _patch_exec(_exec_result(stdout="/workspace/src/a.py:3:    def load(self):\n")):
            plain = await _fn_ref(client, "load")
        assert plain.json()["note"] is None

    async def test_dotted_without_file_path_checks_the_owner_in_every_found_file(self, client):
        """`Foo.bar` with only `Baz.bar` in the repo must not pass without file_path."""
        files = {"src/a.py": "class Baz:\n    def bar(self):\n        pass\n"}
        with _fake_workspace(files):
            wrong_owner = await _fn_ref(client, "Foo.bar")
        with _fake_workspace(files):
            right_owner = await _fn_ref(client, "Baz.bar")
        two = {**files, "src/b.py": "class Foo:\n    def bar(self):\n        pass\n"}
        with _fake_workspace(two):
            second_file = await _fn_ref(client, "Foo.bar")
        assert wrong_owner.json()["valid"] is False
        assert wrong_owner.json()["found_in"] == ["src/a.py"]
        assert "Foo" in wrong_owner.json()["note"]
        assert right_owner.json()["valid"] is True
        assert second_file.json()["valid"] is True
        assert "src/b.py" in second_file.json()["note"]

    async def test_dotted_without_file_path_null_when_a_file_is_undecidable(self, client):
        files = {
            "src/a.py": "class Baz:\n    def bar(self):\n        pass\n",
            "src/b.py": "def bar():\n    pass\n  x = (\n",
        }
        with _fake_workspace(files):
            resp = await _fn_ref(client, "Foo.bar")
        assert resp.json()["valid"] is None
        assert "src/b.py" in resp.json()["note"]

    async def test_dotted_without_file_path_caps_the_files_checked(self, client):
        files = {f"src/m{i:02}.py": "def bar():\n    pass\n" for i in range(11)}
        with _fake_workspace(files):
            capped = await _fn_ref(client, "Foo.bar")
        ten = {k: v for k, v in files.items() if k != "src/m10.py"}
        with _fake_workspace(ten):
            uncapped = await _fn_ref(client, "Foo.bar")
        assert capped.json()["valid"] is None
        assert "first 10 of 11" in capped.json()["note"]
        assert uncapped.json()["valid"] is False

    async def test_inherited_member_follows_the_base(self, client):
        src = (
            "class Base:\n    def bar(self):\n        pass\n\n\n"
            "class Child(Base):\n    pass\n\n\n"
            "class Ext(Exception):\n    pass\n\n\n"
            "class Plain(object):\n    def baz(self):\n        pass\n"
        )
        files = {"src/a.py": src}
        with _fake_workspace(files):
            inherited = await _fn_ref(client, "Child.bar", "src/a.py")
        with _fake_workspace(files):
            external = await _fn_ref(client, "Ext.bar", "src/a.py")
        with _fake_workspace(files):
            object_only = await _fn_ref(client, "Plain.bar", "src/a.py")
        # The base is in the same module and defines `bar`: inherited, true.
        assert inherited.json()["valid"] is True
        assert "inherited from Base (src/a.py:1)" in inherited.json()["note"]
        # A builtin base cannot be followed: null, naming it.
        assert external.json()["valid"] is None
        assert "base 'Exception'" in external.json()["note"]
        # `object` is not a base that can supply `bar`: still a definite false.
        assert object_only.json()["valid"] is False

    async def test_class_named_like_its_file_is_not_satisfied_by_module_level(self, client):
        """In `a/Cls.py`, `Cls.meth` must not pass on a module-level `meth`."""
        files = {"a/Cls.py": "def meth():\n    pass\n\n\nclass Cls:\n    pass\n"}
        with _fake_workspace(files):
            on_module = await _fn_ref(client, "Cls.meth", "a/Cls.py")
        with _fake_workspace(files):
            module_qualified = await _fn_ref(client, "a.Cls.meth", "a/Cls.py")
        on_class = {"a/Cls.py": "class Cls:\n    def meth(self):\n        pass\n"}
        with _fake_workspace(on_class):
            control = await _fn_ref(client, "Cls.meth", "a/Cls.py")
        no_class = {"a/Cls.py": "def meth():\n    pass\n"}
        with _fake_workspace(no_class):
            module_func = await _fn_ref(client, "Cls.meth", "a/Cls.py")
        assert on_module.json()["valid"] is False
        # Fully module-qualified (`a.Cls` IS the module): the module-level `meth`.
        assert module_qualified.json()["valid"] is True
        assert control.json()["valid"] is True
        # No class Cls in the file: `Cls.` is the module path, `meth` module-level.
        assert module_func.json()["valid"] is True

    async def test_relative_imports_are_forwarded_absolute(self, client):
        src = (
            "import os\n"
            "from . import sibling\n"
            "from ..other import (\n"
            "    thing,\n"
            "    other_thing,\n"
            ")\n"
            "\n"
            "def f():\n"
            "    from .deep.mod import x  # noqa\n"
            "    return x\n"
        )
        with (
            _patch_exec(_exec_result(stdout=src)),
            _patch_containers_post(_snippet_body("valid")) as mock,
        ):
            resp = await client.post(
                "/api/validation/code",
                json=_code_req("imports", file_path="src/pkg/sub/mod.py"),
            )
        assert resp.json()["validation_results"]["results"]["imports"]["status"] == "valid"
        sent = mock.await_args.args[1]["code_snippet"]
        lines = sent.splitlines()
        assert lines[0] == "import os"
        assert lines[1] == "from pkg.sub import sibling"
        assert lines[2].startswith("from pkg.other import thing, other_thing")
        # line numbers preserved for everything after the multi-line import
        assert lines[7] == "def f():"
        assert lines[8].strip().startswith("from pkg.sub.deep.mod import x")
        assert "from ." not in sent

    async def test_relative_import_over_the_root_stays_relative(self, client):
        src = "from ... import x\nfrom . import y\n"
        with (
            _patch_exec(_exec_result(stdout=src)),
            _patch_containers_post(_snippet_body("not_computed", reason="relative")) as mock,
        ):
            resp = await client.post(
                "/api/validation/code", json=_code_req("imports", file_path="src/a/b.py")
            )
        sent = mock.await_args.args[1]["code_snippet"]
        assert sent.splitlines()[0] == "from ... import x"
        assert sent.splitlines()[1] == "from a import y"
        assert resp.json()["validation_results"]["results"]["imports"]["status"] == "not_computed"

    async def test_lint_skewed_python_is_not_computed(self, client):
        """Correct 3.14 code (`except A, B:`) that an older pyflakes cannot parse."""
        ok_src = "try:\n    pass\nexcept ValueError, TypeError:\n    pass\n"
        stderr = (
            "/workspace/src/a.py:3:8: multiple exception types must be parenthesized\n"
            "except ValueError, TypeError:\n       ^\n"
        )
        # The venv carries no pyflakes, so the (older) system python3 lints.
        no_venv = _exec_result(exit_code=1, stderr="No module named pyflakes")
        with _patch_exec(
            _exec_result(stdout=ok_src), no_venv, _exec_result(stderr=stderr, exit_code=1)
        ):
            skew = await client.post("/api/validation/code", json=_code_req("lint"))
        bad_src = "def f(:\n    pass\n"
        with _patch_exec(
            _exec_result(stdout=bad_src),
            _exec_result(stderr="/workspace/src/a.py:1:7: invalid syntax\n", exit_code=1),
        ):
            broken = await client.post("/api/validation/code", json=_code_req("lint"))
        s = skew.json()["validation_results"]["results"]["lint"]
        b = broken.json()["validation_results"]["results"]["lint"]
        assert s["status"] == "not_computed"
        assert "linter python older than the code" in s["reason"]
        assert b["status"] == "invalid"

    async def test_lint_refused_by_policy_is_not_computed_not_503(self, client):
        from services.container_workspace import WorkspaceRequestRefusedError

        refused = WorkspaceRequestRefusedError("c1", "Command contains dangerous patterns")
        with _patch_exec(_exec_result(stdout="x = 1\n"), refused, refused, refused):
            resp = await client.post(
                "/api/validation/code", json=_code_req("lint", file_path="src/shutdown.py")
            )
        assert resp.status_code == 200
        lint = resp.json()["validation_results"]["results"]["lint"]
        assert lint["status"] == "not_computed"
        assert "refused by workspace command policy" in lint["reason"]
