"""SQLAlchemy ORM models for gateway-owned tables.

These models mirror the SQLite graph-database tables so the gateway
can work with PostgreSQL through SQLAlchemy while keeping the same
logical schema.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Shared declarative base for all gateway ORM models."""

    pass


class SchemaVersion(Base):
    """Tracks applied schema migrations."""

    __tablename__ = "schema_version"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )


class NodeAudit(Base):
    """Audit log for node operations."""

    __tablename__ = "nodes_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str | None] = mapped_column(
        String(512),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=True,
    )
    operation: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class Node(Base):
    """A code entity stored in the graph."""

    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    properties: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Relationships
    outgoing_edges: Mapped[list[Edge]] = relationship(
        "Edge",
        foreign_keys="Edge.source",
        back_populates="source_node",
        cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[list[Edge]] = relationship(
        "Edge",
        foreign_keys="Edge.target",
        back_populates="target_node",
        cascade="all, delete-orphan",
    )
    audit_entries: Mapped[list[NodeAudit]] = relationship(
        "NodeAudit",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_node_type", "type"),
        Index("idx_node_created_at", "created_at"),
        Index("idx_node_type_created", "type", "created_at"),
        Index("idx_node_content_hash", "content_hash"),
        Index("idx_node_tenant", "tenant_id"),
        Index("idx_node_tenant_type", "tenant_id", "type"),
    )


class Edge(Base):
    """A relationship between two code entities."""

    __tablename__ = "edges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(
        String(512),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    target: Mapped[str] = mapped_column(
        String(512),
        ForeignKey("nodes.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(
        "relationship",
        String(64),
        nullable=False,
    )
    properties: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Relationships
    source_node: Mapped[Node] = relationship(
        "Node",
        foreign_keys=[source],
        back_populates="outgoing_edges",
    )
    target_node: Mapped[Node] = relationship(
        "Node",
        foreign_keys=[target],
        back_populates="incoming_edges",
    )

    __table_args__ = (
        UniqueConstraint("source", "target", "relationship", name="uq_edge_source_target_rel"),
        Index("idx_edge_source", "source"),
        Index("idx_edge_target", "target"),
        Index("idx_edge_relationship", "relationship"),
        Index("idx_edge_source_rel", "source", "relationship"),
        Index("idx_edge_target_rel", "target", "relationship"),
        Index("idx_edge_tenant", "tenant_id"),
        Index("idx_edge_tenant_source", "tenant_id", "source"),
    )
