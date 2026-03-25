"""add drive_id columns for Google Drive file references

Revision ID: ddc320fae194
Revises: a21b045cc4b5
Create Date: 2026-03-25 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'ddc320fae194'
down_revision = 'a21b045cc4b5'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bookings', sa.Column('id_card_drive_id', sa.String(length=255), nullable=True))
    op.add_column('bookings', sa.Column('payment_slip_drive_id', sa.String(length=255), nullable=True))
    op.add_column('expenses', sa.Column('receipt_drive_id', sa.String(length=255), nullable=True))


def downgrade():
    op.drop_column('expenses', 'receipt_drive_id')
    op.drop_column('bookings', 'payment_slip_drive_id')
    op.drop_column('bookings', 'id_card_drive_id')
