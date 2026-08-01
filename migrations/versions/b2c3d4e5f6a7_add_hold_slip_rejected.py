"""add Hold.slip_rejected_at / slip_rejected_reason — Pepper soft-reject

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-01 08:00:00.000000

Additive: two NULLABLE columns on holds so the bot's ❌ Reject can mark a slip
rejected (with a reason) WITHOUT releasing the hold — it stays active on its
normal expiry and the guest can re-upload. Reverting leaves the columns
harmless/unused (or drops them on downgrade).
"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('holds', sa.Column('slip_rejected_at', sa.DateTime(), nullable=True))
    op.add_column('holds', sa.Column('slip_rejected_reason', sa.String(length=255),
                                     nullable=True))


def downgrade():
    op.drop_column('holds', 'slip_rejected_reason')
    op.drop_column('holds', 'slip_rejected_at')
