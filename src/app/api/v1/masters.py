from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...models.identity import UserAccount
from ...models.masters import (
    City,
    Country,
    District,
    MedicalCouncil,
    Medicine,
    PrescriptionOption,
    Specialty,
    StaffDesignation,
    State,
    SubSpecialty,
)
from ...models.organization import StaffAssignment, StaffMember
from ...schemas.masters import (
    CityCreate,
    MedicineCreate,
    MedicineUpdate,
    PrescriptionOptionCategory,
    PrescriptionOptionCreate,
    PrescriptionOptionUpdate,
    SpecialtyCreate,
    StaffDesignationCreate,
    SubSpecialtyCreate,
)

router = APIRouter(prefix="/masters", tags=["masters"])


def normalize_city_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _medicine_item(row: Medicine) -> dict[str, Any]:
    generic_name = " + ".join(filter(None, (row.composition1, row.composition2))) or None
    return {
        "uuid": str(row.id), "name": row.name, "medicine_name": row.name,
        "brand": row.name, "generic_name": generic_name,
        "composition1": row.composition1, "composition2": row.composition2,
        "manufacturer_name": row.manufacturer_name, "medicine_type": row.medicine_type,
        "pack_size_label": row.pack_size_label,
        "price": float(row.price) if row.price is not None else None,
        "source": row.source, "source_id": row.source_id,
        "is_discontinued": row.is_discontinued, "is_active": row.is_active,
    }


def _prescription_option_item(row: PrescriptionOption) -> dict[str, Any]:
    return {
        "uuid": str(row.id), "country_code": row.country_code,
        "category": row.category, "code": row.code, "label": row.label,
        "value": row.value, "description": row.description,
        "sort_order": row.sort_order, "is_active": row.is_active,
    }


async def _require_organization_admin(db: AsyncSession, account: UserAccount) -> None:
    is_admin = await db.scalar(
        select(StaffAssignment.id)
        .join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id)
        .where(
            StaffMember.user_account_id == account.id,
            StaffMember.is_active.is_(True),
            StaffAssignment.role_code == "organization_admin",
            StaffAssignment.is_active.is_(True),
        )
        .limit(1)
    )
    if is_admin is None:
        raise HTTPException(status_code=403, detail="Organization administrator role required.")

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
FALLBACK_MEDICAL_COUNCILS = [
    {"uuid": str(uuid5(NAMESPACE_URL, f"healthos:medical-council:{code}")), "code": code, "name": name, "state_code": state_code, "country_code": "IN", "is_active": True}
    for code, name, state_code in MEDICAL_COUNCILS
]


@router.get("/countries")
async def list_countries(
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    records = (await db.scalars(select(Country).where(Country.is_active.is_(True)).order_by(Country.name))).all()
    items = [{"uuid": str(row.id), "code": row.code, "iso3_code": row.iso3_code, "name": row.name} for row in records]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/states")
async def list_states(
    country_id: UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    records = (await db.scalars(
        select(State).where(State.country_id == country_id, State.is_active.is_(True)).order_by(State.name)
    )).all()
    items = [
        {"uuid": str(row.id), "country_id": str(row.country_id), "code": row.code, "name": row.name, "external_code": row.external_code}
        for row in records
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/districts")
async def list_districts(
    state_id: UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    records = (await db.scalars(
        select(District).where(District.state_id == state_id, District.is_active.is_(True)).order_by(District.name)
    )).all()
    items = [{"uuid": str(row.id), "state_id": str(row.state_id), "code": row.code, "name": row.name} for row in records]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.get("/cities")
async def list_cities(
    state_id: UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    district_id: UUID | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    query = select(City).where(City.state_id == state_id, City.is_active.is_(True))
    if district_id:
        query = query.where(City.district_id == district_id)
    if q:
        query = query.where(City.name.icontains(q.strip(), autoescape=True))
    records = (await db.scalars(query.order_by(City.name).limit(limit))).all()
    items = [
        {"uuid": str(row.id), "state_id": str(row.state_id), "district_id": str(row.district_id) if row.district_id else None, "name": row.name, "external_code": row.external_code, "is_user_added": row.is_user_added}
        for row in records
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("/cities", status_code=status.HTTP_201_CREATED)
async def create_city(
    payload: CityCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    _account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    state_row = await db.get(State, payload.state_id)
    if state_row is None or not state_row.is_active:
        raise HTTPException(status_code=422, detail="State does not exist or is inactive.")
    if payload.district_id:
        district = await db.get(District, payload.district_id)
        if district is None or not district.is_active or district.state_id != payload.state_id:
            raise HTTPException(status_code=422, detail="District does not belong to the selected state.")
    name = " ".join(payload.name.split())
    normalized_name = normalize_city_name(name)
    existing = await db.scalar(select(City).where(
        City.state_id == payload.state_id,
        City.district_id == payload.district_id,
        City.normalized_name == normalized_name,
    ))
    if existing:
        raise HTTPException(status_code=409, detail="City already exists in this state.")
    record = City(state_id=payload.state_id, district_id=payload.district_id, name=name, normalized_name=normalized_name, is_user_added=True)
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {
        "success": True,
        "data": {"uuid": str(record.id), "state_id": str(record.state_id), "district_id": str(record.district_id) if record.district_id else None, "name": record.name, "external_code": None, "is_user_added": True},
        "meta": {},
    }


@router.get("/medicines")
async def search_medicines(
    q: Annotated[str, Query(min_length=3, max_length=100)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    _account: Annotated[UserAccount, Depends(get_current_identity_account)],
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    term = _escape_like(q.casefold().strip())
    contains, prefix = f"%{term}%", f"{term}%"
    name = func.lower(Medicine.name)
    composition = func.lower(
        func.coalesce(Medicine.composition1, literal_column("''"))
        + literal_column("' '")
        + func.coalesce(Medicine.composition2, literal_column("''"))
    )
    query = (
        select(Medicine)
        .where(
            Medicine.is_active.is_(True),
            Medicine.is_discontinued.is_(False),
            or_(name.like(contains, escape="\\"), composition.like(contains, escape="\\")),
        )
        .order_by(
            case((name.like(prefix, escape="\\"), 0), else_=1),
            func.similarity(name, q.casefold().strip()).desc(),
            Medicine.name,
        )
        .limit(limit)
    )
    records = (await db.scalars(query)).all()
    items = [_medicine_item(row) for row in records]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@router.post("/medicines", status_code=status.HTTP_201_CREATED)
async def create_medicine(
    payload: MedicineCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    await _require_organization_admin(db, account)
    name = " ".join(payload.name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Medicine name cannot be blank.")
    manufacturer = " ".join(payload.manufacturer_name.split()) if payload.manufacturer_name else None
    duplicate = await db.scalar(select(Medicine.id).where(
        func.lower(Medicine.name) == name.casefold(),
        func.lower(func.coalesce(Medicine.manufacturer_name, "")) == (manufacturer or "").casefold(),
    ))
    if duplicate:
        raise HTTPException(status_code=409, detail="Medicine already exists for this manufacturer.")
    record = Medicine(
        **payload.model_dump(exclude={"name", "manufacturer_name"}),
        name=name,
        manufacturer_name=manufacturer,
        source="manual",
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {"success": True, "data": _medicine_item(record), "meta": {}}


@router.get("/medicines/{medicine_id}")
async def get_medicine(
    medicine_id: UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    _account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    record = await db.get(Medicine, medicine_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Medicine not found.")
    return {"success": True, "data": _medicine_item(record), "meta": {}}


@router.patch("/medicines/{medicine_id}")
async def update_medicine(
    medicine_id: UUID,
    payload: MedicineUpdate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    await _require_organization_admin(db, account)
    record = await db.get(Medicine, medicine_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Medicine not found.")

    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        changes["name"] = " ".join(changes["name"].split()) if changes["name"] is not None else None
        if not changes["name"]:
            raise HTTPException(status_code=422, detail="Medicine name cannot be blank.")
    if "manufacturer_name" in changes and changes["manufacturer_name"] is not None:
        changes["manufacturer_name"] = " ".join(changes["manufacturer_name"].split()) or None

    if "name" in changes or "manufacturer_name" in changes:
        name = changes.get("name", record.name)
        manufacturer = changes.get("manufacturer_name", record.manufacturer_name)
        duplicate = await db.scalar(select(Medicine.id).where(
            Medicine.id != record.id,
            func.lower(Medicine.name) == name.casefold(),
            func.lower(func.coalesce(Medicine.manufacturer_name, "")) == (manufacturer or "").casefold(),
        ))
        if duplicate:
            raise HTTPException(status_code=409, detail="Medicine already exists for this manufacturer.")

    for field, value in changes.items():
        setattr(record, field, value)
    record.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(record)
    return {"success": True, "data": _medicine_item(record), "meta": {}}


@router.get("/prescription-options")
async def list_prescription_options(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    _account: Annotated[UserAccount, Depends(get_current_identity_account)],
    category: PrescriptionOptionCategory | None = None,
    country_code: Annotated[str, Query(min_length=2, max_length=2)] = "IN",
    include_inactive: bool = False,
) -> dict[str, Any]:
    query = select(PrescriptionOption).where(
        PrescriptionOption.country_code == country_code.upper(),
    )
    if not include_inactive:
        query = query.where(PrescriptionOption.is_active.is_(True))
    if category:
        query = query.where(PrescriptionOption.category == category)
    records = (await db.scalars(query.order_by(
        PrescriptionOption.category, PrescriptionOption.sort_order, PrescriptionOption.label
    ))).all()
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in records:
        groups.setdefault(row.category, []).append(_prescription_option_item(row))
    return {"success": True, "data": {"groups": groups}, "meta": {"count": len(records)}}


@router.post("/prescription-options", status_code=status.HTTP_201_CREATED)
async def create_prescription_option(
    payload: PrescriptionOptionCreate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    await _require_organization_admin(db, account)
    country_code = payload.country_code.upper()
    duplicate = await db.scalar(select(PrescriptionOption.id).where(
        PrescriptionOption.country_code == country_code,
        PrescriptionOption.category == payload.category,
        PrescriptionOption.code == payload.code,
    ))
    if duplicate:
        raise HTTPException(status_code=409, detail="Prescription option code already exists in this category.")
    label, value = " ".join(payload.label.split()), " ".join(payload.value.split())
    if not label or not value:
        raise HTTPException(status_code=422, detail="Label and value cannot be blank.")
    record = PrescriptionOption(
        **payload.model_dump(exclude={"country_code", "label", "value"}),
        country_code=country_code,
        label=label,
        value=value,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return {"success": True, "data": _prescription_option_item(record), "meta": {}}


@router.patch("/prescription-options/{option_id}")
async def update_prescription_option(
    option_id: UUID,
    payload: PrescriptionOptionUpdate,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
) -> dict[str, Any]:
    await _require_organization_admin(db, account)
    record = await db.get(PrescriptionOption, option_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Prescription option not found.")
    changes = payload.model_dump(exclude_unset=True)
    for field in ("label", "value"):
        if field in changes:
            changes[field] = " ".join(changes[field].split()) if changes[field] is not None else None
            if not changes[field]:
                raise HTTPException(status_code=422, detail=f"{field.title()} cannot be blank.")
    for field, value in changes.items():
        setattr(record, field, value)
    record.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(record)
    return {"success": True, "data": _prescription_option_item(record), "meta": {}}


@router.get("/medical-councils")
async def list_medical_councils(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    is_active: bool = True,
) -> dict[str, Any]:
    try:
        query = select(MedicalCouncil).order_by(MedicalCouncil.name)
        if is_active:
            query = query.where(MedicalCouncil.is_active.is_(True))
        records = (await db.scalars(query)).all()
        if records:
            items = [
                {
                    "uuid": str(council.id),
                    "code": council.code,
                    "name": council.name,
                    "state_code": council.state_code,
                    "country_code": council.country_code,
                    "is_active": council.is_active,
                }
                for council in records
            ]
            return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}
    except Exception:
        pass
    return {"success": True, "data": {"items": FALLBACK_MEDICAL_COUNCILS}, "meta": {"count": len(FALLBACK_MEDICAL_COUNCILS)}}


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
