"""A base type, a re-export and a member absence are decided only with PROOF.

Regression tests for an adversarial review of the inheritance / re-export walk:

- a base NAME is located only where the name as written provably binds (same
  file, an import that resolves to the file, the same package / namespace /
  directory) -- never "the one workspace file that defines a type so named";
- a C# member absent from its owner is never ``False`` (extension methods,
  partial types); a Python one is not when it is monkeypatched anywhere; a
  TS one is not when an ``interface Owner`` (declaration merging, module
  augmentation) or a prototype / static assignment exists anywhere;
- a source-less ``export { w as q }`` follows the LOCAL binding ``w`` back
  through its import (``import { y as w }`` -> ``y``), never searches for ``w``;
- C# architecture: nested namespace blocks and ``using`` relative to an
  enclosing namespace are never read as "external".

Same doubles as ``test_validation_multilang``: only the workspace exec call and
the coding-analysis symbols call are faked.
"""

from __future__ import annotations

from tests.unit.test_validation_inheritance_reexports import _base, _nbody
from tests.unit.test_validation_multilang import _cls, _fn, _status, _sym, _symbols, _workspace


async def _pair(client, files, bodies, found, absent, path):
    with _workspace(files), _symbols(bodies):
        f = (await _fn(client, found, file_path=path)).json()
        a = (await _fn(client, absent, file_path=path)).json()
    return f, a


# ---------------------------------------------------------------------------
# A base binds only with proof
# ---------------------------------------------------------------------------


class TestCSharpBase:
    _LEGACY = {
        "src/Legacy/Controller.cs": "namespace Legacy;\nclass Controller { void Render() {} }\n",
    }
    _LEGACY_BODY = {
        "src/Legacy/Controller.cs": _nbody(
            _sym("Controller", "class", 2),
            _sym("Render", "method", 2, "Controller"),
            language="c_sharp",
        )
    }

    def _ctl(self, header):
        files = {**self._LEGACY, "src/App/MyCtl.cs": f"{header}class MyCtl : Controller {{}}\n"}
        line = header.count("\n") + 1
        bodies = {
            **self._LEGACY_BODY,
            "src/App/MyCtl.cs": _nbody(
                _base("MyCtl", "class", ["Controller"], line), language="c_sharp"
            ),
        }
        return files, bodies

    async def test_external_using_and_unrelated_same_name_type_is_null(self, client):
        files, bodies = self._ctl("using Microsoft.AspNetCore.Mvc;\nnamespace App;\n")
        f, a = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.View", "src/App/MyCtl.cs")
        assert f["valid"] is None
        assert a["valid"] is None
        assert "Microsoft.AspNetCore.Mvc" in f["note"]

    async def test_same_namespace_binds(self, client):
        files, bodies = self._ctl("namespace Legacy;\n")
        f, a = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.View", "src/App/MyCtl.cs")
        assert f["valid"] is True
        assert f["found_in"] == ["src/Legacy/Controller.cs"]
        # Found is true; absent is never false in C# (extension methods).
        assert a["valid"] is None
        assert "extension methods" in a["note"]

    async def test_internal_using_binds_unless_a_global_using_exists(self, client):
        files, bodies = self._ctl("using Legacy;\nnamespace App;\n")
        f, _ = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.View", "src/App/MyCtl.cs")
        assert f["valid"] is True
        files["src/Usings.cs"] = "global using Microsoft.AspNetCore.Mvc;\n"
        g, _ = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.View", "src/App/MyCtl.cs")
        assert g["valid"] is None
        assert "global using" in g["note"]

    async def test_extension_method_owner_is_never_false(self, client):
        files = {
            "src/App/Order.cs": "namespace App;\npublic class Order { }\n",
            "src/App/OrderExt.cs": (
                "namespace App;\npublic static class OrderExt "
                "{ public static decimal Total(this Order o) => 0; }\n"
            ),
        }
        bodies = {
            "src/App/Order.cs": _nbody(_sym("Order", "class", 2), language="c_sharp"),
            "src/App/OrderExt.cs": _nbody(
                _sym("OrderExt", "class", 2),
                _sym("Total", "method", 2, "OrderExt"),
                language="c_sharp",
            ),
        }
        with _workspace(files), _symbols(bodies):
            located = (await _fn(client, "Order.Total", file_path="src/App/Order.cs")).json()
            unlocated = (await _fn(client, "Order.Total")).json()
        assert located["valid"] is None
        assert unlocated["valid"] is None


class TestJavaBase:
    _B = {"src/b/Base.java": "package b;\nclass Base { void run() {} }\n"}
    _B_BODY = {
        "src/b/Base.java": _nbody(
            _sym("Base", "class", 2), _sym("run", "method", 2, "Base"), language="java"
        )
    }

    def _child(self, imports, header):
        files = {
            **self._B,
            "src/a/Child.java": f"package a;\n{header}class Child extends Base {{}}\n",
        }
        bodies = {
            **self._B_BODY,
            "src/a/Child.java": _nbody(
                _base("Child", "class", ["Base"], 3), imports=imports, language="java"
            ),
        }
        return files, bodies

    async def test_external_wildcard_and_unrelated_same_name_class_is_null(self, client):
        files, bodies = self._child(
            [{"module": "org.lib", "names": ["*"], "line": 2}], "import org.lib.*;\n"
        )
        f, a = await _pair(client, files, bodies, "Child.run", "Child.stop", "src/a/Child.java")
        assert f["valid"] is None
        assert a["valid"] is None

    async def test_internal_wildcard_and_single_type_import_bind(self, client):
        for imports, header in (
            ([{"module": "b", "names": ["*"], "line": 2}], "import b.*;\n"),
            ([{"module": "b.Base", "names": ["Base"], "line": 2}], "import b.Base;\n"),
        ):
            files, bodies = self._child(imports, header)
            f, a = await _pair(client, files, bodies, "Child.run", "Child.stop", "src/a/Child.java")
            assert f["valid"] is True
            assert f["found_in"] == ["src/b/Base.java"]
            assert a["valid"] is False

    async def test_single_type_import_picks_its_package_among_namesakes(self, client):
        files, bodies = self._child(
            [{"module": "b.Base", "names": ["Base"], "line": 2}], "import b.Base;\n"
        )
        files["src/c/Base.java"] = "package c;\nclass Base { void stop() {} }\n"
        bodies["src/c/Base.java"] = _nbody(
            _sym("Base", "class", 2), _sym("stop", "method", 2, "Base"), language="java"
        )
        f, a = await _pair(client, files, bodies, "Child.run", "Child.stop", "src/a/Child.java")
        assert f["valid"] is True
        assert a["valid"] is False

    async def test_java_lang_name_is_null_despite_a_workspace_namesake(self, client):
        files = {
            "src/a/Worker.java": "package a;\nclass Worker extends Thread {}\n",
            "src/forum/Thread.java": "package forum;\nclass Thread { void reply() {} }\n",
        }
        bodies = {
            "src/a/Worker.java": _nbody(_base("Worker", "class", ["Thread"], 2), language="java"),
            "src/forum/Thread.java": _nbody(
                _sym("Thread", "class", 2), _sym("reply", "method", 2, "Thread"), language="java"
            ),
        }
        f, a = await _pair(
            client, files, bodies, "Worker.reply", "Worker.start", "src/a/Worker.java"
        )
        assert f["valid"] is None
        assert a["valid"] is None
        assert "java.lang" in a["note"]


class TestRustGoCppTsBase:
    async def test_rust_trait_needs_a_use_or_a_declared_module_path(self, client):
        greet = "pub trait Greet {\n    fn hello(&self) {}\n}\n"
        greet_body = _nbody(
            _sym("Greet", "trait"), _sym("hello", "method", 2, "Greet"), language="rust"
        )
        cases = [
            # (lib.rs content, imports, trait as written, verdict for Foo.hello)
            ("pub struct Foo;\nimpl Greet for Foo {}\n", [], "Greet", None),
            (
                "mod greet;\nuse crate::greet::Greet;\npub struct Foo;\nimpl Greet for Foo {}\n",
                [{"module": "crate::greet", "names": ["Greet"], "line": 2}],
                "Greet",
                True,
            ),
            # no `mod greet;`: `greet` is the external crate of that name
            (
                "use greet::Greet;\npub struct Foo;\nimpl Greet for Foo {}\n",
                [{"module": "greet", "names": ["Greet"], "line": 1}],
                "Greet",
                None,
            ),
            ("pub struct Foo;\nimpl greet::Greet for Foo {}\n", [], "greet::Greet", None),
        ]
        for lib, imports, trait, want in cases:
            files = {"src/lib.rs": lib, "src/greet.rs": greet}
            line = lib.splitlines().index(next(x for x in lib.splitlines() if "impl" in x)) + 1
            bodies = {
                "src/lib.rs": _nbody(
                    _sym("Foo", "struct"),
                    imports=imports,
                    impls=[{"type": "Foo", "trait": trait, "line": line}],
                    language="rust",
                ),
                "src/greet.rs": greet_body,
            }
            with _workspace(files), _symbols(bodies):
                r = (await _fn(client, "Foo.hello", file_path="src/lib.rs")).json()
            assert r["valid"] is want, (lib, r["note"])

    async def test_go_base_binds_in_its_own_package_directory_only(self, client):
        base = "package pkg\ntype Base struct{}\nfunc (b Base) Run() {}\n"
        base_body = _nbody(
            _sym("Base", "struct", 2), _sym("Run", "method", 3, "Base"), language="go"
        )
        for where, want in (("pkg/base.go", True), ("other/base.go", None)):
            files = {"pkg/s.go": "package pkg\ntype S struct { Base }\n", where: base}
            bodies = {
                "pkg/s.go": _nbody(_base("S", "struct", ["Base"], 2), language="go"),
                where: base_body,
            }
            with _workspace(files), _symbols(bodies):
                r = (await _fn(client, "S.Run", file_path="pkg/s.go")).json()
            assert r["valid"] is want, where

    async def test_cpp_angle_include_is_null_quoted_include_binds(self, client):
        header = "class Base {\npublic:\n  void run();\n};\n"
        header_body = _nbody(
            _sym("Base", "class"), _sym("run", "method", 3, "Base"), language="cpp"
        )
        for include, want in (("#include <ext/base.h>", None), ('#include "base.hpp"', True)):
            src = f"{include}\nclass D : public Base {{}};\n"
            files = {"src/d.cpp": src, "src/base.hpp": header}
            bodies = {
                "src/d.cpp": _nbody(_base("D", "class", ["Base"], 2), language="cpp"),
                "src/base.hpp": header_body,
            }
            with _workspace(files), _symbols(bodies):
                r = (await _fn(client, "D.run", file_path="src/d.cpp")).json()
            assert r["valid"] is want, include

    async def test_ts_base_neither_defined_nor_imported_is_null(self, client):
        files = {
            "web/store.ts": "export class Store extends Base {}\n",
            "web/base.ts": "export class Base {\n  size() { return 1; }\n}\n",
        }
        bodies = {
            "web/store.ts": _nbody(_base("Store", "class", ["Base"])),
            "web/base.ts": _nbody(_sym("Base", "class"), _sym("size", "method", 2, "Base")),
        }
        f, a = await _pair(client, files, bodies, "Store.size", "Store.clear", "web/store.ts")
        assert f["valid"] is None
        assert a["valid"] is None


# ---------------------------------------------------------------------------
# Members added from elsewhere
# ---------------------------------------------------------------------------


class TestMembersAddedElsewhere:
    async def test_python_monkeypatched_member_is_null(self, client):
        files = {"app/order.py": "class Order:\n    pass\n"}
        with _workspace(files), _symbols({}):
            clean = (await _fn(client, "Order.total", file_path="app/order.py")).json()
        patched = {**files, "app/patch.py": "from app.order import Order\nOrder.total = 0\n"}
        setattr_ = {**files, "app/patch.py": "setattr(Order, 'total', 0)\n"}
        with _workspace(patched), _symbols({}):
            p = (await _fn(client, "Order.total", file_path="app/order.py")).json()
            u = (await _fn(client, "Order.total")).json()
        with _workspace(setattr_), _symbols({}):
            s = (await _fn(client, "Order.total", file_path="app/order.py")).json()
        assert clean["valid"] is False
        assert p["valid"] is None
        assert "app/patch.py:2" in p["note"]
        assert u["valid"] is None
        assert s["valid"] is None

    async def test_ts_module_augmentation_is_null(self, client):
        files = {"web/store.ts": "export class Store {}\n"}
        bodies = {"web/store.ts": _nbody(_sym("Store", "class"))}
        with _workspace(files), _symbols(bodies):
            clean = (await _fn(client, "Store.extra", file_path="web/store.ts")).json()
        augmented = {
            **files,
            "web/aug.ts": "declare module './store' {\n  interface Store { extra(): void }\n}\n",
        }
        assigned = {**files, "web/x.js": "Store.prototype.extra = function () {};\n"}
        for tree in (augmented, assigned):
            with _workspace(tree), _symbols(bodies):
                r = (await _fn(client, "Store.extra", file_path="web/store.ts")).json()
            assert r["valid"] is None, tree
        assert clean["valid"] is False


# ---------------------------------------------------------------------------
# Source-less re-export of an aliased import
# ---------------------------------------------------------------------------


class TestReexportOfLocalAlias:
    _P = {"web/p.ts": "export class y {}\n"}
    _P_BODY = {"web/p.ts": _nbody(_sym("y", "class"))}

    async def _q(self, client, index, names, exported="w"):
        files = {**self._P, "web/index.ts": index}
        bodies = {
            **self._P_BODY,
            "web/index.ts": _nbody(
                imports=[{"module": "./p", "names": names, "line": 1}],
                reexports=[{"name": exported, "from_module": "./p", "alias": "q", "line": 2}],
            ),
        }
        with _workspace(files), _symbols(bodies):
            return (await _cls(client, "q", file_path="web/index.ts")).json()

    async def test_aliased_import_is_followed_by_its_imported_name(self, client):
        r = await self._q(client, "import { y as w } from './p';\nexport { w as q };\n", ["y"])
        assert r["valid"] is True
        assert r["found_in"] == ["web/p.ts"]

    async def test_default_and_namespace_imports_are_null_not_false(self, client):
        default = await self._q(client, "import w from './p';\nexport { w as q };\n", ["w"])
        namespace = await self._q(client, "import * as w from './p';\nexport { w as q };\n", ["*"])
        assert default["valid"] is None
        assert namespace["valid"] is None

    async def test_plain_import_absent_in_source_is_false(self, client):
        r = await self._q(client, "import { z } from './p';\nexport { z as q };\n", ["z"], "z")
        assert r["valid"] is False


# ---------------------------------------------------------------------------
# validateArchitecture: C# namespaces
# ---------------------------------------------------------------------------


class TestCSharpNamespaces:
    _RULE = "no_import:src/Domain->src/Infra"

    async def test_unindented_nested_namespaces_are_not_computed(self, client):
        files = {
            "src/Infra/Db.cs": "namespace A {\nnamespace B {\nclass Db {}\n}\n}\n",
            "src/Domain/Order.cs": "using A.B;\nnamespace Acme.Domain;\n",
        }
        result = await _status(client, self._RULE, files)
        assert result["status"] == "not_computed"

    async def test_relative_using_inside_a_namespace_is_an_edge(self, client):
        files = {
            "src/Infra/Db.cs": "namespace Acme.Infra;\nclass Db {}\n",
            "src/Domain/Order.cs": "namespace Acme\n{\n    using Infra;\n    class Order {}\n}\n",
        }
        bad = await _status(client, self._RULE, files)
        assert bad["status"] == "fail"
        assert bad["violations"][0]["target"] == "src/Infra/Db.cs"
        top = {**files, "src/Domain/Order.cs": "using Infra;\nnamespace Acme;\n"}
        assert (await _status(client, self._RULE, top))["status"] == "pass"


# ---------------------------------------------------------------------------
# C# lookup order: own namespace, ENCLOSING namespaces, then usings
# ---------------------------------------------------------------------------


class TestCSharpEnclosingNamespaces:
    _APP = {"src/App/Controller.cs": "namespace App;\nclass Controller { void Render() {} }\n"}
    _APP_BODY = {
        "src/App/Controller.cs": _nbody(
            _sym("Controller", "class", 2),
            _sym("Render", "method", 2, "Controller"),
            language="c_sharp",
        )
    }
    _DATA = {
        "src/App/Data/Controller.cs": "namespace App.Data;\nclass Controller { void Save() {} }\n"
    }
    _DATA_BODY = {
        "src/App/Data/Controller.cs": _nbody(
            _sym("Controller", "class", 2),
            _sym("Save", "method", 2, "Controller"),
            language="c_sharp",
        )
    }
    _CTL = "src/App/Web/MyCtl.cs"

    def _tree(self, header, *, app=True):
        files = {**self._DATA, self._CTL: f"{header}class MyCtl : Controller {{}}\n"}
        bodies = {
            **self._DATA_BODY,
            self._CTL: _nbody(
                _base("MyCtl", "class", ["Controller"], header.count("\n") + 1),
                language="c_sharp",
            ),
        }
        if app:
            files.update(self._APP)
            bodies.update(self._APP_BODY)
        return files, bodies

    async def test_parent_namespace_binds_without_a_using(self, client):
        files, bodies = self._tree("namespace App.Web;\n")
        f, _ = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.Save", self._CTL)
        assert f["valid"] is True
        assert f["found_in"] == ["src/App/Controller.cs"]

    async def test_parent_namespace_wins_over_an_internal_using(self, client):
        files, bodies = self._tree("using App.Data;\nnamespace App.Web;\n")
        f, a = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.Save", self._CTL)
        assert f["valid"] is True
        assert f["found_in"] == ["src/App/Controller.cs"]
        # App.Data.Controller.Save is NOT what the base binds to: never true.
        assert a["valid"] is None

    async def test_using_binds_when_no_enclosing_namespace_defines_it(self, client):
        files, bodies = self._tree("using App.Data;\nnamespace App.Web;\n", app=False)
        f, _ = await _pair(client, files, bodies, "MyCtl.Save", "MyCtl.Render", self._CTL)
        assert f["valid"] is True
        assert f["found_in"] == ["src/App/Data/Controller.cs"]

    async def test_platform_enclosing_namespace_is_null(self, client):
        # Microsoft.AspNetCore.Mvc (enclosing) holds the external Controller,
        # which C# finds before `using App.Data`.
        files, bodies = self._tree(
            "using App.Data;\nnamespace Microsoft.AspNetCore.Mvc.Web;\n", app=False
        )
        f, _ = await _pair(client, files, bodies, "MyCtl.Save", "MyCtl.Render", self._CTL)
        assert f["valid"] is None
        assert "Microsoft.AspNetCore.Mvc" in f["note"]

    async def test_two_definitions_at_one_level_are_null(self, client):
        files, bodies = self._tree("namespace App.Web;\n")
        files["src/App/Other/Controller.cs"] = "namespace App;\nclass Controller { }\n"
        bodies["src/App/Other/Controller.cs"] = _nbody(
            _sym("Controller", "class", 2), language="c_sharp"
        )
        f, _ = await _pair(client, files, bodies, "MyCtl.Render", "MyCtl.Save", self._CTL)
        assert f["valid"] is None
        assert "ambiguous" in f["note"]


# ---------------------------------------------------------------------------
# Implicit root types: never a false for a member every type has
# ---------------------------------------------------------------------------


class TestImplicitRootMembers:
    async def test_java_object_members_are_true_absent_is_false(self, client):
        files = {"src/a/Plain.java": "package a;\nclass Plain { void run() {} }\n"}
        bodies = {
            "src/a/Plain.java": _nbody(
                _base("Plain", "class", [], 2), _sym("run", "method", 2, "Plain"), language="java"
            )
        }
        with _workspace(files), _symbols(bodies):
            got = {
                m: (await _fn(client, f"Plain.{m}", file_path="src/a/Plain.java")).json()
                for m in ("toString", "equals", "hashCode", "getClass", "clone", "nope")
            }
        for m in ("toString", "equals", "hashCode", "getClass", "clone"):
            assert got[m]["valid"] is True, m
            assert "java.lang.Object" in got[m]["note"]
        assert got["nope"]["valid"] is False

    async def test_java_interface_has_no_protected_object_members(self, client):
        files = {"src/a/I.java": "package a;\ninterface I { }\n"}
        bodies = {"src/a/I.java": _nbody(_base("I", "interface", [], 2), language="java")}
        with _workspace(files), _symbols(bodies):
            t = (await _fn(client, "I.toString", file_path="src/a/I.java")).json()
            c = (await _fn(client, "I.clone", file_path="src/a/I.java")).json()
        assert t["valid"] is True
        assert c["valid"] is not True

    async def test_java_record_accessors_and_enum_statics(self, client):
        files = {
            "src/a/P.java": "package a;\nrecord P(@NotNull Map<String, Integer> m, int... xs) {}\n",
            "src/a/E.java": "package a;\nenum E { A, B }\n",
        }
        bodies = {
            "src/a/P.java": _nbody(_base("P", "record", [], 2), language="java"),
            "src/a/E.java": _nbody(_base("E", "enum", [], 2), language="java"),
        }
        with _workspace(files), _symbols(bodies):
            got = {
                q: (await _fn(client, q, file_path=f"src/a/{q.split('.')[0]}.java")).json()
                for q in ("P.m", "P.xs", "P.equals", "P.zz", "E.values", "E.ordinal", "E.zz")
            }
        for q in ("P.m", "P.xs", "P.equals", "E.values", "E.ordinal"):
            assert got[q]["valid"] is True, q
        assert got["P.zz"]["valid"] is False
        # Enum constants are not extracted: an unknown enum member stays null.
        assert got["E.zz"]["valid"] is None

    async def test_ts_object_prototype_only_when_every_base_resolved(self, client):
        files = {
            "web/base.ts": "export class Base {\n  size() { return 1; }\n}\n",
            "web/a.ts": "import { Base } from './base';\nexport class A extends Base {}\n",
            "web/b.ts": "export class B extends Unknown {}\n",
        }
        bodies = {
            "web/base.ts": _nbody(_sym("Base", "class", 1), _sym("size", "method", 2, "Base")),
            "web/a.ts": _nbody(
                _base("A", "class", ["Base"], 2),
                imports=[{"module": "./base", "names": ["Base"], "line": 1}],
            ),
            "web/b.ts": _nbody(_base("B", "class", ["Unknown"], 1)),
        }
        with _workspace(files), _symbols(bodies):
            a = (await _fn(client, "A.toString", file_path="web/a.ts")).json()
            h = (await _fn(client, "A.hasOwnProperty", file_path="web/a.ts")).json()
            n = (await _fn(client, "A.nope", file_path="web/a.ts")).json()
            b = (await _fn(client, "B.toString", file_path="web/b.ts")).json()
        assert a["valid"] is True
        assert h["valid"] is True
        assert "Object.prototype" in a["note"]
        assert n["valid"] is False
        # An unresolved base may have a null prototype: not proven.
        assert b["valid"] is None

    async def test_csharp_system_object_members_are_true(self, client):
        files = {"src/App/Order.cs": "namespace App;\npublic class Order { }\n"}
        bodies = {"src/App/Order.cs": _nbody(_base("Order", "class", [], 2), language="c_sharp")}
        with _workspace(files), _symbols(bodies):
            r = (await _fn(client, "Order.ToString", file_path="src/App/Order.cs")).json()
        assert r["valid"] is True
        assert "System.Object" in r["note"]

    async def test_python_object_dunders_are_true(self, client):
        files = {
            "pkg/same.py": (
                "class Base:\n    def run(self):\n        pass\n"
                "class C(Base):\n    pass\nclass Plain:\n    pass\n"
            )
        }
        with _workspace(files), _symbols({}):
            got = {
                q: (await _fn(client, q, file_path="pkg/same.py")).json()
                for q in (
                    "C.__init__",
                    "C.__repr__",
                    "Plain.__init__",
                    "Plain.__eq__",
                    "Plain.nope",
                )
            }
            unlocated = (await _fn(client, "Plain.__hash__")).json()
        for q in ("C.__init__", "C.__repr__", "Plain.__init__", "Plain.__eq__"):
            assert got[q]["valid"] is True, (q, got[q]["note"])
        assert got["Plain.nope"]["valid"] is False
        assert unlocated["valid"] is True


# ---------------------------------------------------------------------------
# Python: the base name's LAST binding before the class statement
# ---------------------------------------------------------------------------


class TestPythonLastBinding:
    _BASE = {
        "pkg/__init__.py": "",
        "pkg/base.py": "class Base:\n    def run(self):\n        pass\n",
    }

    async def _run(self, client, source):
        files = {**self._BASE, "pkg/c.py": source}
        with _workspace(files), _symbols({}):
            return (await _fn(client, "C.run", file_path="pkg/c.py")).json()

    async def test_later_import_rebinds_a_local_class(self, client):
        r = await self._run(
            client, "class Base:\n    pass\nfrom pkg.base import Base\nclass C(Base):\n    pass\n"
        )
        assert r["valid"] is True
        assert "pkg/base.py" in r["note"]

    async def test_later_local_class_rebinds_an_import(self, client):
        r = await self._run(
            client, "from pkg.base import Base\nclass Base:\n    pass\nclass C(Base):\n    pass\n"
        )
        assert r["valid"] is False

    async def test_rebinding_after_the_class_statement_does_not_count(self, client):
        r = await self._run(
            client, "from pkg.base import Base\nclass C(Base):\n    pass\nclass Base:\n    pass\n"
        )
        assert r["valid"] is True

    async def test_conditional_binding_is_null(self, client):
        r = await self._run(
            client,
            "try:\n    from pkg.base import Base\nexcept ImportError:\n"
            "    class Base:\n        pass\nclass C(Base):\n    pass\n",
        )
        assert r["valid"] is None

    async def test_non_class_rebinding_is_null(self, client):
        r = await self._run(
            client, "from pkg.base import Base\nBase = make()\nclass C(Base):\n    pass\n"
        )
        assert r["valid"] is None


# ---------------------------------------------------------------------------
# Third review round: unlocated implicit-root members, TS type-alias owners
# ---------------------------------------------------------------------------


class TestThirdRound:
    async def test_unlocated_implicit_root_member_is_true(self, client):
        """No file_path: a member supplied by java.lang.Object is still true.

        The unlocated search collected only definition SITES, so a true with no
        site fell through to "the word is mentioned but not defined" -> False.
        """
        files = {"src/a/Child.java": "package a;\nclass Child { void run() {} }\n"}
        bodies = {
            "src/a/Child.java": _nbody(
                _base("Child", "class", [], 2), _sym("run", "method", 2, "Child"), language="java"
            )
        }
        with _workspace(files), _symbols(bodies):
            t = (await _fn(client, "Child.toString")).json()
            n = (await _fn(client, "Child.nope")).json()
        assert t["valid"] is True
        assert "implicit_root" in t["note"]
        assert "java.lang.Object" in t["note"]
        assert n["valid"] is not True

    async def test_unlocated_python_object_member_says_implicit_root(self, client):
        """No file_path: ``Plain.__repr__`` came back true with "is defined on its
        owner class (not as a def)" (live 2026-10-12), which is not why it is true."""
        files = {
            "gv/dyn.py": "class Plain:\n    def __init__(self):\n        self.size = 3\n",
        }
        with _workspace(files), _symbols({}):
            r = (await _fn(client, "Plain.__repr__")).json()
            a = (await _fn(client, "Plain.size")).json()
        assert r["valid"] is True
        assert "implicit_root" in r["note"]
        assert "not as a `def`" not in r["note"]
        assert a["valid"] is True
        assert "assigned by a method" in a["note"]

    async def test_ts_type_alias_owner_is_never_false(self, client):
        files = {
            "web/base.ts": "export class Base {\n  size() { return 1; }\n}\n",
            "web/t.ts": "import { Base } from './base';\nexport type T = Base & { a: number };\n",
        }
        bodies = {
            "web/base.ts": _nbody(_sym("Base", "class", 1), _sym("size", "method", 2, "Base")),
            "web/t.ts": _nbody(
                _sym("T", "type", 2),
                imports=[{"module": "./base", "names": ["Base"], "line": 1}],
            ),
        }
        with _workspace(files), _symbols(bodies):
            got = {
                m: (await _fn(client, f"T.{m}", file_path="web/t.ts")).json()
                for m in ("size", "a", "nope")
            }
        for m in ("size", "a", "nope"):
            assert got[m]["valid"] is not False, m
