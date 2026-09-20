from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.db.database import async_get_db
from ...models.masters import Specialty, StaffDesignation, SubSpecialty
from ...schemas.masters import (
    SpecialtyCreate,
    StaffDesignationCreate,
    SubSpecialtyCreate,
)

router = APIRouter(prefix="/masters", tags=["masters"])

FALLBACK_SPECIALTIES = [
    {"uuid": "01a0b95d-0001-7000-8000-000000000001", "code": "general_practice", "name": "General Practice", "description": "Comprehensive primary care for individuals and families.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000002", "code": "gynecology", "name": "Gynecology & Obstetrics", "description": "Specialized care for female reproductive health and childbirth.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000003", "code": "virology", "name": "Virology", "description": "Clinical diagnosis and treatment of viral infections and pathology.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000004", "code": "general_medicine", "name": "General Medicine", "description": "Diagnosis and nonsurgical treatment of diseases of internal organs.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000005", "code": "orthopaedics", "name": "Orthopaedics", "description": "Care for bones, joints, ligaments, tendons, and muscles.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000006", "code": "paediatrics", "name": "Paediatrics", "description": "Medical care of infants, children, and adolescents.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000007", "code": "cardiology", "name": "Cardiology", "description": "Disorders of the heart and cardiovascular system.", "is_active": True},
    {"uuid": "01a0b95d-0001-7000-8000-000000000008", "code": "dermatology", "name": "Dermatology", "description": "Diagnosis and treatment of skin, hair, and nail conditions.", "is_active": True},
]

FALLBACK_SUB_SPECIALTIES = [
    {"uuid": "01a0b95d-0002-7000-8000-000000000001", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000001", "code": "general_practice", "name": "General Practice", "description": "General outpatient clinical care.", "is_active": True},
    {"uuid": "01a0b95d-0002-7000-8000-000000000002", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000003", "code": "clinical_virology", "name": "Clinical Virology", "description": "Diagnostic viral immunology and antiviral therapy.", "is_active": True},
    {"uuid": "01a0b95d-0002-7000-8000-000000000003", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000002", "code": "maternal_fetal_medicine", "name": "Maternal-Fetal Medicine", "description": "High-risk pregnancy and prenatal diagnostics.", "is_active": True},
    {"uuid": "01a0b95d-0002-7000-8000-000000000004", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000002", "code": "gynecologic_oncology", "name": "Gynecologic Oncology", "description": "Cancers of the female reproductive system.", "is_active": True},
    {"uuid": "01a0b95d-0002-7000-8000-000000000005", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000007", "code": "interventional_cardiology", "name": "Interventional Cardiology", "description": "Catheter-based cardiac treatments.", "is_active": True},
    {"uuid": "01a0b95d-0002-7000-8000-000000000006", "specialty_uuid": "01a0b95d-0001-7000-8000-000000000005", "code": "pediatric_orthopaedics", "name": "Pediatric Orthopaedics", "description": "Musculoskeletal care for youth.", "is_active": True},
]

FALLBACK_DESIGNATIONS = [
    {"uuid": "01a0b95d-0003-7000-8000-000000000001", "code": "senior_gynecologist", "name": "Senior Gynecologist", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000002", "code": "consultant_physician", "name": "Consultant Physician", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000003", "code": "general_practitioner", "name": "General Practitioner", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000004", "code": "visiting_specialist", "name": "Visiting Specialist", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000005", "code": "resident_medical_officer", "name": "Resident Medical Officer", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000006", "code": "entry_operator", "name": "Entry operator", "category": "administrative", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000007", "code": "assistant", "name": "Assistant", "category": "support", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000008", "code": "practitioner", "name": "Practitioner", "category": "clinical", "is_active": True},
    {"uuid": "01a0b95d-0003-7000-8000-000000000009", "code": "administrator", "name": "Administrator", "category": "administrative", "is_active": True},
]


@router.get("/specialties")
async def list_specialties(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    is_active: bool = True,
) -> dict[str, Any]:
    """List all clinical specialty masters (Primary Clinical Specialty / Clinical Specialization)."""
    try:
        query = select(Specialty).order_by(Specialty.name)
        if is_active:
            query = query.where(Specialty.is_active.is_(True))
        records = (await db.scalars(query)).all()
        if records:
            items = [
                {
                    "uuid": str(s.id),
                    "code": s.code,
                    "name": s.name,
                    "description": s.description,
                    "is_active": s.is_active,
                }
                for s in records
            ]
            return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
    except Exception:
        pass
    return {"success": True, "data": {"items": FALLBACK_SPECIALTIES}, "meta": {"count": len(FALLBACK_SPECIALTIES)}}


@router.post("/specialties", status_code=status.HTTP_201_CREATED)
async def create_specialty(
    payload: SpecialtyCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Create a new clinical specialty master record."""
    clean_code = payload.code.strip().lower().replace(" ", "_")
    existing = await db.scalar(select(Specialty).where(Specialty.code == clean_code))
    if existing:
        raise HTTPException(status_code=409, detail=f"Specialty with code '{clean_code}' already exists.")

    record = Specialty(
        code=clean_code,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        is_active=payload.is_active,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "success": True,
        "data": {
            "uuid": str(record.id),
            "code": record.code,
            "name": record.name,
            "description": record.description,
            "is_active": record.is_active,
        },
        "meta": {},
    }


@router.get("/specialties/{specialty_uuid}")
async def get_specialty(
    specialty_uuid: UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Retrieve details for a single specialty master, including associated sub-specialties."""
    record = await db.get(Specialty, specialty_uuid)
    if record:
        sub_query = select(SubSpecialty).where(
            SubSpecialty.specialty_id == record.id,
            SubSpecialty.is_active.is_(True),
        ).order_by(SubSpecialty.name)
        sub_records = (await db.scalars(sub_query)).all()
        return {
            "success": True,
            "data": {
                "uuid": str(record.id),
                "code": record.code,
                "name": record.name,
                "description": record.description,
                "is_active": record.is_active,
                "sub_specialties": [
                    {
                        "uuid": str(s.id),
                        "code": s.code,
                        "name": s.name,
                        "description": s.description,
                        "is_active": s.is_active,
                    }
                    for s in sub_records
                ],
            },
            "meta": {},
        }
    for item in FALLBACK_SPECIALTIES:
        if item["uuid"] == str(specialty_uuid):
            subs = [
                s for s in FALLBACK_SUB_SPECIALTIES
                if s.get("specialty_uuid") == str(specialty_uuid)
            ]
            data = dict(item)
            data["sub_specialties"] = subs
            return {"success": True, "data": data, "meta": {}}
    raise HTTPException(status_code=404, detail="Specialty master record not found.")


@router.get("/sub-specialties")
async def list_sub_specialties(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    specialty_uuid: UUID | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    """List clinical sub-specialties, optionally filtered by parent specialty."""
    try:
        query = select(SubSpecialty).order_by(SubSpecialty.name)
        if is_active:
            query = query.where(SubSpecialty.is_active.is_(True))
        if specialty_uuid:
            query = query.where(SubSpecialty.specialty_id == specialty_uuid)
        records = (await db.scalars(query)).all()
        if records:
            items = [
                {
                    "uuid": str(s.id),
                    "specialty_id": str(s.specialty_id) if s.specialty_id else None,
                    "specialty_uuid": str(s.specialty_id) if s.specialty_id else None,
                    "code": s.code,
                    "name": s.name,
                    "description": s.description,
                    "is_active": s.is_active,
                }
                for s in records
            ]
            return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
    except Exception:
        pass

    filtered = [
        dict(item, specialty_id=item.get("specialty_uuid"))
        for item in FALLBACK_SUB_SPECIALTIES
        if specialty_uuid is None or item.get("specialty_uuid") == str(specialty_uuid)
    ]
    return {"success": True, "data": {"items": filtered}, "meta": {"count": len(filtered)}}


@router.post("/sub-specialties", status_code=status.HTTP_201_CREATED)
async def create_sub_specialty(
    payload: SubSpecialtyCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Create a new clinical sub-specialty / scope master record."""
    clean_code = payload.code.strip().lower().replace(" ", "_")
    if payload.specialty_id:
        parent = await db.get(Specialty, payload.specialty_id)
        if not parent:
            raise HTTPException(status_code=404, detail="Referenced parent specialty does not exist.")

    record = SubSpecialty(
        specialty_id=payload.specialty_id,
        code=clean_code,
        name=payload.name.strip(),
        description=payload.description.strip() if payload.description else None,
        is_active=payload.is_active,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "success": True,
        "data": {
            "uuid": str(record.id),
            "specialty_id": str(record.specialty_id) if record.specialty_id else None,
            "specialty_uuid": str(record.specialty_id) if record.specialty_id else None,
            "code": record.code,
            "name": record.name,
            "description": record.description,
            "is_active": record.is_active,
        },
        "meta": {},
    }


@router.get("/staff-designations")
async def list_staff_designations(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    category: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    """List staff & specialist designations / titles (e.g. Senior Gynecologist, Consultant)."""
    try:
        query = select(StaffDesignation).order_by(StaffDesignation.name)
        if is_active:
            query = query.where(StaffDesignation.is_active.is_(True))
        if category:
            query = query.where(StaffDesignation.category == category.lower().strip())
        records = (await db.scalars(query)).all()
        if records:
            items = [
                {
                    "uuid": str(d.id),
                    "code": d.code,
                    "name": d.name,
                    "category": d.category,
                    "is_active": d.is_active,
                }
                for d in records
            ]
            return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
    except Exception:
        pass

    filtered = [
        item for item in FALLBACK_DESIGNATIONS
        if category is None or item.get("category") == category.lower().strip()
    ]
    return {"success": True, "data": {"items": filtered}, "meta": {"count": len(filtered)}}


@router.post("/staff-designations", status_code=status.HTTP_201_CREATED)
async def create_staff_designation(
    payload: StaffDesignationCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    """Create a new staff designation / title master record."""
    clean_code = payload.code.strip().lower().replace(" ", "_")
    existing = await db.scalar(select(StaffDesignation).where(StaffDesignation.code == clean_code))
    if existing:
        raise HTTPException(status_code=409, detail=f"Designation with code '{clean_code}' already exists.")

    record = StaffDesignation(
        code=clean_code,
        name=payload.name.strip(),
        category=payload.category.lower().strip() if payload.category else "clinical",
        is_active=payload.is_active,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "success": True,
        "data": {
            "uuid": str(record.id),
            "code": record.code,
            "name": record.name,
            "category": record.category,
            "is_active": record.is_active,
        },
        "meta": {},
    }


@router.get("/visit-reasons")
async def visit_reasons() -> dict[str, Any]:
    """Standard appointment / consultation visit reasons."""
    items = [
        {"code": "examination", "name": "Examination"},
        {"code": "follow_up", "name": "Follow-up"},
        {"code": "prescription_renewal", "name": "Prescription renewal"},
        {"code": "custom", "name": "Custom"},
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/access-roles")
async def access_roles() -> dict[str, Any]:
    """Permitted access roles across HealthOS organizations."""
    items = [
        {"key": "organization_admin", "name": "Organization Administrator"},
        {"key": "facility_operator", "name": "Facility Operator / Receptionist"},
        {"key": "clinical_practitioner", "name": "Practitioner / Doctor"},
        {"key": "practitioner", "name": "Practitioner / Doctor"},
        {"key": "nurse", "name": "Nurse / Clinical Assistant"},
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
