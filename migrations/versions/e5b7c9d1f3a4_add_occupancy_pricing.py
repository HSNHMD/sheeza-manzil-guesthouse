"""add base_occupancy + extra_person_fee to room_types

Revision ID: e5b7c9d1f3a4
Revises: d3a5b7c9e1f2
Create Date: 2026-07-02 09:00:00.000000

BEv2 occupancy pricing. ADDITIVE, reversible.

  - room_types.base_occupancy   — guests included in the room rate (default 2).
  - room_types.extra_person_fee — MVR/night per guest above base (default 0,
    so pre-existing rows charge nothing until an owner sets a value).

max_occupancy already exists and now means the HARD cap per room. See
app/services/occupancy.py for the capacity + cheapest-distribution fee math.
"""

from alembic import op
import sqlalchemy as sa


revision      = 'e5b7c9d1f3a4'
down_revision = 'd3a5b7c9e1f2'
branch_labels = None
depends_on    = None


def upgrade():
    op.add_column('room_types', sa.Column('base_occupancy', sa.Integer,
                                          nullable=False, server_default='2'))
    op.add_column('room_types', sa.Column('extra_person_fee', sa.Float,
                                          nullable=False, server_default='0'))


def downgrade():
    op.drop_column('room_types', 'extra_person_fee')
    op.drop_column('room_types', 'base_occupancy')
