"""add patient realtime channel state and patient delivery recipient

Revision ID: 20261006_46
Revises: 20261006_45

Additive only: a patient-account-level channel generation table and a nullable
patient recipient on the durable delivery job (mutually exclusive with the
staff recipient). Applied to the local/dev database only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261006_46"
down_revision: str | None = "20261006_45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "patient_realtime_channel_state",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "patient_account_id",
            uuid,
            sa.ForeignKey("identity.patient_portal_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "patient_account_id",
            name="uq_communication_patient_realtime_channel_state_account",
        ),
        schema="communication",
    )
    op.create_index(
        "ix_communication_patient_realtime_account",
        "patient_realtime_channel_state",
        ["patient_account_id"],
        schema="communication",
    )

    op.add_column(
        "delivery_job",
        sa.Column("recipient_patient_id", uuid, nullable=True),
        schema="communication",
    )
    op.create_foreign_key(
        "fk_communication_delivery_job_recipient_patient",
        "delivery_job",
        "patient_portal_account",
        ["recipient_patient_id"],
        ["id"],
        source_schema="communication",
        referent_schema="identity",
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_communication_delivery_job_patient",
        "delivery_job",
        ["recipient_patient_id"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_communication_delivery_job_patient",
        table_name="delivery_job",
        schema="communication",
    )
    op.drop_constraint(
        "fk_communication_delivery_job_recipient_patient",
        "delivery_job",
        schema="communication",
        type_="foreignkey",
    )
    op.drop_column("delivery_job", "recipient_patient_id", schema="communication")

    op.drop_index(
        "ix_communication_patient_realtime_account",
        table_name="patient_realtime_channel_state",
        schema="communication",
    )
    op.drop_table("patient_realtime_channel_state", schema="communication")
