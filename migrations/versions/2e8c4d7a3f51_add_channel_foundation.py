"""add Channel Manager Foundation V1

Revision ID: 2e8c4d7a3f51
Revises: 1d9b6a4f5e72
Create Date: 2026-04-30 21:00:00.000000

Phase 1 of the channel manager build documented in
docs/channel_manager_build_phases.md. ZERO real OTA traffic in this
phase — these are purely internal data + workflow scaffolding tables.

Steps:
  1. Add 3 columns to `bookings`:
       source                    (NOT NULL, server_default='direct')
       external_source           (nullable)
       external_reservation_ref  (nullable)
     + composite unique index `(external_source, external_reservation_ref)`
       (partial — applies only when both columns are non-null;
       PostgreSQL only).
  2. Backfill `source='direct'` for every existing booking via
     server_default; UPDATE explicit just to be safe.
  3. Create 5 new tables:
       channel_connections
       channel_room_maps
       channel_rate_plan_maps
       channel_sync_jobs
       channel_sync_logs

The unique-on-non-null index is the **anti-duplicate-import** guard.
Two OTA-imported bookings with the same external_source +
external_reservation_ref pair cannot coexist.

Hand-written. Touches no other tables. Downgrade reverses cleanly.
"""
from alembic import op
import sqlalchemy as sa


revision = '2e8c4d7a3f51'
down_revision = '1d9b6a4f5e72'
branch_labels = None
depends_on = None


def _is_postgres():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    # ── 1. Booking columns ─────────────────────────────────────
    op.add_column('bookings',
                  sa.Column('source', sa.String(30), nullable=False,
                            server_default='direct'))
    op.add_column('bookings',
                  sa.Column('external_source', sa.String(30),
                            nullable=True))
    op.add_column('bookings',
                  sa.Column('external_reservation_ref', sa.String(120),
                            nullable=True))
    op.create_index('ix_bookings_source', 'bookings', ['source'])
    op.create_index('ix_bookings_external_source',
                    'bookings', ['external_source'])
    op.create_index('ix_bookings_external_reservation_ref',
                    'bookings', ['external_reservation_ref'])

    # 2. Backfill (server_default already covers new rows; this is the
    #    safety net for any backend that doesn't apply server_default
    #    on existing rows during ADD COLUMN).
    op.execute(sa.text(
        "UPDATE bookings SET source = 'direct' "
        "WHERE source IS NULL OR source = ''"
    ))

    # Composite unique on the (external_source, external_reservation_ref)
    # pair — anti-duplicate-import guard. Postgres can do this as a
    # partial index (only applies when both columns are non-NULL,
    # which is what we want — a regular UNIQUE would forbid two
    # bookings with both NULLs). SQLite test DB skips.
    if _is_postgres():
        op.create_index(
            'uq_bookings_external_source_ref',
            'bookings',
            ['external_source', 'external_reservation_ref'],
            unique=True,
            postgresql_where=sa.text(
                'external_source IS NOT NULL '
                'AND external_reservation_ref IS NOT NULL'),
        )

    # ── 3. New tables ──────────────────────────────────────────
    op.create_table(
        'channel_connections',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('property_id', sa.Integer(),
                  sa.ForeignKey('properties.id',
                                name='fk_channel_conn_property',
                                ondelete='RESTRICT'),
                  nullable=False, server_default='1'),
        sa.Column('channel_name', sa.String(40), nullable=False),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='inactive'),
        sa.Column('account_label', sa.String(160), nullable=True),
        sa.Column('config_json', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.UniqueConstraint('property_id', 'channel_name',
                             name='uq_channel_connection_property_channel'),
    )
    op.create_index('ix_channel_connections_property_id',
                    'channel_connections', ['property_id'])
    op.create_index('ix_channel_connections_channel_name',
                    'channel_connections', ['channel_name'])
    op.create_index('ix_channel_connections_status',
                    'channel_connections', ['status'])

    op.create_table(
        'channel_room_maps',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('channel_connection_id', sa.Integer(),
                  sa.ForeignKey('channel_connections.id',
                                name='fk_channel_room_map_conn',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('room_type_id', sa.Integer(),
                  sa.ForeignKey('room_types.id',
                                name='fk_channel_room_map_type',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('external_room_id', sa.String(80), nullable=False),
        sa.Column('external_room_name_snapshot', sa.String(160),
                  nullable=True),
        sa.Column('inventory_count_override', sa.Integer(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.UniqueConstraint('channel_connection_id', 'room_type_id',
                             name='uq_channel_room_map_conn_type'),
        sa.UniqueConstraint('channel_connection_id', 'external_room_id',
                             name='uq_channel_room_map_conn_external'),
    )
    op.create_index('ix_channel_room_maps_conn',
                    'channel_room_maps', ['channel_connection_id'])
    op.create_index('ix_channel_room_maps_type',
                    'channel_room_maps', ['room_type_id'])

    op.create_table(
        'channel_rate_plan_maps',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('channel_connection_id', sa.Integer(),
                  sa.ForeignKey('channel_connections.id',
                                name='fk_channel_rate_map_conn',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('rate_plan_id', sa.Integer(),
                  sa.ForeignKey('rate_plans.id',
                                name='fk_channel_rate_map_plan',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('external_rate_plan_id', sa.String(80), nullable=False),
        sa.Column('external_rate_plan_name_snapshot', sa.String(160),
                  nullable=True),
        sa.Column('meal_plan_external_id', sa.String(40), nullable=True),
        sa.Column('cancellation_policy_external_id', sa.String(40),
                  nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.UniqueConstraint('channel_connection_id', 'rate_plan_id',
                             name='uq_channel_rate_map_conn_plan'),
        sa.UniqueConstraint('channel_connection_id',
                             'external_rate_plan_id',
                             name='uq_channel_rate_map_conn_external'),
    )
    op.create_index('ix_channel_rate_plan_maps_conn',
                    'channel_rate_plan_maps', ['channel_connection_id'])
    op.create_index('ix_channel_rate_plan_maps_plan',
                    'channel_rate_plan_maps', ['rate_plan_id'])

    op.create_table(
        'channel_sync_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('channel_connection_id', sa.Integer(),
                  sa.ForeignKey('channel_connections.id',
                                name='fk_sync_job_conn',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('job_type', sa.String(40), nullable=False),
        sa.Column('direction', sa.String(10), nullable=False),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='queued'),
        sa.Column('payload_summary', sa.String(500), nullable=True),
        sa.Column('error_summary', sa.String(500), nullable=True),
        sa.Column('attempt_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('started_at', sa.DateTime(), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('requested_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id',
                                name='fk_sync_job_user',
                                ondelete='SET NULL'),
                  nullable=True),
    )
    op.create_index('ix_channel_sync_jobs_created_at',
                    'channel_sync_jobs', ['created_at'])
    op.create_index('ix_channel_sync_jobs_conn',
                    'channel_sync_jobs', ['channel_connection_id'])
    op.create_index('ix_channel_sync_jobs_status',
                    'channel_sync_jobs', ['status'])

    op.create_table(
        'channel_sync_logs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('channel_connection_id', sa.Integer(),
                  sa.ForeignKey('channel_connections.id',
                                name='fk_sync_log_conn',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('sync_job_id', sa.Integer(),
                  sa.ForeignKey('channel_sync_jobs.id',
                                name='fk_sync_log_job',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('entity_type', sa.String(30), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('direction', sa.String(10), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('message', sa.String(500), nullable=True),
    )
    op.create_index('ix_channel_sync_logs_created_at',
                    'channel_sync_logs', ['created_at'])
    op.create_index('ix_channel_sync_logs_conn',
                    'channel_sync_logs', ['channel_connection_id'])
    op.create_index('ix_channel_sync_logs_job',
                    'channel_sync_logs', ['sync_job_id'])


def downgrade():
    op.drop_index('ix_channel_sync_logs_job',
                  table_name='channel_sync_logs')
    op.drop_index('ix_channel_sync_logs_conn',
                  table_name='channel_sync_logs')
    op.drop_index('ix_channel_sync_logs_created_at',
                  table_name='channel_sync_logs')
    op.drop_table('channel_sync_logs')

    op.drop_index('ix_channel_sync_jobs_status',
                  table_name='channel_sync_jobs')
    op.drop_index('ix_channel_sync_jobs_conn',
                  table_name='channel_sync_jobs')
    op.drop_index('ix_channel_sync_jobs_created_at',
                  table_name='channel_sync_jobs')
    op.drop_table('channel_sync_jobs')

    op.drop_index('ix_channel_rate_plan_maps_plan',
                  table_name='channel_rate_plan_maps')
    op.drop_index('ix_channel_rate_plan_maps_conn',
                  table_name='channel_rate_plan_maps')
    op.drop_table('channel_rate_plan_maps')

    op.drop_index('ix_channel_room_maps_type',
                  table_name='channel_room_maps')
    op.drop_index('ix_channel_room_maps_conn',
                  table_name='channel_room_maps')
    op.drop_table('channel_room_maps')

    op.drop_index('ix_channel_connections_status',
                  table_name='channel_connections')
    op.drop_index('ix_channel_connections_channel_name',
                  table_name='channel_connections')
    op.drop_index('ix_channel_connections_property_id',
                  table_name='channel_connections')
    op.drop_table('channel_connections')

    if _is_postgres():
        try:
            op.drop_index('uq_bookings_external_source_ref',
                          table_name='bookings')
        except Exception:
            pass
    op.drop_index('ix_bookings_external_reservation_ref',
                  table_name='bookings')
    op.drop_index('ix_bookings_external_source',
                  table_name='bookings')
    op.drop_index('ix_bookings_source',
                  table_name='bookings')
    op.drop_column('bookings', 'external_reservation_ref')
    op.drop_column('bookings', 'external_source')
    op.drop_column('bookings', 'source')
