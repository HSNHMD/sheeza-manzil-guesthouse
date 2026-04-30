"""add Stay Segments table (mid-stay room change foundation)

Revision ID: 3f7b1c8e2a04
Revises: 2e8c4d7a3f51
Create Date: 2026-04-30 23:00:00.000000

Foundation for the "mid-stay room change / split stay" UX. Adds a
`stay_segments` table that lets one Booking carry an ordered sequence
of (room_id, start_date, end_date) ranges. The Booking stays the
single source of truth for guest, total dates, folio, payments —
StaySegment rows are an additive overlay describing WHICH room hosts
the guest at WHICH point of the stay.

V1 stops at the schema. The board still renders by Booking.room_id;
the new `flask staging` CLI does not seed segments. Segment-aware
rendering is scheduled for the next sprint. Adding the table now lets
us ship the split-stay backend service + endpoint without a second
migration round-trip.

Reverting drops the table cleanly — there are no FK references TO
stay_segments yet, so this migration is fully reversible.
"""

from alembic import op
import sqlalchemy as sa


revision      = '3f7b1c8e2a04'
down_revision = '2e8c4d7a3f51'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'stay_segments',
        sa.Column('id',         sa.Integer, primary_key=True),
        sa.Column('booking_id', sa.Integer, nullable=False),
        sa.Column('room_id',    sa.Integer, nullable=False),
        sa.Column('start_date', sa.Date,    nullable=False),
        sa.Column('end_date',   sa.Date,    nullable=False),
        sa.Column('notes',      sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime, nullable=False,
                  server_default=sa.func.now()),
        sa.Column('created_by_user_id', sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(['booking_id'], ['bookings.id'],
                                ondelete='CASCADE',
                                name='fk_stay_segments_booking'),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id'],
                                ondelete='RESTRICT',
                                name='fk_stay_segments_room'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'],
                                name='fk_stay_segments_creator'),
    )
    op.create_index('ix_stay_segments_booking_id',
                    'stay_segments', ['booking_id'])
    op.create_index('ix_stay_segments_room_id',
                    'stay_segments', ['room_id'])
    # Composite index for the per-room overlap query that conflict
    # checks will use once segment-aware rendering lands.
    op.create_index('ix_stay_segments_room_dates',
                    'stay_segments',
                    ['room_id', 'start_date', 'end_date'])


def downgrade():
    op.drop_index('ix_stay_segments_room_dates',  table_name='stay_segments')
    op.drop_index('ix_stay_segments_room_id',     table_name='stay_segments')
    op.drop_index('ix_stay_segments_booking_id',  table_name='stay_segments')
    op.drop_table('stay_segments')
