"""add Multi-Property Foundation V1

Revision ID: 1d9b6a4f5e72
Revises: 0c5e7f3b842a
Create Date: 2026-04-30 19:00:00.000000

Phase 1 of the multi-property migration described in
docs/multi_property_migration_strategy.md.

Steps:
  1. Create `properties` table.
  2. Seed one row from the existing `property_settings` singleton.
  3. Add nullable `property_id` to 12 wave-1 tables.
  4. Backfill all existing rows in those tables to property_id=1.
  5. Tighten property_id to NOT NULL via op.alter_column.

The pattern intentionally splits "add nullable" + "backfill" + "tighten"
inside one migration. Splitting these into multiple migrations is
better for very-large-row-count tables, but our staging fleet fits
in a single transaction comfortably.

Wave 1 tables:
  rooms, room_types, rate_plans, rate_overrides, rate_restrictions,
  room_blocks, bookings, booking_groups, invoices, folio_items,
  cashier_transactions, whatsapp_messages.

Deferred (later sprint, with explicit doc):
  guests (privacy-sensitive — Phase 5 of the strategy doc),
  business_date_state, night_audit_runs (Night-Audit phase),
  expenses, bank_transactions, housekeeping_logs, room_blocks
  *(included in wave 1)*, pos_categories, pos_items, guest_orders,
  guest_order_items, activity_logs.

Hand-written. Touches no models outside the wave-1 set.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = '1d9b6a4f5e72'
down_revision = '0c5e7f3b842a'
branch_labels = None
depends_on = None


# Tables that get a property_id column in this migration.
WAVE_1_TABLES = (
    'rooms',
    'room_types',
    'rate_plans',
    'rate_overrides',
    'rate_restrictions',
    'room_blocks',
    'bookings',
    'booking_groups',
    'invoices',
    'folio_items',
    'cashier_transactions',
    'whatsapp_messages',
)


def _is_postgres():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    # ── 1. Create `properties` ─────────────────────────────────
    properties_t = op.create_table(
        'properties',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('name', sa.String(160), nullable=False),
        sa.Column('short_name', sa.String(80), nullable=True),
        sa.Column('timezone', sa.String(64), nullable=False,
                  server_default='Indian/Maldives'),
        sa.Column('currency_code', sa.String(8), nullable=False,
                  server_default='USD'),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('settings_id', sa.Integer(),
                  sa.ForeignKey('property_settings.id',
                                name='fk_properties_settings',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.UniqueConstraint('code', name='uq_properties_code'),
    )
    op.create_index('ix_properties_code', 'properties', ['code'])

    # ── 2. Seed singleton row from PropertySettings ────────────
    bind = op.get_bind()
    settings_row = bind.execute(sa.text(
        "SELECT id, property_name, short_name, currency_code, timezone "
        "FROM property_settings ORDER BY id LIMIT 1"
    )).fetchone()
    if settings_row is not None:
        sid          = settings_row[0]
        sname        = settings_row[1] or 'Default Property'
        short        = settings_row[2]
        currency     = settings_row[3] or 'USD'
        timezone     = settings_row[4] or 'Indian/Maldives'
    else:
        # Property Settings V1 should already have seeded a row, but
        # cover the edge case where it didn't (e.g. a fresh install
        # that ran migrations without the seeded row).
        sid       = None
        sname     = 'Default Property'
        short     = None
        currency  = 'USD'
        timezone  = 'Indian/Maldives'

    now = datetime.utcnow()
    op.bulk_insert(properties_t, [{
        'created_at':    now,
        'updated_at':    now,
        'code':          'default',
        'name':          sname,
        'short_name':    short,
        'timezone':      timezone,
        'currency_code': currency,
        'is_active':     True,
        'notes':         None,
        'settings_id':   sid,
    }])

    # ── 3 + 4 + 5. Add column WITH server_default, backfill, tighten ──
    #
    # The `server_default='1'` is intentional and stays in place after
    # migration. It makes any INSERT that doesn't explicitly set
    # property_id land on the singleton property — which is the V1
    # behaviour we want. When multi-property V2 ships, route handlers
    # will explicitly set property_id from current_property_id() and
    # the DB default becomes a defensive safety net rather than the
    # primary mechanism.
    for table in WAVE_1_TABLES:
        # 3. Add column. server_default='1' fills both new INSERTs
        #    and (for PostgreSQL) existing rows automatically.
        op.add_column(
            table,
            sa.Column('property_id', sa.Integer(),
                      nullable=True, server_default='1'),
        )

        # 4. Backfill explicitly — needed for SQLite, harmless on PG.
        op.execute(
            sa.text(f"UPDATE {table} SET property_id = 1 "
                    f"WHERE property_id IS NULL")
        )

    # 5. Tighten to NOT NULL — done in a second loop so all backfills
    #    finish before we add the constraint (no in-flight NULLs).
    for table in WAVE_1_TABLES:
        with op.batch_alter_table(table, schema=None) as batch:
            batch.alter_column('property_id',
                                existing_type=sa.Integer(),
                                nullable=False,
                                server_default='1')
        # Index for query speed.
        op.create_index(f'ix_{table}_property_id', table, ['property_id'])

    # FK constraints — only on PostgreSQL (SQLite ALTER doesn't support
    # ADD CONSTRAINT; the model relationship is enough for tests).
    if _is_postgres():
        for table in WAVE_1_TABLES:
            op.create_foreign_key(
                f'fk_{table}_property',
                table, 'properties',
                ['property_id'], ['id'],
                ondelete='RESTRICT',
            )


def downgrade():
    # Drop FK + index + column on every wave-1 table, then drop properties.
    if _is_postgres():
        for table in WAVE_1_TABLES:
            try:
                op.drop_constraint(f'fk_{table}_property', table,
                                    type_='foreignkey')
            except Exception:
                pass

    for table in WAVE_1_TABLES:
        try:
            op.drop_index(f'ix_{table}_property_id', table_name=table)
        except Exception:
            pass
        op.drop_column(table, 'property_id')

    op.drop_index('ix_properties_code', table_name='properties')
    op.drop_table('properties')
