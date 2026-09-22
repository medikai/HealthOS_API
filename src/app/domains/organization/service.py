import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.availability import rules_from_facility_schedule
from ...core.config import settings
from ...domains.auth.logto import logto_oidc_client
from ...models.identity import UserAccount
from ...models.masters import City, Country, District, MedicalCouncil, Specialty, State
from ...models.organization import (
    Department,
    Facility,
    FacilitySchedule,
    Organization,
    StaffAssignment,
    StaffMember,
)
from ...models.platform import Feature, FeatureAssignment

VALID_ROLE_CODES = {"organization_admin", "practitioner", "nurse", "receptionist", "billing_staff"}


class AccessService:
    async def create_facility(
        self, db: AsyncSession, actor: UserAccount, organization_id: uuid.UUID,
        name: str, code: str, *, classification: str | None = None,
        street_address: str | None = None, country_id: uuid.UUID | None = None,
        state_id: uuid.UUID | None = None, district_id: uuid.UUID | None = None,
        city_id: uuid.UUID | None = None,
        postal_code: str | None = None, phone: str | None = None,
        timezone: str = "Asia/Kolkata",
    ) -> Facility:
        await self._require_admin(db, actor.id, organization_id)
        if await db.scalar(select(Facility).where(Facility.organization_id == organization_id, Facility.code == code)):
            raise HTTPException(status_code=409, detail="Facility code already exists in this organization.")
        await self._validate_location(db, country_id, state_id, district_id, city_id)
        facility = Facility(
            organization_id=organization_id, name=name, code=code,
            classification=classification, street_address=street_address,
            country_id=country_id, state_id=state_id, district_id=district_id, city_id=city_id,
            postal_code=postal_code, phone=phone,
        )
        db.add(facility)
        await db.flush()
        db.add(FacilitySchedule(facility_id=facility.id, timezone=timezone))
        await db.commit()
        await db.refresh(facility)
        return facility

    async def create_department(
        self, db: AsyncSession, actor: UserAccount, organization_id: uuid.UUID, name: str, code: str, facility_id: uuid.UUID | None
    ) -> Department:
        await self._require_admin(db, actor.id, organization_id)
        if facility_id is not None and await db.scalar(
            select(Facility).where(Facility.id == facility_id, Facility.organization_id == organization_id)
        ) is None:
            raise HTTPException(status_code=404, detail="Facility not found in this organization.")
        if await db.scalar(select(Department).where(Department.organization_id == organization_id, Department.code == code)):
            raise HTTPException(status_code=409, detail="Department code already exists in this organization.")
        department = Department(organization_id=organization_id, facility_id=facility_id, name=name, code=code)
        db.add(department)
        await db.commit()
        await db.refresh(department)
        return department

    async def create_organization(
        self,
        db: AsyncSession,
        account: UserAccount,
        name: str,
        code: str,
        specialty_id: uuid.UUID | None = None,
        medical_council_id: uuid.UUID | None = None,
        medical_council_reg_no: str | None = None,
        clinic_name: str | None = None,
        classification: str | None = None,
        street_address: str | None = None,
        country_id: uuid.UUID | None = None,
        state_id: uuid.UUID | None = None,
        district_id: uuid.UUID | None = None,
        city_id: uuid.UUID | None = None,
        postal_code: str | None = None,
        phone: str | None = None,
        timezone: str = "Asia/Kolkata",
    ) -> Organization:
        if await db.scalar(select(Organization).where((Organization.name == name) | (Organization.code == code))):
            raise HTTPException(status_code=409, detail="Organization name or code already exists.")
        logto_organization_id = None
        if settings.LOGTO_ORGANIZATIONS_ENABLED:
            role_id = settings.LOGTO_ORGANIZATION_ADMIN_ROLE_ID
            if not role_id:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Logto organization admin role is not configured.")
            logto_organization_id = await logto_oidc_client.create_management_organization(name)
            await logto_oidc_client.add_management_organization_member(logto_organization_id, account.logto_user_id)
            await logto_oidc_client.assign_management_organization_role(logto_organization_id, account.logto_user_id, role_id)
        await self._validate_location(db, country_id, state_id, district_id, city_id)
        organization = Organization(name=name, code=code, logto_organization_id=logto_organization_id)
        db.add(organization)
        await db.flush()
        facility = Facility(
            organization_id=organization.id, name=clinic_name or f"{name} Main Clinic", code="MAIN",
            classification=classification, street_address=street_address,
            country_id=country_id, state_id=state_id, district_id=district_id, city_id=city_id,
            postal_code=postal_code, phone=phone, is_active=True,
        )
        db.add(facility)
        await db.flush()
        schedule = FacilitySchedule(facility_id=facility.id, timezone=timezone)
        db.add(schedule)
        member = StaffMember(organization_id=organization.id, user_account_id=account.id)
        db.add(member)
        await db.flush()
        db.add(StaffAssignment(staff_member_id=member.id, facility_id=facility.id, role_code="organization_admin"))

        from ...models.care import Practitioner
        resolved_specialty_id = specialty_id or getattr(account, "registration_specialty_id", None)
        specialty = await db.get(Specialty, resolved_specialty_id) if resolved_specialty_id else None
        if resolved_specialty_id and (specialty is None or not specialty.is_active):
            raise HTTPException(status_code=422, detail="Clinical specialty does not exist or is inactive.")
        if specialty is None and getattr(account, "registration_specialty", None):
            entered = account.registration_specialty.strip()
            specialty = await db.scalar(select(Specialty).where(or_(
                Specialty.code == entered.lower().replace(" ", "_"),
                func.lower(Specialty.name) == entered.lower(),
            )))
        if specialty is None:
            specialty = await db.scalar(select(Specialty).where(Specialty.code == "general_practice"))

        resolved_council_id = medical_council_id or getattr(account, "registration_medical_council_id", None)
        if resolved_council_id:
            council = await db.get(MedicalCouncil, resolved_council_id)
            if council is None or not council.is_active:
                raise HTTPException(status_code=422, detail="Medical council does not exist or is inactive.")

        pract = await db.scalar(select(Practitioner).where(Practitioner.user_account_id == account.id))
        if not pract:
            pract = Practitioner(
                organization_id=organization.id,
                person_name=account.display_name or "Doctor",
                specialty=specialty.name if specialty else (getattr(account, "registration_specialty", None) or "General Practice"),
                specialty_id=specialty.id if specialty else None,
                medical_council_id=resolved_council_id,
                medical_council_reg_no=medical_council_reg_no or getattr(account, "registration_medical_council_reg_no", None),
                user_account_id=account.id,
                is_active=True
            )
            db.add(pract)
            await db.flush()
            for rule in rules_from_facility_schedule(schedule, organization.id, facility.id, pract.id):
                db.add(rule)

        await db.commit()
        await db.refresh(organization)
        return organization

    async def assign_role(self, db: AsyncSession, actor: UserAccount, organization_id: uuid.UUID, logto_user_id: str, role_code: str) -> StaffAssignment:
        self._validate_role(role_code)
        await self._require_admin(db, actor.id, organization_id)
        organization = await db.scalar(select(Organization).where(Organization.id == organization_id))
        if organization is None:
            raise HTTPException(status_code=404, detail="Organization not found.")
        target = await db.scalar(select(UserAccount).where(UserAccount.logto_user_id == logto_user_id))
        if target is None:
            raise HTTPException(status_code=404, detail="The target user must sign in with Logto before a role can be assigned.")
        member = await db.scalar(select(StaffMember).where(StaffMember.organization_id == organization_id, StaffMember.user_account_id == target.id))
        if member is None:
            member = StaffMember(organization_id=organization_id, user_account_id=target.id)
            db.add(member)
            await db.flush()
        assignment = await db.scalar(select(StaffAssignment).where(StaffAssignment.staff_member_id == member.id, StaffAssignment.role_code == role_code))
        if assignment is None:
            assignment = StaffAssignment(staff_member_id=member.id, role_code=role_code)
            db.add(assignment)
        if settings.LOGTO_ORGANIZATIONS_ENABLED:
            role_id = settings.LOGTO_ORGANIZATION_ROLE_IDS.get(role_code)
            if not role_id:
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Logto role mapping is missing for '{role_code}'.")
            if not organization.logto_organization_id:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Organization is not linked to Logto.")
            await logto_oidc_client.add_management_organization_member(organization.logto_organization_id, target.logto_user_id)
            await logto_oidc_client.assign_management_organization_role(organization.logto_organization_id, target.logto_user_id, role_id)
        await db.commit()
        await db.refresh(assignment)
        return assignment

    async def add_feature(self, db: AsyncSession, actor: UserAccount, organization_id: uuid.UUID, code: str, name: str, description: str | None) -> FeatureAssignment:
        await self._require_admin(db, actor.id, organization_id)
        feature = await db.scalar(select(Feature).where(Feature.code == code))
        if feature is None:
            feature = Feature(code=code, name=name, description=description)
            db.add(feature)
            await db.flush()
        assignment = await db.scalar(select(FeatureAssignment).where(FeatureAssignment.organization_id == organization_id, FeatureAssignment.feature_id == feature.id))
        if assignment is None:
            assignment = FeatureAssignment(organization_id=organization_id, feature_id=feature.id)
            db.add(assignment)
            await db.commit()
            await db.refresh(assignment)
        return assignment

    async def _require_admin(self, db: AsyncSession, account_id: uuid.UUID, organization_id: uuid.UUID) -> None:
        statement = select(StaffAssignment.id).join(StaffMember).where(StaffMember.organization_id == organization_id, StaffMember.user_account_id == account_id, StaffAssignment.role_code == "organization_admin", StaffAssignment.is_active.is_(True))
        if await db.scalar(statement) is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization administrator role required.")

    @staticmethod
    def _validate_role(role_code: str) -> None:
        if role_code not in VALID_ROLE_CODES:
            raise HTTPException(status_code=422, detail=f"Unsupported role. Allowed roles: {', '.join(sorted(VALID_ROLE_CODES))}.")

    @staticmethod
    async def _validate_location(
        db: AsyncSession,
        country_id: uuid.UUID | None,
        state_id: uuid.UUID | None,
        district_id: uuid.UUID | None,
        city_id: uuid.UUID | None,
    ) -> None:
        if state_id and not country_id:
            raise HTTPException(status_code=422, detail="country_id is required with state_id.")
        if city_id and not state_id:
            raise HTTPException(status_code=422, detail="state_id is required with city_id.")
        if district_id and not state_id:
            raise HTTPException(status_code=422, detail="state_id is required with district_id.")
        if country_id:
            country = await db.get(Country, country_id)
            if country is None or not country.is_active:
                raise HTTPException(status_code=422, detail="Country does not exist or is inactive.")
        if state_id:
            state_row = await db.get(State, state_id)
            if state_row is None or not state_row.is_active or state_row.country_id != country_id:
                raise HTTPException(status_code=422, detail="State does not belong to the selected country.")
        if district_id:
            district = await db.get(District, district_id)
            if district is None or not district.is_active or district.state_id != state_id:
                raise HTTPException(status_code=422, detail="District does not belong to the selected state.")
        if city_id:
            city = await db.get(City, city_id)
            if city is None or not city.is_active or city.state_id != state_id or city.district_id != district_id:
                raise HTTPException(status_code=422, detail="City does not belong to the selected district.")


access_service = AccessService()
