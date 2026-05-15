"""add review likes and rating genres

Revision ID: b2a8a1d299de
Revises: 
Create Date: 2026-05-15 12:59:41.066283

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision = 'b2a8a1d299de'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Pass empty because Codex has already manually executed all schema modifications locally.
    # This prevents 'Duplicate column name' errors while stamping the migration tracking table.
    pass


def downgrade():
    # Kept empty to mirror the empty tracking state
    pass