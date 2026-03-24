"""add tweet engagement metrics to content table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-24 13:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Engagement metrics — populated by performance tracking job after publishing
    op.add_column('content', sa.Column('tweet_id', sa.String(length=100), nullable=True))
    op.add_column('content', sa.Column('likes', sa.Integer(), nullable=True))
    op.add_column('content', sa.Column('retweets', sa.Integer(), nullable=True))
    op.add_column('content', sa.Column('replies', sa.Integer(), nullable=True))
    op.add_column('content', sa.Column('impressions', sa.Integer(), nullable=True))
    op.add_column('content', sa.Column('engagement_score', sa.Float(), nullable=True))
    op.add_column('content', sa.Column('metrics_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('content', 'metrics_updated_at')
    op.drop_column('content', 'engagement_score')
    op.drop_column('content', 'impressions')
    op.drop_column('content', 'replies')
    op.drop_column('content', 'retweets')
    op.drop_column('content', 'likes')
    op.drop_column('content', 'tweet_id')
