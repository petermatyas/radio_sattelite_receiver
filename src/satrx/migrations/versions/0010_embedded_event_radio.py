"""timed events own their radio settings and processing; single-pass rules; drop satellite defaults

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-31 14:00:00
"""
import json

from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('satellites', schema=None) as batch_op:
        batch_op.drop_constraint('fk_satellites_default_pipeline_id', type_='foreignkey')
        batch_op.drop_column('default_pipeline_id')
        batch_op.drop_column('default_profile_id')

    with op.batch_alter_table('watch_rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('single_pass', sa.Boolean(), server_default=sa.text('0'), nullable=False))

    with op.batch_alter_table('timed_events', schema=None) as batch_op:
        batch_op.add_column(sa.Column('center_freq_hz', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('bandwidth_hz', sa.Integer(), server_default='25000', nullable=False))
        batch_op.add_column(sa.Column('sample_rate_hz', sa.Integer(), server_default='250000', nullable=False))
        batch_op.add_column(sa.Column('gain_db', sa.Float(), nullable=True))

    # a meglévő időpontos események saját (karbantartott) profilt és pipeline-t kapnak a korábban választottak másolatából
    bind = op.get_bind()
    events = bind.execute(sa.text("SELECT id, name, profile_id, pipeline_id FROM timed_events")).mappings().all()
    for ev in events:
        prof = bind.execute(sa.text("SELECT center_freq_hz, bandwidth_hz, sample_rate_hz, gain_db, modulation_hint, doppler_correction, antenna_id "
                                    "FROM radio_profiles WHERE id = :i"), {"i": ev["profile_id"]}).mappings().first()
        pipe = bind.execute(sa.text("SELECT steps, keep_iq FROM pipelines WHERE id = :i"), {"i": ev["pipeline_id"]}).mappings().first()
        if prof is None or pipe is None:
            continue
        tag = f"{ev['name']} [event#{ev['id']}]"
        bind.execute(sa.text(
            "INSERT INTO radio_profiles (name, center_freq_hz, bandwidth_hz, sample_rate_hz, gain_db, modulation_hint, doppler_correction, antenna_id) "
            "VALUES (:n, :f, :b, :r, :g, :m, 0, :a)"),
            {"n": tag, "f": prof["center_freq_hz"], "b": prof["bandwidth_hz"], "r": prof["sample_rate_hz"], "g": prof["gain_db"],
             "m": prof["modulation_hint"], "a": prof["antenna_id"]})
        profile_id = bind.execute(sa.text("SELECT id FROM radio_profiles WHERE name = :n"), {"n": tag}).scalar()
        steps = pipe["steps"] if isinstance(pipe["steps"], str) else json.dumps(pipe["steps"] or [])
        bind.execute(sa.text("INSERT INTO pipelines (name, mode, steps, keep_iq) VALUES (:n, 'timed', :s, :k)"),
                     {"n": tag, "s": steps, "k": pipe["keep_iq"]})
        pipeline_id = bind.execute(sa.text("SELECT id FROM pipelines WHERE name = :n"), {"n": tag}).scalar()
        bind.execute(sa.text(
            "UPDATE timed_events SET profile_id = :p, pipeline_id = :l, steps = :s, keep_iq = :k, center_freq_hz = :f, "
            "bandwidth_hz = :b, sample_rate_hz = :r, gain_db = :g WHERE id = :i"),
            {"p": profile_id, "l": pipeline_id, "s": steps, "k": pipe["keep_iq"], "f": prof["center_freq_hz"], "b": prof["bandwidth_hz"],
             "r": prof["sample_rate_hz"], "g": prof["gain_db"], "i": ev["id"]})


def downgrade() -> None:
    with op.batch_alter_table('timed_events', schema=None) as batch_op:
        batch_op.drop_column('gain_db')
        batch_op.drop_column('sample_rate_hz')
        batch_op.drop_column('bandwidth_hz')
        batch_op.drop_column('center_freq_hz')
    with op.batch_alter_table('watch_rules', schema=None) as batch_op:
        batch_op.drop_column('single_pass')
    with op.batch_alter_table('satellites', schema=None) as batch_op:
        batch_op.add_column(sa.Column('default_profile_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('default_pipeline_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_satellites_default_profile_id', 'radio_profiles', ['default_profile_id'], ['id'], ondelete='SET NULL')
        batch_op.create_foreign_key('fk_satellites_default_pipeline_id', 'pipelines', ['default_pipeline_id'], ['id'], ondelete='SET NULL')
