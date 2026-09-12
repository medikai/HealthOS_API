"""create organization-scoped people and patients"""

from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_03"
down_revision: Union[str, None] = "20260910_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table("person", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("first_name", sa.String(100), nullable=False), sa.Column("last_name", sa.String(100)), sa.Column("phone", sa.String(32)), sa.Column("email", sa.String(320)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("organization_id", "phone", name="uq_identity_person_org_phone"), schema="identity")
    op.create_index("ix_identity_person_organization_id", "person", ["organization_id"], schema="identity")
    op.create_index("ix_identity_person_phone", "person", ["phone"], schema="identity")
    op.create_table("patient", sa.Column("id", uuid, primary_key=True), sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False), sa.Column("person_id", uuid, sa.ForeignKey("identity.person.id"), nullable=False), sa.Column("mrn", sa.String(32), nullable=False), sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.UniqueConstraint("organization_id", "person_id", name="uq_identity_patient_org_person"), sa.UniqueConstraint("organization_id", "mrn", name="uq_identity_patient_org_mrn"), schema="identity")
    op.create_index("ix_identity_patient_organization_id", "patient", ["organization_id"], schema="identity")
    op.create_index("ix_identity_patient_person_id", "patient", ["person_id"], schema="identity")
    op.create_index("ix_identity_patient_mrn", "patient", ["mrn"], schema="identity")


def downgrade() -> None:
    op.drop_table("patient", schema="identity")
    op.drop_table("person", schema="identity")
