"""inline processing steps for channels and timed events

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-31 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('satellite_channels', schema=None) as batch_op:
        batch_op.add_column(sa.Column('steps', sa.JSON(), nullable=True))
    with op.batch_alter_table('timed_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('steps', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('keep_iq', sa.Boolean(), server_default=sa.text('1'), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('timed_events', schema=None) as batch_op:
        batch_op.drop_column('keep_iq')
        batch_op.drop_column('steps')
    with op.batch_alter_table('satellite_channels', schema=None) as batch_op:
        batch_op.drop_column('steps')
