import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.identity import UserAccount
from ...models.organization import Organization, StaffAssignment, StaffMember
from ...models.platform import Feature, FeatureAssignment

VALID_ROLE_CODES = {"organization_admin", "practitioner", "nurse", "receptionist", "billing_staff"}


class AccessService:
    async def create_organization(self, db: AsyncSession, account: UserAccount, name: str, code: str) -> Organization:
        if await db.scalar(select(Organization).where((Organization.name == name) | (Organization.code == code))):
            raise HTTPException(status_code=409, detail="Organization name or code already exists.")
        organization = Organization(name=name, code=code)
        db.add(organization)
        await db.flush()
        member = StaffMember(organization_id=organization.id, user_account_id=account.id)
        db.add(member)
        await db.flush()
        db.add(StaffAssignment(staff_member_id=member.id, role_code="organization_admin"))
        await db.commit()
        await db.refresh(organization)
        return organization

    async def assign_role(self, db: AsyncSession, actor: UserAccount, organization_id: uuid.UUID, logto_user_id: str, role_code: str) -> StaffAssignment:
        self._validate_role(role_code)
        await self._require_admin(db, actor.id, organization_id)
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


access_service = AccessService()
