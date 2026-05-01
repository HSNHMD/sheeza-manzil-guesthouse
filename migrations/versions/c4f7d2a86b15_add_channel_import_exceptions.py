"""add channel_import_exceptions for OTA Reservation Import V1

Revision ID: c4f7d2a86b15
Revises: a8f3c91d5b27
Create Date: 2026-05-01 17:30:00.000000

Creates the manual-review queue table for OTA reservation imports
that couldn't be auto-applied (mapping missing, date conflict,
invalid payload, parse error). Touches no existing tables.
Downgrade drops the table cleanly.

Foreign keys:
  - channel_connection_id → channel_connections.id  ON DELETE CASCADE
  - linked_booking_id     → bookings.id              ON DELETE SET NULL
  - reviewed_by_user_id   → users.id                 ON DELETE SET NULL

Indexes:
  - created_at                    (recent-first list)
  - status                        (filter by lifecycle state)
  - issue_type                    (filter by issue category)
  - channel_connection_id         (per-channel queue view)
  - external_source               (cross-channel lookup)
  - external_reservation_ref      (find exception by OTA ref)
  - linked_booking_id             (back-link from booking detail)
"""

from alembic import op
import sqlalchemy as sa


revision      = 'c4f7d2a86b15'
down_revision = 'a8f3c91d5b27'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'channel_import_exceptions',
        sa.Column('id',         sa.Integer, primary_key=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('channel_connection_id', sa.Integer, nullable=False),
        sa.Column('external_source',
                  sa.String(30), nullable=False),
        sa.Column('external_reservation_ref',
                  sa.String(120), nullable=False),
        sa.Column('issue_type',
                  sa.String(30), nullable=False),
        sa.Column('suggested_action',
                  sa.String(500), nullable=True),
        sa.Column('payload_summary',
                  sa.String(2000), nullable=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='new'),
        sa.Column('linked_booking_id',   sa.Integer, nullable=True),
        sa.Column('reviewed_by_user_id', sa.Integer, nullable=True),
        sa.Column('reviewed_at',         sa.DateTime, nullable=True),
        sa.Column('notes',               sa.String(1000), nullable=True),
        sa.ForeignKeyConstraint(['channel_connection_id'],
                                ['channel_connections.id'],
                                ondelete='CASCADE',
                                name='fk_chiex_connection'),
        sa.ForeignKeyConstraint(['linked_booking_id'],
                                ['bookings.id'],
                                ondelete='SET NULL',
                                name='fk_chiex_booking'),
        sa.ForeignKeyConstraint(['reviewed_by_user_id'],
                                ['users.id'],
                                ondelete='SET NULL',
                                name='fk_chiex_reviewer'),
    )
    op.create_index('ix_chiex_created_at',
                    'channel_import_exceptions', ['created_at'])
    op.create_index('ix_chiex_status',
                    'channel_import_exceptions', ['status'])
    op.create_index('ix_chiex_issue_type',
                    'channel_import_exceptions', ['issue_type'])
    op.create_index('ix_chiex_channel_connection_id',
                    'channel_import_exceptions', ['channel_connection_id'])
    op.create_index('ix_chiex_external_source',
                    'channel_import_exceptions', ['external_source'])
    op.create_index('ix_chiex_external_reservation_ref',
                    'channel_import_exceptions', ['external_reservation_ref'])
    op.create_index('ix_chiex_linked_booking_id',
                    'channel_import_exceptions', ['linked_booking_id'])


def downgrade():
    op.drop_index('ix_chiex_linked_booking_id',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_external_reservation_ref',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_external_source',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_channel_connection_id',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_issue_type',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_status',
                  table_name='channel_import_exceptions')
    op.drop_index('ix_chiex_created_at',
                  table_name='channel_import_exceptions')
    op.drop_table('channel_import_exceptions')
