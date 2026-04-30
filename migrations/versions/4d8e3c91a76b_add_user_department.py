"""add User.department column for role-based landing

Revision ID: 4d8e3c91a76b
Revises: 3f7b1c8e2a04
Create Date: 2026-04-30 23:45:00.000000

Adds a single nullable column `users.department` (string(40)). Used
by services.landing.landing_url_for() to dispatch each user to their
own department home after login (Front Office / Housekeeping /
Restaurant / Accounting). NULL leaves existing behaviour intact:
admin → Dashboard, staff → Dashboard.

Reverting drops the column without data loss for the rest of the
users table.
"""

from alembic import op
import sqlalchemy as sa


revision      = '4d8e3c91a76b'
down_revision = '3f7b1c8e2a04'
branch_labels = None
depends_on    = None


def upgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(sa.Column('department', sa.String(40),
                                      nullable=True))


def downgrade():
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('department')
