"""add activity_events: full user activity log (clicks, input, navigation, search pipeline)

Revision ID: b5d4a9c1f3e2
Revises: f2c8a1d3e5b7
Create Date: 2026-11-04 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b5d4a9c1f3e2'
down_revision: Union[str, None] = 'f2c8a1d3e5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'activity_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('company_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('companies.id'), nullable=True),
        sa.Column('session_id', sa.String(length=64), nullable=True),
        sa.Column('event_type', sa.String(length=50), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_activity_events_session_id', 'activity_events', ['session_id'])
    op.create_index('ix_activity_events_event_type', 'activity_events', ['event_type'])
    op.create_index('ix_activity_events_created_at', 'activity_events', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_activity_events_created_at', table_name='activity_events')
    op.drop_index('ix_activity_events_event_type', table_name='activity_events')
    op.drop_index('ix_activity_events_session_id', table_name='activity_events')
    op.drop_table('activity_events')
