"""add Group Bookings / Master Folios V1

Revision ID: f9a4b8d2c531
Revises: e8b3c4d7f421
Create Date: 2026-04-30 14:00:00.000000

Creates the booking_groups table and adds two nullable columns to
the bookings table:

  - bookings.booking_group_id   FK booking_groups.id (SET NULL)
  - bookings.billing_target     'individual' | 'master', default 'individual'

V1 is purely ADDITIVE. Standalone bookings keep working unchanged —
booking_group_id is NULL on every existing row after the migration,
and billing_target defaults to 'individual'.

Hand-written. Touches only the booking_groups (new) and bookings
(2 nullable adds) tables. Downgrade reverses cleanly.

FK constraints follow the dialect-aware pattern (PostgreSQL gets
named FKs via op.create_foreign_key; SQLite skips them — the model-
level relationship() works in both cases).
"""
from alembic import op
import sqlalchemy as sa


revision = 'f9a4b8d2c531'
down_revision = 'e8b3c4d7f421'
branch_labels = None
depends_on = None


def _is_postgres():
    return op.get_bind().dialect.name == 'postgresql'


def upgrade():
    op.create_table(
        'booking_groups',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('group_code', sa.String(40), nullable=False),
        sa.Column('group_name', sa.String(160), nullable=False),
        sa.Column('primary_contact_guest_id', sa.Integer(),
                  sa.ForeignKey('guests.id',
                                name='fk_booking_groups_contact',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('master_booking_id', sa.Integer(),
                  sa.ForeignKey('bookings.id',
                                name='fk_booking_groups_master',
                                ondelete='SET NULL',
                                use_alter=True),
                  nullable=True),
        sa.Column('billing_mode', sa.String(20), nullable=False,
                  server_default='individual'),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.UniqueConstraint('group_code', name='uq_booking_groups_code'),
    )
    op.create_index('ix_booking_groups_code', 'booking_groups',
                    ['group_code'])
    op.create_index('ix_booking_groups_status', 'booking_groups',
                    ['status'])

    op.add_column('bookings',
                  sa.Column('booking_group_id', sa.Integer(),
                            nullable=True))
    op.add_column('bookings',
                  sa.Column('billing_target', sa.String(20),
                            nullable=False,
                            server_default='individual'))
    op.create_index('ix_bookings_booking_group_id', 'bookings',
                    ['booking_group_id'])

    if _is_postgres():
        op.create_foreign_key(
            'fk_bookings_booking_group',
            'bookings', 'booking_groups',
            ['booking_group_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    if _is_postgres():
        op.drop_constraint('fk_bookings_booking_group', 'bookings',
                            type_='foreignkey')
    op.drop_index('ix_bookings_booking_group_id', table_name='bookings')
    op.drop_column('bookings', 'billing_target')
    op.drop_column('bookings', 'booking_group_id')

    op.drop_index('ix_booking_groups_status',
                  table_name='booking_groups')
    op.drop_index('ix_booking_groups_code',
                  table_name='booking_groups')
    op.drop_table('booking_groups')
