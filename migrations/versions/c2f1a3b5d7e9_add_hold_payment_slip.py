"""add payment-slip storage to holds (BEv2 Phase 2)

Revision ID: c2f1a3b5d7e9
Revises: b1e0f2a4c6d8
Create Date: 2026-07-27 04:00:00.000000

Kairos review item — the public portal lets a guest upload a payment slip
while the booking is still a pending HOLD (before the Booking exists). Store
the slip on the hold (filename + R2 drive_id, same dual-write as bookings);
it transfers to the booking/group at confirmation. ADDITIVE, reversible.
"""

from alembic import op
import sqlalchemy as sa


revision      = 'c2f1a3b5d7e9'
down_revision = 'b1e0f2a4c6d8'
branch_labels = None
depends_on    = None


def upgrade():
    op.add_column('holds', sa.Column('payment_slip_filename',
                                     sa.String(255), nullable=True))
    op.add_column('holds', sa.Column('payment_slip_drive_id',
                                     sa.String(255), nullable=True))


def downgrade():
    op.drop_column('holds', 'payment_slip_drive_id')
    op.drop_column('holds', 'payment_slip_filename')
