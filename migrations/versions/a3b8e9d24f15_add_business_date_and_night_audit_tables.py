"""add business date state + night audit runs

Revision ID: a3b8e9d24f15
Revises: f1c5b2a93e80
Create Date: 2026-04-29 16:00:00.000000

Creates the two tables for Night Audit V1:
  - business_date_state — single-row table holding the property's
    current business date (operator-controlled; does NOT auto-follow
    server clock).
  - night_audit_runs — immutable history of every Night Audit attempt.

Bootstraps the singleton row in business_date_state with
current_business_date = the date the migration runs. The operator can
adjust later via a maintenance route or shell if needed.

Hand-written. Touches no existing tables. Downgrade drops both new
tables and their indexes/FKs.

Same convention as f3a7c91b04e2 / c2b9f4d83a51 / d8a3e1f29c40 /
e7c1a4b89d62 / f1c5b2a93e80.
"""
from datetime import date, datetime

from alembic import op
import sqlalchemy as sa


revision = 'a3b8e9d24f15'
down_revision = 'f1c5b2a93e80'
branch_labels = None
depends_on = None


def upgrade():
    # ── business_date_state ───────────────────────────────────────
    business_date_state = op.create_table(
        'business_date_state',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),

        sa.Column('current_business_date', sa.Date(), nullable=False),

        sa.Column('last_audit_run_at',         sa.DateTime(), nullable=True),
        sa.Column('last_audit_run_by_user_id', sa.Integer(),  nullable=True),

        sa.Column('audit_in_progress', sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column('audit_started_at', sa.DateTime(), nullable=True),
        sa.Column('audit_started_by_user_id', sa.Integer(),  nullable=True),

        sa.ForeignKeyConstraint(
            ['last_audit_run_by_user_id'], ['users.id'],
            name='fk_bds_last_audit_user',
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['audit_started_by_user_id'], ['users.id'],
            name='fk_bds_audit_started_user',
            ondelete='SET NULL',
        ),
    )

    # Seed the singleton row. current_business_date defaults to the
    # date the migration runs. Operator can adjust later if needed.
    now = datetime.utcnow()
    op.bulk_insert(business_date_state, [{
        'current_business_date': date.today(),
        'audit_in_progress':     False,
        'created_at':            now,
        'updated_at':            now,
    }])

    # ── night_audit_runs ──────────────────────────────────────────
    op.create_table(
        'night_audit_runs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at',   sa.DateTime(), nullable=False),
        sa.Column('completed_at', sa.DateTime(), nullable=True),

        sa.Column('business_date_closed', sa.Date(), nullable=False),
        sa.Column('next_business_date',   sa.Date(), nullable=False),

        sa.Column('run_by_user_id', sa.Integer(), nullable=True),

        sa.Column('status', sa.String(length=20), nullable=False,
                  server_default='started'),

        sa.Column('summary_json',    sa.Text(),    nullable=True),
        sa.Column('exception_count', sa.Integer(), nullable=False,
                  server_default='0'),
        sa.Column('warning_count',   sa.Integer(), nullable=False,
                  server_default='0'),

        sa.Column('notes', sa.String(length=500), nullable=True),

        sa.ForeignKeyConstraint(
            ['run_by_user_id'], ['users.id'],
            name='fk_night_audit_runs_user',
            ondelete='SET NULL',
        ),
    )

    op.create_index('ix_night_audit_runs_created_at',
                    'night_audit_runs', ['created_at'])
    op.create_index('ix_night_audit_runs_business_date',
                    'night_audit_runs', ['business_date_closed'])
    op.create_index('ix_night_audit_runs_status',
                    'night_audit_runs', ['status'])


def downgrade():
    op.drop_index('ix_night_audit_runs_status',        table_name='night_audit_runs')
    op.drop_index('ix_night_audit_runs_business_date', table_name='night_audit_runs')
    op.drop_index('ix_night_audit_runs_created_at',    table_name='night_audit_runs')
    op.drop_table('night_audit_runs')
    op.drop_table('business_date_state')
