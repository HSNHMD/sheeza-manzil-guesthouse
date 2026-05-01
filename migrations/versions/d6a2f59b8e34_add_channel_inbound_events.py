"""add channel_inbound_events for OTA Modify/Cancel V1

Revision ID: d6a2f59b8e34
Revises: c4f7d2a86b15
Create Date: 2026-05-01 18:00:00.000000

Creates a single new table that gives services.channel_import the
idempotency surface it needs for modification + cancellation events.

Touches no existing tables. Downgrade drops the table cleanly.

Foreign keys:
  - channel_connection_id → channel_connections.id        ON DELETE CASCADE
  - linked_booking_id     → bookings.id                    ON DELETE SET NULL
  - exception_id          → channel_import_exceptions.id   ON DELETE SET NULL

Indexes:
  - created_at                              (recent-first listing)
  - channel_connection_id                   (per-connection event view)
  - external_event_id                       (lookup by OTA event id)
  - external_reservation_ref                (cross-event view per reservation)
  - event_type                              (filter by event class)
  - result_status                           (filter by outcome)
  - linked_booking_id                       (back-link from booking detail)
  - exception_id                            (back-link from exception detail)
  - UNIQUE(channel_connection_id, external_event_id)  — duplicate guard

The composite UNIQUE is what makes the same OTA event arriving twice
land as `result_status='duplicate_skipped'` instead of double-applying
a modification or cancellation.
"""

from alembic import op
import sqlalchemy as sa


revision      = 'd6a2f59b8e34'
down_revision = 'c4f7d2a86b15'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'channel_inbound_events',
        sa.Column('id',         sa.Integer, primary_key=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('channel_connection_id', sa.Integer, nullable=False),
        sa.Column('external_event_id',
                  sa.String(120), nullable=False),
        sa.Column('external_reservation_ref',
                  sa.String(120), nullable=False),
        sa.Column('event_type',    sa.String(40), nullable=False),
        sa.Column('result_status', sa.String(30), nullable=False),
        sa.Column('linked_booking_id', sa.Integer, nullable=True),
        sa.Column('exception_id',      sa.Integer, nullable=True),
        sa.Column('notes',             sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(['channel_connection_id'],
                                ['channel_connections.id'],
                                ondelete='CASCADE',
                                name='fk_chinev_connection'),
        sa.ForeignKeyConstraint(['linked_booking_id'],
                                ['bookings.id'],
                                ondelete='SET NULL',
                                name='fk_chinev_booking'),
        sa.ForeignKeyConstraint(['exception_id'],
                                ['channel_import_exceptions.id'],
                                ondelete='SET NULL',
                                name='fk_chinev_exception'),
        sa.UniqueConstraint('channel_connection_id', 'external_event_id',
                             name='uq_chinev_connection_event'),
    )
    op.create_index('ix_chinev_created_at',
                    'channel_inbound_events', ['created_at'])
    op.create_index('ix_chinev_channel_connection_id',
                    'channel_inbound_events', ['channel_connection_id'])
    op.create_index('ix_chinev_external_event_id',
                    'channel_inbound_events', ['external_event_id'])
    op.create_index('ix_chinev_external_reservation_ref',
                    'channel_inbound_events', ['external_reservation_ref'])
    op.create_index('ix_chinev_event_type',
                    'channel_inbound_events', ['event_type'])
    op.create_index('ix_chinev_result_status',
                    'channel_inbound_events', ['result_status'])
    op.create_index('ix_chinev_linked_booking_id',
                    'channel_inbound_events', ['linked_booking_id'])
    op.create_index('ix_chinev_exception_id',
                    'channel_inbound_events', ['exception_id'])


def downgrade():
    op.drop_index('ix_chinev_exception_id',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_linked_booking_id',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_result_status',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_event_type',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_external_reservation_ref',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_external_event_id',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_channel_connection_id',
                  table_name='channel_inbound_events')
    op.drop_index('ix_chinev_created_at',
                  table_name='channel_inbound_events')
    op.drop_table('channel_inbound_events')
