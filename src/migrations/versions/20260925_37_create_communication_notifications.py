"""create communication notifications, recipients and preferences"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_37"
down_revision: Union[str, None] = "20260925_36"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "notification_event",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(2), nullable=False),
        sa.Column("dedup_key", sa.String(320), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="update"),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("context", sa.String(512), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("actor_user_id", uuid, sa.ForeignKey("identity.user_account.id"), nullable=True),
        sa.Column("actor_name", sa.String(255), nullable=True),
        sa.Column("actor_role", sa.String(64), nullable=True),
        sa.Column("task_type", sa.String(32), nullable=True),
        sa.Column("task_id", sa.String(128), nullable=True),
        sa.Column("task_state", sa.String(32), nullable=False, server_default="none"),
        sa.Column("action_kind", sa.String(32), nullable=True),
        sa.Column("action_label", sa.String(64), nullable=True),
        sa.Column("resource_type", sa.String(64), nullable=True),
        sa.Column("resource_id", sa.String(128), nullable=True),
        sa.Column("coalesce_key", sa.String(160), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("superseded_by_id", uuid, sa.ForeignKey("communication.notification_event.id"), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("dedup_key", name="uq_communication_notification_event_dedup"),
        schema="communication",
    )
    for name, column in (
        ("organization_id", "organization_id"),
        ("event_type", "event_type"),
        ("category", "category"),
        ("priority", "priority"),
        ("facility_id", "facility_id"),
        ("task_id", "task_id"),
    ):
        op.create_index(
            f"ix_communication_notification_event_{name}",
            "notification_event",
            [column],
            schema="communication",
        )
    op.create_index(
        "ix_communication_notification_event_feed",
        "notification_event",
        ["organization_id", "updated_at"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_event_coalesce",
        "notification_event",
        ["coalesce_key"],
        schema="communication",
    )

    op.create_table(
        "notification_recipient",
        sa.Column("id", uuid, primary_key=True),
        sa.Column(
            "notification_id",
            uuid,
            sa.ForeignKey("communication.notification_event.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("facility_id", uuid, sa.ForeignKey("organization.facility.id"), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "notification_id",
            "staff_member_id",
            name="uq_communication_notification_recipient",
        ),
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_recipient_notification_id",
        "notification_recipient",
        ["notification_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_recipient_organization_id",
        "notification_recipient",
        ["organization_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_recipient_staff_member_id",
        "notification_recipient",
        ["staff_member_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_recipient_inbox",
        "notification_recipient",
        ["staff_member_id", "read_at"],
        schema="communication",
    )

    op.create_table(
        "notification_preference",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, sa.ForeignKey("organization.organization.id"), nullable=False),
        sa.Column("staff_member_id", uuid, sa.ForeignKey("organization.staff_member.id"), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("in_app", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("browser", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sound", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("desktop_alerts", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("quiet_hours_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("quiet_start", sa.String(5), nullable=False, server_default="22:00"),
        sa.Column("quiet_end", sa.String(5), nullable=False, server_default="07:00"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="Asia/Kolkata"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "organization_id",
            "staff_member_id",
            "category",
            name="uq_communication_notification_preference",
        ),
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_preference_organization_id",
        "notification_preference",
        ["organization_id"],
        schema="communication",
    )
    op.create_index(
        "ix_communication_notification_preference_staff_member_id",
        "notification_preference",
        ["staff_member_id"],
        schema="communication",
    )


def downgrade() -> None:
    op.drop_table("notification_preference", schema="communication")
    op.drop_table("notification_recipient", schema="communication")
    op.drop_table("notification_event", schema="communication")
