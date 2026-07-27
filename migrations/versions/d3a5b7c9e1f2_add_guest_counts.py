"""add adults/children guest counts to holds, bookings, booking_groups

Revision ID: d3a5b7c9e1f2
Revises: c2f1a3b5d7e9
Create Date: 2026-07-27 07:00:00.000000

BEv2 portal guest-count (adults + children). ADDITIVE, reversible.

  - holds: per-GROUP totals captured at the guest-details step (server_default
    adults=1 / children=0 for any pre-existing rows).
  - bookings: for a plain single booking (per-group split is deferred — see the
    forward-compat note in services.holds). Nullable — legacy bookings only
    carry num_guests.
  - booking_groups: the per-group totals for a multi-room group (nullable).

Consumers of this data (do NOT simplify away): Green Tax (per-guest-per-night,
child exemptions) and immigration reporting.
"""

from alembic import op
import sqlalchemy as sa


revision      = 'd3a5b7c9e1f2'
down_revision = 'c2f1a3b5d7e9'
branch_labels = None
depends_on    = None


def upgrade():
    op.add_column('holds', sa.Column('adults', sa.Integer, nullable=False,
                                     server_default='1'))
    op.add_column('holds', sa.Column('children', sa.Integer, nullable=False,
                                     server_default='0'))
    op.add_column('bookings', sa.Column('adults', sa.Integer, nullable=True))
    op.add_column('bookings', sa.Column('children', sa.Integer, nullable=True))
    op.add_column('booking_groups', sa.Column('adults', sa.Integer, nullable=True))
    op.add_column('booking_groups', sa.Column('children', sa.Integer, nullable=True))


def downgrade():
    op.drop_column('booking_groups', 'children')
    op.drop_column('booking_groups', 'adults')
    op.drop_column('bookings', 'children')
    op.drop_column('bookings', 'adults')
    op.drop_column('holds', 'children')
    op.drop_column('holds', 'adults')
