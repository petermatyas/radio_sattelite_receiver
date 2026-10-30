"""satellite default pipeline

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-30 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('satellites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('default_pipeline_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_satellites_default_pipeline_id', 'pipelines', ['default_pipeline_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('satellites', schema=None) as batch_op:
        batch_op.drop_constraint('fk_satellites_default_pipeline_id', type_='foreignkey')
        batch_op.drop_column('default_pipeline_id')
