"""Rewrite gateway-owned tables to match current ORM.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-24

The ORM in src/models/database.py was fully rewritten but the original
migrations (0001/0002) were edited in-place instead of adding a new
migration.  Production still has the OLD schema (e.g. nodes with
node_id/node_type/name/file_path/… columns, edges with source_id/
target_id/edge_type/… columns, no nodes_audit table).

This migration drops the four gateway-owned tables and recreates them
to match the current ORM exactly.  Other tables in the database
(analysis_sessions, api_keys, etc.) are left untouched.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Drop OLD tables in dependency order.
    # Use if_exists=True so this migration is idempotent (e.g. if
    # nodes_audit never existed in production).
    # ------------------------------------------------------------------

    # 1. nodes_audit  (FK → nodes)
    op.execute("DROP TABLE IF EXISTS nodes_audit CASCADE")

    # 2. edges  (FKs → nodes)
    op.execute("DROP TABLE IF EXISTS edges CASCADE")

    # 3. nodes
    op.execute("DROP TABLE IF EXISTS nodes CASCADE")

    # 4. schema_version  (standalone)
    op.execute("DROP TABLE IF EXISTS schema_version CASCADE")

    # ------------------------------------------------------------------
    # Recreate tables matching current ORM
    # ------------------------------------------------------------------

    # -- schema_version -------------------------------------------------------
    op.create_table(
        "schema_version",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # -- nodes ----------------------------------------------------------------
    op.create_table(
        "nodes",
        sa.Column("id", sa.String(512), primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("properties", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("tenant_id", sa.String(128), nullable=True),
    )

    op.create_index("idx_node_type", "nodes", ["type"])
    op.create_index("idx_node_created_at", "nodes", ["created_at"])
    op.create_index("idx_node_type_created", "nodes", ["type", "created_at"])
    op.create_index("idx_node_content_hash", "nodes", ["content_hash"])
    op.create_index("idx_node_tenant", "nodes", ["tenant_id"])
    op.create_index("idx_node_tenant_type", "nodes", ["tenant_id", "type"])

    # -- edges ----------------------------------------------------------------
    op.create_table(
        "edges",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "source",
            sa.String(512),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "target",
            sa.String(512),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("relationship", sa.String(64), nullable=False),
        sa.Column("properties", JSONB(), nullable=True),
        sa.Column("weight", sa.Float(), default=1.0),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=True),
        sa.UniqueConstraint("source", "target", "relationship", name="uq_edge_source_target_rel"),
    )

    op.create_index("idx_edge_source", "edges", ["source"])
    op.create_index("idx_edge_target", "edges", ["target"])
    op.create_index("idx_edge_relationship", "edges", ["relationship"])
    op.create_index("idx_edge_source_rel", "edges", ["source", "relationship"])
    op.create_index("idx_edge_target_rel", "edges", ["target", "relationship"])
    op.create_index("idx_edge_tenant", "edges", ["tenant_id"])
    op.create_index("idx_edge_tenant_source", "edges", ["tenant_id", "source"])

    # -- nodes_audit ----------------------------------------------------------
    op.create_table(
        "nodes_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "node_id",
            sa.String(512),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("operation", sa.String(32), nullable=True),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    # Downgrade drops the new-schema tables.  It does NOT recreate the
    # old schema — going back to 0002's original columns would require
    # knowledge of the pre-rewrite column set which is not kept here.
    op.drop_table("nodes_audit")

    op.drop_index("idx_edge_tenant_source", table_name="edges")
    op.drop_index("idx_edge_tenant", table_name="edges")
    op.drop_index("idx_edge_target_rel", table_name="edges")
    op.drop_index("idx_edge_source_rel", table_name="edges")
    op.drop_index("idx_edge_relationship", table_name="edges")
    op.drop_index("idx_edge_target", table_name="edges")
    op.drop_index("idx_edge_source", table_name="edges")
    op.drop_table("edges")

    op.drop_index("idx_node_tenant_type", table_name="nodes")
    op.drop_index("idx_node_tenant", table_name="nodes")
    op.drop_index("idx_node_content_hash", table_name="nodes")
    op.drop_index("idx_node_type_created", table_name="nodes")
    op.drop_index("idx_node_created_at", table_name="nodes")
    op.drop_index("idx_node_type", table_name="nodes")
    op.drop_table("nodes")

    op.drop_table("schema_version")
