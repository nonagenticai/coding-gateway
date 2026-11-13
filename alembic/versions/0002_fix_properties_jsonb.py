"""No-op: properties columns are already JSONB in 0001_gateway_baseline.

This migration previously existed to fix a mismatch where properties
columns might have been created as TEXT/JSON instead of JSONB. Since
0001_gateway_baseline now creates both nodes.properties and
edges.properties as JSONB from the start, this migration is a no-op.

Kept to preserve the revision chain (0001 -> 0002).
"""

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nothing to do -- 0001 already creates properties columns as JSONB.
    pass


def downgrade() -> None:
    pass
