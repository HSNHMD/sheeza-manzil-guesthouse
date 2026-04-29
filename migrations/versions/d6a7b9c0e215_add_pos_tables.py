"""add POS / F&B V1 tables

Revision ID: d6a7b9c0e215
Revises: c5d2a3f8e103
Create Date: 2026-04-30 10:30:00.000000

Creates the two POS catalog tables:

  - pos_categories   — top-level groupings (e.g. "Drinks", "Mains")
  - pos_items        — sellable line items, FK pos_categories

POS V1 is a CATALOG + SALE FLOW only. Every sale creates FolioItem
(and optionally CashierTransaction) rows via the existing services.
This migration adds NO columns to existing tables — POS leaves the
folio / cashier / booking schemas untouched.

Hand-written. Touches no other tables. Downgrade reverses cleanly.
"""
from alembic import op
import sqlalchemy as sa


revision = 'd6a7b9c0e215'
down_revision = 'c5d2a3f8e103'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'pos_categories',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False,
                  server_default='100'),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('name', name='uq_pos_categories_name'),
    )

    op.create_table(
        'pos_items',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('category_id', sa.Integer(),
                  sa.ForeignKey('pos_categories.id',
                                name='fk_pos_items_category',
                                ondelete='CASCADE'),
                  nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('price', sa.Float(), nullable=False, server_default='0'),
        sa.Column('default_item_type', sa.String(30), nullable=False,
                  server_default='restaurant'),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False,
                  server_default='100'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pos_items_category', 'pos_items', ['category_id'])


def downgrade():
    op.drop_index('ix_pos_items_category', table_name='pos_items')
    op.drop_table('pos_items')
    op.drop_table('pos_categories')
