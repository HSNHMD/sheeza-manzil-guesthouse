"""add Pepper (Telegram agent) tables — Phase 0

Revision ID: a1b2c3d4e5f6
Revises: e5b7c9d1f3a4
Create Date: 2026-07-31 15:30:00.000000

Purely ADDITIVE: three NEW tables (pepper_users, pepper_flows, pepper_outbox);
no existing table is touched. A code revert leaves them harmless and unused, so
Phase 0 rollback is clean. The bot never accesses these directly — only via the
localhost internal API (unix socket + bearer).

Hand-written. FK on pepper_outbox.booking_id → bookings.id (SET NULL) is inlined
in create_table (works on both PostgreSQL and SQLite).
"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = 'e5b7c9d1f3a4'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pepper_users',
        sa.Column('telegram_id', sa.BigInteger(), primary_key=True),
        sa.Column('display_name', sa.String(length=120), nullable=True),
        sa.Column('role', sa.String(length=20), nullable=False,
                  server_default='staff'),
        sa.Column('added_by', sa.BigInteger(), nullable=True),
        sa.Column('added_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('revoked_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'pepper_flows',
        sa.Column('telegram_id', sa.BigInteger(), primary_key=True),
        sa.Column('step', sa.String(length=40), nullable=True),
        sa.Column('draft_json', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        'pepper_outbox',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('booking_id', sa.Integer(),
                  sa.ForeignKey('bookings.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('reference', sa.String(length=64), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_pepper_outbox_created_at', 'pepper_outbox',
                    ['created_at'])
    op.create_index('ix_pepper_outbox_undelivered', 'pepper_outbox',
                    ['delivered_at', 'created_at'])


def downgrade():
    op.drop_index('ix_pepper_outbox_undelivered', table_name='pepper_outbox')
    op.drop_index('ix_pepper_outbox_created_at', table_name='pepper_outbox')
    op.drop_table('pepper_outbox')
    op.drop_table('pepper_flows')
    op.drop_table('pepper_users')
