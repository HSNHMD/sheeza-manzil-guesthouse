"""add Booking Engine V2 holds table + hold TTL settings

Revision ID: b1e0f2a4c6d8
Revises: d6a2f59b8e34
Create Date: 2026-07-27 00:00:00.000000

Booking Engine V2 Phase 1 — inventory/holds core. ADDITIVE only.

Creates one new table `holds` (selection + pending holds as first-class,
type-level records — no room assignment; assignment happens at confirmation)
and adds two nullable-with-server-default TTL columns to `property_settings`.

Touches no existing data. Downgrade drops the table + columns cleanly.

Design notes:
  - `holds` is type-level (room_type_id, qty) — never room-specific. It is the
    "selection hold" (15m) and "pending hold" (6h) surface. Expiry is a STATE
    transition (state -> 'expired'), never a delete.
  - No stored availability counters: `holds` records intent, not a cached count.
    sellable() computes live from rooms − OOO − assigned − held − pending.
  - property_id carries a server_default of '1' (multi-property wave-1 convention).

Foreign keys:
  - room_type_id            → room_types.id       ON DELETE CASCADE
  - booking_group_id        → booking_groups.id   ON DELETE SET NULL
  - lead_guest_id           → guests.id           ON DELETE SET NULL
  - created_by_user_id      → users.id            ON DELETE SET NULL
  - released_by_user_id     → users.id            ON DELETE SET NULL
  - converted_group_id      → booking_groups.id   ON DELETE SET NULL
"""

from alembic import op
import sqlalchemy as sa


revision      = 'b1e0f2a4c6d8'
down_revision = 'd6a2f59b8e34'
branch_labels = None
depends_on    = None


def upgrade():
    op.create_table(
        'holds',
        sa.Column('id',          sa.Integer, primary_key=True),
        sa.Column('property_id', sa.Integer, nullable=False, server_default='1'),
        sa.Column('created_at',  sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at',  sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column('room_type_id', sa.Integer, nullable=False),
        sa.Column('qty',          sa.Integer, nullable=False, server_default='1'),
        sa.Column('check_in_date',  sa.Date, nullable=False),
        sa.Column('check_out_date', sa.Date, nullable=False),
        # 'selection' (stage 1, 15m) | 'pending' (stage 2, 6h)
        sa.Column('hold_type', sa.String(20), nullable=False),
        # 'active' | 'converted' | 'released' | 'expired'
        sa.Column('state',     sa.String(20), nullable=False, server_default='active'),
        sa.Column('expires_at', sa.DateTime, nullable=False),
        # selection holds are tied to a browser session; pending holds carry lead guest/contact
        sa.Column('session_token', sa.String(64), nullable=True),
        sa.Column('guest_name',    sa.String(160), nullable=True),
        sa.Column('contact',       sa.String(120), nullable=True),
        sa.Column('lead_guest_id', sa.Integer, nullable=True),
        # multi-type holds (2x Standard + 1x Deluxe) share one intent group
        sa.Column('booking_group_id', sa.Integer, nullable=True),
        # lifecycle bookkeeping (never delete)
        sa.Column('released_reason', sa.String(255), nullable=True),
        sa.Column('released_at',     sa.DateTime, nullable=True),
        sa.Column('released_by_user_id', sa.Integer, nullable=True),
        sa.Column('converted_group_id',  sa.Integer, nullable=True),
        sa.Column('converted_at',        sa.DateTime, nullable=True),
        sa.Column('created_by_user_id',  sa.Integer, nullable=True),
        sa.ForeignKeyConstraint(['room_type_id'], ['room_types.id'],
                                ondelete='CASCADE', name='fk_holds_room_type'),
        sa.ForeignKeyConstraint(['booking_group_id'], ['booking_groups.id'],
                                ondelete='SET NULL', name='fk_holds_group'),
        sa.ForeignKeyConstraint(['lead_guest_id'], ['guests.id'],
                                ondelete='SET NULL', name='fk_holds_lead_guest'),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'],
                                ondelete='SET NULL', name='fk_holds_created_by'),
        sa.ForeignKeyConstraint(['released_by_user_id'], ['users.id'],
                                ondelete='SET NULL', name='fk_holds_released_by'),
        sa.ForeignKeyConstraint(['converted_group_id'], ['booking_groups.id'],
                                ondelete='SET NULL', name='fk_holds_converted_group'),
    )
    # sellable() filters on (state, hold_type, room_type_id, date range) hot path
    op.create_index('ix_holds_state', 'holds', ['state'])
    op.create_index('ix_holds_type_state', 'holds', ['room_type_id', 'state'])
    op.create_index('ix_holds_expires_at', 'holds', ['expires_at'])
    op.create_index('ix_holds_session_token', 'holds', ['session_token'])
    op.create_index('ix_holds_group', 'holds', ['booking_group_id'])
    op.create_index('ix_holds_dates', 'holds', ['check_in_date', 'check_out_date'])

    # Hold TTLs live in PropertySettings (config, not hardcoded). server_default
    # so existing singleton row picks up 15m / 6h without a data migration.
    op.add_column('property_settings',
                  sa.Column('selection_hold_ttl_minutes', sa.Integer,
                            nullable=False, server_default='15'))
    op.add_column('property_settings',
                  sa.Column('pending_hold_ttl_hours', sa.Integer,
                            nullable=False, server_default='6'))


def downgrade():
    op.drop_column('property_settings', 'pending_hold_ttl_hours')
    op.drop_column('property_settings', 'selection_hold_ttl_minutes')
    op.drop_index('ix_holds_dates', table_name='holds')
    op.drop_index('ix_holds_group', table_name='holds')
    op.drop_index('ix_holds_session_token', table_name='holds')
    op.drop_index('ix_holds_expires_at', table_name='holds')
    op.drop_index('ix_holds_type_state', table_name='holds')
    op.drop_index('ix_holds_state', table_name='holds')
    op.drop_table('holds')
