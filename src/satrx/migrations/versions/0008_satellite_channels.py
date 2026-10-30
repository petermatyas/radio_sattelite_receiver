"""satellite channels

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-30 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'satellite_channels',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('norad_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('center_freq_hz', sa.Integer(), nullable=False),
        sa.Column('bandwidth_hz', sa.Integer(), nullable=False),
        sa.Column('sample_rate_hz', sa.Integer(), nullable=False),
        sa.Column('gain_db', sa.Float(), nullable=True),
        sa.Column('doppler', sa.Boolean(), nullable=False),
        sa.Column('keep_iq', sa.Boolean(), nullable=False),
        sa.Column('settings', sa.JSON(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=True),
        sa.Column('pipeline_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['norad_id'], ['satellites.norad_id'], name='fk_satellite_channels_norad_id', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('satellite_channels', schema=None) as batch_op:
        batch_op.create_index('ix_satellite_channels_norad_id', ['norad_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('satellite_channels', schema=None) as batch_op:
        batch_op.drop_index('ix_satellite_channels_norad_id')
    op.drop_table('satellite_channels')
