"""add fragment and relevance to collection_items

Revision ID: a3f7c2e9b1d4
Revises: 6919c624d179
Create Date: 2026-10-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3f7c2e9b1d4'
down_revision: Union[str, None] = '6919c624d179'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('collection_items', sa.Column('fragment', sa.Text(), nullable=True))
    op.add_column('collection_items', sa.Column('relevance', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('collection_items', 'relevance')
    op.drop_column('collection_items', 'fragment')
