"""add specialist masters and practitioner mappings

Revision ID: 20260920_19
Revises: 20260920_18
Create Date: 2026-09-20
"""

from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

import sqlalchemy as sa
from alembic import op

revision: str = "20260920_19"
down_revision: str | None = "20260920_18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPECIALTY_UUIDS: dict[str, UUID] = {
    "general_practice": UUID("01a0b95d-0001-7000-8000-000000000001"),
    "gynecology": UUID("01a0b95d-0001-7000-8000-000000000002"),
    "obstetrics_and_gynecology": UUID("01a0b95d-0001-7000-8000-000000000002"),
    "virology": UUID("01a0b95d-0001-7000-8000-000000000003"),
    "general_medicine": UUID("01a0b95d-0001-7000-8000-000000000004"),
    "orthopaedics": UUID("01a0b95d-0001-7000-8000-000000000005"),
    "paediatrics": UUID("01a0b95d-0001-7000-8000-000000000006"),
    "cardiology": UUID("01a0b95d-0001-7000-8000-000000000007"),
    "dermatology": UUID("01a0b95d-0001-7000-8000-000000000008"),
    "neurology": UUID("01a0b95d-0001-7000-8000-000000000009"),
    "oncology": UUID("01a0b95d-0001-7000-8000-000000000010"),
    "ent": UUID("01a0b95d-0001-7000-8000-000000000011"),
    "ophthalmology": UUID("01a0b95d-0001-7000-8000-000000000012"),
    "psychiatry": UUID("01a0b95d-0001-7000-8000-000000000013"),
}

SUB_SPECIALTY_UUIDS: dict[str, UUID] = {
    "general_practice": UUID("01a0b95d-0002-7000-8000-000000000001"),
    "clinical_virology": UUID("01a0b95d-0002-7000-8000-000000000002"),
    "maternal_fetal_medicine": UUID("01a0b95d-0002-7000-8000-000000000003"),
    "gynecologic_oncology": UUID("01a0b95d-0002-7000-8000-000000000004"),
    "interventional_cardiology": UUID("01a0b95d-0002-7000-8000-000000000005"),
    "pediatric_orthopaedics": UUID("01a0b95d-0002-7000-8000-000000000006"),
    "reproductive_endocrinology": UUID("01a0b95d-0002-7000-8000-000000000007"),
    "urogynecology": UUID("01a0b95d-0002-7000-8000-000000000008"),
    "family_medicine": UUID("01a0b95d-0002-7000-8000-000000000009"),
    "electrophysiology": UUID("01a0b95d-0002-7000-8000-000000000010"),
    "sports_medicine": UUID("01a0b95d-0002-7000-8000-000000000011"),
    "neonatology": UUID("01a0b95d-0002-7000-8000-000000000012"),
}

DESIGNATION_UUIDS: dict[str, UUID] = {
    "senior_gynecologist": UUID("01a0b95d-0003-7000-8000-000000000001"),
    "senior_gynecologist_obstetrician": UUID("01a0b95d-0003-7000-8000-000000000001"),
    "consultant_physician": UUID("01a0b95d-0003-7000-8000-000000000002"),
    "general_practitioner": UUID("01a0b95d-0003-7000-8000-000000000003"),
    "visiting_specialist": UUID("01a0b95d-0003-7000-8000-000000000004"),
    "resident_medical_officer": UUID("01a0b95d-0003-7000-8000-000000000005"),
    "entry_operator": UUID("01a0b95d-0003-7000-8000-000000000006"),
    "assistant": UUID("01a0b95d-0003-7000-8000-000000000007"),
    "practitioner": UUID("01a0b95d-0003-7000-8000-000000000008"),
    "administrator": UUID("01a0b95d-0003-7000-8000-000000000009"),
    "consultant_specialist": UUID("01a0b95d-0003-7000-8000-000000000010"),
    "senior_consultant": UUID("01a0b95d-0003-7000-8000-000000000011"),
    "medical_officer": UUID("01a0b95d-0003-7000-8000-000000000012"),
}

SEED_SPECIALTIES = [
    ("general_practice", "General Practice / Internal Medicine", "Comprehensive primary care for individuals and families."),
    ("gynecology", "Gynecology & Obstetrics", "Care for female reproductive system, pregnancy, childbirth, and postpartum."),
    ("virology", "Virology", "Diagnosis, management, and treatment of viral infections and pathology."),
    ("general_medicine", "General Medicine", "Comprehensive non-surgical medical care of adult illnesses."),
    ("orthopaedics", "Orthopaedics", "Musculoskeletal system care, joints, bones, and sports injuries."),
    ("paediatrics", "Paediatrics", "Comprehensive medical care for infants, children, and adolescents."),
    ("cardiology", "Cardiology", "Disorders of the heart and cardiovascular system."),
    ("dermatology", "Dermatology", "Diagnosis and treatment of skin, hair, and nail conditions."),
    ("neurology", "Neurology", "Disorders of the brain, spinal cord, nerves, and muscle systems."),
    ("oncology", "Oncology", "Diagnosis and treatment of tumors and cancerous conditions."),
    ("ent", "ENT (Ear, Nose & Throat)", "Diseases and conditions of the ear, nose, throat, and related head structures."),
    ("ophthalmology", "Ophthalmology", "Eye and visual care including medical and surgical diagnostics."),
    ("psychiatry", "Psychiatry", "Mental health, behavioral conditions, and cognitive well-being."),
]

SEED_SUB_SPECIALTIES = [
    ("general_practice", "general_practice", "General Practice", "General outpatient clinical care."),
    ("general_practice", "family_medicine", "Family Medicine", "Comprehensive continuity of care across all life stages."),
    ("virology", "clinical_virology", "Clinical Virology", "Diagnostic viral immunology and antiviral therapy."),
    ("gynecology", "maternal_fetal_medicine", "Maternal-Fetal Medicine", "High-risk pregnancy and prenatal diagnostics."),
    ("gynecology", "gynecologic_oncology", "Gynecologic Oncology", "Cancers of the female reproductive system."),
    ("gynecology", "reproductive_endocrinology", "Reproductive Endocrinology", "Hormonal functioning and fertility treatment."),
    ("gynecology", "urogynecology", "Urogynecology & Pelvic Reconstruction", "Pelvic floor disorders and reconstructive surgery."),
    ("cardiology", "interventional_cardiology", "Interventional Cardiology", "Catheter-based cardiac treatments."),
    ("cardiology", "electrophysiology", "Cardiac Electrophysiology", "Heart rhythm disorders and arrhythmia ablation."),
    ("orthopaedics", "pediatric_orthopaedics", "Pediatric Orthopaedics", "Musculoskeletal care for youth."),
    ("orthopaedics", "sports_medicine", "Sports Medicine & Arthroscopy", "Athletic injury rehabilitation and arthroscopic joint care."),
    ("paediatrics", "neonatology", "Neonatal-Perinatal Care", "Specialized intensive care for newborns and premature infants."),
]

SEED_DESIGNATIONS = [
    ("senior_gynecologist", "Senior Gynecologist & Obstetrician", "clinical"),
    ("consultant_physician", "Consultant Physician", "clinical"),
    ("consultant_specialist", "Consultant Specialist", "clinical"),
    ("general_practitioner", "General Practitioner", "clinical"),
    ("visiting_specialist", "Visiting Specialist", "clinical"),
    ("resident_medical_officer", "Resident Medical Officer", "clinical"),
    ("senior_consultant", "Senior Consultant", "clinical"),
    ("medical_officer", "Medical Officer", "clinical"),
    ("practitioner", "Practitioner", "clinical"),
    ("entry_operator", "Entry operator", "administrative"),
    ("assistant", "Assistant", "support"),
    ("administrator", "Administrator", "administrative"),
]


def _spec_id(code: str) -> UUID:
    clean = code.strip().lower()
    return SPECIALTY_UUIDS.get(clean, uuid5(NAMESPACE_URL, f"healthos:master:specialty:{clean}"))


def _sub_spec_id(code: str) -> UUID:
    clean = code.strip().lower()
    return SUB_SPECIALTY_UUIDS.get(clean, uuid5(NAMESPACE_URL, f"healthos:master:sub_specialty:{clean}"))


def _desig_id(code: str) -> UUID:
    clean = code.strip().lower()
    return DESIGNATION_UUIDS.get(clean, uuid5(NAMESPACE_URL, f"healthos:master:designation:{clean}"))


def upgrade() -> None:
    bind = op.get_bind()

    # 0. Ensure schema exists
    bind.execute(sa.text("CREATE SCHEMA IF NOT EXISTS platform;"))

    # 1. platform.specialty
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS platform.specialty (
            id UUID PRIMARY KEY,
            code VARCHAR(128) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_platform_specialty_code UNIQUE (code)
        );
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_platform_specialty_code ON platform.specialty (code);"))

    # 2. platform.sub_specialty
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS platform.sub_specialty (
            id UUID PRIMARY KEY,
            specialty_id UUID REFERENCES platform.specialty(id) ON DELETE SET NULL,
            code VARCHAR(128) NOT NULL,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_platform_sub_specialty_code UNIQUE (code)
        );
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_platform_sub_specialty_code ON platform.sub_specialty (code);"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_platform_sub_specialty_specialty_id ON platform.sub_specialty (specialty_id);"))

    # 3. platform.staff_designation
    bind.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS platform.staff_designation (
            id UUID PRIMARY KEY,
            code VARCHAR(128) NOT NULL,
            name VARCHAR(255) NOT NULL,
            category VARCHAR(64) DEFAULT 'clinical',
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            CONSTRAINT uq_platform_staff_designation_code UNIQUE (code)
        );
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_platform_staff_designation_code ON platform.staff_designation (code);"))

    # 4. identity.practitioner mapping columns
    bind.execute(sa.text("""
        ALTER TABLE identity.practitioner
            ADD COLUMN IF NOT EXISTS specialty_id UUID REFERENCES platform.specialty(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS sub_specialty_id UUID REFERENCES platform.sub_specialty(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS designation_id UUID REFERENCES platform.staff_designation(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS medical_council_reg_no VARCHAR(100),
            ADD COLUMN IF NOT EXISTS has_prescription_authority BOOLEAN NOT NULL DEFAULT TRUE,
            ADD COLUMN IF NOT EXISTS prescription_authority_status VARCHAR(32) NOT NULL DEFAULT 'authorized';
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_identity_practitioner_specialty_id ON identity.practitioner (specialty_id);"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_identity_practitioner_sub_specialty_id ON identity.practitioner (sub_specialty_id);"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_identity_practitioner_designation_id ON identity.practitioner (designation_id);"))

    # 5. organization.staff_invitation mapping columns
    bind.execute(sa.text("""
        ALTER TABLE organization.staff_invitation
            ADD COLUMN IF NOT EXISTS specialty_id UUID REFERENCES platform.specialty(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS sub_specialty_id UUID REFERENCES platform.sub_specialty(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS designation_id UUID REFERENCES platform.staff_designation(id) ON DELETE SET NULL,
            ADD COLUMN IF NOT EXISTS medical_council_reg_no VARCHAR(100);
    """))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_organization_staff_invitation_specialty_id ON organization.staff_invitation (specialty_id);"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_organization_staff_invitation_sub_specialty_id ON organization.staff_invitation (sub_specialty_id);"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_organization_staff_invitation_designation_id ON organization.staff_invitation (designation_id);"))

    # 6. Seed initial master data
    for code, name, desc in SEED_SPECIALTIES:
        bind.execute(
            sa.text("""
                INSERT INTO platform.specialty (id, code, name, description, is_active)
                VALUES (:id, :code, :name, :description, TRUE)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    is_active = TRUE;
            """),
            {"id": _spec_id(code), "code": code, "name": name, "description": desc},
        )

    for spec_code, code, name, desc in SEED_SUB_SPECIALTIES:
        bind.execute(
            sa.text("""
                INSERT INTO platform.sub_specialty (id, specialty_id, code, name, description, is_active)
                VALUES (:id, :specialty_id, :code, :name, :description, TRUE)
                ON CONFLICT (code) DO UPDATE SET
                    specialty_id = EXCLUDED.specialty_id,
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    is_active = TRUE;
            """),
            {"id": _sub_spec_id(code), "specialty_id": _spec_id(spec_code), "code": code, "name": name, "description": desc},
        )

    for code, name, cat in SEED_DESIGNATIONS:
        bind.execute(
            sa.text("""
                INSERT INTO platform.staff_designation (id, code, name, category, is_active)
                VALUES (:id, :code, :name, :category, TRUE)
                ON CONFLICT (code) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    is_active = TRUE;
            """),
            {"id": _desig_id(code), "code": code, "name": name, "category": cat},
        )

    # 7. Backfill existing practitioners with appropriate specialty, sub_specialty, designation
    cardio_spec = _spec_id("cardiology")
    cardio_sub = _sub_spec_id("interventional_cardiology")
    consultant_desig = _desig_id("consultant_physician")
    gp_spec = _spec_id("general_practice")
    gp_sub = _sub_spec_id("general_practice")
    gp_desig = _desig_id("general_practitioner")
    gen_med_spec = _spec_id("general_medicine")

    # Map Cardiology practitioners
    bind.execute(
        sa.text("""
            UPDATE identity.practitioner
            SET specialty_id = :spec_id,
                sub_specialty_id = :sub_spec_id,
                designation_id = :desig_id,
                has_prescription_authority = TRUE,
                prescription_authority_status = 'authorized',
                medical_council_reg_no = COALESCE(medical_council_reg_no, 'MCI-CARD-' || UPPER(SUBSTRING(id::text, 1, 6)))
            WHERE specialty ILIKE '%cardio%'
        """),
        {"spec_id": cardio_spec, "sub_spec_id": cardio_sub, "desig_id": consultant_desig},
    )

    # Map Internal Medicine / General Medicine practitioners
    bind.execute(
        sa.text("""
            UPDATE identity.practitioner
            SET specialty_id = :spec_id,
                sub_specialty_id = :sub_spec_id,
                designation_id = :desig_id,
                has_prescription_authority = TRUE,
                prescription_authority_status = 'authorized',
                medical_council_reg_no = COALESCE(medical_council_reg_no, 'MCI-MED-' || UPPER(SUBSTRING(id::text, 1, 6)))
            WHERE specialty ILIKE '%internal%' OR specialty ILIKE '%general medicine%'
        """),
        {"spec_id": gen_med_spec, "sub_spec_id": gp_sub, "desig_id": consultant_desig},
    )

    # Map General Practice / Family Medicine / Remaining practitioners
    bind.execute(
        sa.text("""
            UPDATE identity.practitioner
            SET specialty_id = COALESCE(specialty_id, :spec_id),
                sub_specialty_id = COALESCE(sub_specialty_id, :sub_spec_id),
                designation_id = COALESCE(designation_id, :desig_id),
                has_prescription_authority = TRUE,
                prescription_authority_status = 'authorized',
                medical_council_reg_no = COALESCE(medical_council_reg_no, 'MCI-GP-' || UPPER(SUBSTRING(id::text, 1, 6)))
            WHERE specialty_id IS NULL
        """),
        {"spec_id": gp_spec, "sub_spec_id": gp_sub, "desig_id": gp_desig},
    )

    # Backfill staff invitations for practitioners
    bind.execute(
        sa.text("""
            UPDATE organization.staff_invitation
            SET specialty_id = COALESCE(specialty_id, :spec_id),
                sub_specialty_id = COALESCE(sub_specialty_id, :sub_spec_id),
                designation_id = COALESCE(designation_id, :desig_id),
                medical_council_reg_no = COALESCE(medical_council_reg_no, 'MCI-INV-' || UPPER(SUBSTRING(id::text, 1, 6)))
            WHERE role_code = 'practitioner' AND specialty_id IS NULL
        """),
        {"spec_id": gp_spec, "sub_spec_id": gp_sub, "desig_id": gp_desig},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        ALTER TABLE identity.practitioner
            DROP COLUMN IF EXISTS has_prescription_authority,
            DROP COLUMN IF EXISTS prescription_authority_status,
            DROP COLUMN IF EXISTS medical_council_reg_no,
            DROP COLUMN IF EXISTS designation_id,
            DROP COLUMN IF EXISTS sub_specialty_id,
            DROP COLUMN IF EXISTS specialty_id;

        ALTER TABLE organization.staff_invitation
            DROP COLUMN IF EXISTS medical_council_reg_no,
            DROP COLUMN IF EXISTS designation_id,
            DROP COLUMN IF EXISTS sub_specialty_id,
            DROP COLUMN IF EXISTS specialty_id;

        DROP TABLE IF EXISTS platform.staff_designation CASCADE;
        DROP TABLE IF EXISTS platform.sub_specialty CASCADE;
        DROP TABLE IF EXISTS platform.specialty CASCADE;
    """))
