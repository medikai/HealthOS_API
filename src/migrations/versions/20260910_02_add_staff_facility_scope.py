"""add facility scope to staff assignments"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_02"
down_revision: Union[str, None] = "20260910_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("staff_assignment", sa.Column("facility_id", postgresql.UUID(as_uuid=True), nullable=True), schema="organization")
    op.create_foreign_key("fk_staff_assignment_facility", "staff_assignment", "facility", ["facility_id"], ["id"], source_schema="organization", referent_schema="organization")
    op.create_index("ix_organization_staff_assignment_facility_id", "staff_assignment", ["facility_id"], schema="organization")


def downgrade() -> None:
    op.drop_index("ix_organization_staff_assignment_facility_id", table_name="staff_assignment", schema="organization")
    op.drop_constraint("fk_staff_assignment_facility", "staff_assignment", schema="organization", type_="foreignkey")
    op.drop_column("staff_assignment", "facility_id", schema="organization")
