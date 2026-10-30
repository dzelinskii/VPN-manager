"""add AWG interop fields (container_name, is_external, wipe_detected, external_peers_seen)

Revision ID: c9f3a4b5d6e7
Revises: b8e2f3a4c5d6
Create Date: 2026-10-30 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "c9f3a4b5d6e7"
down_revision = "b8e2f3a4c5d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "awg_services",
        sa.Column(
            "container_name",
            sa.String(length=100),
            nullable=False,
            server_default="vpnbot-awg",
        ),
    )
    op.add_column(
        "awg_services",
        sa.Column("is_external", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "awg_services",
        sa.Column("wipe_detected", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "awg_services",
        sa.Column("external_peers_seen", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("awg_services", "external_peers_seen")
    op.drop_column("awg_services", "wipe_detected")
    op.drop_column("awg_services", "is_external")
    op.drop_column("awg_services", "container_name")
