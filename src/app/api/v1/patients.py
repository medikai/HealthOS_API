import base64
import json
import re
import secrets
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.db.database import async_get_db
from ...core.timezones import (
    DEFAULT_TIMEZONE,
    local_datetime,
    timezone,
    to_timezone,
)
from ...domains.governance.audit import record_audit
from ...models.care import (
    Appointment,
    Diagnosis,
    Encounter,
    Practitioner,
    Prescription,
    PrescriptionItem,
    QueueEntry,
    SoapNote,
    Vital,
)
from ...models.identity import Patient, Person, UserAccount
from ...models.organization import Facility, FacilitySchedule
from ...schemas.patients import PatientCreate, PatientUpdate
from .bootstrap import _staff_context
from .scheduling import _scope

router = APIRouter(prefix="/patients", tags=["patients"])

MAX_OFFSET = 10_000


async def _org(db: AsyncSession, account: UserAccount):
    _, organization = await _staff_context(db, account)
    return organization


def _item(patient: Patient, person: Person) -> dict[str, Any]:
    name = f"{person.first_name} {person.last_name or ''}".strip()
    return {
        "uuid": str(patient.id),
        "mrn": patient.mrn,
        "medical_record_number": patient.mrn,
        "display_name": name,
        "first_name": person.first_name,
        "last_name": person.last_name,
        "phone": person.phone,
        "phone_masked": person.phone,
        "email": person.email,
        "date_of_birth": person.date_of_birth,
        "gender": person.gender,
        "status": "active" if patient.is_active else "inactive",
    }


def _escape_like(text: str) -> str:
    """Escape SQL LIKE/ILIKE wildcards: backslash, percent, and underscore."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _phone_search_variants(raw_phone: str) -> list[str]:
    """Documented Phone Normalization Policy:

    Strips presentation characters (spaces, dashes, parentheses, dots).
    Produces normalized lookup candidates (e.g., raw, stripped, 10-digit with/without +91/0).
    Never mutates stored phone data or changes phone uniqueness.
    """
    cleaned = re.sub(r"[\s\-\(\)\.\,\/]+", "", raw_phone)
    if not cleaned:
        return []

    variants = [raw_phone.strip()]
    if cleaned != raw_phone.strip():
        variants.append(cleaned)

    digits_only = re.sub(r"\D", "", cleaned)
    if len(digits_only) == 10:
        variants.extend([digits_only, f"+91{digits_only}", f"0{digits_only}"])
    elif len(digits_only) > 10 and digits_only.startswith("91") and len(digits_only) == 12:
        ten = digits_only[2:]
        variants.extend([ten, f"+91{ten}", f"+{digits_only}", digits_only, f"0{ten}"])
    elif cleaned.startswith("+91") and len(cleaned) == 13:
        ten = cleaned[3:]
        variants.extend([ten, f"+91{ten}", f"0{ten}"])
    elif cleaned.startswith("0") and len(digits_only) == 11:
        ten = digits_only[1:]
        variants.extend([ten, f"+91{ten}", f"0{ten}"])

    # Deduplicate while preserving order
    seen = set()
    unique_variants = []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            unique_variants.append(v)
    return unique_variants


def _has_facility_activity(facility_id: UUID):
    return or_(
        exists(
            select(1).select_from(Appointment).where(
                Appointment.patient_id == Patient.id,
                Appointment.organization_id == Patient.organization_id,
                Appointment.facility_id == facility_id,
            )
        ),
        exists(
            select(1).select_from(Encounter).where(
                Encounter.patient_id == Patient.id,
                Encounter.organization_id == Patient.organization_id,
                Encounter.facility_id == facility_id,
            )
        ),
        exists(
            select(1).select_from(QueueEntry).where(
                QueueEntry.patient_id == Patient.id,
                QueueEntry.organization_id == Patient.organization_id,
                QueueEntry.facility_id == facility_id,
            )
        ),
    )


def _encode_cursor(
    v: int,
    org_id: UUID,
    facility_id: UUID | None,
    q: str,
    last_values: tuple[int, str, str, str],
) -> str:
    payload = {
        "v": v,
        "org_id": str(org_id),
        "facility_id": str(facility_id) if facility_id else None,
        "q": q,
        "last_values": list(last_values),
    }
    raw_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw_bytes).decode("ascii")


def _decode_cursor(
    cursor_str: str, org_id: UUID, facility_id: UUID | None, q: str
) -> tuple[int, str, str, UUID]:
    try:
        raw_bytes = base64.urlsafe_b64decode(cursor_str.encode("ascii"))
        payload = json.loads(raw_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid pagination cursor format.",
        )

    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported pagination cursor version.",
        )

    if payload.get("org_id") != str(org_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cursor does not match the authorized organization scope.",
        )

    expected_facility = str(facility_id) if facility_id else None
    if payload.get("facility_id") != expected_facility:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cursor does not match the requested facility scope.",
        )

    if payload.get("q", "") != q:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cursor does not match the current search query.",
        )

    last_values = payload.get("last_values")
    if not isinstance(last_values, list) or len(last_values) != 4:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor keyset values.",
        )

    rank, first_name_lower, last_name_lower, patient_uuid_str = last_values
    try:
        patient_id = UUID(str(patient_uuid_str))
        rank = int(rank)
        first_name_lower = str(first_name_lower)
        last_name_lower = str(last_name_lower)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed cursor keyset values.",
        )

    return rank, first_name_lower, last_name_lower, patient_id


async def _execute_patient_search(
    db: AsyncSession,
    account: UserAccount,
    q: str = "",
    limit: int = 20,
    offset: int = 0,
    cursor: str | None = None,
    facility_id: UUID | str | None = None,
    facility_uuid: UUID | str | None = None,
) -> dict[str, Any]:
    # Normalize potential FastAPI Query default object if called directly in tests
    target_facility_id: UUID | None = None
    for candidate in (facility_id, facility_uuid):
        if isinstance(candidate, UUID):
            target_facility_id = candidate
            break
        elif isinstance(candidate, str) and candidate.strip():
            try:
                target_facility_id = UUID(candidate.strip())
                break
            except (ValueError, TypeError):
                pass

    # 1. Scope & authorize organization
    organization = await _org(db, account)

    # 2. Scope & authorize facility if requested
    if target_facility_id is not None:
        await _scope(db, account, target_facility_id)

    # 3. Validate pagination parameters
    cursor_val = cursor if isinstance(cursor, str) else None
    offset_val = offset if isinstance(offset, int) else 0
    limit_val = limit if isinstance(limit, int) else 20
    q_val = q if isinstance(q, str) else ""

    if cursor_val and offset_val > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ambiguous pagination: specify either cursor or offset, not both.",
        )
    if offset_val > MAX_OFFSET:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Offset exceeds maximum allowed limit of {MAX_OFFSET}.",
        )

    clean_q = q_val.strip()
    full_name_col = Person.first_name + " " + func.coalesce(Person.last_name, "")
    first_name_lower_col = func.lower(Person.first_name)
    last_name_lower_col = func.lower(func.coalesce(Person.last_name, ""))

    if clean_q:
        lower_q = clean_q.lower()
        escaped_q = _escape_like(clean_q)
        phone_variants = _phone_search_variants(clean_q)

        # Build ranking expressions
        exact_phone_cond = (
            Person.phone.in_(phone_variants)
            if phone_variants
            else (Person.phone == clean_q)
        )
        exact_mrn_cond = func.lower(Patient.mrn) == lower_q
        exact_name_cond = or_(
            func.lower(Person.first_name) == lower_q,
            func.lower(Person.last_name) == lower_q,
            func.lower(full_name_col) == lower_q,
        )

        prefix_name_mrn_cond = or_(
            Person.first_name.ilike(f"{escaped_q}%", escape="\\"),
            Person.last_name.ilike(f"{escaped_q}%", escape="\\"),
            full_name_col.ilike(f"{escaped_q}%", escape="\\"),
            Patient.mrn.ilike(f"{escaped_q}%", escape="\\"),
        )
        prefix_phone_cond = (
            or_(
                *[
                    Person.phone.ilike(f"{_escape_like(pv)}%", escape="\\")
                    for pv in phone_variants
                ]
            )
            if phone_variants
            else Person.phone.ilike(f"{escaped_q}%", escape="\\")
        )

        # Gated search: Require >= 3 characters for broad substring/contains search
        is_short = len(clean_q) < 3

        if is_short:
            where_match = or_(
                exact_phone_cond,
                exact_mrn_cond,
                exact_name_cond,
                prefix_name_mrn_cond,
                prefix_phone_cond,
            )
        else:
            substring_cond = or_(
                Person.first_name.ilike(f"%{escaped_q}%", escape="\\"),
                Person.last_name.ilike(f"%{escaped_q}%", escape="\\"),
                full_name_col.ilike(f"%{escaped_q}%", escape="\\"),
                Person.phone.ilike(f"%{escaped_q}%", escape="\\"),
                Patient.mrn.ilike(f"%{escaped_q}%", escape="\\"),
            )
            where_match = or_(
                exact_phone_cond,
                exact_mrn_cond,
                exact_name_cond,
                prefix_name_mrn_cond,
                prefix_phone_cond,
                substring_cond,
            )

        rank_expr = case(
            (or_(exact_phone_cond, exact_mrn_cond), 1),
            (exact_name_cond, 2),
            (or_(prefix_name_mrn_cond, prefix_phone_cond), 3),
            else_=4,
        ).label("search_rank")

    else:
        rank_expr = func.coalesce(0, 0).label("search_rank")
        where_match = None

    # Base query joined on actual patient/person relationships scoped to organization
    query = (
        select(Patient, Person, rank_expr)
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )

    if where_match is not None:
        query = query.where(where_match)

    # Facility scoping if requested
    if target_facility_id is not None:
        query = query.where(_has_facility_activity(target_facility_id))

    # Deterministic sort order
    query = query.order_by(
        rank_expr.asc(),
        first_name_lower_col.asc(),
        last_name_lower_col.asc(),
        Patient.id.asc(),
    )

    # Keyset cursor filtering
    if cursor_val:
        c_rank, c_first, c_last, c_id = _decode_cursor(
            cursor_val, organization.id, target_facility_id, clean_q
        )
        keyset_predicate = or_(
            rank_expr > c_rank,
            and_(
                rank_expr == c_rank,
                first_name_lower_col > c_first,
            ),
            and_(
                rank_expr == c_rank,
                first_name_lower_col == c_first,
                last_name_lower_col > c_last,
            ),
            and_(
                rank_expr == c_rank,
                first_name_lower_col == c_first,
                last_name_lower_col == c_last,
                Patient.id > c_id,
            ),
        )
        query = query.where(keyset_predicate)
    elif offset_val > 0:
        query = query.offset(offset_val)

    # Fetch limit + 1 to check has_more without COUNT(*)
    query = query.limit(limit_val + 1)

    rows = (await db.execute(query)).all()

    has_more = len(rows) > limit_val
    page_rows = rows[:limit_val]
    items = [_item(patient, person) for patient, person, _ in page_rows]

    next_cursor = None
    if has_more and page_rows:
        last_patient, last_person, last_rank = page_rows[-1]
        next_cursor = _encode_cursor(
            v=1,
            org_id=organization.id,
            facility_id=target_facility_id,
            q=clean_q,
            last_values=(
                int(last_rank),
                last_person.first_name.lower(),
                (last_person.last_name or "").lower(),
                str(last_patient.id),
            ),
        )

    return {
        "success": True,
        "data": {
            "items": items,
            "total": len(items),
            "limit": limit_val,
            "offset": offset_val if not cursor_val else None,
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
        "meta": {
            "count": len(items),
            "has_more": has_more,
        },
    }


@router.get("")
async def list_patients(
    q: str = Query(default=""),
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None),
    facility_id: UUID | None = Query(default=None),
    facility_uuid: UUID | None = Query(default=None),
) -> dict[str, Any]:
    return await _execute_patient_search(
        db=db,
        account=account,
        q=q,
        limit=limit,
        offset=offset,
        cursor=cursor,
        facility_id=facility_id,
        facility_uuid=facility_uuid,
    )


@router.get("/search")
async def search_patients(
    q: str = Query(default=""),
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None),
    facility_id: UUID | None = Query(default=None),
    facility_uuid: UUID | None = Query(default=None),
) -> dict[str, Any]:
    return await _execute_patient_search(
        db=db,
        account=account,
        q=q,
        limit=limit,
        offset=offset,
        cursor=cursor,
        facility_id=facility_id,
        facility_uuid=facility_uuid,
    )


@router.get("/duplicates")
async def duplicate_patients(
    phone: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
) -> dict[str, Any]:
    organization = await _org(db, account)
    query = (
        select(Patient, Person)
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if phone:
        phone_variants = _phone_search_variants(phone)
        query = query.where(Person.phone.in_(phone_variants))
    if first_name:
        escaped_fn = _escape_like(first_name.strip())
        query = query.where(Person.first_name.ilike(escaped_fn, escape="\\"))
    if last_name:
        escaped_ln = _escape_like(last_name.strip())
        query = query.where(Person.last_name.ilike(escaped_ln, escape="\\"))
    rows = (await db.execute(query.limit(20))).all()
    return {
        "success": True,
        "data": {"items": [_item(patient, person) for patient, person in rows]},
        "meta": {"count": len(rows)},
    }


async def _execute_relevant_patients(
    db: AsyncSession,
    account: UserAccount,
    facility_id: UUID | str | None = None,
    facility_uuid: UUID | str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    # 1. Normalize target facility UUID
    target_facility_id: UUID | None = None
    for candidate in (facility_id, facility_uuid):
        if isinstance(candidate, UUID):
            target_facility_id = candidate
            break
        elif isinstance(candidate, str) and candidate.strip():
            try:
                target_facility_id = UUID(candidate.strip())
                break
            except (ValueError, TypeError):
                pass

    # 2. Scope & authorize organization and facility access
    organization = await _org(db, account)
    if target_facility_id is not None:
        await _scope(db, account, target_facility_id)

    # 3. Resolve facility timezone
    tz_name = DEFAULT_TIMEZONE
    if target_facility_id is not None:
        fetched_tz = await db.scalar(
            select(FacilitySchedule.timezone).where(
                FacilitySchedule.facility_id == target_facility_id
            )
        )
        if fetched_tz:
            tz_name = fetched_tz
    else:
        fetched_tz = await db.scalar(
            select(FacilitySchedule.timezone)
            .join(Facility, Facility.id == FacilitySchedule.facility_id)
            .where(
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
            )
            .limit(1)
        )
        if fetched_tz:
            tz_name = fetched_tz

    tz = timezone(tz_name)
    now_tz = datetime.now(tz)
    today_date = now_tz.date()
    today_start_utc = local_datetime(today_date, time(0, 0, 0), tz).astimezone(UTC)
    tomorrow_start_utc = today_start_utc + timedelta(days=1)
    seven_days_end_utc = today_start_utc + timedelta(days=8)
    ninety_days_ago_utc = datetime.now(UTC) - timedelta(days=90)

    raw_limit = limit if isinstance(limit, int) else 10
    limit_val = min(max(raw_limit, 1), 10)
    seen_patient_ids: set[UUID] = set()
    candidates: list[dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Tier 1: Patients in today's active queue or with today's non-cancelled appointments
    # -------------------------------------------------------------------------
    tier1_items: list[tuple[float, dict[str, Any]]] = []

    # 1a. Today's active queue
    queue_conditions = [
        QueueEntry.organization_id == organization.id,
        QueueEntry.queue_date == today_date,
        QueueEntry.status.in_(["waiting", "called", "skipped", "in_consultation"]),
        Patient.is_active.is_(True),
    ]
    if target_facility_id is not None:
        queue_conditions.append(QueueEntry.facility_id == target_facility_id)

    queue_query = (
        select(QueueEntry, Patient, Person)
        .join(
            Patient,
            and_(
                Patient.id == QueueEntry.patient_id,
                Patient.organization_id == QueueEntry.organization_id,
            ),
        )
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(*queue_conditions)
        .order_by(QueueEntry.token_number.asc(), QueueEntry.id.asc())
        .limit(limit_val)
    )
    queue_rows = (await db.execute(queue_query)).all()
    for q_entry, pat, per in queue_rows:
        if pat.id not in seen_patient_ids:
            seen_patient_ids.add(pat.id)
            item_data = _item(pat, per)
            item_data["relevance_reason"] = f"In today's queue (Token #{q_entry.token_number})"
            item_data["relevance_tier"] = 1
            item_data["relevance_code"] = "today_queue"
            # Prioritize queue entries with lower token numbers
            tier1_items.append((float(q_entry.token_number), item_data))

    # 1b. Today's non-cancelled appointments
    appt_conditions = [
        Appointment.organization_id == organization.id,
        Appointment.scheduled_start >= today_start_utc,
        Appointment.scheduled_start < tomorrow_start_utc,
        Appointment.status != "cancelled",
        Patient.is_active.is_(True),
    ]
    if target_facility_id is not None:
        appt_conditions.append(Appointment.facility_id == target_facility_id)

    appt_query = (
        select(Appointment, Patient, Person)
        .join(
            Patient,
            and_(
                Patient.id == Appointment.patient_id,
                Patient.organization_id == Appointment.organization_id,
            ),
        )
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(*appt_conditions)
        .order_by(Appointment.scheduled_start.asc(), Appointment.id.asc())
        .limit(limit_val)
    )
    appt_rows = (await db.execute(appt_query)).all()
    for appt, pat, per in appt_rows:
        if pat.id not in seen_patient_ids:
            seen_patient_ids.add(pat.id)
            appt_time_local = to_timezone(appt.scheduled_start, tz).strftime("%H:%M")
            item_data = _item(pat, per)
            item_data["relevance_reason"] = f"Today's appointment ({appt_time_local})"
            item_data["relevance_tier"] = 1
            item_data["relevance_code"] = "today_appointment"
            # Normalize timestamp for ordering with queue tokens
            tier1_items.append((appt.scheduled_start.timestamp(), item_data))

    for _, item_data in tier1_items[:limit_val]:
        candidates.append(item_data)
        if len(candidates) >= limit_val:
            break

    # -------------------------------------------------------------------------
    # Tier 2: Nearest upcoming booked appointments in next 7 days
    # -------------------------------------------------------------------------
    if len(candidates) < limit_val:
        remaining_limit = limit_val - len(candidates)
        tier2_conditions = [
            Appointment.organization_id == organization.id,
            Appointment.scheduled_start >= tomorrow_start_utc,
            Appointment.scheduled_start < seven_days_end_utc,
            Appointment.status == "booked",
            Patient.is_active.is_(True),
        ]
        if target_facility_id is not None:
            tier2_conditions.append(Appointment.facility_id == target_facility_id)
        if seen_patient_ids:
            tier2_conditions.append(Appointment.patient_id.not_in(list(seen_patient_ids)))

        tier2_query = (
            select(Appointment, Patient, Person)
            .join(
                Patient,
                and_(
                    Patient.id == Appointment.patient_id,
                    Patient.organization_id == Appointment.organization_id,
                ),
            )
            .join(
                Person,
                and_(
                    Person.id == Patient.person_id,
                    Person.organization_id == Patient.organization_id,
                ),
            )
            .where(*tier2_conditions)
            .order_by(Appointment.scheduled_start.asc(), Appointment.id.asc())
            .limit(remaining_limit)
        )
        tier2_rows = (await db.execute(tier2_query)).all()
        for appt, pat, per in tier2_rows:
            if pat.id not in seen_patient_ids and len(candidates) < limit_val:
                seen_patient_ids.add(pat.id)
                appt_date_local = to_timezone(appt.scheduled_start, tz).strftime("%b %d, %H:%M")
                item_data = _item(pat, per)
                item_data["relevance_reason"] = f"Upcoming appointment ({appt_date_local})"
                item_data["relevance_tier"] = 2
                item_data["relevance_code"] = "upcoming_appointment"
                candidates.append(item_data)

    # -------------------------------------------------------------------------
    # Tier 3: Recently completed visits in last 90 days
    # -------------------------------------------------------------------------
    if len(candidates) < limit_val:
        remaining_limit = limit_val - len(candidates)
        tier3_conditions = [
            Encounter.organization_id == organization.id,
            Encounter.status == "completed",
            Encounter.completed_at >= ninety_days_ago_utc,
            Patient.is_active.is_(True),
        ]
        if target_facility_id is not None:
            tier3_conditions.append(Encounter.facility_id == target_facility_id)
        if seen_patient_ids:
            tier3_conditions.append(Encounter.patient_id.not_in(list(seen_patient_ids)))

        tier3_query = (
            select(Encounter, Patient, Person)
            .join(
                Patient,
                and_(
                    Patient.id == Encounter.patient_id,
                    Patient.organization_id == Encounter.organization_id,
                ),
            )
            .join(
                Person,
                and_(
                    Person.id == Patient.person_id,
                    Person.organization_id == Patient.organization_id,
                ),
            )
            .where(*tier3_conditions)
            .order_by(Encounter.completed_at.desc(), Encounter.id.desc())
            .limit(remaining_limit)
        )
        tier3_rows = (await db.execute(tier3_query)).all()
        for enc, pat, per in tier3_rows:
            if pat.id not in seen_patient_ids and len(candidates) < limit_val:
                seen_patient_ids.add(pat.id)
                completed_date_str = (
                    to_timezone(enc.completed_at, tz).strftime("%b %d, %Y")
                    if enc.completed_at
                    else "recently"
                )
                item_data = _item(pat, per)
                item_data["relevance_reason"] = f"Recent completed visit ({completed_date_str})"
                item_data["relevance_tier"] = 3
                item_data["relevance_code"] = "recent_visit"
                candidates.append(item_data)

    # -------------------------------------------------------------------------
    # Tier 4: Recently registered active patients to fill remaining places
    # -------------------------------------------------------------------------
    if len(candidates) < limit_val:
        remaining_limit = limit_val - len(candidates)
        tier4_conditions = [
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        ]
        if seen_patient_ids:
            tier4_conditions.append(Patient.id.not_in(list(seen_patient_ids)))

        if target_facility_id is not None:
            tier4_conditions.append(_has_facility_activity(target_facility_id))

        tier4_query = (
            select(Patient, Person)
            .join(
                Person,
                and_(
                    Person.id == Patient.person_id,
                    Person.organization_id == Patient.organization_id,
                ),
            )
            .where(*tier4_conditions)
            .order_by(Patient.created_at.desc(), Patient.id.desc())
            .limit(remaining_limit)
        )
        tier4_rows = (await db.execute(tier4_query)).all()
        for pat, per in tier4_rows:
            if pat.id not in seen_patient_ids and len(candidates) < limit_val:
                seen_patient_ids.add(pat.id)
                item_data = _item(pat, per)
                item_data["relevance_reason"] = "Recently registered patient"
                item_data["relevance_tier"] = 4
                item_data["relevance_code"] = "recent_registration"
                candidates.append(item_data)

    return {
        "success": True,
        "data": {
            "items": candidates,
            "total": len(candidates),
            "limit": limit_val,
            "facility_id": str(target_facility_id) if target_facility_id else None,
            "facility_timezone": tz_name,
        },
        "meta": {
            "count": len(candidates),
            "facility_timezone": tz_name,
        },
    }


@router.get("/relevant")
async def relevant_patients(
    facility_id: UUID | None = Query(default=None),
    facility_uuid: UUID | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=10),
    account: Annotated[UserAccount, Depends(get_current_identity_account)] = ...,
    db: Annotated[AsyncSession, Depends(async_get_db)] = ...,
) -> dict[str, Any]:
    return await _execute_relevant_patients(
        db=db,
        account=account,
        facility_id=facility_id,
        facility_uuid=facility_uuid,
        limit=limit,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_patient(
    payload: PatientCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await _org(db, account)
    normalized = payload.normalized()
    if normalized.phone:
        existing = await db.scalar(
            select(Person).where(
                Person.organization_id == organization.id,
                Person.phone == normalized.phone,
            )
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A patient with this phone already exists.",
            )
    person = Person(
        organization_id=organization.id,
        first_name=normalized.first_name,
        last_name=normalized.last_name,
        phone=normalized.phone,
        email=normalized.email,
        date_of_birth=normalized.date_of_birth,
        gender=normalized.gender,
    )
    db.add(person)
    await db.flush()
    patient = Patient(
        organization_id=organization.id,
        person_id=person.id,
        mrn=f"MRN-{secrets.token_hex(5).upper()}",
    )
    db.add(patient)
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="patient.created",
        resource_type="patient",
        resource_id=patient.id,
    )
    await db.commit()
    return {"success": True, "data": _item(patient, person), "meta": {}}


@router.get("/{patient_uuid}")
async def get_patient(
    patient_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await _org(db, account)
    row = await db.execute(
        select(Patient, Person)
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    return {"success": True, "data": _item(*result), "meta": {}}


@router.patch("/{patient_uuid}")
async def update_patient(
    patient_uuid: UUID,
    payload: PatientUpdate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await _org(db, account)
    row = await db.execute(
        select(Patient, Person)
        .join(
            Person,
            and_(
                Person.id == Patient.person_id,
                Person.organization_id == Patient.organization_id,
            ),
        )
        .where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    result = row.first()
    if result is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    _, person = result
    normalized = payload.normalized()
    person.first_name, person.last_name, person.phone, person.email = (
        normalized.first_name,
        normalized.last_name,
        normalized.phone,
        normalized.email,
    )
    person.date_of_birth, person.gender = (
        normalized.date_of_birth,
        normalized.gender,
    )
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="patient.updated",
        resource_type="patient",
        resource_id=patient_uuid,
    )
    await db.commit()
    return {"success": True, "data": _item(*result), "meta": {}}


@router.get("/{patient_uuid}/context")
async def patient_context(
    patient_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    person = await db.get(Person, patient.person_id)
    active_appointments = await db.scalar(
        select(func.count(Appointment.id)).where(
            Appointment.patient_id == patient.id,
            Appointment.organization_id == organization.id,
            Appointment.status.in_(["booked", "checked_in", "in_consultation"]),
        )
    )
    return {
        "success": True,
        "data": {
            "patient": _item(patient, person),
            "active_appointments": active_appointments or 0,
        },
        "meta": {},
    }


@router.get("/{patient_uuid}/timeline")
async def patient_timeline(
    patient_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")
    appointments = (
        await db.scalars(
            select(Appointment)
            .where(
                Appointment.patient_id == patient.id,
                Appointment.organization_id == organization.id,
            )
            .order_by(Appointment.scheduled_start.desc())
            .limit(limit)
        )
    ).all()
    encounters = (
        await db.scalars(
            select(Encounter)
            .where(
                Encounter.patient_id == patient.id,
                Encounter.organization_id == organization.id,
            )
            .order_by(Encounter.started_at.desc())
            .limit(limit)
        )
    ).all()
    items = [
        {
            "type": "appointment",
            "uuid": str(a.id),
            "status": a.status,
            "occurred_at": a.scheduled_start.isoformat(),
        }
        for a in appointments
    ] + [
        {
            "type": "encounter",
            "uuid": str(e.id),
            "status": e.status,
            "occurred_at": e.started_at.isoformat(),
        }
        for e in encounters
    ]
    items.sort(key=lambda x: x["occurred_at"], reverse=True)
    return {
        "success": True,
        "data": {"items": items[:limit]},
        "meta": {"count": min(len(items), limit)},
    }


@router.get("/{patient_uuid}/encounters")
async def list_patient_encounters(
    patient_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")

    limit_val = limit if isinstance(limit, int) else getattr(limit, "default", 50)
    rows = (
        await db.execute(
            select(Encounter, Facility, Practitioner)
            .outerjoin(Facility, Facility.id == Encounter.facility_id)
            .outerjoin(Practitioner, Practitioner.id == Encounter.practitioner_id)
            .where(
                Encounter.patient_id == patient.id,
                Encounter.organization_id == organization.id,
            )
            .order_by(Encounter.started_at.desc())
            .limit(limit_val)
        )
    ).all()

    encounter_ids = [row[0].id for row in rows if row and getattr(row[0], "id", None)]

    soaps_by_enc: dict[UUID, SoapNote] = {}
    diagnoses_by_enc: dict[UUID, list[Diagnosis]] = {}
    prescriptions_by_enc: dict[UUID, tuple[Prescription, list[PrescriptionItem]]] = {}
    vitals_by_enc: dict[UUID, list[Vital]] = {}

    if encounter_ids:
        soaps = (
            await db.scalars(
                select(SoapNote).where(SoapNote.encounter_id.in_(encounter_ids))
            )
        ).all()
        soaps_by_enc = {s.encounter_id: s for s in soaps}

        diagnoses = (
            await db.scalars(
                select(Diagnosis)
                .where(Diagnosis.encounter_id.in_(encounter_ids))
                .order_by(Diagnosis.is_primary.desc())
            )
        ).all()
        for d in diagnoses:
            diagnoses_by_enc.setdefault(d.encounter_id, []).append(d)

        vitals = (
            await db.scalars(
                select(Vital)
                .where(Vital.encounter_id.in_(encounter_ids))
                .order_by(Vital.recorded_at.desc())
            )
        ).all()
        for v in vitals:
            vitals_by_enc.setdefault(v.encounter_id, []).append(v)

        rx_rows = (
            await db.execute(
                select(Prescription, PrescriptionItem)
                .outerjoin(
                    PrescriptionItem,
                    PrescriptionItem.prescription_id == Prescription.id,
                )
                .where(Prescription.encounter_id.in_(encounter_ids))
                .order_by(Prescription.id.desc())
            )
        ).all()
        for rx, item in rx_rows:
            if rx.encounter_id not in prescriptions_by_enc:
                prescriptions_by_enc[rx.encounter_id] = (rx, [])
            if item is not None:
                prescriptions_by_enc[rx.encounter_id][1].append(item)

    items = []
    for row in rows:
        encounter, facility, practitioner = row
        soap = soaps_by_enc.get(encounter.id)
        enc_diagnoses = diagnoses_by_enc.get(encounter.id, [])
        enc_vitals = vitals_by_enc.get(encounter.id, [])
        rx_data = prescriptions_by_enc.get(encounter.id)

        rx_dict = None
        if rx_data:
            rx, rx_items = rx_data
            meds = [
                {
                    "uuid": str(i.id),
                    "id": str(i.id),
                    "medicine_id": str(i.medicine_id) if getattr(i, "medicine_id", None) else None,
                    "medicine_name": i.medicine_name,
                    "name": i.medicine_name,
                    "dosage": i.dosage,
                    "dose": i.dosage,
                    "frequency": i.frequency,
                    "duration": i.duration,
                    "strength": i.strength,
                    "brand": i.brand,
                    "route": i.route,
                    "timing": i.timing,
                    "instructions": i.instructions,
                }
                for i in rx_items
            ]
            rx_dict = {
                "uuid": str(rx.id),
                "status": rx.status,
                "advice": rx.advice,
                "items": meds,
                "medications": meds,
                "signed_at": rx.signed_at.isoformat() if rx.signed_at else None,
            }

        items.append({
            "uuid": str(encounter.id),
            "encounter_uuid": str(encounter.id),
            "appointment_uuid": str(encounter.appointment_id) if encounter.appointment_id else None,
            "queue_entry_uuid": str(encounter.queue_entry_id) if encounter.queue_entry_id else None,
            "status": encounter.status,
            "started_at": encounter.started_at.isoformat() if encounter.started_at else None,
            "completed_at": encounter.completed_at.isoformat() if encounter.completed_at else None,
            "facility": {
                "uuid": str(facility.id),
                "name": facility.name,
            } if facility else None,
            "practitioner": {
                "uuid": str(practitioner.id),
                "name": practitioner.person_name,
                "specialty": practitioner.specialty,
            } if practitioner else None,
            "soap": {
                "uuid": str(soap.id),
                "status": soap.status,
                "subjective": soap.subjective or "",
                "objective": soap.objective or "",
                "assessment": soap.assessment or "",
                "plan": soap.plan or "",
                "custom_fields": soap.custom_fields or {},
                "signed_at": soap.signed_at.isoformat() if soap.signed_at else None,
            } if soap else None,
            "diagnoses": [
                {
                    "uuid": str(d.id),
                    "code": d.code,
                    "description": d.description,
                    "is_primary": d.is_primary,
                }
                for d in enc_diagnoses
            ],
            "vitals": [
                {
                    "uuid": str(v.id),
                    "name": v.name,
                    "value": v.value,
                    "unit": v.unit,
                    "recorded_at": v.recorded_at.isoformat() if v.recorded_at else None,
                }
                for v in enc_vitals
            ],
            "prescription": rx_dict,
        })

    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items)},
    }


@router.get("/{patient_uuid}/prescriptions")
async def list_patient_prescriptions(
    patient_uuid: UUID,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    organization = await _org(db, account)
    patient = await db.scalar(
        select(Patient).where(
            Patient.id == patient_uuid,
            Patient.organization_id == organization.id,
            Patient.is_active.is_(True),
        )
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found.")

    limit_val = limit if isinstance(limit, int) else getattr(limit, "default", 50)
    rx_rows = (
        await db.execute(
            select(Prescription, Encounter, Facility, Practitioner)
            .join(Encounter, Encounter.id == Prescription.encounter_id)
            .outerjoin(Facility, Facility.id == Encounter.facility_id)
            .outerjoin(Practitioner, Practitioner.id == Encounter.practitioner_id)
            .where(
                Encounter.patient_id == patient.id,
                Encounter.organization_id == organization.id,
            )
            .order_by(
                Prescription.signed_at.desc().nullslast(),
                Prescription.id.desc(),
            )
            .limit(limit_val)
        )
    ).all()

    rx_ids = [row[0].id for row in rx_rows if row and getattr(row[0], "id", None)]
    items_by_rx: dict[UUID, list[PrescriptionItem]] = {}
    if rx_ids:
        pi_rows = (
            await db.scalars(
                select(PrescriptionItem).where(PrescriptionItem.prescription_id.in_(rx_ids))
            )
        ).all()
        for pi in pi_rows:
            items_by_rx.setdefault(pi.prescription_id, []).append(pi)

    items = []
    for rx, encounter, facility, practitioner in rx_rows:
        pi_list = items_by_rx.get(rx.id, [])
        meds = [
            {
                "uuid": str(i.id),
                "id": str(i.id),
                "medicine_id": str(i.medicine_id) if getattr(i, "medicine_id", None) else None,
                "medicine_name": i.medicine_name,
                "name": i.medicine_name,
                "dosage": i.dosage,
                "dose": i.dosage,
                "frequency": i.frequency,
                "duration": i.duration,
                "strength": i.strength,
                "brand": i.brand,
                "route": i.route,
                "timing": i.timing,
                "instructions": i.instructions,
            }
            for i in pi_list
        ]
        items.append({
            "uuid": str(rx.id),
            "prescription_uuid": str(rx.id),
            "encounter_uuid": str(encounter.id),
            "appointment_uuid": str(encounter.appointment_id) if encounter.appointment_id else None,
            "status": rx.status,
            "advice": rx.advice,
            "signed_at": rx.signed_at.isoformat() if rx.signed_at else None,
            "encounter_started_at": encounter.started_at.isoformat() if encounter.started_at else None,
            "facility": {
                "uuid": str(facility.id),
                "name": facility.name,
            } if facility else None,
            "practitioner": {
                "uuid": str(practitioner.id),
                "name": practitioner.person_name,
                "specialty": practitioner.specialty,
            } if practitioner else None,
            "items": meds,
            "medications": meds,
        })

    return {
        "success": True,
        "data": {"items": items},
        "meta": {"count": len(items)},
    }

