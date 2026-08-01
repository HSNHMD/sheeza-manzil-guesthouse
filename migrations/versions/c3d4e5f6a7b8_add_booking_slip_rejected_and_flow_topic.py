"""add Booking.slip_rejected_* + pepper_flows.chat_id/thread_id — Pepper Phase 2

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-01 12:00:00.000000

Two additive changes for the bot-created booking's own slip flow (D-slip) and the
guided /newbooking restart recovery:

1. ``bookings.slip_rejected_at`` / ``slip_rejected_reason`` — mirror the Hold
   columns (b2c3d4e5f6a7) so ❌ Reject can soft-reject a booking's slip WITHOUT
   dropping it out of pending_verification; the file is kept and staff can
   re-attach. A valid slip = filename on file AND not slip_rejected.

2. ``pepper_flows.chat_id`` / ``thread_id`` — so a flow snapshot knows WHICH
   forum topic (chat + message_thread) to resume in after a bot restart. The
   original pepper_flows (a1b2c3d4e5f6) only carried telegram_id/step/draft_json;
   without chat/thread the bot can't post the "I restarted; @user, we were at
   step N" resume message into the right topic.

All four columns are NULLABLE — a code revert leaves them harmless/unused, and
downgrade drops them. Inlined so it runs on both PostgreSQL and SQLite.
"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('bookings', sa.Column('slip_rejected_at', sa.DateTime(),
                                        nullable=True))
    op.add_column('bookings', sa.Column('slip_rejected_reason',
                                        sa.String(length=255), nullable=True))
    op.add_column('pepper_flows', sa.Column('chat_id', sa.BigInteger(),
                                            nullable=True))
    op.add_column('pepper_flows', sa.Column('thread_id', sa.BigInteger(),
                                            nullable=True))


def downgrade():
    op.drop_column('pepper_flows', 'thread_id')
    op.drop_column('pepper_flows', 'chat_id')
    op.drop_column('bookings', 'slip_rejected_reason')
    op.drop_column('bookings', 'slip_rejected_at')
