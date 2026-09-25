"""create communication schema, durable delivery jobs and realtime channel state"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_35"
down_revision: Union[str, None] = "20260923_34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS communication")
    uuid = postgresql.UUID(as_uuid=True)
    jsonb = postgresql.JSONB(astext_type=sa.Text())

    op.create_table(
        "delivery_job",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("dedup_key", sa.String(320), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("payload", jsonb, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=True),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("recipient_staff_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", jsonb, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_communication_delivery_job_dedup"),
        schema="communication",
    )
    op.create_index("ix_communication_delivery_job_channel", "delivery_job", ["channel"], schema="communication")
    op.create_index("ix_communication_delivery_job_organization_id", "delivery_job", ["organization_id"], schema="communication")
    op.create_index("ix_communication_delivery_job_facility_id", "delivery_job", ["facility_id"], schema="communication")
    op.create_index("ix_communication_delivery_job_recipient_staff_id", "delivery_job", ["recipient_staff_id"], schema="communication")
    op.create_index("ix_communication_delivery_job_claim", "delivery_job", ["status", "due_at"], schema="communication")
    op.create_index("ix_communication_delivery_job_lease", "delivery_job", ["status", "lease_expires_at"], schema="communication")
    op.create_index("ix_communication_delivery_job_recipient", "delivery_job", ["organization_id", "recipient_staff_id"], schema="communication")

    op.create_table(
        "realtime_channel_state",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id",
            "staff_member_id",
            name="uq_communication_realtime_channel_state_principal",
        ),
        schema="communication",
    )
    op.create_index(
        "ix_communication_realtime_channel_state_organization_id",
        "realtime_channel_state",
        ["organization_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_realtime_channel_state_staff_member_id",
        "realtime_channel_state",
        ["staff_member_id"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_table("realtime_channel_state", schema="communication")
    op.drop_table("delivery_job", schema="communication")
