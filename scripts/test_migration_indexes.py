"""
Verification script for HealthOS Database Indexes.
Audits the presence of the 10 foreign key indexes across care and organization schemas.
Usage:
    python scripts/test_migration_indexes.py
"""

import asyncio
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text
from src.app.core.db.database import async_engine

EXPECTED_INDEXES = [
    ("care", "encounter", "ix_care_encounter_practitioner_id", "practitioner_id"),
    ("care", "queue_entry", "ix_care_queue_entry_practitioner_id", "practitioner_id"),
    ("care", "queue_entry", "ix_care_queue_entry_appointment_id", "appointment_id"),
    ("care", "queue_entry", "ix_care_queue_entry_organization_id", "organization_id"),
    ("care", "practitioner_availability_rule", "ix_care_practitioner_availability_rule_organization_id", "organization_id"),
    ("care", "practitioner_availability_exception", "ix_care_practitioner_availability_exception_organization_id", "organization_id"),
    ("care", "soap_note", "ix_care_soap_note_signed_by_user_id", "signed_by_user_id"),
    ("care", "prescription", "ix_care_prescription_signed_by_user_id", "signed_by_user_id"),
    ("care", "vital", "ix_care_vital_recorded_by_user_id", "recorded_by_user_id"),
    ("organization", "staff_invitation", "ix_organization_staff_invitation_facility_id", "facility_id"),
]

async def check_indexes():
    print("=" * 70)
    print("  HEALTHOS FOREIGN KEY INDEX VERIFICATION")
    print("=" * 70)

    query = text("""
        SELECT schemaname, tablename, indexname 
        FROM pg_indexes 
        WHERE schemaname IN ('care', 'organization', 'identity', 'governance', 'platform');
    """)

    async with async_engine.connect() as conn:
        result = await conn.execute(query)
        rows = result.fetchall()
        existing = {(r[0], r[1], r[2]) for r in rows}

    missing = []
    found = []

    for schema, table, index_name, col in EXPECTED_INDEXES:
        if (schema, table, index_name) in existing:
            found.append((schema, table, index_name, col))
            print(f"  [OK] {schema}.{table} ({col}) -> {index_name}")
        else:
            missing.append((schema, table, index_name, col))
            print(f"  [MISSING] {schema}.{table} ({col}) -> {index_name}")

    print("-" * 70)
    print(f"Status: {len(found)} / {len(EXPECTED_INDEXES)} foreign key indexes present.")

    if missing:
        print(f"\nTo apply the missing {len(missing)} indexes, run:")
        print("    uv run alembic upgrade head")
    else:
        print("\nAll 10 foreign key indexes are active and verified!")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(check_indexes())

