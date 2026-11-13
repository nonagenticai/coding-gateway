"""Baseline - create all gateway-owned tables.

Revision ID: 0001
Revises: None
Create Date: 2026-03-04

Creates: schema_version, nodes, edges, nodes_audit
Matches ORM in src/models/database.py exactly.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
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

    # Indexes matching Node.__table_args__
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

    # Indexes matching Edge.__table_args__
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
    # Drop in reverse dependency order: audit → edges → nodes → schema_version
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
