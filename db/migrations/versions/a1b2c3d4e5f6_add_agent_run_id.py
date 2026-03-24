"""add agent_run_id to content and interactions

Revision ID: a1b2c3d4e5f6
Revises: ed504fd0fd30
Create Date: 2026-03-24 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'ed504fd0fd30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Links content rows back to R2 autonomous run traces for fine-tuning
    op.add_column('content', sa.Column('agent_run_id', sa.Text(), nullable=True))
    op.add_column('interactions', sa.Column('agent_run_id', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('interactions', 'agent_run_id')
    op.drop_column('content', 'agent_run_id')
