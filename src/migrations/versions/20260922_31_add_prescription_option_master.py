"""add India prescription option master

Revision ID: 20260922_31
Revises: 20260922_30
"""

import uuid as uuid_pkg
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260922_31"
down_revision: str | None = "20260922_30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OPTIONS = [
    ("dosage", "quarter_tablet", "¼ tablet", "¼ tablet", None),
    ("dosage", "half_tablet", "½ tablet", "½ tablet", None),
    ("dosage", "one_tablet", "1 tablet", "1 tablet", None),
    ("dosage", "one_half_tablets", "1½ tablets", "1½ tablets", None),
    ("dosage", "two_tablets", "2 tablets", "2 tablets", None),
    ("dosage", "one_capsule", "1 capsule", "1 capsule", None),
    ("dosage", "five_ml", "5 mL", "5 mL", None),
    ("dosage", "ten_ml", "10 mL", "10 mL", None),
    ("dosage", "fifteen_ml", "15 mL", "15 mL", None),
    ("dosage", "one_drop", "1 drop", "1 drop", None),
    ("dosage", "two_drops", "2 drops", "2 drops", None),
    ("dosage", "one_puff", "1 puff", "1 puff", None),
    ("dosage", "two_puffs", "2 puffs", "2 puffs", None),
    ("dosage", "one_application", "1 application", "1 application", None),
    ("dosage", "one_sachet", "1 sachet", "1 sachet", None),
    ("route", "oral", "Oral", "Oral", None),
    ("route", "sublingual", "Sublingual", "Sublingual", None),
    ("route", "buccal", "Buccal", "Buccal", None),
    ("route", "topical", "Topical", "Topical", None),
    ("route", "inhalation", "Inhalation", "Inhalation", None),
    ("route", "nebulization", "Nebulization", "Nebulization", None),
    ("route", "intranasal", "Intranasal", "Intranasal", None),
    ("route", "ophthalmic", "Eye (ophthalmic)", "Ophthalmic", None),
    ("route", "otic", "Ear (otic)", "Otic", None),
    ("route", "rectal", "Rectal", "Rectal", None),
    ("route", "vaginal", "Vaginal", "Vaginal", None),
    ("route", "subcutaneous", "Subcutaneous", "Subcutaneous", None),
    ("route", "intramuscular", "Intramuscular", "Intramuscular", None),
    ("route", "intravenous", "Intravenous", "Intravenous", None),
    ("route", "intradermal", "Intradermal", "Intradermal", None),
    ("frequency", "od", "Once daily (OD)", "Once daily", "OD"),
    ("frequency", "bd", "Twice daily (BD/BID)", "Twice daily", "BD/BID"),
    ("frequency", "tds", "Three times daily (TDS/TID)", "Three times daily", "TDS/TID"),
    ("frequency", "qid", "Four times daily (QID)", "Four times daily", "QID"),
    ("frequency", "q4h", "Every 4 hours", "Every 4 hours", None),
    ("frequency", "q6h", "Every 6 hours", "Every 6 hours", None),
    ("frequency", "q8h", "Every 8 hours", "Every 8 hours", None),
    ("frequency", "q12h", "Every 12 hours", "Every 12 hours", None),
    ("frequency", "alternate_days", "On alternate days", "On alternate days", None),
    ("frequency", "weekly", "Once weekly", "Once weekly", None),
    ("frequency", "sos", "As needed (SOS)", "As needed", "SOS"),
    ("frequency", "stat", "Immediately (STAT)", "Immediately", "STAT"),
    ("timing", "early_morning", "Early morning", "Early morning", "CM"),
    ("timing", "morning", "Morning", "Morning", None),
    ("timing", "afternoon", "Afternoon", "Afternoon", None),
    ("timing", "evening", "Evening", "Evening", None),
    ("timing", "bedtime", "At bedtime", "At bedtime", "HS"),
    ("timing", "before_meals", "Before meals", "Before meals", "AC"),
    ("timing", "after_meals", "After meals", "After meals", "PC"),
    ("timing", "with_meals", "With meals", "With meals", None),
    ("timing", "empty_stomach", "On an empty stomach", "On an empty stomach", None),
    ("duration", "one_day", "1 day", "1 day", None),
    ("duration", "three_days", "3 days", "3 days", None),
    ("duration", "five_days", "5 days", "5 days", None),
    ("duration", "seven_days", "7 days", "7 days", None),
    ("duration", "ten_days", "10 days", "10 days", None),
    ("duration", "fourteen_days", "14 days", "14 days", None),
    ("duration", "twenty_one_days", "21 days", "21 days", None),
    ("duration", "twenty_eight_days", "28 days", "28 days", None),
    ("duration", "one_month", "1 month", "1 month", None),
    ("duration", "two_months", "2 months", "2 months", None),
    ("duration", "three_months", "3 months", "3 months", None),
    ("duration", "until_review", "Continue until review", "Continue until review", None),
    ("instructions", "shake_well", "Shake well before use", "Shake well before use", None),
    ("instructions", "swallow_whole", "Swallow whole", "Swallow whole", None),
    ("instructions", "do_not_crush", "Do not crush or chew", "Do not crush or chew", None),
    ("instructions", "dissolve_in_water", "Dissolve in water before taking", "Dissolve in water before taking", None),
    ("instructions", "complete_course", "Complete the full course", "Complete the full course", None),
    ("instructions", "external_use", "For external use only", "For external use only", None),
    ("instructions", "apply_thin_layer", "Apply a thin layer", "Apply a thin layer", None),
    ("instructions", "rinse_after_use", "Rinse mouth after use", "Rinse mouth after use", None),
    ("instructions", "drink_water", "Take with a full glass of water", "Take with a full glass of water", None),
]


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "prescription_option",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("country_code", sa.String(2), nullable=False, server_default="IN"),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("label", sa.String(128), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('dosage', 'route', 'frequency', 'timing', 'duration', 'instructions')",
            name="ck_platform_prescription_option_category",
        ),
        sa.UniqueConstraint(
            "country_code", "category", "code",
            name="uq_platform_prescription_option_country_category_code",
        ),
        schema="platform",
    )
    op.create_index(
        "ix_platform_prescription_option_category",
        "prescription_option", ["category"], schema="platform",
    )

    table = sa.table(
        "prescription_option",
        sa.column("id", uuid), sa.column("country_code", sa.String),
        sa.column("category", sa.String), sa.column("code", sa.String),
        sa.column("label", sa.String), sa.column("value", sa.String),
        sa.column("description", sa.String), sa.column("sort_order", sa.Integer),
        sa.column("is_active", sa.Boolean),
        schema="platform",
    )
    positions: defaultdict[str, int] = defaultdict(int)
    rows = []
    for category, code, label, value, description in OPTIONS:
        positions[category] += 10
        rows.append({
            "id": uuid_pkg.uuid5(uuid_pkg.NAMESPACE_URL, f"healthos:prescription-option:IN:{category}:{code}"),
            "country_code": "IN", "category": category, "code": code,
            "label": label, "value": value, "description": description,
            "sort_order": positions[category], "is_active": True,
        })
    op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_index("ix_platform_prescription_option_category", table_name="prescription_option", schema="platform")
    op.drop_table("prescription_option", schema="platform")
