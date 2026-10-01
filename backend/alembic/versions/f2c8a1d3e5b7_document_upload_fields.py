"""document upload fields: file_size, status, error_message

Revision ID: f2c8a1d3e5b7
Revises: a3f7c2e9b1d4
Create Date: 2026-10-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c8a1d3e5b7'
down_revision: Union[str, None] = 'a3f7c2e9b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('documents', sa.Column('file_size', sa.BigInteger(), nullable=False, server_default='0'))
    op.add_column('documents', sa.Column('status', sa.String(length=20), nullable=False, server_default='processing'))
    op.add_column('documents', sa.Column('error_message', sa.Text(), nullable=True))
    op.drop_column('documents', 'indexed')


def downgrade() -> None:
    op.add_column('documents', sa.Column('indexed', sa.Boolean(), nullable=False, server_default='false'))
    op.drop_column('documents', 'error_message')
    op.drop_column('documents', 'status')
    op.drop_column('documents', 'file_size')
