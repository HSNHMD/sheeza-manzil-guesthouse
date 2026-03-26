"""add notes to room

Revision ID: e4f7a2b1c8d3
Revises: ddc320fae194
Create Date: 2026-03-27 01:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e4f7a2b1c8d3'
down_revision = 'ddc320fae194'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('rooms', sa.Column('notes', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('rooms', 'notes')
