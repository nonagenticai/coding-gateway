"""Inherited members, re-exports, C++ concepts, sharded architecture scans and C#.

Same doubles as ``test_validation_multilang``: only the workspace exec call and
the coding-analysis symbols call are faked; resolution and verdicts run for
real. Every case is a true/false(/null) set through one code path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from tests.unit.test_validation_multilang import (
    _arch,
    _cls,
    _exec,
    _fn,
    _rule,
    _status,
    _sym,
    _symbols,
    _workspace,
)


def _nbody(*symbols, imports=(), reexports=(), impls=(), language="typescript", error=None):
    """A symbols body from the coding-analysis that reports bases/impls/reexports."""
    return {
        "language": language,
        "parsed": True,
        "parse_error_line": error,
        "symbols": list(symbols),
        "imports": list(imports),
        "reexports": list(reexports),
        "impls": list(impls),
    }


def _base(name, kind, bases, line=1, parent=None):
    return {**_sym(name, kind, line, parent), "bases": list(bases)}


# ---------------------------------------------------------------------------
# Inherited members (non-Python, via symbols)
# ---------------------------------------------------------------------------


class TestInheritedMembers:
    _FILES = {
        "web/store.ts": (
            "import { Base } from './base';\nimport { Component } from 'react';\n"
            "export class Store extends Base {}\nexport class View extends Component {}\n"
        ),
        "web/base.ts": "export class Base {\n  size() { return 1; }\n}\n",
    }
    _BODIES = {
        "web/store.ts": _nbody(
            _base("Store", "class", ["Base"], 3),
            _base("View", "class", ["Component"], 4),
            imports=[
                {"module": "./base", "names": ["Base"], "line": 1},
                {"module": "react", "names": ["Component"], "line": 2},
            ],
        ),
        "web/base.ts": _nbody(_sym("Base", "class"), _sym("size", "method", 2, "Base")),
    }

    async def test_ts_member_on_base_true_absent_false_external_null(self, client):
        with _workspace(self._FILES), _symbols(self._BODIES):
            found = await _fn(client, "Store.size", file_path="web/store.ts")
            absent = await _fn(client, "Store.clear", file_path="web/store.ts")
            external = await _fn(client, "View.render", file_path="web/store.ts")
        f, a, e = found.json(), absent.json(), external.json()
        assert f["valid"] is True
        assert f["found_in"] == ["web/base.ts"]
        assert "inherited from Base (web/base.ts:2)" in f["note"]
        assert a["valid"] is False
        assert "every base was resolved" in a["note"]
        assert e["valid"] is None
        assert "Component" in e["note"] and "react" in e["note"]

    async def test_unlocated_inherited_member(self, client):
        with _workspace(self._FILES), _symbols(self._BODIES):
            found = await _fn(client, "Store.size")
            absent = await _fn(client, "Store.clear")
        assert found.json()["valid"] is True
        assert "web/base.ts" in found.json()["found_in"]
        assert absent.json()["valid"] is False

    async def test_java_base_found_by_name_in_another_file(self, client):
        files = {
            "src/a/Child.java": "package a;\nclass Child extends Parent {}\n",
            "src/a/Parent.java": "package a;\nclass Parent { void run() {} }\n",
            "src/a/Repo.java": "package a;\ninterface Repo extends JpaRepository {}\n",
        }
        bodies = {
            "src/a/Child.java": _nbody(_base("Child", "class", ["Parent"], 2), language="java"),
            "src/a/Parent.java": _nbody(
                _sym("Parent", "class", 2), _sym("run", "method", 2, "Parent"), language="java"
            ),
            "src/a/Repo.java": _nbody(
                _base("Repo", "interface", ["JpaRepository"], 2), language="java"
            ),
        }
        with _workspace(files), _symbols(bodies):
            found = await _fn(client, "Child.run", file_path="src/a/Child.java")
            absent = await _fn(client, "Child.stop", file_path="src/a/Child.java")
            external = await _fn(client, "Repo.findAll", file_path="src/a/Repo.java")
        assert found.json()["valid"] is True
        assert found.json()["found_in"] == ["src/a/Parent.java"]
        assert absent.json()["valid"] is False
        assert external.json()["valid"] is None
        assert "JpaRepository" in external.json()["note"]

    async def test_qualified_and_parse_error_are_null(self, client):
        files = {"src/a/C.java": "package a;\nclass C extends x.y.Base {}\nclass D {}\n"}
        bodies = {
            "src/a/C.java": _nbody(
                _base("C", "class", ["x.y.Base"], 2), _sym("D", "class", 3), language="java"
            )
        }
        broken = {"src/a/C.java": _nbody(_sym("D", "class", 3), language="java", error=9)}
        with _workspace(files), _symbols(bodies):
            qualified = await _fn(client, "C.run", file_path="src/a/C.java")
            no_bases = await _fn(client, "D.run", file_path="src/a/C.java")
        with _workspace(files), _symbols(broken):
            parse_error = await _fn(client, "D.run", file_path="src/a/C.java")
        assert qualified.json()["valid"] is None
        assert "qualified" in qualified.json()["note"]
        assert no_bases.json()["valid"] is False
        assert parse_error.json()["valid"] is None
        assert "parse error" in parse_error.json()["note"]

    async def test_rust_trait_default_method(self, client):
        files = {
            "src/lib.rs": "mod greet;\npub struct Foo;\nimpl greet::Greet for Foo {}\n",
            "src/greet.rs": "pub trait Greet {\n    fn hello(&self) {}\n}\n",
        }
        bodies = {
            # coding-analysis reports the trait as written: ``greet::Greet``.
            "src/lib.rs": _nbody(
                _sym("Foo", "struct", 2),
                impls=[{"type": "Foo", "trait": "greet::Greet", "line": 3}],
                language="rust",
            ),
            "src/greet.rs": _nbody(
                _sym("Greet", "trait"), _sym("hello", "method", 2, "Greet"), language="rust"
            ),
        }
        with _workspace(files), _symbols(bodies):
            found = await _fn(client, "Foo.hello", file_path="src/lib.rs")
            absent = await _fn(client, "Foo.bye", file_path="src/lib.rs")
        assert found.json()["valid"] is True
        assert found.json()["found_in"] == ["src/greet.rs"]
        # Rust impl blocks can live anywhere in the crate: never a false.
        assert absent.json()["valid"] is None
        assert "impl" in absent.json()["note"]


# ---------------------------------------------------------------------------
# Re-exports
# ---------------------------------------------------------------------------


class TestReExportChains:
    _FILES = {
        "web/index.ts": (
            "export { Store } from './store';\nexport { Ghost } from './store';\n"
            "export { Button } from '@ui/kit';\n"
        ),
        "web/store.ts": "export class Store {}\n",
    }
    _BODIES = {
        "web/index.ts": _nbody(
            reexports=[
                {"name": "Store", "from_module": "./store", "alias": None, "line": 1},
                {"name": "Ghost", "from_module": "./store", "alias": None, "line": 2},
                {"name": "Button", "from_module": "@ui/kit", "alias": None, "line": 3},
            ]
        ),
        "web/store.ts": _nbody(_sym("Store", "class")),
    }

    async def test_named_barrel_true_false_and_package_null(self, client):
        with _workspace(self._FILES), _symbols(self._BODIES):
            found = await _cls(client, "Store", file_path="web/index.ts")
            absent = await _cls(client, "Ghost", file_path="web/index.ts")
            package = await _cls(client, "Button", file_path="web/index.ts")
            unlisted = await _cls(client, "Other", file_path="web/index.ts")
        f = found.json()
        assert f["valid"] is True
        assert f["found_in"] == ["web/store.ts"]
        assert "re-exported via web/index.ts" in f["note"]
        assert absent.json()["valid"] is False
        assert "followed to the end" in absent.json()["note"]
        assert package.json()["valid"] is None
        assert "@ui/kit" in package.json()["note"]
        assert unlisted.json()["valid"] is False

    async def test_export_star_chain(self, client):
        files = {
            "web/index.ts": "export * from './a';\n",
            "web/a.ts": "export * from './inner/b';\n",
            "web/inner/b.ts": "export class Deep {}\n",
        }
        bodies = {
            "web/index.ts": _nbody(
                reexports=[{"name": "*", "from_module": "./a", "alias": None, "line": 1}]
            ),
            "web/a.ts": _nbody(
                reexports=[{"name": "*", "from_module": "./inner/b", "alias": None, "line": 1}]
            ),
            "web/inner/b.ts": _nbody(_sym("Deep", "class")),
        }
        with _workspace(files), _symbols(bodies):
            found = await _cls(client, "Deep", file_path="web/index.ts")
            absent = await _cls(client, "Nope", file_path="web/index.ts")
        assert found.json()["valid"] is True
        assert found.json()["found_in"] == ["web/inner/b.ts"]
        assert absent.json()["valid"] is False

    async def test_aliased_reexport_and_namespace_star(self, client):
        files = {
            "web/index.ts": (
                "export { Store as Shop } from './store';\nexport * as ns from './store';\n"
            ),
            "web/store.ts": "export class Store {}\n",
        }
        bodies = {
            "web/index.ts": _nbody(
                reexports=[
                    {"name": "Store", "from_module": "./store", "alias": "Shop", "line": 1},
                    {"name": "*", "from_module": "./store", "alias": "ns", "line": 2},
                ]
            ),
            "web/store.ts": _nbody(_sym("Store", "class")),
        }
        with _workspace(files), _symbols(bodies):
            aliased = await _cls(client, "Shop", file_path="web/index.ts")
            namespace = await _cls(client, "ns", file_path="web/index.ts")
        assert aliased.json()["valid"] is True
        assert namespace.json()["valid"] is None
        assert "namespace" in namespace.json()["note"]


class TestPythonReexportAndInheritance:
    _FILES = {
        "pkg/__init__.py": "from .models import User\nfrom extlib import Thing\n",
        "pkg/models.py": "class User:\n    pass\n",
        "app/base.py": "class Base:\n    def run(self):\n        pass\n",
        "app/child.py": (
            "from app.base import Base\nfrom .base import Base as B2\n\n"
            "class Child(Base):\n    pass\n\n\nclass Other(B2):\n    pass\n"
        ),
    }

    async def test_package_reexport_true_false_external_null(self, client):
        with _workspace(self._FILES), _symbols({}):
            found = await _cls(client, "pkg.User")
            absent = await _cls(client, "pkg.Ghost")
            external = await _cls(client, "pkg.Thing")
        assert found.json()["valid"] is True
        assert "pkg/models.py" in found.json()["found_in"]
        assert "re-exported via 'pkg/__init__.py'" in found.json()["note"]
        assert absent.json()["valid"] is False
        assert external.json()["valid"] is None
        assert "extlib" in external.json()["note"]

    async def test_base_in_another_module(self, client):
        with _workspace(self._FILES), _symbols({}):
            found = await _fn(client, "Child.run", file_path="app/child.py")
            relative = await _fn(client, "Other.run")
            absent = await _fn(client, "Child.stop")
        assert found.json()["valid"] is True
        assert "inherited from Base (app/base.py:1)" in found.json()["note"]
        assert relative.json()["valid"] is True
        assert absent.json()["valid"] is False


class TestConcept:
    async def test_concept_is_a_class_reference(self, client):
        files = {"src/c.cpp": "template<typename T> concept Addable = true;\n"}
        bodies = {"src/c.cpp": _nbody(_sym("Addable", "concept"), language="cpp")}
        with _workspace(files), _symbols(bodies):
            found = await _cls(client, "Addable", file_path="src/c.cpp")
            fn = await _fn(client, "Addable", file_path="src/c.cpp")
        assert found.json()["valid"] is True
        # A concept is not a function: the name exists as another kind.
        assert fn.json()["valid"] is None


# ---------------------------------------------------------------------------
# validateArchitecture: sharded scans
# ---------------------------------------------------------------------------

_BIG = {
    "app/__init__.py": "",
    "app/domain/__init__.py": "",
    "app/domain/user.py": "from app.infra.db import connect\n",
    "app/infra/__init__.py": "",
    "app/infra/db.py": "x = 1\n",
    "tools/run.py": "import os\n",
}


def _clipping(files, clip):
    """The ``_workspace`` double, except that *clip(argv)* commands come back clipped."""
    inner = _workspace(files).new
    calls: list[list[str]] = []

    async def _run(container_name, command, tenant_id, timeout=30):
        calls.append(command)
        if clip(command):
            return _exec("/workspace/partial", 0, truncated=True)
        return await inner(container_name, command, tenant_id, timeout)

    mock = AsyncMock(side_effect=_run)
    return patch("services.container_workspace.exec_in_workspace", new=mock), calls


def _is_root_search(command):
    whole = command[0] == "find" and "-maxdepth" not in command and command[1] == "/workspace"
    return whole or (command[:2] == ["grep", "-rnE"] and command[-1] == "/workspace")


async def _post(client, rules, **kw):
    return (
        await client.post(
            "/api/validation/architecture", json={"container_name": "c1", "rules": rules, **kw}
        )
    ).json()["validation_results"]


class TestShardedScan:
    async def test_clipped_root_is_split_into_children_and_completes(self, client):
        rule = "no_import:app/domain->app/infra"
        whole = await _arch(client, [rule], _BIG)
        baseline = whole.json()["validation_results"]
        ctx, calls = _clipping(_BIG, _is_root_search)
        with ctx:
            split = await _post(client, [rule])
        assert baseline["verdict"] == split["verdict"] == "invalid"
        assert split["scan"]["complete"] is True
        assert split["scan"]["exec_calls"] > baseline["scan"]["exec_calls"]
        assert split["scan"]["files"] == baseline["scan"]["files"] == 6
        assert any(c[0] == "grep" and c[-1] == "/workspace/app" for c in calls)
        honoured = {**_BIG, "app/domain/user.py": "import os\n"}
        ctx, _ = _clipping(honoured, _is_root_search)
        with ctx:
            assert (await _post(client, [rule]))["verdict"] == "valid"

    async def test_a_clipped_single_file_is_not_computed(self, client):
        ctx, _ = _clipping(_BIG, lambda c: c[0] == "grep")
        with ctx:
            vr = await _post(client, ["no_import:app/domain->app/infra"])
        assert vr["verdict"] == "not_computed"
        assert "alone exceed" in vr["notes"][0]
        assert vr["scan"]["complete"] is False

    async def test_budget_exhaustion_is_not_computed(self, client):
        ctx, _ = _clipping(_BIG, _is_root_search)
        with ctx, patch("services.architecture_rules.EXEC_CALL_BUDGET", 3):
            vr = await _post(client, ["no_import:app/domain->app/infra"])
        assert vr["verdict"] == "not_computed"
        assert "3-exec-call budget" in vr["notes"][0]
        assert vr["scan"]["complete"] is False
        assert vr["scan"]["exec_calls"] == 3

    async def test_scope_starts_the_import_greps_at_its_literal_prefix(self, client):
        ctx, calls = _clipping(_BIG, lambda c: False)
        with ctx:
            vr = await _post(client, ["no_import:app/domain->app/infra"], scope="app/domain/*")
        assert vr["verdict"] == "invalid"
        greps = [c for c in calls if c[:2] == ["grep", "-rnE"]]
        assert greps and all(c[-1] == "/workspace/app/domain" for c in greps)


# ---------------------------------------------------------------------------
# validateArchitecture: C#
# ---------------------------------------------------------------------------


class TestCSharpUsing:
    _CS = {
        "src/Domain/Order.cs": "using System.Text;\nusing Acme.Infra;\nnamespace Acme.Domain;\n",
        "src/Infra/Db.cs": "namespace Acme.Infra\n{\n    class Db {}\n}\n",
        "src/Shared/Clock.cs": "namespace Acme.Shared;\n",
    }

    async def test_no_import_violated_and_honoured(self, client):
        rule = "no_import:src/Domain->src/Infra"
        bad = await _status(client, rule, self._CS)
        good = await _status(
            client,
            rule,
            {**self._CS, "src/Domain/Order.cs": "using System.Text;\nusing Acme.Shared;\n"},
        )
        assert bad["status"] == "fail"
        assert bad["violations"][0]["target"] == "src/Infra/Db.cs"
        assert good["status"] == "pass"

    async def test_using_static_and_alias(self, client):
        rule = "no_import:src/Domain->src/Infra"
        static = {**self._CS, "src/Domain/Order.cs": "using static Acme.Infra.Db;\n"}
        alias = {**self._CS, "src/Domain/Order.cs": "using DbAlias = Acme.Infra.Db;\n"}
        assert (await _status(client, rule, static))["status"] == "fail"
        assert (await _status(client, rule, alias))["status"] == "fail"

    async def test_unresolved_internal_looking_namespace_is_not_computed(self, client):
        files = {**self._CS, "src/Domain/Order.cs": "using Acme.Infra.Ghost;\n"}
        result = await _status(client, "no_import:src/Domain->src/Infra", files)
        assert result["status"] == "not_computed"
        assert "unresolved internal import Acme.Infra.Ghost" in result["reason"]

    async def test_internal_global_using_makes_every_cs_rule_abstain(self, client):
        files = {**self._CS, "src/Shared/Usings.cs": "global using Acme.Infra;\n"}
        result = await _status(client, "no_import:src/Domain->src/Infra", files)
        assert result["status"] == "not_computed"
        assert "global using" in result["reason"]

    async def test_graph_rules_compute_over_csharp(self, client):
        cyc = {
            "src/A/A.cs": "using Acme.B;\nnamespace Acme.A;\n",
            "src/B/B.cs": "using Acme.A;\nnamespace Acme.B;\n",
        }
        vr = (await _arch(client, ["no_cycles"], cyc)).json()["validation_results"]
        assert _rule(vr)["status"] == "fail"


class TestImportedNotExported:
    async def test_esm_import_only_is_false_commonjs_export_is_null(self, client):
        files = {
            "web/a.ts": "import { Foo } from './foo';\nuse(Foo);\n",
            "web/b.js": "const { Foo } = require('./foo');\nmodule.exports = { Foo };\n",
        }
        imported = [{"module": "./foo", "names": ["Foo"], "line": 1}]
        bodies = {
            "web/a.ts": _nbody(imports=imported),
            "web/b.js": _nbody(imports=imported, language="javascript"),
        }
        with _workspace(files), _symbols(bodies):
            esm = await _cls(client, "Foo", file_path="web/a.ts")
            cjs = await _cls(client, "Foo", file_path="web/b.js")
        assert esm.json()["valid"] is False
        assert cjs.json()["valid"] is None


class TestPythonInstanceAttributesAndDynamicHooks:
    """Live 2026-10-12 (cv-m2-hello, gv/dyn.py): ``Plain.size`` set in ``__init__``
    answered False (a real attribute rejected as a phantom) and a ghost member of a
    class defining ``__getattr__`` answered False (absence cannot be proven)."""

    _FILES = {
        "gv/__init__.py": "",
        "gv/dyn.py": (
            "class Dyn:\n    def __getattr__(self, name):\n        return name\n\n\n"
            "class Plain:\n    __slots__ = ('slot_a',)\n\n"
            "    def __init__(self):\n        self.size = 3\n        self.items: list = []\n\n"
            "    def grow(this):\n        this.grown = True\n\n"
            "    @classmethod\n    def make(cls):\n        cls.registry = {}\n\n"
            "    def own(self):\n        other = object()\n        other.foreign = 1\n\n\n"
            "class Child(Dyn):\n    pass\n\n\n"
            "class PlainChild(Plain):\n    pass\n\n\n"
            "class Wrap:\n    def __getattribute__(self, name):\n        return 1\n"
        ),
    }

    async def test_instance_attributes_are_true(self, client):
        with _workspace(self._FILES), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="gv/dyn.py")).json()
                for q in (
                    "Plain.size",
                    "Plain.items",
                    "Plain.grown",
                    "Plain.registry",
                    "Plain.slot_a",
                    "PlainChild.size",
                )
            }
        for q, body in got.items():
            assert body["valid"] is True, (q, body)
        assert "gv/dyn.py':10" in got["Plain.size"]["note"]
        assert "base Plain" in got["PlainChild.size"]["note"]

    async def test_attribute_on_another_object_is_not_the_owners(self, client):
        with _workspace(self._FILES), _symbols({}):
            foreign = await _fn(client, "Plain.foreign", file_path="gv/dyn.py")
            ghost = await _fn(client, "Plain.ghost", file_path="gv/dyn.py")
        assert foreign.json()["valid"] is False
        assert ghost.json()["valid"] is False

    async def test_dynamic_attribute_hooks_make_absence_null(self, client):
        with _workspace(self._FILES), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="gv/dyn.py")).json()
                for q in ("Dyn.ghost", "Child.ghost", "Wrap.ghost")
            }
        for q, body in got.items():
            assert body["valid"] is None, (q, body)
            assert "dynamic_attribute" in body["note"], (q, body)

    # Validator 2026-10-12: each of these answered true (the method walk took a
    # staticmethod's argument as the receiver, descended into nested scopes and
    # counted statements that only READ the attribute).
    _NOT_CREATED = {
        "gv/__init__.py": "",
        "gv/m.py": (
            "class P:\n"
            "    @staticmethod\n    def s(x):\n        x.stat = 1\n\n"
            "    def n(self):\n        def h(self):\n            self.nested = 1\n        return h\n\n"
            "    def k(self):\n        class Inner:\n            def __init__(self):\n"
            "                self.inner_cls = 1\n        return Inner\n\n"
            "    def c(self, k):\n        self.cache[k] = 1\n\n"
            "    def a(self):\n        self.cnt += 1\n\n"
            "    def d(self):\n        self.chain.sub = 1\n\n"
            "    def lam(self):\n        f = lambda self: setattr(self, 'lamb', 1)\n\n"
            "    def decl(self):\n        self.declared: int\n\n"
            "    def made(self):\n        self.pa, [*self.pb] = 1, [2]\n"
            "        setattr(self, 'viaset', 1)\n        for self.loop in ():\n            pass\n\n"
            "class S:\n    __slots__ = NAMES\n\n"
            "class SD:\n    __slots__ = {'sd': 'doc'}\n\n"
            "def __getattr__(name):\n    return name\n"
        ),
    }

    async def test_only_statements_that_create_the_attribute_count(self, client):
        with _workspace(self._NOT_CREATED), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="gv/m.py")).json()
                for q in (
                    "P.stat", "P.nested", "P.inner_cls", "P.cache", "P.cnt", "P.chain",
                    "P.lamb", "P.declared", "P.pa", "P.pb", "P.viaset", "P.loop", "SD.sd",
                )
            }  # fmt: skip
        created = {"P.pa", "P.pb", "P.viaset", "P.loop", "SD.sd"}
        for q, body in got.items():
            assert body["valid"] is (q in created), (q, body)

    async def test_opaque_slots_and_module_getattr_make_absence_null(self, client):
        with _workspace(self._NOT_CREATED), _symbols({}):
            slots = (await _fn(client, "S.zz", file_path="gv/m.py")).json()
            module = (await _fn(client, "m.ghost", file_path="gv/m.py")).json()
        assert slots["valid"] is None and "__slots__" in slots["note"], slots
        assert module["valid"] is None and "__getattr__" in module["note"], module


class TestPythonComputedAttributeNames:
    """Validator 2026-10-12: a class whose methods create attributes under computed
    names (``setattr(self, k, v)``, ``self.__dict__.update(...)``, ...) or whose
    ``__slots__`` lists ``__dict__`` answered False for an absent member; absence
    cannot be proven there, so it is null."""

    _FILES = {
        "gv/__init__.py": "",
        "gv/c.py": (
            "class SetA:\n    def __init__(self, **kw):\n        self.real = 1\n"
            "        for k, v in kw.items():\n            setattr(self, k, v)\n\n"
            "class DictU:\n    def __init__(self, d):\n        self.__dict__.update(d)\n\n"
            "class VarsU:\n    def __init__(self, d):\n        vars(self).update(d)\n\n"
            "class DictItem:\n    def __init__(self, k):\n        self.__dict__[k] = 1\n"
            "        self.__dict__['lit'] = 2\n\n"
            "class ObjSet:\n    def __init__(self, k):\n        object.__setattr__(self, k, 1)\n\n"
            "class SlotDict:\n    __slots__ = ('a', '__dict__')\n\n"
            "class Plain:\n    def __init__(self, k):\n        setattr(self, 'fixed', 1)\n"
            "        other = {}\n        other[k] = 1\n"
        ),
    }

    async def test_absent_member_is_null(self, client):
        with _workspace(self._FILES), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="gv/c.py")).json()
                for q in (
                    "SetA.ghost", "DictU.ghost", "VarsU.ghost", "DictItem.ghost",
                    "ObjSet.ghost", "SlotDict.ghost",
                )
            }  # fmt: skip
        for q, body in got.items():
            assert body["valid"] is None, (q, body)
            assert "dynamic_attribute" in body["note"], (q, body)

    async def test_present_member_still_true_and_plain_absence_false(self, client):
        with _workspace(self._FILES), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="gv/c.py")).json()
                for q in ("SetA.real", "DictItem.lit", "SlotDict.a", "Plain.fixed", "Plain.ghost")
            }
        for q in ("SetA.real", "DictItem.lit", "SlotDict.a", "Plain.fixed"):
            assert got[q]["valid"] is True, (q, got[q])
        assert got["Plain.ghost"]["valid"] is False, got["Plain.ghost"]


# ---------------------------------------------------------------------------
# validateArchitecture: fully-qualified references (Java, C#)
# ---------------------------------------------------------------------------


def _is_fq_search(command):
    return (
        command[0] == "grep"
        and "-e" in command
        and command[command.index("-e") + 1].endswith(
            (r"\.[A-Z]", r"\.[A-Za-z_]", r"\.[[:space:]]*[A-Za-z_]")
        )
    )


def _failing(files, fail):
    """The ``_workspace`` double, except that *fail(argv)* commands exit 2."""
    inner = _workspace(files).new

    async def _run(container_name, command, tenant_id, timeout=30):
        if fail(command):
            return _exec("", 2, "grep: boom")
        return await inner(container_name, command, tenant_id, timeout)

    return patch("services.container_workspace.exec_in_workspace", new=AsyncMock(side_effect=_run))


class TestFullyQualifiedReferences:
    """A qualified reference to an internal type needs no import line: before
    2026-10-12 ``no_import`` / ``layers`` passed over it (the scanner never saw it)."""

    _JAVA = {
        "src/com/acme/domain/Order.java": (
            "package com.acme.domain;\n\npublic class Order {\n"
            "    Object o = new com.acme.infra.Db().load();\n}\n"
        ),
        "src/com/acme/domain/Line.java": "package com.acme.domain;\npublic class Line {}\n",
        "src/com/acme/infra/Db.java": "package com.acme.infra;\npublic class Db {}\n",
    }
    _JRULE = "no_import:src/com/acme/domain->src/com/acme/infra"
    _CS = {
        "src/Domain/Order.cs": (
            "namespace Acme.Domain;\n\nclass Order {\n"
            "    void Run() { var db = new Acme.Infra.Db(); Acme.Infra.Db.Open(); }\n}\n"
        ),
        "src/Domain/Line.cs": "namespace Acme.Domain;\nclass Line {}\n",
        "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
    }
    _CRULE = "no_import:src/Domain->src/Infra"

    async def test_java_fq_reference_is_a_violation_removed_passes(self, client):
        bad = await _status(client, self._JRULE, self._JAVA)
        assert bad["status"] == "fail"
        v = bad["violations"][0]
        assert (v["file"], v["line"]) == ("src/com/acme/domain/Order.java", 4)
        assert v["imports"] == "fq:com.acme.infra.Db"
        assert v["target"] == "src/com/acme/infra/Db.java"
        # `a>b`: a is the TOP layer, so domain (lower) must not use infra (top).
        up = await _status(client, "layers:src/com/acme/infra>src/com/acme/domain", self._JAVA)
        assert up["status"] == "fail"
        assert up["violations"][0]["imports"] == "fq:com.acme.infra.Db"
        down = await _status(client, "layers:src/com/acme/domain>src/com/acme/infra", self._JAVA)
        assert down["status"] == "pass"
        clean = {**self._JAVA, "src/com/acme/domain/Order.java": "package com.acme.domain;\n"}
        assert (await _status(client, self._JRULE, clean))["status"] == "pass"

    async def test_csharp_fq_reference_is_a_violation_removed_passes(self, client):
        bad = await _status(client, self._CRULE, self._CS)
        assert bad["status"] == "fail"
        v = bad["violations"][0]
        assert (v["file"], v["line"]) == ("src/Domain/Order.cs", 4)
        assert v["imports"] == "fq:Acme.Infra.Db"
        assert v["target"] == "src/Infra/Db.cs"
        clean = {**self._CS, "src/Domain/Order.cs": "namespace Acme.Domain;\nclass Order {}\n"}
        assert (await _status(client, self._CRULE, clean))["status"] == "pass"

    async def test_many_names_grep_by_roots_and_filter_here(self, client):
        with patch("services.architecture_rules.FQ_ALTERNATION_CAP", 1):
            java = await _status(client, self._JRULE, self._JAVA)
            cs = await _status(client, self._CRULE, self._CS)
        assert java["status"] == cs["status"] == "fail"
        assert java["violation_count"] == cs["violation_count"] == 1
        # Partially qualified C#: the segment fallback (3 segments) and the
        # any-dotted-name fallback (cap 1) both still find `Infra.Db`.
        for cap in (1, 3):
            with patch("services.architecture_rules.FQ_ALTERNATION_CAP", cap):
                partial = await _status(client, self._CRULE, self._PCS)
            assert partial["status"] == "fail", (cap, partial)

    async def test_own_package_or_namespace_is_no_edge(self, client):
        java = {
            **self._JAVA,
            "src/com/acme/domain/Order.java": (
                "package com.acme.domain;\nclass Order { com.acme.domain.Line l; }\n"
            ),
        }
        cs = {
            **self._CS,
            "src/Domain/Order.cs": "namespace Acme.Domain;\nclass Order { Acme.Domain.Line l; }\n",
        }
        assert (
            await _status(
                client,
                "no_import:src/com/acme/domain/Order.java->src/com/acme/domain/Line.java",
                java,
            )
        )["status"] == "pass"
        assert (await _status(client, "no_import:src/Domain/Order.cs->src/Domain/Line.cs", cs))[
            "status"
        ] == "pass"

    async def test_clipped_or_failed_fq_search_is_not_computed(self, client):
        for files, rule in ((self._JAVA, self._JRULE), (self._CS, self._CRULE)):
            clean = {k: v for k, v in files.items() if "Order" not in k}
            clean[next(k for k in files if "Order" in k)] = (
                "package com.acme.domain;\n" if rule == self._JRULE else "namespace Acme.Domain;\n"
            )
            ctx, _ = _clipping(clean, _is_fq_search)
            with ctx:
                clipped = await _post(client, [rule])
            with _failing(clean, _is_fq_search):
                failed = await _post(client, [rule])
            for vr in (clipped, failed):
                assert vr["verdict"] == "not_computed", vr
                assert _rule(vr)["status"] == "not_computed"
                assert "fully-qualified reference search" in _rule(vr)["reason"]
            assert "alone exceed" in _rule(clipped)["reason"]

    async def test_qualified_name_in_a_string_or_comment_is_no_edge(self, client):
        java = {
            **self._JAVA,
            "src/com/acme/domain/Order.java": (
                "package com.acme.domain;\nclass Order {\n"
                '    String s = "com.acme.infra.Db"; // com.acme.infra.Db\n}\n'
            ),
        }
        cs = {
            **self._CS,
            "src/Domain/Order.cs": (
                'namespace Acme.Domain;\nclass Order { string s = "Acme.Infra.Db"; '
                '/* Acme.Infra.Db */ string v = @"Acme.Infra.Db"; }\n'
            ),
        }
        assert (await _status(client, self._JRULE, java))["status"] == "pass"
        assert (await _status(client, self._CRULE, cs))["status"] == "pass"

    # C# PARTIALLY qualified names: the first segment binds through the
    # enclosing namespaces (innermost first), then through the file's usings.
    _PCS = {
        "src/Domain/Order.cs": (
            "namespace Acme.Domain;\n\nclass Order {\n    void Run() { var db = new Infra.Db(); }\n}\n"
        ),
        "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
    }

    async def test_csharp_partial_name_through_enclosing_namespace(self, client):
        bad = await _status(client, self._CRULE, self._PCS)
        assert bad["status"] == "fail", bad
        v = bad["violations"][0]
        assert (v["file"], v["line"]) == ("src/Domain/Order.cs", 4)
        assert v["imports"] == "fq:Acme.Infra.Db"
        assert v["target"] == "src/Infra/Db.cs"
        clean = {**self._PCS, "src/Domain/Order.cs": "namespace Acme.Domain;\nclass Order {}\n"}
        assert (await _status(client, self._CRULE, clean))["status"] == "pass"

    async def test_csharp_partial_name_through_a_using(self, client):
        files = {
            "src/App/Order.cs": (
                "using Acme;\nnamespace Other.App;\nclass Order { Infra.Db db; }\n"
            ),
            "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
        }
        bad = await _status(client, "no_import:src/App->src/Infra", files)
        assert bad["status"] == "fail", bad
        assert bad["violations"][0]["imports"] == "fq:Acme.Infra.Db"
        clean = {**files, "src/App/Order.cs": "using Acme;\nnamespace Other.App;\nclass Order {}\n"}
        assert (await _status(client, "no_import:src/App->src/Infra", clean))["status"] == "pass"

    async def test_csharp_partial_name_binds_the_innermost_namespace(self, client):
        files = {
            **self._PCS,
            "src/Domain/Infra/Db.cs": "namespace Acme.Domain.Infra;\nclass Db {}\n",
        }
        inner = await _status(client, "no_import:src/Domain/Order.cs->src/Domain/Infra", files)
        assert inner["status"] == "fail", inner
        assert inner["violations"][0]["imports"] == "fq:Acme.Domain.Infra.Db"
        outer = await _status(client, "no_import:src/Domain/Order.cs->src/Infra", files)
        assert outer["status"] == "pass", outer

    async def test_csharp_partial_name_ambiguous_through_usings_is_not_computed(self, client):
        files = {
            "src/App/Order.cs": (
                "using Acme;\nusing Beta;\nnamespace Other.App;\nclass Order { Infra.Db db; }\n"
            ),
            "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
            "src/Beta/Db.cs": "namespace Beta.Infra;\nclass Db {}\n",
        }
        with _workspace(files):
            vr = await _post(client, ["no_import:src/App->src/Infra"])
        assert vr["verdict"] == "not_computed", vr
        assert "ambiguous" in _rule(vr)["reason"]

    # Validator 2026-10-12: a line STARTING with a declaration was skipped whole,
    # so a reference after it on the same line passed.
    async def test_reference_after_a_declaration_on_the_same_line(self, client):
        cs_cases = (
            "namespace Acme.Domain { class Order { Acme.Infra.Db d; } }\n",
            "namespace Acme.Domain; class Order { Acme.Infra.Db d; }\n",
            "using System; class Order { Acme.Infra.Db d; }\n",
            "using System; namespace Acme.Domain { class Order { Acme.Infra.Db d; } }\n",
            "namespace Acme.Domain { class Order { Infra.Db d; } }\n",
        )
        for source in cs_cases:
            got = await _status(client, self._CRULE, {**self._CS, "src/Domain/Order.cs": source})
            assert got["status"] == "fail", (source, got)
            assert [v["imports"] for v in got["violations"]] == ["fq:Acme.Infra.Db"], source
        java_cases = (
            "package com.acme.domain;\nimport java.util.List; class Order { com.acme.infra.Db d; }\n",
            "package com.acme.domain; import java.util.*; public class Order "
            "{ com.acme.infra.Db d; }\n",
        )
        for source in java_cases:
            files = {**self._JAVA, "src/com/acme/domain/Order.java": source}
            got = await _status(client, self._JRULE, files)
            assert got["status"] == "fail", (source, got)
            assert [v["imports"] for v in got["violations"]] == ["fq:com.acme.infra.Db"], source

    async def test_a_declarations_own_name_is_no_fq_edge(self, client):
        cs = {
            **self._CS,
            "src/Domain/Order.cs": "using Acme.Infra; namespace Acme.Domain { class Order {} }\n",
        }
        java = {
            **self._JAVA,
            "src/com/acme/domain/Order.java": (
                "package com.acme.domain; import com.acme.infra.Db; class Order {}\n"
            ),
        }
        got_cs = await _status(client, self._CRULE, cs)
        got_java = await _status(client, self._JRULE, java)
        assert [v["imports"] for v in got_cs["violations"]] == ["Acme.Infra"], got_cs
        assert [v["imports"] for v in got_java["violations"]] == ["com.acme.infra.Db"], got_java

    async def test_java_spaced_dots_and_lower_case_class(self, client):
        spaced = {
            **self._JAVA,
            "src/com/acme/domain/Order.java": (
                "package com.acme.domain;\nclass Order { Object o = new com.acme.infra . Db(); }\n"
            ),
        }
        got = await _status(client, self._JRULE, spaced)
        assert got["status"] == "fail", got
        assert got["violations"][0]["imports"] == "fq:com.acme.infra.Db"
        lower = {
            **self._JAVA,
            "src/com/acme/domain/Order.java": (
                "package com.acme.domain;\nclass Order { com.acme.infra.db x; }\n"
            ),
        }
        # No file for the class: an unresolved internal reference, never a pass.
        assert (await _status(client, self._JRULE, lower))["status"] == "not_computed"
        with_file = {
            **lower,
            "src/com/acme/infra/db.java": "package com.acme.infra;\nclass db {}\n",
        }
        got = await _status(client, self._JRULE, with_file)
        assert got["status"] == "fail", got
        assert got["violations"][0]["target"] == "src/com/acme/infra/db.java"

    async def test_csharp_lower_case_local_is_no_edge(self, client):
        files = {
            **self._PCS,
            "src/Domain/Order.cs": (
                "namespace Acme.Domain;\nclass Order { void Run(Thing infra) { infra.Db(); } }\n"
            ),
        }
        assert (await _status(client, self._CRULE, files))["status"] == "pass"
