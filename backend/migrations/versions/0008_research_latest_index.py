"""index for the latest research item of each title (validation lists)

Revision ID: 0008
Revises: 0007
"""
from alembic import op


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Additive only: no data is changed. Serves "latest validation of each (strategy, instrument, timeframe)".
    op.create_index('ix_research_kind_title_created', 'research_items', ['kind', 'title', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_research_kind_title_created', table_name='research_items')
