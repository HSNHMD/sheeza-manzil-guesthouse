"""add rates & inventory v1 tables

Revision ID: c5d2a3f8e103
Revises: b4c1f2d6e892
Create Date: 2026-04-30 09:00:00.000000

Creates the four Rates & Inventory V1 tables:

  - room_types       — sellable room category catalog
  - rate_plans       — sellable rate plans (FK room_types)
  - rate_overrides   — date-range nightly rate overrides
  - rate_restrictions — per-day restrictions (min/max stay, CTA/CTD,
                       stop_sell)

And ADDS one nullable column to the rooms table:

  - rooms.room_type_id  — FK room_types.id, ondelete SET NULL.
                          Backfilled from the distinct existing
                          Room.room_type free-text strings.

Data migration: populates room_types from the distinct existing
Room.room_type values, then sets each Room.room_type_id to point at
the corresponding catalog row. The legacy Room.room_type STRING
column is preserved so existing templates/queries keep working.

Hand-written. Touches no other tables. Downgrade reverses cleanly.

Implementation notes:
  - FK creation is dialect-aware: PostgreSQL gets explicit named FKs;
    SQLite gets inline FKs at column-creation time. See migration
    b4c1f2d6e892 for the same pattern.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import column, table


revision = 'c5d2a3f8e103'
down_revision = 'b4c1f2d6e892'
branch_labels = None
depends_on = None


def _is_postgres():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    # ── room_types ──────────────────────────────────────────────
    op.create_table(
        'room_types',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(20), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('max_occupancy', sa.Integer(), nullable=False,
                  server_default='2'),
        sa.Column('base_capacity', sa.Integer(), nullable=False,
                  server_default='2'),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('code', name='uq_room_types_code'),
    )

    # ── rate_plans ──────────────────────────────────────────────
    op.create_table(
        'rate_plans',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(30), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('room_type_id', sa.Integer(),
                  sa.ForeignKey('room_types.id',
                                name='fk_rate_plans_room_type',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('base_rate', sa.Float(), nullable=False,
                  server_default='0'),
        sa.Column('currency', sa.String(8), nullable=False,
                  server_default='USD'),
        sa.Column('is_refundable', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('code', name='uq_rate_plans_code'),
    )

    # ── rate_overrides ──────────────────────────────────────────
    op.create_table(
        'rate_overrides',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('room_type_id', sa.Integer(),
                  sa.ForeignKey('room_types.id',
                                name='fk_rate_overrides_room_type',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('rate_plan_id', sa.Integer(),
                  sa.ForeignKey('rate_plans.id',
                                name='fk_rate_overrides_rate_plan',
                                ondelete='CASCADE'),
                  nullable=True),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('nightly_rate', sa.Float(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_rate_overrides_room_type_dates',
                    'rate_overrides',
                    ['room_type_id', 'start_date', 'end_date'])

    # ── rate_restrictions ───────────────────────────────────────
    op.create_table(
        'rate_restrictions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('room_type_id', sa.Integer(),
                  sa.ForeignKey('room_types.id',
                                name='fk_rate_restrictions_room_type',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('min_stay', sa.Integer(), nullable=True),
        sa.Column('max_stay', sa.Integer(), nullable=True),
        sa.Column('closed_to_arrival', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('closed_to_departure', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('stop_sell', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_rate_restrictions_room_type_dates',
                    'rate_restrictions',
                    ['room_type_id', 'start_date', 'end_date'])

    # ── rooms.room_type_id ──────────────────────────────────────
    op.add_column('rooms',
                  sa.Column('room_type_id', sa.Integer(), nullable=True))

    if _is_postgres():
        op.create_foreign_key(
            'fk_rooms_room_type_id',
            'rooms', 'room_types',
            ['room_type_id'], ['id'],
            ondelete='SET NULL',
        )

    # ── Data migration: backfill room_types from existing Room.room_type ──
    bind = op.get_bind()
    rooms_table = sa.table(
        'rooms',
        column('id', sa.Integer),
        column('room_type', sa.String),
        column('room_type_id', sa.Integer),
        column('capacity', sa.Integer),
    )
    rt_table = sa.table(
        'room_types',
        column('id', sa.Integer),
        column('code', sa.String),
        column('name', sa.String),
        column('max_occupancy', sa.Integer),
        column('base_capacity', sa.Integer),
        column('is_active', sa.Boolean),
        column('created_at', sa.DateTime),
        column('updated_at', sa.DateTime),
    )

    from datetime import datetime
    now = datetime.utcnow()

    distinct_types = bind.execute(sa.text(
        "SELECT DISTINCT room_type, MAX(capacity) AS cap "
        "FROM rooms WHERE room_type IS NOT NULL "
        "GROUP BY room_type"
    )).fetchall()

    code_counter = 0
    name_to_id = {}
    for row in distinct_types:
        rtype = row[0] or ''
        cap   = int(row[1] or 2)
        if not rtype.strip():
            continue
        # Make a stable short code: first 3 letters upper, suffix if needed
        base_code = ''.join(c for c in rtype.upper() if c.isalnum())[:3] or 'RT'
        code = base_code
        # ensure uniqueness within this batch
        if code in (v for v in name_to_id.keys()):
            code_counter += 1
            code = f'{base_code}{code_counter}'
        op.bulk_insert(rt_table, [{
            'code':          code,
            'name':          rtype,
            'max_occupancy': cap,
            'base_capacity': cap,
            'is_active':     True,
            'created_at':    now,
            'updated_at':    now,
        }])
        # Read back the id for FK linking
        new_id = bind.execute(sa.text(
            "SELECT id FROM room_types WHERE code = :c"
        ), {'c': code}).scalar()
        name_to_id[rtype] = new_id

    for rtype, new_id in name_to_id.items():
        bind.execute(
            sa.text("UPDATE rooms SET room_type_id = :rt_id "
                    "WHERE room_type = :rtype"),
            {'rt_id': new_id, 'rtype': rtype},
        )


def downgrade():
    if _is_postgres():
        op.drop_constraint('fk_rooms_room_type_id', 'rooms',
                           type_='foreignkey')
    op.drop_column('rooms', 'room_type_id')

    op.drop_index('ix_rate_restrictions_room_type_dates',
                  table_name='rate_restrictions')
    op.drop_table('rate_restrictions')

    op.drop_index('ix_rate_overrides_room_type_dates',
                  table_name='rate_overrides')
    op.drop_table('rate_overrides')

    op.drop_table('rate_plans')
    op.drop_table('room_types')
