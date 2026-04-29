"""add room housekeeping assignment + audit fields

Revision ID: b4c1f2d6e892
Revises: a3b8e9d24f15
Create Date: 2026-04-29 16:30:00.000000

Adds four NULLABLE columns to the existing rooms table to support
Housekeeping V1:

  - assigned_to_user_id        — FK users.id, ondelete SET NULL.
  - assigned_at                — DateTime nullable.
  - housekeeping_updated_at    — DateTime nullable. Timestamp of the
                                 last housekeeping_status change.
  - housekeeping_updated_by_user_id — FK users.id, ondelete SET NULL.

NO data migration. Existing rooms keep their current
housekeeping_status (default 'clean') untouched. The four new columns
are purely additive.

NO rename of housekeeping_status. The vocabulary is widening from
{clean, dirty, in_progress} → {clean, dirty, in_progress, inspected,
out_of_order} but that's a string column with no DB-level constraint;
no schema change is needed for the new values.

Hand-written. Touches no other tables. Downgrade reverses cleanly.

Implementation notes:
  - Uses op.add_column / op.drop_column directly. PostgreSQL handles
    these natively. SQLite (test only) supports ADD COLUMN; we omit
    FK constraints at the migration level because SQLite ALTER TABLE
    can't ADD CONSTRAINT and alembic batch-mode hits a topological
    sort bug when adding multiple FK-bearing columns in one go.
  - The relationship() declarations in app.models.Room work in both
    SQLite and PostgreSQL because SQLAlchemy resolves them via the
    Python-level join condition, not via DB-level FK constraints.
  - On PostgreSQL the FK constraints are added in a follow-up
    op.create_foreign_key() call, which IS supported there.
"""
from alembic import op
import sqlalchemy as sa


revision = 'b4c1f2d6e892'
down_revision = 'a3b8e9d24f15'
branch_labels = None
depends_on = None


def _is_postgres():
    bind = op.get_bind()
    return bind.dialect.name == 'postgresql'


def upgrade():
    op.add_column('rooms', sa.Column(
        'assigned_to_user_id', sa.Integer(), nullable=True))
    op.add_column('rooms', sa.Column(
        'assigned_at', sa.DateTime(), nullable=True))
    op.add_column('rooms', sa.Column(
        'housekeeping_updated_at', sa.DateTime(), nullable=True))
    op.add_column('rooms', sa.Column(
        'housekeeping_updated_by_user_id', sa.Integer(), nullable=True))

    if _is_postgres():
        op.create_foreign_key(
            'fk_rooms_assigned_to_user',
            'rooms', 'users',
            ['assigned_to_user_id'], ['id'],
            ondelete='SET NULL',
        )
        op.create_foreign_key(
            'fk_rooms_hk_updated_by_user',
            'rooms', 'users',
            ['housekeeping_updated_by_user_id'], ['id'],
            ondelete='SET NULL',
        )


def downgrade():
    if _is_postgres():
        op.drop_constraint('fk_rooms_hk_updated_by_user', 'rooms',
                           type_='foreignkey')
        op.drop_constraint('fk_rooms_assigned_to_user', 'rooms',
                           type_='foreignkey')
    op.drop_column('rooms', 'housekeeping_updated_by_user_id')
    op.drop_column('rooms', 'housekeeping_updated_at')
    op.drop_column('rooms', 'assigned_at')
    op.drop_column('rooms', 'assigned_to_user_id')
