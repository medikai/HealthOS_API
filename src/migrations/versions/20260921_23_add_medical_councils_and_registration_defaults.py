"""add medical council master and registration defaults

Revision ID: 20260921_23
Revises: 20260921_22
"""

import json
from collections.abc import Sequence
from datetime import time
from uuid import NAMESPACE_URL, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_23"
down_revision: str | None = "20260921_22"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEDICAL_COUNCILS = [
    ("nmc", "National Medical Commission", None),
    ("apmc", "Andhra Pradesh Medical Council", "AP"),
    ("arpmc", "Arunachal Pradesh Medical Council", "AR"),
    ("amc", "Assam Medical Council", "AS"),
    ("bmc", "Bihar Medical Council", "BR"),
    ("cgmc", "Chattisgarh Medical Council", "CG"),
    ("dmc", "Delhi Medical Council", "DL"),
    ("gmc_goa", "Goa Medical Council", "GA"),
    ("gmc_gujarat", "Gujarat Medical Council", "GJ"),
    ("hmc", "Haryana Medical Council", "HR"),
    ("hpmc", "Himanchal Pradesh Medical Council", "HP"),
    ("jkmc", "Jammu & Kashmir Medical Council", "JK"),
    ("jmc", "Jharkhand Medical Council", "JH"),
    ("kmc", "Karnataka Medical Council", "KA"),
    ("kerala_mc", "Kerala Medical Council", "KL"),
    ("mpmc", "Madhya Pradesh Medical Council", "MP"),
    ("mmc", "Maharashtra Medical Council", "MH"),
    ("manipur_mc", "Manipur Medical Council", "MN"),
    ("mizoram_mc", "Mizoram Medical Council", "MZ"),
    ("nagaland_mc", "Nagaland Medical Council", "NL"),
    ("ocmr", "Orissa Council of Medical Registration", "OD"),
    ("pmc", "Punjab Medical Council", "PB"),
    ("rmc", "Rajasthan Medical Council", "RJ"),
    ("smc", "Sikkim Medical Council", "SK"),
    ("tnmc", "Tamil Nadu Medical Council", "TN"),
    ("tsmc", "Telangana State Medical Council", "TS"),
    ("tripura_smc", "Tripura State Medical Council", "TR"),
    ("upmc", "Uttar Pradesh Medical Council", "UP"),
    ("ukmc", "Uttarakhand Medical Council", "UK"),
    ("wbmc", "West Bengal Medical Council", "WB"),
]


def _id(kind: str, *values: object):
    return uuid5(NAMESPACE_URL, ":".join(("healthos", kind, *(str(value) for value in values))))


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        CREATE TABLE platform.medical_council (
            id UUID PRIMARY KEY,
            code VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            state_code VARCHAR(2),
            country_code VARCHAR(2) NOT NULL DEFAULT 'IN',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """))
    bind.execute(sa.text("CREATE INDEX ix_platform_medical_council_code ON platform.medical_council (code)"))
    for code, name, state_code in MEDICAL_COUNCILS:
        bind.execute(sa.text("""
            INSERT INTO platform.medical_council (id, code, name, state_code)
            VALUES (:id, :code, :name, :state_code)
        """), {"id": _id("medical-council", code), "code": code, "name": name, "state_code": state_code})

    bind.execute(sa.text("""
        ALTER TABLE identity.practitioner
            ADD COLUMN medical_council_id UUID REFERENCES platform.medical_council(id) ON DELETE SET NULL
    """))
    bind.execute(sa.text("""
        CREATE INDEX ix_identity_practitioner_medical_council_id
            ON identity.practitioner (medical_council_id)
    """))
    bind.execute(sa.text("""
        ALTER TABLE identity.user_account
            ADD COLUMN registration_specialty VARCHAR(255),
            ADD COLUMN registration_specialty_id UUID REFERENCES platform.specialty(id) ON DELETE SET NULL,
            ADD COLUMN registration_medical_council_id UUID REFERENCES platform.medical_council(id) ON DELETE SET NULL,
            ADD COLUMN registration_medical_council_reg_no VARCHAR(100)
    """))

    # Every facility needs a real schedule row; an in-memory default cannot produce bookable slots.
    facilities = bind.execute(sa.text("SELECT id FROM organization.facility WHERE is_active")).scalars().all()
    for facility_id in facilities:
        bind.execute(sa.text("""
            INSERT INTO organization.facility_schedule
                (id, facility_id, operating_start, operating_end, slot_interval_minutes, days_of_week, timezone)
            VALUES
                (:id, :facility_id, '08:00', '20:00', 30,
                 '["monday","tuesday","wednesday","thursday","friday","saturday"]', 'Asia/Kolkata')
            ON CONFLICT (facility_id) DO NOTHING
        """), {"id": _id("facility-schedule", facility_id), "facility_id": facility_id})

    rows = bind.execute(sa.text("""
        SELECT p.organization_id, f.id AS facility_id, p.id AS practitioner_id,
               s.operating_start, s.operating_end, s.slot_interval_minutes, s.days_of_week
        FROM identity.practitioner p
        JOIN organization.facility f ON f.organization_id = p.organization_id AND f.is_active
        JOIN organization.facility_schedule s ON s.facility_id = f.id
        WHERE p.is_active
          AND NOT EXISTS (
              SELECT 1 FROM care.practitioner_availability_rule r
              WHERE r.facility_id = f.id AND r.practitioner_id = p.id
          )
    """)).mappings().all()
    weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for row in rows:
        for day in json.loads(row["days_of_week"]):
            day = day.lower()
            if day not in weekdays:
                continue
            weekday = weekdays.index(day)
            bind.execute(sa.text("""
                INSERT INTO care.practitioner_availability_rule
                    (id, organization_id, facility_id, practitioner_id, weekday,
                     start_time, end_time, slot_duration_minutes, valid_from, status)
                VALUES
                    (:id, :organization_id, :facility_id, :practitioner_id, :weekday,
                     :start_time, :end_time, :slot_minutes, CURRENT_DATE, 'active')
                ON CONFLICT (facility_id, practitioner_id, weekday, start_time) DO NOTHING
            """), {
                "id": _id("registration-availability", row["facility_id"], row["practitioner_id"], weekday),
                "organization_id": row["organization_id"],
                "facility_id": row["facility_id"],
                "practitioner_id": row["practitioner_id"],
                "weekday": weekday,
                "start_time": time.fromisoformat(row["operating_start"]),
                "end_time": time.fromisoformat(row["operating_end"]),
                "slot_minutes": row["slot_interval_minutes"],
            })


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        ALTER TABLE identity.user_account
            DROP COLUMN registration_medical_council_reg_no,
            DROP COLUMN registration_medical_council_id,
            DROP COLUMN registration_specialty_id,
            DROP COLUMN registration_specialty
    """))
    bind.execute(sa.text("ALTER TABLE identity.practitioner DROP COLUMN medical_council_id"))
    bind.execute(sa.text("DROP TABLE platform.medical_council"))
