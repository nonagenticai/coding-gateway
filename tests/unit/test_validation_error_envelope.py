"""What a PRODUCTION caller actually receives from the validation refusals.

⭐ WHY THIS FILE EXISTS, AND WHY IT CANNOT LIVE IN ``test_validation_endpoints.py``.

Every other endpoint test in this repo runs against an application whose error
envelope has been REMOVED. ``tests/conftest.py`` stubs the whole ``coding_client``
package, and among the stubs is::

    setattr(api_exception_handlers_module, "register_exception_handlers",
            lambda app, **kwargs: None)

``app_factory._add_exception_handlers`` calls that no-op, so every endpoint test
sees FastAPI's raw ``{"detail": ...}``. The deployed service does not: it installs
``coding_client.api.exception_handlers``, whose ``_handle_http`` rebuilds every
``HTTPException`` into ``{"error": {code, message, timestamp, request_id,
details?}}`` — forwarding only ``message`` and ``detail["details"]``, and setting
``code = f"HTTP_{status}"``.

So the component that decides what a caller can distinguish was a stub in the only
environment the tests ran in, and PR #74's first two commits asserted a
discriminator that exists nowhere outside that stub:

    ⛔ RETRACTED CLAIM (commit bf717fe, and the ``_workspace_error`` docstring):
       "All four outcomes are now distinguishable at the caller with no access to
        our logs: 200 / 404 / 503 + dict detail (clipped) / 503 + string detail
        (timeout or backend)."

There is no ``detail`` key on the wire. ``detail.error_code`` reads ``None`` on
every response. This file grades that claim where it is actually made — against the
REAL envelope, loaded from the installed package and bypassing the stub — and pins
what genuinely ships, including the part that is NOT separable.

⛔ OUT OF SCOPE HERE: fixing the envelope. ``_handle_http`` dropping ``error_code``
is a house-wide defect — the cited house standard ``routers/jobs.py:138`` loses
``TOO_MANY_JOBS`` the same way and answers ``code: HTTP_429``. The pattern was
copied faithfully; the pattern is broken. These tests therefore assert the CURRENT
truth. When the envelope is fixed they will fail loudly, which is the correct
direction: this file is the record of what callers were promised versus given.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Deliberately duplicated from test_validation_endpoints.py rather than imported:
# that module is the source of truth for the ExecResponse shape
# (coding-containers container_exec.py:149-171), and a cross-module test import
# would couple two files whose only relationship is that they mock the same call.


def _exec_result(
    stdout: str = "",
    exit_code: int = 0,
    truncated: bool = False,
    bytes_omitted: int = 0,
) -> dict:
    return {
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": "",
        "duration": 0.01,
        "truncated": truncated,
        "bytes_omitted": bytes_omitted,
    }


def _patch_exec(result_or_exc):
    """Patch the ONE HTTP call, leaving every parsing and mapping decision real."""
    if isinstance(result_or_exc, BaseException):
        mock = AsyncMock(side_effect=result_or_exc)
    else:
        mock = AsyncMock(return_value=result_or_exc)
    return patch("services.container_workspace.exec_in_workspace", new=mock)


def _load_real_exception_handlers() -> ModuleType:
    """Load the INSTALLED ``coding_client.api.exception_handlers``, not the stub.

    ``sys.modules`` holds conftest's fake for that name, so the module is loaded
    from its file by a spec under a private name. The real package is located via
    ``coding_client.api.metrics``, which conftest deliberately preserves as the
    genuine module for exactly this kind of reason.

    Every step carries a hard assertion. A helper that silently fell back to the
    stub would turn this whole file into theatre — the precise failure it exists
    to catch.
    """
    real_sibling = sys.modules.get("coding_client.api.metrics")
    assert real_sibling is not None and getattr(real_sibling, "__file__", None), (
        "conftest is expected to preserve the REAL coding_client.api.metrics; "
        "without it the installed package cannot be located and this test would "
        "silently grade the stub instead of the envelope."
    )

    path = Path(real_sibling.__file__).with_name("exception_handlers.py")
    assert path.is_file(), f"real envelope module not found next to metrics.py: {path}"

    source = path.read_text()
    # POSITIVE CONTROL on the file itself, before it is executed: these two
    # strings are the envelope's defining behaviour. If a future coding-client
    # moves or renames them, this fails here rather than quietly passing on a
    # module that no longer does what the assertions below describe.
    assert "_handle_http" in source, f"{path} does not define _handle_http"
    assert 'code=f"HTTP_{' in source, f"{path} no longer derives code from the status"

    spec = importlib.util.spec_from_file_location("_real_cc_exception_handlers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # NEGATIVE CONTROL: prove we did not just re-acquire conftest's no-op lambda.
    stub = sys.modules["coding_client.api.exception_handlers"]
    assert module.register_exception_handlers is not stub.register_exception_handlers, (
        "loaded module is the conftest stub; the envelope would not be exercised"
    )
    return module


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_envelope_register():
    """The gateway's own ``register_exception_handlers``, bound to the REAL envelope.

    Rebinds ``middleware.exception_handlers`` — the shim ``app_factory`` actually
    calls — onto the installed module for the duration of the test, so the
    production composition (shim -> settings.debug -> coding_client handlers) is
    what runs, not a hand-rolled approximation of it. Restored in ``finally``; the
    restore is itself asserted, because a leaked real envelope would change the
    body every OTHER endpoint test reads.
    """
    real_module = _load_real_exception_handlers()
    import middleware.exception_handlers as shim

    try:
        with patch.dict(sys.modules, {"coding_client.api.exception_handlers": real_module}):
            importlib.reload(shim)
            assert shim._register is real_module.register_exception_handlers
            yield shim.register_exception_handlers
    finally:
        importlib.reload(shim)
        assert shim._register is not real_module.register_exception_handlers, (
            "the real envelope leaked past this fixture into the rest of the session"
        )


@pytest.fixture
async def envelope_client(app, real_envelope_register):
    """A client for the app with the production error envelope installed.

    ``create_app()`` already ran its (stubbed, no-op) registration; handlers are
    looked up at request time and the middleware stack is built lazily on the
    first call, so registering here is the same composition, just with the real
    function.
    """
    import httpx
    from httpx._transports.asgi import ASGITransport

    real_envelope_register(app)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_FILE_REF = {"file_path": "src/main.py", "container_name": "c1"}
_CLIPPED = _exec_result(
    stdout="/workspace/.venv/x/main.py\n", exit_code=0, truncated=True, bytes_omitted=1048576
)
_TIMED_OUT = _exec_result(stdout="", exit_code=124)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTheDeployedDiscriminator:
    async def test_the_envelope_is_actually_installed(self, envelope_client):
        """Guard the guard: without this, every assertion below could be vacuous.

        If the fixture ever failed to displace the stub, the responses would carry
        ``detail`` and the tests would still "pass" by asserting the stubbed shape.
        So the very first thing measured is that the shape CHANGED.
        """
        with _patch_exec(_CLIPPED):
            resp = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)

        body = resp.json()
        assert resp.status_code == 503
        assert set(body) == {"error"}, f"not the KAG envelope: {body}"
        assert "detail" not in body
        assert {"code", "message", "timestamp", "request_id"} <= set(body["error"])

    async def test_error_code_does_not_survive_the_envelope(self, envelope_client):
        """⛔ The retracted claim, graded. ``WORKSPACE_SEARCH_INCOMPLETE`` is NOT on the wire.

        ``_workspace_error`` raises it inside ``detail``; ``_handle_http`` forwards
        only ``message`` and ``detail["details"]``. The string therefore appears
        nowhere in the response a caller reads, and ``code`` is the generic
        ``HTTP_503`` shared with every other 503 this service emits.
        """
        with _patch_exec(_CLIPPED):
            resp = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)

        raw = json.dumps(resp.json())
        assert "WORKSPACE_SEARCH_INCOMPLETE" not in raw
        assert "error_code" not in raw
        assert resp.json()["error"]["code"] == "HTTP_503"

    async def test_clipped_is_separable_from_timeout_and_backend_only_by_details(
        self, envelope_client
    ):
        """What genuinely ships: ``error.details.tool`` + ``bytes_omitted``, and nothing else.

        All three outcomes are driven through the same route, the same
        ``_workspace_error`` and the same envelope, because asserting the clipped
        case alone would pass against an implementation that decorated EVERY 503
        identically.
        """
        from services.container_workspace import WorkspaceBackendUnavailableError

        with _patch_exec(_CLIPPED):
            clipped = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)
        with _patch_exec(_TIMED_OUT):
            timed_out = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)
        with _patch_exec(WorkspaceBackendUnavailableError("container exec API unreachable: boom")):
            backend = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)

        assert clipped.status_code == timed_out.status_code == backend.status_code == 503
        clipped_err = clipped.json()["error"]
        timeout_err = timed_out.json()["error"]
        backend_err = backend.json()["error"]

        # THE discriminator, as deployed. Not `error_code`; not `code`.
        assert clipped_err["details"] == {
            "container_name": "c1",
            "tool": "find",
            "bytes_omitted": 1048576,
        }
        # ...and it is separable ONLY because the other two carry no `details` at
        # all: their `detail` is a plain string, which `_handle_http` maps to
        # `details=None`, which `_error_response` then omits from the body. That is
        # incidental, not structural — any unrelated HTTPException whose detail dict
        # happens to have a `details` key produces the same shape, and `_global`'s
        # BaseExceptionGroup branch drops `details` outright.
        assert "details" not in timeout_err
        assert "details" not in backend_err

    async def test_a_timeout_and_a_dead_backend_are_not_separable_as_deployed(
        self, envelope_client
    ):
        """⚠️ Stating the gap rather than asserting a fiction.

        The PR deferred giving these two their own codes. This is what that
        deferral costs a caller today: byte-identical structure, identical
        ``code``, no ``details`` on either. The ONLY difference is free English in
        ``message``, which nothing should be parsed out of. If a future change
        makes them separable, this test fails and that is the signal to delete it.
        """
        from services.container_workspace import WorkspaceBackendUnavailableError

        with _patch_exec(_TIMED_OUT):
            timed_out = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)
        with _patch_exec(WorkspaceBackendUnavailableError("container exec API unreachable: boom")):
            backend = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)

        volatile = {"message", "timestamp", "request_id"}
        timeout_err = timed_out.json()["error"]
        backend_err = backend.json()["error"]

        assert set(timeout_err) == set(backend_err)
        assert (
            {k: v for k, v in timeout_err.items() if k not in volatile}
            == {k: v for k, v in backend_err.items() if k not in volatile}
            == {"code": "HTTP_503"}
        )
        # The messages DO differ — so the information exists, it simply is not
        # machine-readable. Pinned so "they are identical" cannot be misread as
        # "the timeout case lost its diagnosis".
        assert "exit 124" in timeout_err["message"]
        assert "unreachable" in backend_err["message"]
        assert timeout_err["message"] != backend_err["message"]

    async def test_grep_is_labelled_distinctly_from_find(self, envelope_client):
        """``details.tool`` is the only field that says WHICH search was clipped."""
        with _patch_exec(_exec_result(stdout="", exit_code=1, truncated=True, bytes_omitted=2048)):
            resp = await envelope_client.post(
                "/api/validation/function-reference",
                json={"function_name": "do_stuff", "container_name": "c1"},
            )

        assert resp.status_code == 503
        assert resp.json()["error"]["details"] == {
            "container_name": "c1",
            "tool": "grep",
            "bytes_omitted": 2048,
        }

    async def test_a_genuine_miss_is_still_a_verdict_through_the_envelope(self, envelope_client):
        """THE CONTROL. The envelope must not turn 200 verdicts into refusals.

        Without this, every assertion above is consistent with an application that
        had started answering 503 to everything.
        """
        with _patch_exec(_exec_result(stdout="", exit_code=0)):
            missing = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)
        with _patch_exec(_exec_result(stdout="/workspace/src/main.py\n", exit_code=0)):
            found = await envelope_client.post("/api/validation/file-reference", json=_FILE_REF)

        assert missing.status_code == found.status_code == 200
        assert "error" not in missing.json() and "error" not in found.json()
        assert missing.json()["valid"] is False
        assert found.json()["valid"] is True


class TestValidateCodeRefusalIsHandled:
    async def test_an_incomplete_read_is_not_computed_not_a_verdict_or_500(self, envelope_client):
        """GAP-2 closed: an incomplete read is answered, through the REAL envelope.

        ``read_workspace_file_checked`` raises ``WorkspaceSearchIncompleteError`` for
        a timed-out, clipped or exit-code-less ``cat``. ``validate_code`` turns that
        into a 200 whose every type is ``not_computed`` and which carries NO
        ``valid`` key -- not an unhandled 500, and not a verdict on half a file.
        """
        from services.container_workspace import WorkspaceSearchIncompleteError

        exc = WorkspaceSearchIncompleteError(container_name="c1", tool="cat", bytes_omitted=99)
        with patch(
            "routers.validation.read_workspace_file_checked", new=AsyncMock(side_effect=exc)
        ):
            resp = await envelope_client.post(
                "/api/validation/code",
                json={
                    "container_name": "c1",
                    "file_path": "src/main.py",
                    "validation_type": "syntax",
                },
            )

        assert resp.status_code == 200, resp.text
        vr = resp.json()["validation_results"]
        assert vr["status"] == "not_computed"
        assert "valid" not in vr
        assert vr["results"]["syntax"]["status"] == "not_computed"
