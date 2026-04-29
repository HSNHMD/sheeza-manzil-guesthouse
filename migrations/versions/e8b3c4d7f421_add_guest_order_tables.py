"""add Online Menu / QR Ordering V1 tables

Revision ID: e8b3c4d7f421
Revises: d6a7b9c0e215
Create Date: 2026-04-30 12:00:00.000000

Creates the two guest-order tables:

  - guest_orders        — top-level order row, FK booking (nullable)
  - guest_order_items   — line items, snapshot name + price at submit

V1 reuses the POS catalog (pos_categories + pos_items) for the menu
display; this migration only adds the order tables. No columns added
to existing tables. Folio integration goes through staff-explicit
"post to room" actions and reuses the existing FolioItem / source_module
pattern — no schema change needed there either.

Hand-written. Touches no other tables. Downgrade reverses cleanly.
"""
from alembic import op
import sqlalchemy as sa


revision = 'e8b3c4d7f421'
down_revision = 'd6a7b9c0e215'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'guest_orders',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('public_token', sa.String(40), nullable=False),
        sa.Column('booking_id', sa.Integer(),
                  sa.ForeignKey('bookings.id',
                                name='fk_guest_orders_booking',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('room_number_input', sa.String(20), nullable=True),
        sa.Column('guest_name_input', sa.String(120), nullable=True),
        sa.Column('contact_phone', sa.String(40), nullable=True),
        sa.Column('notes', sa.String(500), nullable=True),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='new'),
        sa.Column('total_amount', sa.Float(), nullable=False,
                  server_default='0'),
        sa.Column('source', sa.String(20), nullable=False,
                  server_default='guest_menu'),
        sa.Column('confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('confirmed_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id',
                                name='fk_guest_orders_confirmed_by',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('delivered_at', sa.DateTime(), nullable=True),
        sa.Column('delivered_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id',
                                name='fk_guest_orders_delivered_by',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id',
                                name='fk_guest_orders_cancelled_by',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('cancel_reason', sa.String(255), nullable=True),
        sa.Column('posted_to_folio_at', sa.DateTime(), nullable=True),
        sa.Column('posted_by_user_id', sa.Integer(),
                  sa.ForeignKey('users.id',
                                name='fk_guest_orders_posted_by',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('folio_item_ids', sa.String(255), nullable=True),
        sa.Column('metadata_json', sa.Text(), nullable=True),
        sa.UniqueConstraint('public_token', name='uq_guest_orders_token'),
    )
    op.create_index('ix_guest_orders_created_at', 'guest_orders',
                    ['created_at'])
    op.create_index('ix_guest_orders_booking_id', 'guest_orders',
                    ['booking_id'])
    op.create_index('ix_guest_orders_status', 'guest_orders',
                    ['status'])
    op.create_index('ix_guest_orders_token', 'guest_orders',
                    ['public_token'])

    op.create_table(
        'guest_order_items',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('order_id', sa.Integer(),
                  sa.ForeignKey('guest_orders.id',
                                name='fk_guest_order_items_order',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('pos_item_id', sa.Integer(),
                  sa.ForeignKey('pos_items.id',
                                name='fk_guest_order_items_pos_item',
                                ondelete='SET NULL'),
                  nullable=True),
        sa.Column('item_name_snapshot', sa.String(120), nullable=False),
        sa.Column('item_type_snapshot', sa.String(30), nullable=False,
                  server_default='restaurant'),
        sa.Column('unit_price', sa.Float(), nullable=False,
                  server_default='0'),
        sa.Column('quantity', sa.Float(), nullable=False,
                  server_default='1'),
        sa.Column('line_total', sa.Float(), nullable=False,
                  server_default='0'),
        sa.Column('note', sa.String(255), nullable=True),
    )
    op.create_index('ix_guest_order_items_order',
                    'guest_order_items', ['order_id'])


def downgrade():
    op.drop_index('ix_guest_order_items_order',
                  table_name='guest_order_items')
    op.drop_table('guest_order_items')

    op.drop_index('ix_guest_orders_token', table_name='guest_orders')
    op.drop_index('ix_guest_orders_status', table_name='guest_orders')
    op.drop_index('ix_guest_orders_booking_id', table_name='guest_orders')
    op.drop_index('ix_guest_orders_created_at', table_name='guest_orders')
    op.drop_table('guest_orders')
