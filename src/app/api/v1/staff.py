import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_identity_account
from ...core.availability import rules_from_facility_schedule
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.security import create_access_token, get_password_hash
from ...domains.auth.logto import logto_oidc_client
from ...domains.governance.audit import record_audit
from ...models.care import Practitioner, PractitionerAvailabilityRule
from ...models.identity import UserAccount
from ...models.masters import Specialty, StaffDesignation, SubSpecialty
from ...models.organization import (
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffInvitation,
    StaffMember,
)
from ...schemas.staff import (
    StaffAssignmentsInput,
    StaffCreate,
    StaffInviteAccept,
    StaffInviteCreate,
    StaffRolesInput,
    StaffUpdate,
)
from .bootstrap import _staff_context

router = APIRouter(tags=["staff"])
admin_router = APIRouter(prefix="/admin/staff", tags=["admin-staff"])
staff_router = APIRouter(prefix="/staff", tags=["staff"])


async def _admin(db: AsyncSession, account: UserAccount) -> tuple[StaffMember, Organization]:
    staff, organization = await _staff_context(db, account)
    role = await db.scalar(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff.id, StaffAssignment.role_code.in_(["organization_admin", "owner", "administrator"]), StaffAssignment.is_active.is_(True)))
    if role is None:
        raise HTTPException(status_code=403, detail="Staff administration access is required.")
    return staff, organization


def _item(
    member: StaffMember,
    account: UserAccount,
    assignments: list[StaffAssignment],
    practitioner: Practitioner | None = None,
    specialty_name: str | None = None,
    sub_specialty_name: str | None = None,
    designation_name: str | None = None,
) -> dict[str, Any]:
    role_codes = sorted({a.role_code for a in assignments})
    facility_uuids = [str(a.facility_id) for a in assignments if a.facility_id]

    desig = designation_name
    if not desig:
        if "owner" in role_codes:
            desig = "Owner & Practice Director"
        elif "organization_admin" in role_codes:
            desig = "Organization Administrator"
        elif practitioner:
            desig = "Practitioner"
        elif "nurse" in role_codes:
            desig = "Registered Nurse"
        elif "facility_operator" in role_codes:
            desig = "Front Desk & Facility Operator"
        else:
            desig = "Staff Member"

    return {
        "uuid": str(member.id),
        "user_uuid": str(account.id),
        "display_name": account.display_name or (practitioner.person_name if practitioner else "Staff Member"),
        "name": account.display_name or (practitioner.person_name if practitioner else "Staff Member"),
        "email": account.email or "",
        "status": "active" if member.is_active else "inactive",
        "role_codes": role_codes,
        "facility_uuids": facility_uuids,
        "is_practitioner": practitioner is not None,
        "practitioner_uuid": str(practitioner.id) if practitioner else None,
        "specialty": (practitioner.specialty or specialty_name) if practitioner else None,
        "specialty_id": str(practitioner.specialty_id) if (practitioner and practitioner.specialty_id) else None,
        "specialty_name": specialty_name,
        "sub_specialty_id": str(practitioner.sub_specialty_id) if (practitioner and practitioner.sub_specialty_id) else None,
        "sub_specialty_name": sub_specialty_name,
        "designation_id": str(practitioner.designation_id) if (practitioner and practitioner.designation_id) else None,
        "designation_name": desig,
        "medical_council_reg_no": practitioner.medical_council_reg_no if practitioner else None,
        "has_prescription_authority": practitioner.has_prescription_authority if practitioner else False,
        "prescription_authority_status": practitioner.prescription_authority_status if practitioner else "none",
    }


@staff_router.get("")
@admin_router.get("")
async def list_staff(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    facility_uuid: str | None = None,
    status: str = "active",
    q: str | None = None,
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    parsed_facility = UUID(facility_uuid) if facility_uuid and facility_uuid.lower() != "all" else None
    query = (
        select(
            StaffMember,
            UserAccount,
            Practitioner,
            Specialty.name.label("specialty_name"),
            SubSpecialty.name.label("sub_specialty_name"),
            StaffDesignation.name.label("designation_name"),
        )
        .join(UserAccount, UserAccount.id == StaffMember.user_account_id)
        .outerjoin(
            Practitioner,
            and_(
                Practitioner.user_account_id == StaffMember.user_account_id,
                Practitioner.organization_id == StaffMember.organization_id,
                Practitioner.is_active.is_(True),
            ),
        )
        .outerjoin(Specialty, Specialty.id == Practitioner.specialty_id)
        .outerjoin(SubSpecialty, SubSpecialty.id == Practitioner.sub_specialty_id)
        .outerjoin(StaffDesignation, StaffDesignation.id == Practitioner.designation_id)
        .where(StaffMember.organization_id == organization.id)
    )
    if status == "active":
        query = query.where(StaffMember.is_active.is_(True))
    if q:
        query = query.where(
            or_(
                UserAccount.display_name.ilike(f"%{q}%"),
                UserAccount.email.ilike(f"%{q}%"),
                Practitioner.person_name.ilike(f"%{q}%"),
            )
        )
    rows = (await db.execute(query)).all()
    items = []
    for member, user, practitioner, spec_name, sub_spec_name, desig_name in rows:
        assignments = (
            await db.scalars(
                select(StaffAssignment).where(
                    StaffAssignment.staff_member_id == member.id,
                    StaffAssignment.is_active.is_(True),
                )
            )
        ).all()
        if parsed_facility:
            has_facility = any(a.facility_id == parsed_facility for a in assignments)
            is_org_admin = any(a.role_code in ["organization_admin", "owner", "administrator"] for a in assignments)
            if not has_facility and not is_org_admin:
                continue
        items.append(_item(member, user, assignments, practitioner, spec_name, sub_spec_name, desig_name))
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}


@admin_router.post("", status_code=201)
async def create_staff(payload: StaffCreate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    user = await db.get(UserAccount, payload.user_account_uuid)
    if user is None:
        raise HTTPException(status_code=404, detail="User account not found. Provision the user in Logto first.")
    existing = await db.scalar(select(StaffMember).where(StaffMember.organization_id == organization.id, StaffMember.user_account_id == user.id))
    if existing:
        raise HTTPException(status_code=409, detail="User is already staff in this organization.")
    member = StaffMember(organization_id=organization.id, user_account_id=user.id)
    db.add(member)
    await db.flush()
    for facility_id in payload.facility_uuids:
        db.add(StaffAssignment(staff_member_id=member.id, facility_id=facility_id, role_code=payload.role_code))
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.created", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"uuid": str(member.id), "user_uuid": str(user.id)}, "meta": {}}


@admin_router.get("/{staff_uuid}")
async def get_staff(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == staff_uuid, StaffAssignment.is_active.is_(True)))).all()
    return {"success": True, "data": _item(*row, assignments), "meta": {}}


@admin_router.patch("/{staff_uuid}")
async def update_staff(staff_uuid: UUID, payload: StaffUpdate, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    if payload.display_name is not None:
        row[1].display_name = payload.display_name
    if payload.email is not None:
        row[1].email = payload.email
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.updated", resource_type="staff_member", resource_id=staff_uuid)
    await db.commit()
    return {"success": True, "data": {"uuid": str(staff_uuid), "display_name": row[1].display_name, "email": row[1].email}, "meta": {}}


@admin_router.post("/{staff_uuid}/deactivate")
async def deactivate_staff(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    member = await db.scalar(select(StaffMember).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))
    if member is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    member.is_active = False
    assignments = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id))).all()
    for assignment in assignments:
        assignment.is_active = False
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.deactivated", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"uuid": str(member.id), "status": "inactive"}, "meta": {}}


@admin_router.get("/{staff_uuid}/assignments")
async def get_assignments(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    values = (await db.execute(select(StaffAssignment).join(StaffMember, StaffMember.id == StaffAssignment.staff_member_id).where(StaffAssignment.staff_member_id == staff_uuid, StaffMember.organization_id == organization.id, StaffAssignment.is_active.is_(True)))).scalars().all()
    return {"success": True, "data": {"items": [{"uuid": str(a.id), "facility_uuid": str(a.facility_id) if a.facility_id else None, "role_code": a.role_code} for a in values]}, "meta": {}}


@admin_router.put("/{staff_uuid}/assignments")
async def replace_assignments(staff_uuid: UUID, payload: StaffAssignmentsInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    member = await db.scalar(select(StaffMember).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))
    if member is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    facilities = (await db.scalars(select(Facility).where(Facility.organization_id == organization.id, Facility.id.in_(payload.assignments), Facility.is_active.is_(True)))).all()
    if len(facilities) != len(set(payload.assignments)):
        raise HTTPException(status_code=400, detail="One or more facilities are not in this organization.")
    existing = (await db.scalars(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id))).all()
    for assignment in existing:
        assignment.is_active = False
    for facility_id in payload.assignments:
        db.add(StaffAssignment(staff_member_id=member.id, facility_id=facility_id, role_code="facility_operator", is_active=True))
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.assignments.replaced", resource_type="staff_member", resource_id=member.id)
    await db.commit()
    return {"success": True, "data": {"facility_uuids": [str(x) for x in payload.assignments]}, "meta": {}}


@admin_router.get("/{staff_uuid}/roles")
async def get_roles(staff_uuid: UUID, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    return {"success": True, "data": {"logto_user_id": row[1].logto_user_id, "role_keys": []}, "meta": {"source": "logto"}}


@admin_router.put("/{staff_uuid}/roles")
async def replace_roles(staff_uuid: UUID, payload: StaffRolesInput, account: Annotated[UserAccount, Depends(get_current_identity_account)], db: Annotated[AsyncSession, Depends(async_get_db)]) -> dict[str, Any]:
    _, organization = await _admin(db, account)
    row = (await db.execute(select(StaffMember, UserAccount).join(UserAccount, UserAccount.id == StaffMember.user_account_id).where(StaffMember.id == staff_uuid, StaffMember.organization_id == organization.id))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Staff member not found.")
    unknown = [key for key in payload.role_keys if key not in settings.LOGTO_ORGANIZATION_ROLE_IDS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported role keys: {', '.join(unknown)}")
    if not organization.logto_organization_id:
        raise HTTPException(status_code=409, detail="Organization is not linked to Logto.")
    for key in payload.role_keys:
        await logto_oidc_client.assign_management_organization_role(organization.logto_organization_id, row[1].logto_user_id, settings.LOGTO_ORGANIZATION_ROLE_IDS[key])
    await record_audit(db, organization_id=organization.id, actor_user_id=account.id, action="staff.roles.replaced", resource_type="staff_member", resource_id=staff_uuid)
    await db.commit()
    return {"success": True, "data": {"role_keys": payload.role_keys}, "meta": {"source": "logto"}}


@staff_router.post("/invitations", status_code=status.HTTP_201_CREATED)
@admin_router.post("/invitations", status_code=status.HTTP_201_CREATED)
async def create_invitation(
    payload: StaffInviteCreate,
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    role = await db.scalar(
        select(StaffAssignment).where(
            StaffAssignment.staff_member_id == staff.id,
            StaffAssignment.role_code.in_(["organization_admin", "owner", "administrator"]),
            StaffAssignment.is_active.is_(True),
        )
    )
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Staff administration access is required.")

    target_facility_id = None
    if payload.facility_uuid:
        fac = await db.scalar(
            select(Facility).where(
                Facility.id == payload.facility_uuid,
                Facility.organization_id == organization.id,
            )
        )
        if not fac:
            raise HTTPException(status_code=400, detail="Specified facility not found in this organization.")
        target_facility_id = fac.id
    else:
        fac = await db.scalar(
            select(Facility).where(
                Facility.organization_id == organization.id,
                Facility.is_active.is_(True),
            )
        )
        if fac:
            target_facility_id = fac.id

    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(days=7)
    invite = StaffInvitation(
        organization_id=organization.id,
        facility_id=target_facility_id,
        email=payload.email.lower().strip(),
        full_name=payload.full_name.strip(),
        role_code=payload.role_code,
        specialty_id=payload.specialty_id,
        sub_specialty_id=payload.sub_specialty_id,
        designation_id=payload.designation_id,
        medical_council_reg_no=payload.medical_council_reg_no,
        token=token,
        expires_at=expires_at,
        status="pending",
    )
    db.add(invite)
    await record_audit(
        db,
        organization_id=organization.id,
        actor_user_id=account.id,
        action="staff.invited",
        resource_type="staff_invitation",
        resource_id=invite.id,
    )
    await db.commit()
    await db.refresh(invite)
    return {
        "success": True,
        "data": {
            "id": str(invite.id),
            "email": invite.email,
            "full_name": invite.full_name,
            "role_code": invite.role_code,
            "specialty_id": str(invite.specialty_id) if invite.specialty_id else None,
            "sub_specialty_id": str(invite.sub_specialty_id) if invite.sub_specialty_id else None,
            "designation_id": str(invite.designation_id) if invite.designation_id else None,
            "medical_council_reg_no": invite.medical_council_reg_no,
            "facility_uuid": str(invite.facility_id) if invite.facility_id else None,
            "token": invite.token,
            "expires_at": invite.expires_at.isoformat(),
            "status": invite.status,
            "invite_url": f"/staff/accept?token={invite.token}",
        },
        "meta": {},
    }



@staff_router.get("/invitations")
@admin_router.get("/invitations")
async def list_invitations(
    account: Annotated[UserAccount, Depends(get_current_identity_account)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    staff, organization = await _staff_context(db, account)
    invites = (
        await db.scalars(
            select(StaffInvitation)
            .where(StaffInvitation.organization_id == organization.id)
            .order_by(StaffInvitation.created_at.desc())
        )
    ).all()
    items = [
        {
            "id": str(inv.id),
            "email": inv.email,
            "full_name": inv.full_name,
            "role_code": inv.role_code,
            "specialty_id": str(inv.specialty_id) if inv.specialty_id else None,
            "sub_specialty_id": str(inv.sub_specialty_id) if inv.sub_specialty_id else None,
            "designation_id": str(inv.designation_id) if inv.designation_id else None,
            "medical_council_reg_no": inv.medical_council_reg_no,
            "facility_uuid": str(inv.facility_id) if inv.facility_id else None,
            "status": inv.status,
            "token": inv.token,
            "expires_at": inv.expires_at.isoformat(),
            "created_at": inv.created_at.isoformat(),
        }
        for inv in invites
    ]
    return {"success": True, "data": {"items": items}, "meta": {"count": len(items)}}



@staff_router.get("/invitations/validate")
@admin_router.get("/invitations/validate")
async def validate_invitation(
    token: str,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invite = await db.scalar(select(StaffInvitation).where(StaffInvitation.token == token))
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found or invalid.")
    if invite.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invitation has already been {invite.status}.")
    if invite.expires_at < datetime.now(UTC):
        invite.status = "expired"
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation has expired.")

    org = await db.get(Organization, invite.organization_id)
    fac = await db.get(Facility, invite.facility_id) if invite.facility_id else None

    return {
        "success": True,
        "data": {
            "valid": True,
            "email": invite.email,
            "full_name": invite.full_name,
            "role_code": invite.role_code,
            "specialty_id": str(invite.specialty_id) if invite.specialty_id else None,
            "sub_specialty_id": str(invite.sub_specialty_id) if invite.sub_specialty_id else None,
            "designation_id": str(invite.designation_id) if invite.designation_id else None,
            "medical_council_reg_no": invite.medical_council_reg_no,
            "organization_name": org.name if org else "Clinic",
            "organization_id": str(org.id) if org else None,
            "facility_name": fac.name if fac else None,
            "facility_uuid": str(fac.id) if fac else None,
            "expires_at": invite.expires_at.isoformat(),
        },
        "meta": {},
    }



@staff_router.post("/invitations/accept")
@admin_router.post("/invitations/accept")
async def accept_invitation(
    payload: StaffInviteAccept,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, Any]:
    invite = await db.scalar(select(StaffInvitation).where(StaffInvitation.token == payload.token))
    if not invite:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found or invalid.")
    if invite.status != "pending":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invitation has already been {invite.status}.")
    if invite.expires_at < datetime.now(UTC):
        invite.status = "expired"
        await db.commit()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation has expired.")

    account = await db.scalar(select(UserAccount).where(UserAccount.email == invite.email))
    if not account:
        account = UserAccount(
            logto_user_id=f"local:{invite.email}",
            email=invite.email,
            display_name=payload.full_name or invite.full_name,
            password_hash=get_password_hash(payload.password),
            is_active=True,
        )
        db.add(account)
        await db.flush()
    else:
        account.password_hash = get_password_hash(payload.password)
        if payload.full_name:
            account.display_name = payload.full_name
        account.is_active = True
        await db.flush()

    member = await db.scalar(
        select(StaffMember).where(
            StaffMember.organization_id == invite.organization_id,
            StaffMember.user_account_id == account.id,
        )
    )
    if not member:
        member = StaffMember(organization_id=invite.organization_id, user_account_id=account.id, is_active=True)
        db.add(member)
        await db.flush()
    else:
        member.is_active = True

    target_facility_id = invite.facility_id
    if not target_facility_id:
        primary_facility = await db.scalar(
            select(Facility).where(Facility.organization_id == invite.organization_id, Facility.is_active.is_(True))
        )
        if primary_facility:
            target_facility_id = primary_facility.id

    assignment = await db.scalar(
        select(StaffAssignment).where(
            StaffAssignment.staff_member_id == member.id,
            StaffAssignment.role_code == invite.role_code,
        )
    )
    if not assignment:
        assignment = StaffAssignment(
            staff_member_id=member.id,
            facility_id=target_facility_id,
            role_code=invite.role_code,
            is_active=True,
        )
        db.add(assignment)
    else:
        assignment.is_active = True
        if target_facility_id:
            assignment.facility_id = target_facility_id

    if invite.role_code in ["practitioner", "doctor"]:
        pract = await db.scalar(
            select(Practitioner).where(
                Practitioner.user_account_id == account.id,
                Practitioner.organization_id == invite.organization_id,
            )
        )
        spec_name = "General Practice"
        if invite.specialty_id:
            spec = await db.get(Specialty, invite.specialty_id)
            if spec:
                spec_name = spec.name

        if not pract:
            pract = Practitioner(
                organization_id=invite.organization_id,
                person_name=account.display_name,
                specialty=spec_name,
                specialty_id=invite.specialty_id,
                sub_specialty_id=invite.sub_specialty_id,
                designation_id=invite.designation_id,
                medical_council_reg_no=invite.medical_council_reg_no,
                has_prescription_authority=True,
                prescription_authority_status="authorized",
                user_account_id=account.id,
                is_active=True,
            )
            db.add(pract)
            await db.flush()
        else:
            if invite.specialty_id:
                pract.specialty_id = invite.specialty_id
                pract.specialty = spec_name
            if invite.sub_specialty_id:
                pract.sub_specialty_id = invite.sub_specialty_id
            if invite.designation_id:
                pract.designation_id = invite.designation_id
            if invite.medical_council_reg_no:
                pract.medical_council_reg_no = invite.medical_council_reg_no

        has_working_hours = await db.scalar(select(PractitionerAvailabilityRule.id).where(
            PractitionerAvailabilityRule.facility_id == target_facility_id,
            PractitionerAvailabilityRule.practitioner_id == pract.id,
        )) if target_facility_id else None
        schedule = await db.scalar(select(FacilitySchedule).where(
            FacilitySchedule.facility_id == target_facility_id,
        )) if target_facility_id and not has_working_hours else None
        if schedule:
            for rule in rules_from_facility_schedule(schedule, invite.organization_id, target_facility_id, pract.id):
                db.add(rule)


    invite.status = "accepted"
    invite.accepted_at = datetime.now(UTC)

    await record_audit(
        db,
        organization_id=invite.organization_id,
        actor_user_id=account.id,
        action="staff.invitation_accepted",
        resource_type="staff_member",
        resource_id=member.id,
    )
    await db.commit()
    await db.refresh(account)

    access_token = await create_access_token({
        "sub": str(account.id),
        "email": account.email,
        "name": account.display_name,
        "ver": account.credentials_version,
    })

    return {
        "success": True,
        "data": {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "uuid": str(account.id),
                "email": account.email,
                "display_name": account.display_name,
                "role_code": invite.role_code,
            },
        },
        "meta": {},
    }


router.include_router(admin_router)
router.include_router(staff_router)
