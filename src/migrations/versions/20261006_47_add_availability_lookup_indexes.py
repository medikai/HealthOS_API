"""add availability lookup indexes

Revision ID: 20261006_47
Revises: 20261006_46

Additive index-only change. The patient availability endpoint resolves a
practitioner schedule, legacy rules/exceptions, and overlapping appointments
per calendar day; these composites match those predicates at scale. No data
is modified.

Manual DataGrip equivalent (run, then `alembic stamp 20261006_47`):

    CREATE INDEX IF NOT EXISTS ix_care_appointment_practitioner_status_start
        ON care.appointment (practitioner_id, status, scheduled_start);
    CREATE INDEX IF NOT EXISTS ix_care_practitioner_schedule_lookup
        ON care.practitioner_schedule
            (organization_id, facility_id, practitioner_id, is_active, effective_from);
    CREATE INDEX IF NOT EXISTS ix_care_practitioner_availability_exception_lookup
        ON care.practitioner_availability_exception
            (organization_id, facility_id, practitioner_id, exception_date);
    CREATE INDEX IF NOT EXISTS ix_care_practitioner_availability_rule_lookup
        ON care.practitioner_availability_rule
            (organization_id, facility_id, practitioner_id, status);
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20261006_47"
down_revision: str | None = "20261006_46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_care_appointment_practitioner_status_start",
        "appointment",
        ["practitioner_id", "status", "scheduled_start"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_practitioner_schedule_lookup",
        "practitioner_schedule",
        ["organization_id", "facility_id", "practitioner_id", "is_active", "effective_from"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_practitioner_availability_exception_lookup",
        "practitioner_availability_exception",
        ["organization_id", "facility_id", "practitioner_id", "exception_date"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_practitioner_availability_rule_lookup",
        "practitioner_availability_rule",
        ["organization_id", "facility_id", "practitioner_id", "status"],
        schema="care",
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_care_practitioner_availability_rule_lookup",
        table_name="practitioner_availability_rule",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_practitioner_availability_exception_lookup",
        table_name="practitioner_availability_exception",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_practitioner_schedule_lookup",
        table_name="practitioner_schedule",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_appointment_practitioner_status_start",
        table_name="appointment",
        schema="care",
        if_exists=True,
    )
