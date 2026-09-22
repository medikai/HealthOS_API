"""add relevant patients query optimization indexes

Revision ID: 20260922_25
Revises: 20260922_24
Create Date: 2026-09-22 10:08:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260922_25"
down_revision: str | None = "20260922_24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. care.encounter: facility_id + status + completed_at (Tier 3 recent visits)
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_care_encounter_facility_status_completed
            ON care.encounter (facility_id, status, completed_at DESC);
            """
        )
    )

    # 2. care.encounter: organization_id + status + completed_at (Tier 3 recent visits org-level)
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_care_encounter_org_status_completed
            ON care.encounter (organization_id, status, completed_at DESC);
            """
        )
    )

    # 3. identity.patient: organization_id + is_active + created_at (Tier 4 recently registered active patients)
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_identity_patient_org_active_created
            ON identity.patient (organization_id, is_active, created_at DESC);
            """
        )
    )

    # 4. care.appointment: organization_id + status + scheduled_start (Tier 1 & 2 appointment queries org-level)
    op.execute(
        sa.text(
            """
            CREATE INDEX IF NOT EXISTS ix_care_appointment_org_status_start
            ON care.appointment (organization_id, status, scheduled_start);
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_appointment_org_status_start",
        table_name="appointment",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_identity_patient_org_active_created",
        table_name="patient",
        schema="identity",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_encounter_org_status_completed",
        table_name="encounter",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_encounter_facility_status_completed",
        table_name="encounter",
        schema="care",
        if_exists=True,
    )
