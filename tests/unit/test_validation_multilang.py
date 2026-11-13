"""Multi-language references, validateArchitecture, and GAP-2 at the endpoints.

Every behavioural case is a PAIR (or a triple): the true and the false answer
through the same code path with the same doubles, so a handler that answers a
constant fails. Only the workspace exec call and the coding-analysis symbols
call are doubled; the parsing and the verdict logic run for real.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from services.architecture_rules import (
    Edge,
    _rust_use_tokens,
    evaluate_rule,
    parse_edges,
    parse_jsonc,
    split_statements,
)
from services.container_workspace import SEARCHED_LANGUAGES
from services.symbol_index import SymbolsFetch, fetch_symbols, match_symbol

ROOT = "/workspace/"


def _exec(stdout: str = "", exit_code: int | None = 0, stderr: str = "", truncated=False):
    result = {"stdout": stdout, "stderr": stderr, "truncated": truncated, "bytes_omitted": 0}
    if exit_code is not None:
        result["exit_code"] = exit_code
    return result


def _workspace(files: dict[str, str], overrides: dict[str, dict] | None = None):
    """An ``exec_in_workspace`` double over an in-memory tree.

    Interprets ``cat``, ``find`` (listing by -name), ``grep -rnE`` (with
    --include) and ``grep -rlw``. *overrides* maps an argv kind ("cat:<rel>",
    "rlw", "rnE", "find") to a canned result, to model a failing call.
    """
    overrides = overrides or {}

    def _includes(command: list[str]) -> tuple[str, ...]:
        return tuple(a[len("--include=*") :] for a in command if a.startswith("--include=*"))

    async def _run(container_name, command, tenant_id, timeout=30):
        if command[0] == "cat":
            rel = command[1][len(ROOT) :]
            if f"cat:{rel}" in overrides:
                return overrides[f"cat:{rel}"]
            if rel in files:
                return _exec(files[rel])
            return _exec("", 1, f"cat: {command[1]}: No such file or directory")
        if command[0] == "find":
            if "find" in overrides:
                return overrides["find"]
            wanted = {
                command[i + 1]
                for i, a in enumerate(command)
                if a == "-path" and command[i - 1] != "-not"
            }
            if wanted:  # find_module_files: exact candidate paths
                return _exec("\n".join(f"{ROOT}{rel}" for rel in files if f"{ROOT}{rel}" in wanted))
            root = command[1][len(ROOT) :] if command[1].startswith(ROOT) else ""
            under = [rel for rel in files if not root or rel.startswith(root + "/")]
            if "-type" in command and command[command.index("-type") + 1] == "d":
                depth = root.count("/") + 1 if root else 0
                dirs = {"/".join(rel.split("/")[: depth + 1]) for rel in under}
                return _exec("\n".join(f"{ROOT}{d}" for d in sorted(dirs) if d not in files))
            if "-maxdepth" in command:
                under = [rel for rel in under if _dir_of_test(rel) == root]
            exts = tuple(command[i + 1][1:] for i, a in enumerate(command) if a == "-name")
            hits = [f"{ROOT}{rel}" for rel in under if rel.endswith(exts)]
            return _exec("\n".join(hits))
        if command[0] == "grep" and command[1] == "-rlw":
            if "rlw" in overrides:
                return overrides["rlw"]
            word = command[command.index("-e") + 1]
            hits = [
                f"{ROOT}{rel}"
                for rel, text in files.items()
                if rel.endswith(_includes(command)) and re.search(rf"\b{word}\b", text)
            ]
            return _exec("\n".join(hits), 0 if hits else 1)
        if command[0] == "grep" and command[1] in ("-rnE", "-nHE"):
            if "rnE" in overrides:
                return overrides["rnE"]
            if "-e" in command:
                pattern = command[command.index("-e") + 1]
            else:
                pattern = command[2] if command[1] == "-nHE" else command[-2]
            rx = re.compile(
                pattern.replace("[[:space:]]", r"\s").replace("[[:alnum:]_.]", r"[\w.]")
            )
            if command[1] == "-nHE":
                start = command.index("-e") + 2 if "-e" in command else 3
                targets = command[start:]
                selected = {rel: files[rel] for rel in (t[len(ROOT) :] for t in targets)}
            else:
                root = command[-1][len(ROOT) :] if command[-1].startswith(ROOT) else ""
                selected = {
                    rel: text
                    for rel, text in files.items()
                    if rel.endswith(_includes(command)) and (not root or rel.startswith(root + "/"))
                }
            includes = tuple(selected) and ("",)
            if "-A" in command:
                return _grep_after(selected, rx, includes, int(command[command.index("-A") + 1]))
            out = [
                f"{ROOT}{rel}:{i}:{line}"
                for rel, text in selected.items()
                for i, line in enumerate(text.splitlines(), 1)
                if rx.search(line)
            ]
            return _exec("\n".join(out), 0 if out else 1)
        raise AssertionError(f"unexpected argv {command}")

    mock = AsyncMock(side_effect=_run)
    return patch("services.container_workspace.exec_in_workspace", new=mock)


def _grep_after(files: dict[str, str], rx, includes: tuple[str, ...], after: int):
    """GNU ``grep -rnE -A <after>`` output: ``f:N:`` match, ``f-N-`` context, ``--`` gap."""
    out: list[str] = []
    for rel, text in files.items():
        if not rel.endswith(includes):
            continue
        lines = text.splitlines()
        hits = {i for i, line in enumerate(lines, 1) if rx.search(line)}
        shown = sorted({n for h in hits for n in range(h, min(h + after, len(lines)) + 1)})
        previous = None
        for n in shown:
            if out and previous != n - 1:
                out.append("--")
            sep = ":" if n in hits else "-"
            out.append(f"{ROOT}{rel}{sep}{n}{sep}{lines[n - 1]}")
            previous = n
    return _exec("\n".join(out), 0 if out else 1)


def _sym(name, kind, line=1, parent=None):
    return {"name": name, "kind": kind, "line": line, "end_line": line, "parent": parent}


def _body(*symbols, parse_error_line=None, language="typescript"):
    return {
        "language": language,
        "parsed": True,
        "parse_error_line": parse_error_line,
        "symbols": list(symbols),
        "imports": [],
    }


def _symbols(by_file: dict[str, SymbolsFetch | dict]):
    """Patch the symbols client: ``file_path -> body`` (or a SymbolsFetch error)."""

    async def _fetch(code, file_path, tenant_id):
        value = by_file.get(file_path, _body())
        return value if isinstance(value, SymbolsFetch) else SymbolsFetch(value)

    mock = AsyncMock(side_effect=_fetch)
    return patch("routers.validation.fetch_symbols", new=mock)


async def _fn(client, name, **kw):
    return await client.post(
        "/api/validation/function-reference",
        json={"function_name": name, "container_name": "c1", **kw},
    )


async def _cls(client, name, **kw):
    return await client.post(
        "/api/validation/class-reference",
        json={"class_name": name, "container_name": "c1", **kw},
    )


# ---------------------------------------------------------------------------
# Located, non-Python
# ---------------------------------------------------------------------------

_TS = {"web/app.ts": "export const handle = () => 1;\nexport class Store {}\n"}


class TestLocatedTypeScript:
    async def test_present_and_absent_differ(self, client):
        body = _body(_sym("handle", "const_function"), _sym("Store", "class", 2))
        with _workspace(_TS), _symbols({"web/app.ts": body}):
            present = await _fn(client, "handle", file_path="web/app.ts")
            absent = await _fn(client, "ghost", file_path="web/app.ts")
            cls = await _cls(client, "Store", file_path="web/app.ts")
        p, a = present.json(), absent.json()
        assert p["valid"] is True and p["in_file"] is True
        assert p["found_in"] == ["web/app.ts"]
        assert p["language_supported"] is True
        assert p["searched_languages"] == ["typescript"]
        assert p["details"]["kind"] == "const_function"
        assert a["valid"] is False and a["in_file"] is False
        assert cls.json()["valid"] is True

    async def test_parse_error_makes_absence_null(self, client):
        clean = _body(_sym("handle", "function"))
        broken = _body(_sym("handle", "function"), parse_error_line=7)
        with _workspace(_TS), _symbols({"web/app.ts": clean}):
            refuted = await _fn(client, "ghost", file_path="web/app.ts")
        with _workspace(_TS), _symbols({"web/app.ts": broken}):
            unknown = await _fn(client, "ghost", file_path="web/app.ts")
            still_found = await _fn(client, "handle", file_path="web/app.ts")
        assert refuted.json()["valid"] is False
        assert unknown.json()["valid"] is None
        assert "parse error at line 7" in unknown.json()["note"]
        assert still_found.json()["valid"] is True

    async def test_analysis_down_is_null_not_false(self, client):
        down = SymbolsFetch(None, "coding-analysis symbol extraction answered 503: grammar")
        with _workspace(_TS), _symbols({"web/app.ts": down}):
            resp = await _fn(client, "handle", file_path="web/app.ts")
        assert resp.status_code == 200
        assert resp.json()["valid"] is None
        assert "503" in resp.json()["note"]

    async def test_variable_or_namespace_only_is_null_never_false(self, client):
        body = _body(_sym("config", "variable"), _sym("Api", "namespace"))
        with _workspace(_TS), _symbols({"web/app.ts": body}):
            var = await _fn(client, "config", file_path="web/app.ts")
            ns = await _cls(client, "Api", file_path="web/app.ts")
        assert var.json()["valid"] is None
        assert "as a variable" in var.json()["note"]
        assert ns.json()["valid"] is None
        assert "as a namespace" in ns.json()["note"]

    async def test_property_counts_only_for_a_dotted_name(self, client):
        body = _body(_sym("Store", "class"), _sym("size", "property", 2, parent="Store"))
        with _workspace(_TS), _symbols({"web/app.ts": body}):
            dotted = await _fn(client, "Store.size", file_path="web/app.ts")
            bare = await _fn(client, "size", file_path="web/app.ts")
        assert dotted.json()["valid"] is True
        assert bare.json()["valid"] is None
        assert "property" in bare.json()["note"]

    async def test_missing_file_is_false_and_incomplete_read_is_null(self, client):
        with _workspace(_TS), _symbols({}):
            missing = await _fn(client, "handle", file_path="web/none.ts")
        timed_out = {"cat:web/app.ts": _exec("", 124)}
        with _workspace(_TS, timed_out), _symbols({"web/app.ts": _body()}):
            unknown = await _fn(client, "handle", file_path="web/app.ts")
        assert missing.json()["valid"] is False
        assert unknown.json()["valid"] is None
        assert "exit 124" in unknown.json()["note"]

    async def test_header_absence_is_null(self, client):
        files = {"inc/a.h": "Foo make(Bar);\n", "src/a.cc": "int other() { return 1; }\n"}
        with _workspace(files), _symbols({"inc/a.h": _body(language="cpp")}):
            header = await _fn(client, "make", file_path="inc/a.h")
            source = await _fn(client, "make", file_path="src/a.cc")
        assert header.json()["valid"] is None
        assert "header" in header.json()["note"]
        assert source.json()["valid"] is False


class TestLocatedGoDotted:
    _GO = {"srv/server.go": "type Server struct{}\nfunc (s *Server) Start() {}\n"}
    _BODY = _body(
        _sym("Server", "struct"),
        _sym("Start", "method", 2, parent="Server"),
        _sym("Stop", "method", 3, parent="Client"),
        language="go",
    )

    async def test_owner_method_present_absent_and_wrong_owner(self, client):
        with _workspace(self._GO), _symbols({"srv/server.go": self._BODY}):
            start = await _fn(client, "Server.Start", file_path="srv/server.go")
            stop = await _fn(client, "Server.Stop", file_path="srv/server.go")
            ghost = await _fn(client, "Ghost.Run", file_path="srv/server.go")
            bare_stop = await _fn(client, "Stop", file_path="srv/server.go")
        assert start.json()["valid"] is True
        assert start.json()["searched_languages"] == ["go"]
        # The owner type IS here; the member may be embedded/implemented elsewhere.
        assert stop.json()["valid"] is None
        assert "bases were not checked" in stop.json()["note"]
        assert "embedded" in stop.json()["note"]
        # The owner is in no checked file: a definite false.
        assert ghost.json()["valid"] is False
        assert "No type 'Ghost'" in ghost.json()["note"]
        assert bare_stop.json()["valid"] is True

    async def test_namespace_qualifier_is_null(self, client):
        body = _body(_sym("ns", "namespace"), _sym("make", "function"), language="cpp")
        files = {"src/a.cc": "namespace ns { int make() { return 1; } }\n"}
        with _workspace(files), _symbols({"src/a.cc": body}):
            resp = await _fn(client, "ns.make", file_path="src/a.cc")
        assert resp.json()["valid"] is None
        assert "not a type defined there" in resp.json()["note"]


# ---------------------------------------------------------------------------
# Unlocated: Python grep + candidate discovery
# ---------------------------------------------------------------------------


class TestUnlocatedDiscovery:
    _FILES = {
        "src/main.py": "def unrelated():\n    pass\n",
        "web/util.ts": "export function formatDate(d) { return d; }\n",
        "web/use.ts": "import { formatDate } from './util';\n",
    }

    async def test_ts_only_symbol_found_and_ghost_false(self, client):
        bodies = {
            "web/util.ts": _body(_sym("formatDate", "function")),
            "web/use.ts": _body(),
        }
        with _workspace(self._FILES), _symbols(bodies):
            found = await _fn(client, "formatDate")
            ghost = await _fn(client, "parseDate")
        f, g = found.json(), ghost.json()
        assert f["valid"] is True
        assert f["found_in"] == ["web/util.ts"]
        assert f["searched_languages"] == list(SEARCHED_LANGUAGES)
        assert g["valid"] is False
        assert "no non-python source file contains the name" in g["note"].lower()

    async def test_candidate_that_only_mentions_the_name_is_refuted(self, client):
        bodies = {"web/util.ts": _body(), "web/use.ts": _body()}
        with _workspace(self._FILES), _symbols(bodies):
            resp = await _fn(client, "formatDate")
        assert resp.json()["valid"] is False
        assert "web/use.ts" in resp.json()["note"]

    async def test_capped_candidates_are_null(self, client):
        files = {f"web/m{i:02}.ts": "formatDate(1);\n" for i in range(21)}
        with _workspace(files), _symbols({}):
            capped = await _fn(client, "formatDate")
        with _workspace(dict(list(files.items())[:20])), _symbols({}):
            uncapped = await _fn(client, "formatDate")
        assert capped.json()["valid"] is None
        assert "first 20 of 21" in capped.json()["note"]
        assert uncapped.json()["valid"] is False

    async def test_incomplete_candidate_grep_is_null(self, client):
        clipped = {"rlw": _exec("/workspace/web/util.ts", 0, truncated=True)}
        failed = {"rlw": _exec("", 2, "grep: Permission denied")}
        with _workspace(self._FILES, clipped), _symbols({}):
            a = await _fn(client, "formatDate")
        with _workspace(self._FILES, failed), _symbols({}):
            b = await _fn(client, "formatDate")
        assert a.json()["valid"] is None
        assert b.json()["valid"] is None
        assert "could not finish" in a.json()["note"]

    async def test_python_hit_skips_discovery(self, client):
        files = {"src/main.py": "def formatDate():\n    pass\n"}
        with _workspace(files) as mock, _symbols({}) as sym:
            resp = await _fn(client, "formatDate")
        assert resp.json()["valid"] is True
        assert [c.args[1][1] for c in mock.await_args_list] == ["-rnE"]
        sym.assert_not_awaited()

    async def test_unlocated_class_in_java(self, client):
        files = {"src/Main.java": "public class Repo {}\n"}
        bodies = {"src/Main.java": _body(_sym("Repo", "class"), language="java")}
        with _workspace(files), _symbols(bodies):
            hit = await _cls(client, "Repo")
            miss = await _cls(client, "Ghost")
        assert hit.json()["valid"] is True
        assert hit.json()["found_in"] == ["src/Main.java"]
        assert miss.json()["valid"] is False

    async def test_analysis_down_during_discovery_is_null(self, client):
        down = SymbolsFetch(None, "coding-analysis is unreachable")
        with _workspace(self._FILES), _symbols({"web/use.ts": down, "web/util.ts": down}):
            resp = await _fn(client, "formatDate")
        assert resp.json()["valid"] is None
        assert "unavailable" in resp.json()["note"] or "unreachable" in resp.json()["note"]


# ---------------------------------------------------------------------------
# The symbols client
# ---------------------------------------------------------------------------


class _FakeClient:
    def __init__(self, response=None, exc=None):
        self.response, self.exc, self.calls = response, exc, []

    async def post(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.response


class TestFetchSymbols:
    async def _call(self, fake):
        with patch("routers.proxy.get_proxy_client", new=AsyncMock(return_value=fake)):
            return await fetch_symbols("x = 1", "a.ts", "t1")

    async def test_ok_and_failures(self):
        ok = await self._call(_FakeClient(httpx.Response(200, json=_body())))
        down = await self._call(_FakeClient(httpx.Response(503, text="grammar")))
        unparsed = await self._call(_FakeClient(httpx.Response(200, json={"parsed": False})))
        timeout = await self._call(_FakeClient(exc=httpx.ReadTimeout("slow")))
        assert ok.body is not None and ok.error is None
        assert down.body is None and "503" in (down.error or "")
        assert unparsed.body is None
        assert timeout.body is None and "timed out" in (timeout.error or "")

    async def test_posts_content_to_the_symbols_path(self):
        fake = _FakeClient(httpx.Response(200, json=_body()))
        await self._call(fake)
        url, kw = fake.calls[0]
        assert url.endswith("/api/analysis/symbols")
        assert kw["json"] == {"code": "x = 1", "file_path": "a.ts"}

    def test_match_rules_pair(self):
        body = _body(_sym("f", "function"), _sym("C", "class"))
        assert match_symbol(body, "f", "function", "a.ts", "").verdict is True
        assert match_symbol(body, "g", "function", "a.ts", "").verdict is False
        assert match_symbol(body, "C", "function", "a.ts", "").verdict is None
        assert match_symbol(body, "f", "class", "a.ts", "").verdict is None


# ---------------------------------------------------------------------------
# GAP-2 at the endpoint
# ---------------------------------------------------------------------------


class TestValidateCodeIncompleteRead:
    @pytest.mark.parametrize(
        "result",
        [_exec("", 124), _exec("def f(", 0, truncated=True), _exec("", None)],
        ids=["timeout", "clipped", "no-exit-code"],
    )
    async def test_incomplete_read_is_not_computed(self, client, result):
        with _workspace({}, {"cat:src/a.py": result}):
            resp = await client.post(
                "/api/validation/code",
                json={"container_name": "c1", "file_path": "src/a.py", "validation_type": "syntax"},
            )
        assert resp.status_code == 200
        vr = resp.json()["validation_results"]
        assert vr["status"] == "not_computed"
        assert "valid" not in vr

    async def test_real_absence_is_404_and_real_file_is_a_verdict(self, client):
        with _workspace({"src/a.py": "def f(:\n"}):
            absent = await client.post(
                "/api/validation/code",
                json={"container_name": "c1", "file_path": "src/b.py", "validation_type": "syntax"},
            )
            present = await client.post(
                "/api/validation/code",
                json={"container_name": "c1", "file_path": "src/a.py", "validation_type": "syntax"},
            )
        assert absent.status_code == 404
        assert present.json()["validation_results"]["valid"] is False

    async def test_owner_check_with_clipped_read_is_null(self, client):
        files = {"src/a.py": "class Foo:\n    def bar(self):\n        pass\n"}
        with _workspace(files):
            ok = await _fn(client, "Foo.bar", file_path="src/a.py")
        with _workspace(files, {"cat:src/a.py": _exec("class Foo:\n", 0, truncated=True)}):
            clipped = await _fn(client, "Foo.bar", file_path="src/a.py")
        assert ok.json()["valid"] is True
        assert clipped.json()["valid"] is None


# ---------------------------------------------------------------------------
# validateArchitecture
# ---------------------------------------------------------------------------

_REPO = {
    "src/api/routes.py": "from service.users import create\n",
    "src/service/users.py": "from domain.user import User\nimport os\n",
    "src/domain/user.py": "class User:\n    pass\n",
    "src/infra/db.py": "import sqlite3\n",
}


async def _arch(client, rules, files, overrides=None, **kw):
    with _workspace(files, overrides):
        return await client.post(
            "/api/validation/architecture",
            json={"container_name": "c1", "rules": rules, **kw},
        )


class TestValidateArchitecture:
    async def test_no_import_violated_and_honoured(self, client):
        bad = {**_REPO, "src/domain/user.py": "from infra.db import connect\nclass User: ...\n"}
        rule = "no_import:src/domain/*->src/infra/*"
        honoured = (await _arch(client, [rule], _REPO)).json()["validation_results"]
        violated = (await _arch(client, [rule], bad)).json()["validation_results"]
        assert honoured["verdict"] == "valid"
        assert honoured["rules"][0]["status"] == "pass"
        assert violated["verdict"] == "invalid"
        v = violated["rules"][0]["violations"][0]
        assert v == {
            "file": "src/domain/user.py",
            "line": 1,
            "imports": "infra.db",
            "target": "src/infra/db.py",
        }
        assert honoured["files_scanned"] == 4
        assert "python" in honoured["languages_scanned"]

    async def test_no_import_matches_raw_external_module(self, client):
        rule = "no_import:src/infra/*->sqlite3"
        vr = (await _arch(client, [rule], _REPO)).json()["validation_results"]
        assert vr["verdict"] == "invalid"

    async def test_layers_violated_and_honoured(self, client):
        rule = "layers:src/api>src/service>src/domain"
        ok = (await _arch(client, [rule], _REPO)).json()["validation_results"]
        bad_repo = {**_REPO, "src/domain/user.py": "from api.routes import x\n"}
        bad = (await _arch(client, [rule], bad_repo)).json()["validation_results"]
        assert ok["verdict"] == "valid"
        assert bad["verdict"] == "invalid"
        assert bad["rules"][0]["violations"][0]["file"] == "src/domain/user.py"

    async def test_cycle_detected_and_absent(self, client):
        cyclic = {
            "web/a.ts": "import { b } from './b';\n",
            "web/b.ts": "const a = require('./a');\n",
        }
        acyclic = {**cyclic, "web/b.ts": "export const b = 1;\n"}
        bad = (await _arch(client, ["no_cycles"], cyclic)).json()["validation_results"]
        ok = (await _arch(client, ["no_cycles"], acyclic)).json()["validation_results"]
        assert bad["verdict"] == "invalid"
        assert bad["rules"][0]["violations"][0]["cycle"] == ["web/a.ts", "web/b.ts"]
        assert ok["verdict"] == "valid"
        assert bad["languages_scanned"] == ["javascript_typescript"]

    async def test_fan_out(self, client):
        files = {
            "src/hub.py": "import a\nimport b\nimport os\n",
            "src/a.py": "",
            "src/b.py": "",
        }
        over = (await _arch(client, ["max_fan_out:1"], files)).json()["validation_results"]
        within = (await _arch(client, ["max_fan_out:2"], files)).json()["validation_results"]
        assert over["verdict"] == "invalid"
        assert over["rules"][0]["violations"][0]["imports"] == 2
        assert within["verdict"] == "valid"

    async def test_unparsed_rule_alone_is_not_computed(self, client):
        vr = (await _arch(client, ["services must be clean"], _REPO)).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert vr["rules"][0]["reason"] == "unparsed rule: services must be clean"

    async def test_mixed_pass_and_unparsed_is_not_computed_all_pass_is_valid(self, client):
        mixed = (await _arch(client, ["no_cycles", "be nice"], _REPO)).json()
        alone = (await _arch(client, ["no_cycles"], _REPO)).json()
        vr = mixed["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert [r["status"] for r in vr["rules"]] == ["pass", "not_computed"]
        assert any("be nice" in n for n in vr["notes"])
        assert alone["validation_results"]["verdict"] == "valid"

    async def test_mixed_fail_and_unparsed_is_invalid(self, client):
        rules = ["no_import:src/api/*->src/service/*", "be nice"]
        vr = (await _arch(client, rules, _REPO)).json()["validation_results"]
        assert vr["verdict"] == "invalid"

    async def test_glob_matching_nothing_is_not_computed_not_pass(self, client):
        vr = (await _arch(client, ["no_import:lib/*->src/*"], _REPO)).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert "matches no scanned source file" in vr["rules"][0]["reason"]

    @pytest.mark.parametrize(
        "overrides",
        [
            {"rnE": _exec("/workspace/src/api/routes.py:1:import x", 0, truncated=True)},
            {"rnE": _exec("", 124)},
            {"find": _exec("", 1, "find: permission denied")},
        ],
        ids=["clipped-grep", "timeout-grep", "failed-find"],
    )
    async def test_incomplete_search_is_not_computed(self, client, overrides):
        rules = ["no_import:src/domain/*->src/infra/*"]
        vr = (await _arch(client, rules, _REPO, overrides)).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert vr["rules"][0]["status"] == "not_computed"

    async def test_file_cap_is_not_computed(self, client):
        files = {f"src/m{i}.py": "" for i in range(6)}
        with patch("services.architecture_rules.FILE_CAP", 5):
            vr = (await _arch(client, ["no_cycles"], files)).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert "5-file" in vr["notes"][0]
        assert vr["scan"]["complete"] is False

    async def test_unknown_container_is_404(self, client):
        from services.container_workspace import ContainerNotResolvableError

        mock = AsyncMock(side_effect=ContainerNotResolvableError("c1"))
        with patch("services.container_workspace.exec_in_workspace", new=mock):
            resp = await client.post(
                "/api/validation/architecture",
                json={"container_name": "c1", "rules": ["no_cycles"]},
            )
        assert resp.status_code == 404

    async def test_empty_rules_and_missing_container_are_422(self, client):
        empty = await client.post(
            "/api/validation/architecture", json={"container_name": "c", "rules": []}
        )
        missing = await client.post("/api/validation/architecture", json={"rules": ["r"]})
        assert empty.status_code == missing.status_code == 422

    async def test_violations_are_capped_at_50(self, client):
        files = {f"src/domain/m{i}.py": "import infra.db\n" for i in range(51)}
        files["src/infra/db.py"] = ""
        vr = (await _arch(client, ["no_import:src/domain/*->src/infra/*"], files)).json()[
            "validation_results"
        ]
        rule = vr["rules"][0]
        assert len(rule["violations"]) == 50
        assert rule["truncated"] is True
        assert rule["violation_count"] == 51


class TestImportParsing:
    def test_other_languages(self):
        files = frozenset({"inc/util.h", "src/a.cc", "src/main/java/a/b/C.java"})
        cpp = parse_edges(
            "cpp",
            [
                {"file": "src/a.cc", "line": 1, "source": '#include "../inc/util.h"'},
                {"file": "src/a.cc", "line": 2, "source": "#include <vector>"},
            ],
            files,
        )
        assert [e.target for e in cpp] == ["inc/util.h", None]
        java = parse_edges(
            "java", [{"file": "x.java", "line": 3, "source": "import a.b.C;"}], files
        )
        assert java[0].target == "src/main/java/a/b/C.java"
        rust = parse_edges(
            "rust", [{"file": "x.rs", "line": 1, "source": "use crate::db::Pool;"}], files
        )
        assert rust[0].module == "crate::db::Pool"
        go = parse_edges("go", [{"file": "m.go", "line": 2, "source": '\t"github.com/x/y"'}], files)
        assert go[0].module == "github.com/x/y"
        cs = parse_edges(
            "c_sharp", [{"file": "p.cs", "line": 1, "source": "using App.Data;"}], files
        )
        assert cs[0].module == "App.Data"

    def test_python_prose_is_not_an_import(self):
        hits = [{"file": "a.py", "line": 1, "source": "    from the start we import things"}]
        assert parse_edges("python", hits, frozenset()) == []

    def test_raw_module_glob_uses_slashes(self):
        edge = Edge("src/x.rs", 1, "crate::infra::db", None)
        result = evaluate_rule("no_import:src/*->crate/infra/*", [edge], ["src/x.rs"])
        assert result["status"] == "fail"


# ---------------------------------------------------------------------------
# Absence exceptions: owner present, re-exports, macros
# ---------------------------------------------------------------------------


class TestDottedOwnerPresent:
    async def test_ts_class_without_member_is_null_owner_absent_is_false(self, client):
        files = {"web/a.ts": "export class Store extends Base {}\n"}
        body = _body(_sym("Store", "class"))
        with _workspace(files), _symbols({"web/a.ts": body}):
            inherited = await _fn(client, "Store.size", file_path="web/a.ts")
            no_owner = await _fn(client, "Cart.size", file_path="web/a.ts")
        assert inherited.json()["valid"] is None
        assert "inherited" in inherited.json()["note"]
        assert no_owner.json()["valid"] is False


class TestReExports:
    _BODY = _body()  # a barrel defines nothing itself

    @pytest.mark.parametrize(
        "text",
        [
            "export { Foo } from './foo';\n",
            "export {\n  Bar, Foo as Baz } from './foo';\n",
            "export * from './foo';\n// Foo\n",
            "import Foo = require('./foo');\nexport = Foo;\n",
            "module.exports = require('./foo'); // Foo\n",
        ],
        ids=["named", "multi-line-list", "star", "export-equals", "cjs"],
    )
    async def test_located_barrel_is_null(self, client, text):
        files = {"web/index.ts": text}
        with _workspace(files), _symbols({"web/index.ts": self._BODY}):
            resp = await _cls(client, "Foo", file_path="web/index.ts")
        assert resp.json()["valid"] is None
        assert "re-export" in resp.json()["note"]

    async def test_imported_name_is_null_plain_file_is_false(self, client):
        files = {"web/a.ts": "import { Foo } from './foo';\nuse(Foo);\n", "web/b.ts": "x();\n"}
        imported = {**_body(), "imports": [{"module": "./foo", "names": ["Foo"], "line": 1}]}
        with _workspace(files), _symbols({"web/a.ts": imported, "web/b.ts": _body()}):
            via_import = await _cls(client, "Foo", file_path="web/a.ts")
            plain = await _cls(client, "Foo", file_path="web/b.ts")
        assert via_import.json()["valid"] is None
        assert "imports 'Foo'" in via_import.json()["note"]
        assert plain.json()["valid"] is False

    async def test_export_list_without_the_name_stays_false(self, client):
        files = {"web/a.ts": "export { Other } from './o';\nconst Foo2 = 1;\n"}
        with _workspace(files), _symbols({"web/a.ts": _body()}):
            resp = await _cls(client, "Foo", file_path="web/a.ts")
        assert resp.json()["valid"] is False

    async def test_unlocated_barrel_candidate_is_null(self, client):
        barrel = {"web/index.ts": "export { Foo } from './foo';\n"}
        plain = {"web/use.ts": "const x = Foo;\n"}
        with _workspace(barrel), _symbols({"web/index.ts": _body()}):
            via_barrel = await _cls(client, "Foo")
        with _workspace(plain), _symbols({"web/use.ts": _body()}):
            mention = await _cls(client, "Foo")
        assert via_barrel.json()["valid"] is None
        assert "re-export" in via_barrel.json()["note"]
        assert mention.json()["valid"] is False


class TestMacroGenerated:
    async def test_rust_and_c_mention_is_null_go_mention_is_false(self, client):
        files = {
            "src/lib.rs": "make_handler!(serve);\n",
            "src/gen.c": "DEFINE_FN(serve)\n",
            "srv/main.go": "func main() { serve() }\n",
            "src/other.rs": "fn unrelated() {}\n",
        }
        bodies = {
            "src/lib.rs": _body(language="rust"),
            "src/gen.c": _body(language="cpp"),
            "srv/main.go": _body(_sym("main", "function"), language="go"),
            "src/other.rs": _body(_sym("unrelated", "function"), language="rust"),
        }
        with _workspace(files), _symbols(bodies):
            rust = await _fn(client, "serve", file_path="src/lib.rs")
            c = await _fn(client, "serve", file_path="src/gen.c")
            go = await _fn(client, "serve", file_path="srv/main.go")
            rust_absent = await _fn(client, "serve", file_path="src/other.rs")
        assert rust.json()["valid"] is None
        assert "may be generated by a macro" in rust.json()["note"]
        assert c.json()["valid"] is None
        assert go.json()["valid"] is False
        assert rust_absent.json()["valid"] is False

    async def test_unlocated_rust_candidate_is_null_java_is_false(self, client):
        rust = {"src/lib.rs": "make_handler!(Serve);\n"}
        java = {"src/A.java": "class A { Serve s; }\n"}
        with _workspace(rust), _symbols({"src/lib.rs": _body(language="rust")}):
            r = await _cls(client, "Serve")
        with _workspace(java), _symbols({"src/A.java": _body(_sym("A", "class"))}):
            j = await _cls(client, "Serve")
        assert r.json()["valid"] is None
        assert "macro" in r.json()["note"]
        assert j.json()["valid"] is False


# ---------------------------------------------------------------------------
# validateArchitecture: evidence, resolution, globs
# ---------------------------------------------------------------------------


def _rule(vr):
    return vr["rules"][0]


class TestArchitectureEvidence:
    async def test_no_internal_edge_is_not_computed(self, client):
        external = {"src/a.py": "import os\n", "src/b.py": "import json\n"}
        internal = {"src/a.py": "import b\n", "src/b.py": "import json\n"}
        for rule in ("no_cycles", "max_fan_out:3"):
            none = (await _arch(client, [rule], external)).json()["validation_results"]
            some = (await _arch(client, [rule], internal)).json()["validation_results"]
            assert _rule(none)["status"] == "not_computed"
            assert "no resolved internal import edge" in _rule(none)["reason"]
            assert _rule(some)["status"] == "pass"

    async def test_csharp_importer_resolves_external_and_internal_using(self, client):
        mixed = {
            "src/a.py": "import b\n",
            "src/b.py": "",
            "cs/P.cs": "using App.Data;\nclass P {}\n",
        }
        vr = (await _arch(client, ["no_cycles", "max_fan_out:5"], mixed)).json()[
            "validation_results"
        ]
        # `App.Data` is declared nowhere and shares nothing with an internal
        # namespace: external, so the C# importer no longer blocks the graph rules.
        assert [r["status"] for r in vr["rules"]] == ["pass", "pass"]
        ghost = {**mixed, "cs/Q.cs": "namespace App.Data.Core;\n"}
        vr = (
            await _arch(client, ["no_cycles"], {**ghost, "cs/P.cs": "using App.Data.X;\n"})
        ).json()["validation_results"]
        assert _rule(vr)["status"] == "not_computed"
        assert "unresolved internal import App.Data.X" in _rule(vr)["reason"]

    async def test_empty_workspace_is_not_computed_for_every_rule(self, client):
        rules = ["no_cycles", "max_fan_out:1", "no_import:a->b", "layers:a>b"]
        vr = (await _arch(client, rules, {})).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert {r["status"] for r in vr["rules"]} == {"not_computed"}

    async def test_to_glob_matching_nothing_is_not_computed(self, client):
        typo = (await _arch(client, ["no_import:src/domain/*->src/infrastructure/*"], _REPO)).json()
        real = (await _arch(client, ["no_import:src/domain/*->src/infra/*"], _REPO)).json()
        assert _rule(typo["validation_results"])["status"] == "not_computed"
        assert "to_glob matches nothing" in _rule(typo["validation_results"])["reason"]
        assert _rule(real["validation_results"])["status"] == "pass"

    async def test_layer_glob_matching_nothing_is_not_computed(self, client):
        typo = "layers:src/api>src/services>src/domain"
        real = "layers:src/api>src/service>src/domain"
        bad = (await _arch(client, [typo], _REPO)).json()["validation_results"]
        ok = (await _arch(client, [real], _REPO)).json()["validation_results"]
        assert _rule(bad)["status"] == "not_computed"
        assert "src/services" in _rule(bad)["reason"]
        assert _rule(ok)["status"] == "pass"


class TestArchitectureResolution:
    async def test_go_module_packages_cycle(self, client):
        mod = "module example.com/app\n\ngo 1.22\n"
        cyclic = {
            "go.mod": mod,
            "a/a.go": 'package a\n\nimport "example.com/app/b"\n',
            "b/b.go": 'package b\n\nimport (\n\t"fmt"\n\t"example.com/app/a"\n)\n',
            "b/b_test.go": "package b\n",
        }
        acyclic = {**cyclic, "b/b.go": 'package b\n\nimport "fmt"\n'}
        bad = (await _arch(client, ["no_cycles"], cyclic)).json()["validation_results"]
        ok = (await _arch(client, ["no_cycles"], acyclic)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0]["cycle"] == ["a/a.go", "b/b.go"]
        assert _rule(ok)["status"] == "pass"
        no_mod = {k: v for k, v in cyclic.items() if k != "go.mod"}
        blind = (await _arch(client, ["no_cycles"], no_mod)).json()["validation_results"]
        assert _rule(blind)["status"] == "not_computed"
        assert "go (outside any go.mod)" in _rule(blind)["reason"]

    async def test_go_no_import_by_resolved_package(self, client):
        files = {
            "go.mod": "module example.com/app\n",
            "domain/user.go": 'package domain\nimport "example.com/app/infra"\n',
            "infra/db.go": "package infra\n",
        }
        clean = {**files, "domain/user.go": "package domain\n"}
        rule = "no_import:domain/*->infra/*"
        bad = (await _arch(client, [rule], files)).json()["validation_results"]
        ok = (await _arch(client, [rule], clean)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0]["target"] == "infra/db.go"
        assert _rule(ok)["status"] == "pass"

    async def test_rust_crate_super_and_groups(self, client):
        cyclic = {
            "src/lib.rs": "mod a;\nmod b;\n",
            "src/a.rs": "use crate::{b::Thing, c};\n",
            "src/b/mod.rs": "mod inner;\n",
            "src/b/inner.rs": "use super::super::a::run;\n",
            "src/c.rs": "",
        }
        # b/mod.rs does not import a: no cycle via inner -> a -> b(mod.rs)
        acyclic_graph = (await _arch(client, ["no_cycles"], cyclic)).json()["validation_results"]
        assert _rule(acyclic_graph)["status"] == "pass"
        cyclic["src/b/mod.rs"] = "use crate::a::run;\n"
        bad = (await _arch(client, ["no_cycles"], cyclic)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0]["cycle"] == ["src/a.rs", "src/b/mod.rs"]
        fan = (await _arch(client, ["max_fan_out:1:src/a.rs"], cyclic)).json()
        assert _rule(fan["validation_results"])["violations"][0]["target"] == [
            "src/b/mod.rs",
            "src/c.rs",
        ]

    async def test_python_multi_line_from_import_violates_and_honours(self, client):
        files = {
            "src/__init__.py": "",
            "src/infra/__init__.py": "",
            "src/other.py": "",
            "src/domain/__init__.py": "",
            "src/domain/user.py": (
                "import os\nfrom src import (\n    infra,  # the db\n    other,\n)\n"
            ),
        }
        rule = "no_import:src/domain->src/infra"
        bad = (await _arch(client, [rule], files)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0] == {
            "file": "src/domain/user.py",
            "line": 2,
            "imports": "src.infra",
            "target": "src/infra/__init__.py",
        }
        clean = {**files, "src/domain/user.py": "import os\nfrom src import (\n    other,\n)\n"}
        ok = (await _arch(client, [rule], clean)).json()["validation_results"]
        assert _rule(ok)["status"] == "pass"
        only_os = {**files, "src/domain/user.py": "import os\n"}
        ok_os = (await _arch(client, [rule], only_os)).json()["validation_results"]
        assert _rule(ok_os)["status"] == "pass"

    async def test_rust_multi_line_group_violates_and_honours(self, client):
        files = {
            "src/lib.rs": "mod domain;\nmod infra;\n",
            "src/infra/mod.rs": "pub mod db;\n",
            "src/infra/db.rs": "",
            "src/domain.rs": (
                "use std::{\n    fmt,\n    io,\n};\nuse crate::{\n    infra::db, // store\n};\n"
            ),
        }
        rule = "no_import:src/domain.rs->src/infra"
        bad = (await _arch(client, [rule], files)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        v = _rule(bad)["violations"][0]
        assert (v["line"], v["imports"], v["target"]) == (5, "crate::infra::db", "src/infra/db.rs")
        std_only = {**files, "src/domain.rs": "use std::{\n    fmt,\n    io,\n};\n"}
        ok = (await _arch(client, [rule], std_only)).json()["validation_results"]
        assert _rule(ok)["status"] == "pass"

    @pytest.mark.parametrize(
        ("path", "opening"),
        [("src/domain/user.py", "from src import ("), ("src/domain.rs", "use crate::{")],
        ids=["python", "rust"],
    )
    async def test_unclosed_multi_line_import_is_not_computed(self, client, path, opening):
        body = opening + "\n" + "    x,\n" * 45
        # `x` exists: an import of a missing internal module is not computed by itself.
        files = {"src/infra.py": "", "src/infra.rs": "", "src/x.py": "", "src/x.rs": "", path: body}
        closed = {**files, path: body + (")\n" if path.endswith(".py") else "};\n")}
        rule = f"no_import:{path}->src/infra*"
        nc = (await _arch(client, [rule], files)).json()["validation_results"]
        assert _rule(nc)["status"] == "not_computed"
        assert "not closed within 40 lines" in _rule(nc)["reason"]
        # Closed, but past the 40-line window: still unread, never a pass.
        late = (await _arch(client, [rule], closed)).json()["validation_results"]
        assert _rule(late)["status"] == "not_computed"
        short = {
            **files,
            path: opening + "\n    x,\n" + (")\n" if path.endswith(".py") else "};\n"),
        }
        read = (await _arch(client, [rule], short)).json()["validation_results"]
        assert _rule(read)["status"] == "pass"

    async def test_python_from_import_submodule_and_relative(self, client):
        files = {
            "app/__init__.py": "",
            "app/domain/__init__.py": "",
            "app/domain/user.py": "from ..infra import db\n",
            "app/infra/__init__.py": "",
            "app/infra/db.py": "",
            "app/infra/cache.py": "",
        }
        rule = "no_import:app/domain/*->app/infra/db.py"
        bad = (await _arch(client, [rule], files)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0]["target"] == "app/infra/db.py"
        other = {**files, "app/domain/user.py": "from ..infra import cache\n"}
        ok = (await _arch(client, [rule], other)).json()["validation_results"]
        assert _rule(ok)["status"] == "pass"
        absolute = {**files, "app/domain/user.py": "from app.infra import db, cache\n"}
        abs_bad = (await _arch(client, [rule], absolute)).json()["validation_results"]
        assert _rule(abs_bad)["status"] == "fail"
        dot = {**files, "app/domain/user.py": "from . import helpers\n"}
        dot["app/domain/helpers.py"] = "from app.infra import db\n"
        cyc = (await _arch(client, ["no_cycles:app/domain/*"], dot)).json()["validation_results"]
        assert _rule(cyc)["status"] == "pass"
        dot["app/domain/helpers.py"] = "from app.domain import user\n"
        cyc2 = (await _arch(client, ["no_cycles:app/domain/*"], dot)).json()["validation_results"]
        assert _rule(cyc2)["status"] == "fail"

    async def test_ts_path_alias_and_unresolved_alias(self, client):
        tsconfig = (
            '{\n  // comment\n  "compilerOptions": {\n    "baseUrl": ".",\n'
            '    "paths": { "@/*": ["src/*"] }, /* trailing */\n  },\n}\n'
        )
        files = {
            "tsconfig.json": tsconfig,
            "src/domain/user.ts": "import { db } from '@/infra/db';\n",
            "src/infra/db.ts": "export const db = 1;\n",
        }
        rule = "no_import:src/domain/*->src/infra/*"
        bad = (await _arch(client, [rule], files)).json()["validation_results"]
        assert _rule(bad)["status"] == "fail"
        assert _rule(bad)["violations"][0]["target"] == "src/infra/db.ts"
        clean = {**files, "src/domain/user.ts": "import x from 'react';\n"}
        ok = (await _arch(client, [rule], clean)).json()["validation_results"]
        assert _rule(ok)["status"] == "pass"
        missing = {**files, "src/domain/user.ts": "import { db } from '@/infra/gone';\n"}
        nc = (await _arch(client, [rule], missing)).json()["validation_results"]
        assert _rule(nc)["status"] == "not_computed"
        assert "unresolved alias" in _rule(nc)["reason"]
        base_url = {**files, "src/domain/user.ts": "import { db } from 'src/infra/db';\n"}
        via_base = (await _arch(client, [rule], base_url)).json()["validation_results"]
        assert _rule(via_base)["status"] == "fail"

    async def test_ts_nearest_config_and_relative_extends(self, client):
        files = {
            "tsconfig.base.json": '{"compilerOptions": {"paths": {"~/*": ["pkg/web/src/*"]}}}',
            "pkg/web/tsconfig.json": '{"extends": "../../tsconfig.base.json"}',
            "tsconfig.json": '{"compilerOptions": {"paths": {"~/*": ["nowhere/*"]}}}',
            "pkg/web/src/a.ts": "import { b } from '~/b';\n",
            "pkg/web/src/b.ts": "import { a } from '~/a';\n",
        }
        vr = (await _arch(client, ["no_cycles"], files)).json()["validation_results"]
        assert _rule(vr)["status"] == "fail"
        files["pkg/web/tsconfig.json"] = '{"compilerOptions": {}}'
        nc = (await _arch(client, ["no_cycles"], files)).json()["validation_results"]
        assert _rule(nc)["status"] == "not_computed"

    async def test_unparseable_tsconfig_is_not_computed(self, client):
        files = {"tsconfig.json": "{ nope", "web/a.ts": "import './b';\n", "web/b.ts": ""}
        vr = (await _arch(client, ["no_cycles"], files)).json()["validation_results"]
        assert vr["verdict"] == "not_computed"
        assert "tsconfig.json" in vr["notes"][0]


def test_parse_jsonc_keeps_glob_strings():
    text = '{"paths": {"@/*": ["src/*"]}, // c\n "a": "http://x", /* b */ "z": [1,],}'
    assert parse_jsonc(text) == {"paths": {"@/*": ["src/*"]}, "a": "http://x", "z": [1]}


# ---------------------------------------------------------------------------
# validateArchitecture: an internal import that does not resolve is never a pass
# ---------------------------------------------------------------------------


async def _status(client, rule, files):
    return _rule((await _arch(client, [rule], files)).json()["validation_results"])


class TestArchitectureNoFalsePass:
    """The validator's four mini-repos (t1-t4) fail; their clean twins pass."""

    async def test_python_namespace_package_and_semicolon(self, client):
        ns = {
            "src/app/domain/model.py": "from app import infra\n",
            "src/app/infra/db.py": "x = 1\n",
            "src/app/api/m.py": "import app.domain.model\n",
        }
        rule = "no_import:src/app/domain->src/app/infra"
        bad = await _status(client, rule, ns)
        assert bad["status"] == "fail"
        assert bad["violations"][0]["target"] == "src/app/infra"
        layers = await _status(client, "layers:src/app/api>src/app/infra>src/app/domain", ns)
        assert layers["status"] == "fail"
        clean = {**ns, "src/app/domain/model.py": "import dataclasses\n"}
        assert (await _status(client, rule, clean))["status"] == "pass"
        semi = {**ns, "src/app/domain/model.py": "import os; import app.infra.db\n"}
        assert (await _status(client, rule, semi))["status"] == "fail"
        semi_ok = {**ns, "src/app/domain/model.py": "import os; import json\n"}
        assert (await _status(client, rule, semi_ok))["status"] == "pass"

    async def test_python_monorepo_package_root(self, client):
        mono = {
            "backend/app/__init__.py": "",
            "backend/app/domain/__init__.py": "",
            "backend/app/domain/model.py": "from app.infra.db import engine\n",
            "backend/app/infra/__init__.py": "",
            "backend/app/infra/db.py": "engine = 1\n",
        }
        rule = "no_import:backend/app/domain->backend/app/infra"
        bad = await _status(client, rule, mono)
        assert bad["status"] == "fail"
        assert bad["violations"][0]["target"] == "backend/app/infra/db.py"
        clean = {**mono, "backend/app/domain/model.py": "import os\n"}
        assert (await _status(client, rule, clean))["status"] == "pass"

    async def test_ts_css_import_resolves_as_a_leaf(self, client):
        nx = {
            "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}',
            "src/lib/db.ts": "export const db = 1\n",
            "src/styles/Button.module.css": ".a{}\n",
            "src/components/Button.tsx": (
                "import { db } from '@/lib/db'\nimport s from '@/styles/Button.module.css'\n"
            ),
        }
        assert (await _status(client, "no_import:src/components->src/lib", nx))["status"] == "fail"
        css = await _status(client, "no_import:src/components->src/styles", nx)
        assert css["status"] == "fail"
        assert css["violations"][0]["target"] == "src/styles/Button.module.css"
        clean = {
            **nx,
            "src/components/Button.tsx": "import s from './x.css'\n",
            "src/components/x.css": "",
        }
        assert (await _status(client, "no_import:src/components->src/lib", clean))[
            "status"
        ] == "pass"
        # The css leaf is not a graph node: one code edge, no cycle.
        assert (await _status(client, "no_cycles", nx))["status"] == "pass"
        missing = {**nx, "src/components/Button.tsx": "import s from './gone.css'\n"}
        nc = await _status(client, "no_import:src/components->src/lib", missing)
        assert nc["status"] == "not_computed"
        assert "unresolved internal import ./gone.css" in nc["reason"]

    async def test_go_import_with_trailing_comment(self, client):
        go = {
            "go.mod": "module github.com/acme/svc\n",
            "internal/handler/h.go": "package handler\n",
            "internal/store/s.go": (
                'package store\n\nimport (\n\tdb "github.com/acme/svc/internal/handler" // c\n)\n'
            ),
        }
        rule = "no_import:internal/store->internal/handler"
        assert (await _status(client, rule, go))["status"] == "fail"
        single = {
            **go,
            "internal/store/s.go": 'package store\nimport "github.com/acme/svc/internal/handler" // c\n',
        }
        assert (await _status(client, rule, single))["status"] == "fail"
        clean = {**go, "internal/store/s.go": 'package store\n\nimport "fmt" // c\n'}
        assert (await _status(client, rule, clean))["status"] == "pass"

    async def test_rust_workspace_crate_paths(self, client):
        ws = {
            "Cargo.toml": "[workspace]\nmembers = ['crates/*']\n",
            "crates/domain/Cargo.toml": '[package]\nname = "domain"\n',
            "crates/domain/src/lib.rs": "pub mod model;\nuse infra_core::db::Pool;\n",
            "crates/domain/src/model.rs": "",
            "crates/infra/Cargo.toml": '[package]\nname = "infra-core"\n',
            "crates/infra/src/lib.rs": "pub mod db;\n",
            "crates/infra/src/db.rs": "",
        }
        rule = "no_import:crates/domain->crates/infra"
        bad = await _status(client, rule, ws)
        assert bad["status"] == "fail"
        assert bad["violations"][0]["target"] == "crates/infra/src/db.rs"
        clean = {**ws, "crates/domain/src/lib.rs": "pub mod model;\nuse std::fmt;\n"}
        assert (await _status(client, rule, clean))["status"] == "pass"
        # No [package] name at all: the crate dir name stands in for it.
        unnamed = {
            **ws,
            "crates/infra/Cargo.toml": "",
            "crates/domain/src/lib.rs": "use infra::db::Pool;\n",
        }
        assert (await _status(client, rule, unnamed))["status"] == "fail"

    async def test_java_wildcard_and_static_imports(self, client):
        base = "src/main/java/com/acme"
        jv = {
            f"{base}/domain/Order.java": "package com.acme.domain;\nimport com.acme.infra.*;\n",
            f"{base}/domain/Item.java": "package com.acme.domain;\nimport java.util.List;\n",
            f"{base}/infra/Db.java": "package com.acme.infra;\n",
        }
        rule = f"no_import:{base}/domain->{base}/infra"
        wildcard = await _status(client, rule, jv)
        assert wildcard["status"] == "fail"
        assert wildcard["violations"][0]["target"] == f"{base}/infra/Db.java"
        static = {
            **jv,
            f"{base}/domain/Order.java": (
                "package com.acme.domain;\nimport static com.acme.infra.Db.connect;\n"
            ),
        }
        assert (await _status(client, rule, static))["status"] == "fail"
        clean = {
            **jv,
            f"{base}/domain/Order.java": "package com.acme.domain;\nimport java.util.Map;\n",
        }
        assert (await _status(client, rule, clean))["status"] == "pass"

    async def test_csharp_no_import_and_layers_are_computed(self, client):
        cs = {
            "src/Domain/Order.cs": "using Acme.Infra;\n",
            "src/Infra/Db.cs": "namespace Acme.Infra;\n",
        }
        for rule in ("no_import:src/Domain->src/Infra", "layers:src/Infra>src/Domain"):
            result = await _status(client, rule, cs)
            assert result["status"] == "fail"
            assert result["violations"][0]["target"] == "src/Infra/Db.cs"
        py = {"src/Domain/order.py": "import os\n", "src/Infra/db.py": ""}
        assert (await _status(client, "no_import:src/Domain->src/Infra", py))["status"] == "pass"


class TestArchitectureSameLineImports:
    """An import that is not the first statement on its line is still an edge."""

    @pytest.mark.parametrize(
        "source",
        [
            "x = 1; from app.infra.db import Db\n",
            "import os; import app.infra.db\n",
            "import os ; from app.infra import db\n",
        ],
    )
    async def test_python(self, client, source):
        files = {"src/app/domain/model.py": source, "src/app/infra/db.py": "Db = 1\n"}
        rule = "no_import:src/app/domain->src/app/infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/app/domain/model.py": 'x = "a; import app.infra.db"; import os\n'}
        assert (await _status(client, rule, ok))["status"] == "pass"

    @pytest.mark.parametrize(
        "source",
        [
            "import a from 'x'; import b from './infra/db'\n",
            "const a = 1; import './infra/db'\n",
        ],
    )
    async def test_js(self, client, source):
        files = {"src/domain/m.ts": source, "src/domain/infra/db.ts": "export const b = 1\n"}
        rule = "no_import:src/domain/m.ts->src/domain/infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/domain/m.ts": "const s = \"; import './infra/db'\"\n"}
        assert (await _status(client, rule, ok))["status"] == "pass"

    @pytest.mark.parametrize(
        "source",
        [
            "package com.acme.domain; import com.acme.infra.*;\n",
            "package com.acme.domain; import java.util.List; import com.acme.infra.Db;\n",
        ],
    )
    async def test_java(self, client, source):
        files = {
            "src/Domain/Order.java": source,
            "src/Infra/Db.java": "package com.acme.infra;\nclass Db {}\n",
        }
        rule = "no_import:src/Domain->src/Infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/Domain/Order.java": "package com.acme.domain; import java.util.*;\n"}
        assert (await _status(client, rule, ok))["status"] == "pass"

    @pytest.mark.parametrize(
        "source",
        ["using System; using Acme.Infra;\n", "using System;using static Acme.Infra.Db;\n"],
    )
    async def test_csharp(self, client, source):
        files = {
            "src/Domain/Order.cs": source + "namespace Acme.Domain;\n",
            "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
        }
        rule = "no_import:src/Domain->src/Infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/Domain/Order.cs": "using System; using System.Linq;\n"}
        assert (await _status(client, rule, ok))["status"] == "pass"

    async def test_go(self, client):
        files = {
            "go.mod": "module github.com/acme/svc\n",
            "internal/handler/h.go": "package handler\n",
            "internal/store/s.go": 'package store; import "github.com/acme/svc/internal/handler"\n',
        }
        rule = "no_import:internal/store->internal/handler"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "internal/store/s.go": 'package store; import "fmt"\n'}
        assert (await _status(client, rule, ok))["status"] == "pass"

    async def test_rust(self, client):
        files = {
            "src/lib.rs": "pub mod domain;\npub mod infra;\n",
            "src/infra.rs": "pub struct Db;\n",
            "src/domain.rs": "use std::fmt; use crate::infra::Db;\n",
        }
        rule = "no_import:src/domain.rs->src/infra.rs"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/domain.rs": "use std::fmt; use std::io;\n"}
        assert (await _status(client, rule, ok))["status"] == "pass"


class TestArchitectureSplitNeverLosesAnEdge:
    """Validator 2026-10-12: the lexical statement split read ``//`` inside a JS
    regex literal as a comment and dropped the import after it (a false pass the
    whole-line scan did not have). Edges are now the UNION of the whole-line scan
    and the per-statement scan, so splitting can only add one."""

    @pytest.mark.parametrize(
        "source",
        [
            'const re=/^https?:\\/\\//; import {x} from "../infra/db";\n',
            "const r=/\\/\\*/; export * from '../infra/db'; /* */\n",
            "const t=`//`; export * from '../infra/db';\n",
            'const u="http://x"; import {x} from "../infra/db";\n',
            "var s='it\\'s // x'; import y from '../infra/db'\n",
            "x = a // b; import {x} from '../infra/db';\n",
        ],
    )
    async def test_js_comment_and_string_lookalikes(self, client, source):
        files = {"src/domain/a.js": source, "src/infra/db.js": "export const x = 1\n"}
        rule = "no_import:src/domain->src/infra"
        assert (await _status(client, rule, files))["status"] == "fail"

    @pytest.mark.parametrize(
        "source",
        [
            'x = "#"; import app.infra.db\n',
            "x = '#'; from app.infra.db import Db\n",
            'x = "a#b"; import os; import app.infra.db  # trailing\n',
        ],
    )
    async def test_python_hash_inside_a_string(self, client, source):
        files = {"src/app/domain/model.py": source, "src/app/infra/db.py": "Db = 1\n"}
        rule = "no_import:src/app/domain->src/app/infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        ok = {**files, "src/app/domain/model.py": "import os  # ; import app.infra.db\n"}
        assert (await _status(client, rule, ok))["status"] == "pass"

    @pytest.mark.parametrize(
        "source",
        [
            "use std::fmt; mod m { use crate::infra::Db; }\n",
            "use std::fmt; fn f() { use crate::infra::Db; }\n",
            "use std::fmt;  fn f<'a>(x: &'a str) {} use crate::infra::Db; /* t */\n",
            "use std::fmt;  fn f() {} use crate::infra::{Db,\n",
        ],
    )
    async def test_rust_use_the_split_did_not_read_stays_unread(self, client, source):
        """Validator 2026-10-12: a `use` after `{` / `}` is not a statement start
        for the split, so it was never read; dropping the whole-line unread marker
        then made the rule PASS where the whole-line scan said not computed."""
        files = {
            "src/lib.rs": "pub mod domain;\npub mod infra;\n",
            "src/infra.rs": "pub struct Db;\n",
            "src/domain.rs": source,
        }
        rule = "no_import:src/domain.rs->src/infra.rs"
        assert (await _status(client, rule, files))["status"] == "not_computed"

    async def test_rust_every_use_read_drops_the_whole_line_marker(self, client):
        files = {
            "src/lib.rs": "pub mod domain;\npub mod infra;\n",
            "src/infra.rs": "pub struct Db;\n",
            "src/domain.rs": "use std::fmt; use std::io;\n",
        }
        rule = "no_import:src/domain.rs->src/infra.rs"
        assert (await _status(client, rule, files))["status"] == "pass"

    def test_rust_raw_string_is_one_literal(self):
        files = frozenset({"src/lib.rs", "src/infra.rs", "src/domain.rs"})
        for source in (
            'let r = r#"x"; use crate::infra::Db; "#; use std::fmt;',
            'let r = br##"x"#; use crate::infra::Db; "##; use std::fmt;',
            'let r = r"x; use crate::infra::Db; "; use std::fmt;',
        ):
            edges = parse_edges(
                "rust", [{"file": "src/domain.rs", "line": 1, "source": source}], files
            )
            assert all(e.target != "src/infra.rs" for e in edges), (source, edges)
        pieces = split_statements("rust", 'let r = r#"x"; use crate::infra::db; "#; use a;')
        assert pieces == ['let r = r#"x"; use crate::infra::db; "#;', " use a;"]

    def test_rust_use_token_count(self):
        assert _rust_use_tokens("use std::fmt; use std::io;") == 2
        assert _rust_use_tokens("use a; mod m { use b; }") == 2
        # Any literal, comment, attribute or macro on the line: not counted (None keeps
        # the whole-line marker). A C-string cr#"..."# once slipped past the lexer.
        assert _rust_use_tokens('let s = "use \\" use"; use a; // use b') is None
        assert _rust_use_tokens("r#use x; /* use */ use a;") is None
        assert (
            _rust_use_tokens('use a::b; static S: &CStr = cr#"say "hi"#; use crate::infra::db;')
            is None
        )
        assert _rust_use_tokens("#[cfg(test)] use a; use b;") is None
        assert _rust_use_tokens("fn f<'a>(x: &'a str) {} use a;") is None
        assert _rust_use_tokens("/* /* */ */ use a;") is None
        assert _rust_use_tokens('let s = "use a;') is None
        assert _rust_use_tokens('let s = r#"use a;"') is None
        assert _rust_use_tokens("macro_rules! m { () => { use a; } }") is None


class TestUnresolvedInternalImport:
    """A first segment that names an internal package but resolves to no file."""

    @pytest.mark.parametrize(
        ("files", "importer", "missing", "present"),
        [
            (
                # `app` is a directory here, but no package root makes it importable.
                {"svc/app/infra/db.py": "", "svc/app/domain/m.py": ""},
                "svc/app/domain/m.py",
                "from app.infra.db import x\n",
                "import os\n",
            ),
            (
                {"web/infra/db.ts": "", "web/domain/m.ts": ""},
                "web/domain/m.ts",
                "import { x } from '../infra/gone'\n",
                "import { x } from '../infra/db'\n",
            ),
            (
                # No lib.rs/main.rs: `crate::gone` has no crate-root file to fall back to.
                {"src/infra/mod.rs": "", "src/domain/mod.rs": ""},
                "src/domain/mod.rs",
                "use crate::gone::X;\n",
                "use crate::infra::X;\n",
            ),
            (
                {
                    "j/com/acme/infra/Db.java": "package com.acme.infra;\n",
                    "j/com/acme/domain/A.java": "package com.acme.domain;\n",
                },
                "j/com/acme/domain/A.java",
                "package com.acme.domain;\nimport com.acme.infra.Gone;\n",
                "package com.acme.domain;\nimport com.acme.infra.Db;\n",
            ),
        ],
        ids=["python", "ts-relative", "rust", "java"],
    )
    async def test_missing_is_not_computed_present_is_a_verdict(
        self, client, files, importer, missing, present
    ):
        domain, infra = _dir_of_test(importer), _dir_of_test(importer).replace("domain", "infra")
        rule = f"no_import:{domain}->{infra}"
        nc = await _status(client, rule, {**files, importer: missing})
        assert nc["status"] == "not_computed"
        assert "unresolved internal import" in nc["reason"]
        assert importer in nc["reason"]
        verdict = await _status(client, rule, {**files, importer: present})
        assert verdict["status"] in ("pass", "fail")


def _dir_of_test(path: str) -> str:
    return path.rsplit("/", 1)[0]


class TestUnsearchedFileTypes:
    _FILES = {"web/util.ts": "export const other = 1;\n"}

    async def test_vue_mention_is_null_ghost_is_false(self, client):
        vue = {**self._FILES, "web/Comp.vue": "<script>export function formatDate(d) {}</script>\n"}
        with _workspace(vue), _symbols({}):
            mentioned = await _fn(client, "formatDate")
        with _workspace(self._FILES), _symbols({}):
            ghost = await _fn(client, "formatDate")
        m, g = mentioned.json(), ghost.json()
        assert m["valid"] is None
        assert ".vue" in m["note"]
        assert "web/Comp.vue" in m["note"]
        assert g["valid"] is False


# ---------------------------------------------------------------------------
# Unlocated dotted name: the OWNER is looked up, so an inherited member is null
# ---------------------------------------------------------------------------


class TestUnlocatedInheritedMember:
    _JAVA = {
        "src/main/java/a/UserRepo.java": (
            "package a;\npublic interface UserRepo extends JpaRepository<User, Long> {\n"
            "  User findByEmail(String e);\n}\n"
        )
    }
    _GO = {
        "go.mod": "module x\n",
        "srv/s.go": "package srv\n\ntype Server struct {\n\t*http.Server\n}\n",
    }
    _TS = {"src/repo.ts": "export class UserRepo extends Repository<User> {\n  byEmail(e) {}\n}\n"}

    @pytest.mark.parametrize(
        ("files", "path", "owner", "kind", "member", "language"),
        [
            (_JAVA, "src/main/java/a/UserRepo.java", "UserRepo", "interface", "findAll", "java"),
            (_GO, "srv/s.go", "Server", "struct", "ListenAndServe", "go"),
            (_TS, "src/repo.ts", "UserRepo", "class", "save", "typescript"),
        ],
    )
    async def test_owner_defined_is_null_ghost_owner_is_false(
        self, client, files, path, owner, kind, member, language
    ):
        bodies = {path: _body(_sym(owner, kind), language=language)}
        with _workspace(files), _symbols(bodies):
            inherited = await _fn(client, f"{owner}.{member}")
            ghost = await _fn(client, f"Ghost.{member}")
        i, g = inherited.json(), ghost.json()
        assert i["valid"] is None
        assert "inherited" in i["note"] and path in i["note"]
        assert g["valid"] is False

    async def test_owner_with_the_member_is_true(self, client):
        bodies = {
            "src/repo.ts": _body(
                _sym("UserRepo", "class"), _sym("byEmail", "method", 2, "UserRepo")
            )
        }
        with _workspace(self._TS), _symbols(bodies):
            resp = await _fn(client, "UserRepo.byEmail")
        assert resp.json()["valid"] is True


class TestPythonOwnerFallback:
    _FILES = {
        "app/repo.py": (
            "from sqlalchemy.orm import Session\n\n"
            "class UserSession(Session):\n    def by_email(self, e):\n        pass\n\n"
            "class Plain:\n    size = 3\n    def run(self):\n        pass\n"
        )
    }

    async def test_unlocated_inherited_is_null_plain_and_ghost_are_false(self, client):
        with _workspace(self._FILES), _symbols({}):
            inherited = await _fn(client, "UserSession.query")
            plain = await _fn(client, "Plain.query")
            ghost = await _fn(client, "Ghost.query")
            attribute = await _fn(client, "Plain.size")
        assert inherited.json()["valid"] is None
        assert "inherited" in inherited.json()["note"]
        assert plain.json()["valid"] is False
        assert ghost.json()["valid"] is False
        assert attribute.json()["valid"] is True

    async def test_located_inherited_is_null_plain_is_false(self, client):
        with _workspace(self._FILES), _symbols({}):
            inherited = await _fn(client, "UserSession.query", file_path="app/repo.py")
            plain = await _fn(client, "Plain.query", file_path="app/repo.py")
            missing = await _fn(client, "Plain.query", file_path="app/gone.py")
        assert inherited.json()["valid"] is None
        assert plain.json()["valid"] is False
        assert missing.json()["valid"] is False

    async def test_decorated_or_nested_owner_is_null(self, client):
        files = {
            "app/m.py": (
                "@dataclass\nclass Point:\n    x: int\n\n"
                "class Outer:\n    class Inner:\n        go = 1\n"
            )
        }
        with _workspace(files), _symbols({}):
            decorated = await _fn(client, "Point.replace")
            nested = await _fn(client, "Inner.go", file_path="app/m.py")
        assert decorated.json()["valid"] is None
        assert nested.json()["valid"] is None


# ---------------------------------------------------------------------------
# validateArchitecture: workspaces, component files, dynamic imports, verdict
# ---------------------------------------------------------------------------

_PNPM = {
    "package.json": '{"name": "acme", "private": true}\n',
    "packages/core/package.json": '{"name": "@acme/core"}\n',
    "packages/core/src/index.ts": "export * from './money'\n",
    "packages/core/src/money.ts": "export const add = (a, b) => a + b\n",
    "packages/ui/package.json": (
        '{"name": "@acme/ui", "main": "dist/index.js", '
        '"exports": {".": {"import": "./dist/index.mjs"}, "./price": "./src/Price.tsx"}}\n'
    ),
    "packages/ui/src/index.ts": "export { Price } from './Price'\n",
    "packages/ui/src/Price.tsx": "import { add } from '@acme/core'\nexport const Price = 1\n",
}


class TestArchitectureWorkspacePackages:
    async def test_package_name_resolves_without_tsconfig_paths(self, client):
        rule = "no_import:packages/core->packages/ui"
        clean = await _status(client, rule, _PNPM)
        bad_root = {**_PNPM, "packages/core/src/money.ts": "import { Price } from '@acme/ui'\n"}
        bad_sub = {
            **_PNPM,
            "packages/core/src/money.ts": "import { Price } from '@acme/ui/price'\n",
        }
        assert clean["status"] == "pass"
        root = await _status(client, rule, bad_root)
        sub = await _status(client, rule, bad_sub)
        assert root["status"] == "fail"
        assert root["violations"][0]["target"] == "packages/ui/src/index.ts"
        assert sub["status"] == "fail"
        assert sub["violations"][0]["target"] == "packages/ui/src/Price.tsx"

    async def test_unresolvable_workspace_package_is_not_computed(self, client):
        files = {
            **_PNPM,
            "packages/core/src/money.ts": "import { x } from '@acme/ui/missing'\n",
        }
        nc = await _status(client, "no_import:packages/core->packages/ui", files)
        assert nc["status"] == "not_computed"
        assert "workspace package" in nc["reason"]

    async def test_root_name_is_skipped_only_without_root_sources(self, client):
        importer = {"packages/core/src/money.ts": "import x from 'acme'\n"}
        skipped = await _status(
            client, "no_import:packages/core->packages/ui", {**_PNPM, **importer}
        )
        own = {**_PNPM, **importer, "index.ts": "export const y = 1\n"}
        kept = await _status(client, "no_import:packages/core->index.ts", own)
        assert skipped["status"] == "pass"  # 'acme' names the monorepo: external
        assert kept["status"] == "fail"
        assert kept["violations"][0]["target"] == "index.ts"


class TestArchitectureComponentFiles:
    async def test_vue_and_mts_importers_are_scanned(self, client):
        base = {
            "src/domain/a.ts": "export const x = 1\n",
            "src/infra/db.ts": "export const y = 1\n",
        }
        vue = {
            **base,
            "src/infra/C.vue": "<script setup lang='ts'>\nimport { x } from '../domain/a'\n</script>\n",
        }
        clean_vue = {
            **base,
            "src/infra/C.vue": "<script setup>\nimport { y } from './db'\n</script>\n",
        }
        mts = {**base, "src/domain/b.mts": "import { y } from '../infra/db'\n"}
        assert (await _status(client, "no_import:src/infra->src/domain", vue))["status"] == "fail"
        assert (await _status(client, "no_import:src/infra->src/domain", clean_vue))[
            "status"
        ] == "pass"
        assert (await _status(client, "no_import:src/domain->src/infra", mts))["status"] == "fail"


class TestArchitectureDynamicImports:
    _BASE = {"src/infra/db.ts": "export const db = 1\n", "src/domain/b.ts": "export const b = 1\n"}

    @pytest.mark.parametrize(
        "line",
        [
            "export async function f() { const m = await import('../infra/db') }\n",
            "const C = dynamic(() => import('@/infra/db'))\n",
            "const C = lazy(() => import(/* webpackChunkName: 'db' */ '../infra/db'))\n",
            "const db = require('../infra/db').db\n",
        ],
    )
    async def test_literal_dynamic_import_is_an_edge(self, client, line):
        files = {
            **self._BASE,
            "tsconfig.json": '{"compilerOptions": {"paths": {"@/*": ["./src/*"]}}}\n',
            "src/domain/a.ts": line,
        }
        clean = {**files, "src/domain/a.ts": "export const a = 1\n"}
        assert (await _status(client, "no_import:src/domain->src/infra", files))["status"] == "fail"
        assert (await _status(client, "no_import:src/domain->src/infra", clean))["status"] == "pass"

    @pytest.mark.parametrize(
        ("path", "line"),
        [
            ("src/domain/a.ts", "const m = await import(name)\n"),
            ("src/domain/a.ts", "const m = await import('../infra/' + name)\n"),
            ("src/domain/a.ts", "const m = require(path.join(dir, 'x'))\n"),
            ("src/domain/a.py", "m = importlib.import_module(name)\n"),
            ("src/domain/a.py", "m = __import__(f'app.{name}')\n"),
        ],
    )
    async def test_non_literal_dynamic_import_is_not_computed(self, client, path, line):
        files = {**self._BASE, "src/infra/x.py": "y = 1\n", path: line}
        nc = await _status(client, "no_import:src/domain->src/infra", files)
        assert nc["status"] == "not_computed"
        assert f"non-literal dynamic import in {path}" in nc["reason"]
        outside = await _status(client, "no_import:src/infra->src/domain", files)
        assert outside["status"] == "pass"

    async def test_python_literal_import_module_is_an_edge(self, client):
        files = {
            "src/app/__init__.py": "",
            "src/app/domain/__init__.py": "",
            "src/app/domain/m.py": "import importlib\nrepo = importlib.import_module('app.infra.repo')\n",
            "src/app/infra/__init__.py": "",
            "src/app/infra/repo.py": "x = 1\n",
        }
        clean = {**files, "src/app/domain/m.py": "import importlib\n"}
        dunder = {**files, "src/app/domain/m.py": "repo = __import__('app.infra.repo')\n"}
        rule = "no_import:src/app/domain->src/app/infra"
        assert (await _status(client, rule, files))["status"] == "fail"
        assert (await _status(client, rule, dunder))["status"] == "fail"
        assert (await _status(client, rule, clean))["status"] == "pass"


class TestArchitectureVerdict:
    async def test_valid_needs_every_rule_to_pass(self, client):
        files = {"src/a.py": "import b\n", "src/b.py": "import json\n"}
        rules_ok = ["no_cycles", "max_fan_out:3"]
        all_pass = (await _arch(client, rules_ok, files)).json()["validation_results"]
        some_nc = (await _arch(client, [*rules_ok, "no_import:ghost->src"], files)).json()[
            "validation_results"
        ]
        one_fail = (
            await _arch(client, [*rules_ok, "no_import:ghost->src", "max_fan_out:0"], files)
        ).json()["validation_results"]
        assert all_pass["verdict"] == "valid"
        assert some_nc["verdict"] == "not_computed"
        assert one_fail["verdict"] == "invalid"


async def test_rust_c_string_line_never_passes(client):
    """Validator 2026-10-12: base not_computed, c91e841 pass, truth fail."""
    rule = "no_import:rs/src/domain/**->rs/src/infra/**"
    line = (
        'use a::b; static S: &CStr = cr#"say "hi"#; use crate::infra::db; '
        "const Q: char = '\"'; fn f() {}\n"
    )
    files = {
        "rs/Cargo.toml": '[package]\nname = "rs"\n',
        "rs/src/lib.rs": "mod domain;\nmod infra;\n",
        "rs/src/domain/mod.rs": "mod order;\n",
        "rs/src/domain/order.rs": line,
        "rs/src/infra/mod.rs": "pub mod db;\n",
        "rs/src/infra/db.rs": "pub struct Db;\n",
    }
    assert (await _status(client, rule, files))["status"] in ("fail", "not_computed")
