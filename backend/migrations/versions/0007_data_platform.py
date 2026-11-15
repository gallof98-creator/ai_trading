"""data platform catalog and ingestion log

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('data_series',
    sa.Column('dataset', sa.String(length=32), nullable=False),
    sa.Column('provider', sa.String(length=24), nullable=False),
    sa.Column('key', sa.String(length=96), nullable=False),
    sa.Column('timeframe', sa.String(length=8), nullable=False),
    sa.Column('rows', sa.BigInteger(), nullable=False),
    sa.Column('bytes', sa.BigInteger(), nullable=False),
    sa.Column('first_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cadence_seconds', sa.Integer(), nullable=True),
    sa.Column('last_ingested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_status', sa.String(length=12), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('dataset', 'provider', 'key', 'timeframe')
    )
    op.create_table('data_ingestion_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(length=24), nullable=False),
    sa.Column('dataset', sa.String(length=32), nullable=False),
    sa.Column('key', sa.String(length=96), nullable=False),
    sa.Column('timeframe', sa.String(length=8), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('status', sa.String(length=12), nullable=False),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('rate_limited', sa.Integer(), nullable=False),
    sa.Column('fetch_ms', sa.Integer(), nullable=False),
    sa.Column('write_ms', sa.Integer(), nullable=False),
    sa.Column('rows_in', sa.Integer(), nullable=False),
    sa.Column('rows_new', sa.Integer(), nullable=False),
    sa.Column('rows_revised', sa.Integer(), nullable=False),
    sa.Column('quality', sa.JSON(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('actor', sa.String(length=320), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('data_ingestion_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_data_ingestion_runs_dataset'), ['dataset'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_ingestion_runs_provider'), ['provider'], unique=False)
        batch_op.create_index(batch_op.f('ix_data_ingestion_runs_started_at'), ['started_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('data_ingestion_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_data_ingestion_runs_started_at'))
        batch_op.drop_index(batch_op.f('ix_data_ingestion_runs_provider'))
        batch_op.drop_index(batch_op.f('ix_data_ingestion_runs_dataset'))

    op.drop_table('data_ingestion_runs')
    op.drop_table('data_series')
