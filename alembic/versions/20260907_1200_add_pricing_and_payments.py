"""add price_kopecks and payments table

Revision ID: b8e2f3a4c5d6
Revises: a7d1e2f3b4c5
Create Date: 2026-09-07 12:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "b8e2f3a4c5d6"
down_revision = "a7d1e2f3b4c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription_templates", sa.Column("price_kopecks", sa.Integer(), nullable=True)
    )
    op.add_column("subscriptions", sa.Column("price_kopecks", sa.Integer(), nullable=True))

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("subscription_id", sa.Integer(), nullable=True),
        sa.Column("client_id", sa.Integer(), nullable=True),
        sa.Column("amount_kopecks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="RUB"),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="succeeded"),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        sa.UniqueConstraint("external_id", name="uq_payments_external_id"),
    )


def downgrade() -> None:
    op.drop_table("payments")
    op.drop_column("subscriptions", "price_kopecks")
    op.drop_column("subscription_templates", "price_kopecks")
