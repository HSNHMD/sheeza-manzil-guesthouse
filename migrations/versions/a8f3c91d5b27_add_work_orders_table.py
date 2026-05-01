"""add work_orders table for Maintenance V1

Revision ID: a8f3c91d5b27
Revises: 4d8e3c91a76b
Create Date: 2026-05-02 12:00:00.000000

Creates the single new table for Maintenance / Work Orders V1.
Touches no existing tables. Downgrade drops the table cleanly.

Foreign keys:
  - room_id     → rooms.id      ON DELETE SET NULL
  - booking_id  → bookings.id   ON DELETE SET NULL
  - assigned_to_user_id → users.id (no cascade)
  - reported_by_user_id → users.id (no cascade)

Indexes:
  - created_at         (recent-first list query)
  - status             (filter by status)
  - room_id            (per-room work-order lookups from rail badges)
  - assigned_to_user_id (per-staff workload list)
"""

from alembic import op
import sqlalchemy as sa


revision      = 'a8f3c91d5b27'
down_revision = '4d8e3c91a76b'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'work_orders',
        sa.Column('id',         sa.Integer, primary_key=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('room_id',    sa.Integer, nullable=True),
        sa.Column('booking_id', sa.Integer, nullable=True),
        sa.Column('title',       sa.String(160), nullable=False),
        sa.Column('description', sa.Text,        nullable=True),
        sa.Column('category', sa.String(20), nullable=False,
                  server_default='general'),
        sa.Column('priority', sa.String(10), nullable=False,
                  server_default='medium'),
        sa.Column('status',   sa.String(20), nullable=False,
                  server_default='new'),
        sa.Column('assigned_to_user_id', sa.Integer, nullable=True),
        sa.Column('reported_by_user_id', sa.Integer, nullable=True),
        sa.Column('due_date',         sa.Date,     nullable=True),
        sa.Column('resolved_at',      sa.DateTime, nullable=True),
        sa.Column('resolution_notes', sa.String(1000), nullable=True),
        sa.Column('metadata_json',    sa.Text, nullable=True),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id'],
                                ondelete='SET NULL',
                                name='fk_work_orders_room'),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'],
                                ondelete='SET NULL',
                                name='fk_work_orders_booking'),
        sa.ForeignKeyConstraint(['assigned_to_user_id'], ['users.id'],
                                name='fk_work_orders_assigned'),
        sa.ForeignKeyConstraint(['reported_by_user_id'], ['users.id'],
                                name='fk_work_orders_reporter'),
    )
    op.create_index('ix_work_orders_created_at',
                    'work_orders', ['created_at'])
    op.create_index('ix_work_orders_status',
                    'work_orders', ['status'])
    op.create_index('ix_work_orders_room_id',
                    'work_orders', ['room_id'])
    op.create_index('ix_work_orders_assigned_to_user_id',
                    'work_orders', ['assigned_to_user_id'])


def downgrade():
    op.drop_index('ix_work_orders_assigned_to_user_id',
                  table_name='work_orders')
    op.drop_index('ix_work_orders_room_id', table_name='work_orders')
    op.drop_index('ix_work_orders_status',  table_name='work_orders')
    op.drop_index('ix_work_orders_created_at', table_name='work_orders')
    op.drop_table('work_orders')
