"""add foreign key indexes to care and organization tables

Revision ID: 20260919_16
Revises: 20260918_15
Create Date: 2026-09-19 15:42:00.000000

"""
from typing import Sequence, Union
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260919_16"
down_revision: Union[str, None] = "20260918_15"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. care.encounter: practitioner_id
    op.create_index(
        "ix_care_encounter_practitioner_id",
        "encounter",
        ["practitioner_id"],
        schema="care",
        if_not_exists=True,
    )

    # 2. care.queue_entry: practitioner_id, appointment_id, organization_id
    op.create_index(
        "ix_care_queue_entry_practitioner_id",
        "queue_entry",
        ["practitioner_id"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_queue_entry_appointment_id",
        "queue_entry",
        ["appointment_id"],
        schema="care",
        if_not_exists=True,
    )
    op.create_index(
        "ix_care_queue_entry_organization_id",
        "queue_entry",
        ["organization_id"],
        schema="care",
        if_not_exists=True,
    )

    # 3. care.practitioner_availability_rule: organization_id
    op.create_index(
        "ix_care_practitioner_availability_rule_organization_id",
        "practitioner_availability_rule",
        ["organization_id"],
        schema="care",
        if_not_exists=True,
    )

    # 4. care.practitioner_availability_exception: organization_id
    op.create_index(
        "ix_care_practitioner_availability_exception_organization_id",
        "practitioner_availability_exception",
        ["organization_id"],
        schema="care",
        if_not_exists=True,
    )

    # 5. care.soap_note: signed_by_user_id
    op.create_index(
        "ix_care_soap_note_signed_by_user_id",
        "soap_note",
        ["signed_by_user_id"],
        schema="care",
        if_not_exists=True,
    )

    # 6. care.prescription: signed_by_user_id
    op.create_index(
        "ix_care_prescription_signed_by_user_id",
        "prescription",
        ["signed_by_user_id"],
        schema="care",
        if_not_exists=True,
    )

    # 7. care.vital: recorded_by_user_id
    op.create_index(
        "ix_care_vital_recorded_by_user_id",
        "vital",
        ["recorded_by_user_id"],
        schema="care",
        if_not_exists=True,
    )

    # 8. organization.staff_invitation: facility_id
    op.create_index(
        "ix_organization_staff_invitation_facility_id",
        "staff_invitation",
        ["facility_id"],
        schema="organization",
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_staff_invitation_facility_id",
        table_name="staff_invitation",
        schema="organization",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_vital_recorded_by_user_id",
        table_name="vital",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_prescription_signed_by_user_id",
        table_name="prescription",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_soap_note_signed_by_user_id",
        table_name="soap_note",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_practitioner_availability_exception_organization_id",
        table_name="practitioner_availability_exception",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_practitioner_availability_rule_organization_id",
        table_name="practitioner_availability_rule",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_queue_entry_organization_id",
        table_name="queue_entry",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_queue_entry_appointment_id",
        table_name="queue_entry",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_queue_entry_practitioner_id",
        table_name="queue_entry",
        schema="care",
        if_exists=True,
    )
    op.drop_index(
        "ix_care_encounter_practitioner_id",
        table_name="encounter",
        schema="care",
        if_exists=True,
    )

