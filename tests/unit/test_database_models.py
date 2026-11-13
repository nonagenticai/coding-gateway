"""Tests for models.database — ORM model definitions.

The conftest installs stubs for models.database.  We temporarily replace
the sys.modules entry with the real module, then restore it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

# Load the real models.database module. SQLAlchemy Mapped[] annotations
# require the module to be in sys.modules with its proper name.
_src_dir = os.path.join(os.path.dirname(__file__), "..", "..", "src")
_models_path = os.path.join(_src_dir, "models", "database.py")

# Stash the stub
_stub = sys.modules.get("models.database")

# Register under the real name so Mapped[] resolves
_spec = importlib.util.spec_from_file_location("models.database", os.path.abspath(_models_path))
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["models.database"] = _mod
_spec.loader.exec_module(_mod)

Base = _mod.Base
SchemaVersion = _mod.SchemaVersion
NodeAudit = _mod.NodeAudit
Node = _mod.Node
Edge = _mod.Edge

# Restore the stub so other tests aren't affected
# (We keep references to the real classes above.)
if _stub is not None:
    sys.modules["models.database"] = _stub


class TestBaseModel:
    def test_base_exists(self):
        assert Base is not None
        assert hasattr(Base, "metadata")


class TestSchemaVersionModel:
    def test_tablename(self):
        assert SchemaVersion.__tablename__ == "schema_version"

    def test_columns(self):
        table = SchemaVersion.__table__
        col_names = {c.name for c in table.columns}
        assert "version" in col_names
        assert "description" in col_names
        assert "applied_at" in col_names

    def test_primary_key(self):
        table = SchemaVersion.__table__
        pk_cols = [c.name for c in table.primary_key.columns]
        assert "version" in pk_cols


class TestNodeAuditModel:
    def test_tablename(self):
        assert NodeAudit.__tablename__ == "nodes_audit"

    def test_columns(self):
        table = NodeAudit.__table__
        col_names = {c.name for c in table.columns}
        assert "id" in col_names
        assert "node_id" in col_names
        assert "operation" in col_names
        assert "timestamp" in col_names
        assert "tenant_id" in col_names

    def test_foreign_key(self):
        table = NodeAudit.__table__
        fks = list(table.foreign_keys)
        fk_targets = {str(fk.target_fullname) for fk in fks}
        assert "nodes.id" in fk_targets


class TestNodeModel:
    def test_tablename(self):
        assert Node.__tablename__ == "nodes"

    def test_columns(self):
        table = Node.__table__
        col_names = {c.name for c in table.columns}
        expected = {
            "id",
            "type",
            "properties",
            "created_at",
            "updated_at",
            "content_hash",
            "file_size",
            "tenant_id",
        }
        assert expected.issubset(col_names)

    def test_primary_key(self):
        table = Node.__table__
        pk_cols = [c.name for c in table.primary_key.columns]
        assert "id" in pk_cols

    def test_indexes(self):
        table = Node.__table__
        index_names = {idx.name for idx in table.indexes}
        assert "idx_node_type" in index_names
        assert "idx_node_tenant" in index_names

    def test_relationships_defined(self):
        mapper = Node.__mapper__
        rel_names = {r.key for r in mapper.relationships}
        assert "outgoing_edges" in rel_names
        assert "incoming_edges" in rel_names
        assert "audit_entries" in rel_names


class TestEdgeModel:
    def test_tablename(self):
        assert Edge.__tablename__ == "edges"

    def test_columns(self):
        table = Edge.__table__
        col_names = {c.name for c in table.columns}
        expected = {
            "id",
            "source",
            "target",
            "relationship",
            "properties",
            "weight",
            "created_at",
            "tenant_id",
        }
        assert expected.issubset(col_names)

    def test_primary_key(self):
        table = Edge.__table__
        pk_cols = [c.name for c in table.primary_key.columns]
        assert "id" in pk_cols

    def test_foreign_keys(self):
        table = Edge.__table__
        fk_targets = {str(fk.target_fullname) for fk in table.foreign_keys}
        assert "nodes.id" in fk_targets

    def test_unique_constraint(self):
        table = Edge.__table__
        constraint_names = {c.name for c in table.constraints if c.name}
        assert "uq_edge_source_target_rel" in constraint_names

    def test_indexes(self):
        table = Edge.__table__
        index_names = {idx.name for idx in table.indexes}
        assert "idx_edge_source" in index_names
        assert "idx_edge_target" in index_names
        assert "idx_edge_tenant" in index_names

    def test_relationships_defined(self):
        mapper = Edge.__mapper__
        rel_names = {r.key for r in mapper.relationships}
        assert "source_node" in rel_names
        assert "target_node" in rel_names
