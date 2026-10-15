"""clip hook card

The scorer now writes a short hook of its own, burned across the top of the
clip for the first second and a half. It is kept apart from `hook`, which
stays the opening line of what was actually said.

Revision ID: 0002_hook_card
Revises: 0001_initial
Create Date: 2026-10-15
"""
from alembic import op
import sqlalchemy as sa

revision = '0002_hook_card'
down_revision = '0001_initial'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('clips', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hook_card', sa.Text(), nullable=False,
                                      server_default=''))


def downgrade():
    with op.batch_alter_table('clips', schema=None) as batch_op:
        batch_op.drop_column('hook_card')
