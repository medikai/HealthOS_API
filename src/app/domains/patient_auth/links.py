"""Audited portal-account → clinical-patient record links.

Linking is never derived from name/DOB/phone matching. It is created only by:
1. staff-authorized, expiring, single-use, recipient-bound invitation activation;
2. authenticated patient request (self-start or clinic-assisted claims) that a
   staff member explicitly approves against a validated organization-scoped
   clinical patient.

The link table is the explicit mapping between the portal principal and
organization-scoped clinical patients, so staff ``UserAccount.person_id`` is
never overwritten and one account can hold links across organizations.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...models.identity import (
    Patient,
    PatientLinkInvitation,
    PatientPortalAccount,
    PatientRecordLink,
)
from ...models.organization import Organization
from ..governance.audit import record_audit
from .phone import normalize_phone

LINK_STATUSES = ("pending", "verified", "rejected", "revoked")
ACTIVE_LINK_STATUSES = ("pending", "verified")

_INVITATION_PURPOSE = "portal_link"


class PatientLinkError(RuntimeError):
    code = "PATIENT_LINK_ERROR"


class InvitationInvalid(PatientLinkError):
    code = "LINK_CODE_INVALID"


class InvitationExpired(PatientLinkError):
    code = "LINK_CODE_EXPIRED"


class InvitationNotPending(PatientLinkError):
    code = "LINK_INVITATION_NOT_PENDING"


class LinkAlreadyExists(PatientLinkError):
    code = "LINK_ALREADY_EXISTS"


class LinkAlreadyVerified(PatientLinkError):
    code = "LINK_ALREADY_VERIFIED"


class LinkNotFound(PatientLinkError):
    code = "LINK_NOT_FOUND"


class LinkNotPending(PatientLinkError):
    code = "LINK_REQUEST_NOT_PENDING"


class ClinicNotAvailable(PatientLinkError):
    code = "CLINIC_NOT_AVAILABLE"


class ClinicalPatientInvalid(PatientLinkError):
    code = "CLINICAL_PATIENT_INVALID"


def _digest(pepper: str, value: str) -> str:
    return hmac.new(pepper.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


class PatientLinkService:
    def __init__(
        self,
        *,
        pepper: str,
        invitation_ttl_hours: int,
        invitation_max_ttl_hours: int,
    ) -> None:
        self.pepper = pepper
        self.invitation_ttl_hours = invitation_ttl_hours
        self.invitation_max_ttl_hours = invitation_max_ttl_hours

    def invitation_token_hash(self, token: str) -> str:
        return _digest(self.pepper, f"patient_link_invitation:{token}")

    async def create_invitation(
        self,
        db: AsyncSession,
        *,
        organization: Organization,
        patient: Patient,
        facility_id: UUID | None,
        phone: str,
        created_by_user_id: UUID | None,
        ttl_hours: int | None = None,
    ) -> tuple[PatientLinkInvitation, str]:
        canonical = normalize_phone(phone)
        hours = ttl_hours or self.invitation_ttl_hours
        hours = max(1, min(int(hours), self.invitation_max_ttl_hours))
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        invitation = PatientLinkInvitation(
            organization_id=organization.id,
            facility_id=facility_id,
            patient_id=patient.id,
            phone=canonical,
            token_hash=self.invitation_token_hash(token),
            purpose=_INVITATION_PURPOSE,
            status="pending",
            created_by_user_id=created_by_user_id,
            expires_at=now + timedelta(hours=hours),
        )
        db.add(invitation)
        await record_audit(
            db,
            organization_id=organization.id,
            facility_id=facility_id,
            actor_user_id=created_by_user_id,
            action="patient.link_invitation_created",
            resource_type="patient_link_invitation",
            resource_id=invitation.id,
            patient_id=patient.id,
            details={"purpose": _INVITATION_PURPOSE, "expires_at": invitation.expires_at.isoformat()},
        )
        await db.commit()
        await db.refresh(invitation)
        return invitation, token

    async def list_invitations(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        status: str | None = None,
        patient_id: UUID | None = None,
        facility_id: UUID | None = None,
        facility_ids: list[UUID] | None = None,
    ) -> list[PatientLinkInvitation]:
        query = select(PatientLinkInvitation).where(
            PatientLinkInvitation.organization_id == organization_id
        )
        if status:
            query = query.where(PatientLinkInvitation.status == status)
        if patient_id:
            query = query.where(PatientLinkInvitation.patient_id == patient_id)
        if facility_id:
            query = query.where(PatientLinkInvitation.facility_id == facility_id)
        if facility_ids is not None:
            query = query.where(PatientLinkInvitation.facility_id.in_(facility_ids))
        return list(
            (await db.scalars(query.order_by(PatientLinkInvitation.created_at.desc()))).all()
        )

    async def revoke_invitation(
        self,
        db: AsyncSession,
        *,
        invitation: PatientLinkInvitation,
        actor_user_id: UUID | None,
    ) -> PatientLinkInvitation:
        if invitation.status != "pending":
            raise InvitationNotPending("This invitation is no longer pending.")
        now = datetime.now(UTC)
        result = await db.execute(
            update(PatientLinkInvitation)
            .where(
                PatientLinkInvitation.id == invitation.id,
                PatientLinkInvitation.status == "pending",
            )
            .values(status="revoked", revoked_at=now, revoked_by_user_id=actor_user_id)
        )
        if not result.rowcount:
            raise InvitationNotPending("This invitation is no longer pending.")
        await record_audit(
            db,
            organization_id=invitation.organization_id,
            facility_id=invitation.facility_id,
            actor_user_id=actor_user_id,
            action="patient.link_invitation_revoked",
            resource_type="patient_link_invitation",
            resource_id=invitation.id,
            patient_id=invitation.patient_id,
        )
        await db.commit()
        await db.refresh(invitation)
        return invitation

    async def activate(
        self,
        db: AsyncSession,
        *,
        account: PatientPortalAccount,
        token: str,
    ) -> PatientRecordLink:
        now = datetime.now(UTC)
        invitation = await db.scalar(
            select(PatientLinkInvitation).where(
                PatientLinkInvitation.token_hash == self.invitation_token_hash(token)
            )
        )
        if invitation is None:
            raise InvitationInvalid("The link code is invalid.")
        if invitation.purpose != _INVITATION_PURPOSE:
            raise InvitationInvalid("The link code is invalid.")
        if invitation.status == "accepted":
            if invitation.accepted_by_account_id == account.id and invitation.accepted_link_id:
                existing = await db.get(PatientRecordLink, invitation.accepted_link_id)
                if existing is not None:
                    return existing
            raise InvitationInvalid("The link code is invalid.")
        if invitation.expires_at <= now:
            await db.execute(
                update(PatientLinkInvitation)
                .where(
                    PatientLinkInvitation.id == invitation.id,
                    PatientLinkInvitation.status == "pending",
                )
                .values(status="expired")
            )
            await db.commit()
            raise InvitationExpired("The link code has expired.")
        if invitation.status != "pending":
            raise InvitationInvalid("The link code is invalid.")
        # Intended-recipient binding: the verified account phone must match.
        if invitation.phone != account.phone:
            raise InvitationInvalid("The link code is invalid.")

        patient = await db.get(Patient, invitation.patient_id)
        if (
            patient is None
            or not patient.is_active
            or patient.organization_id != invitation.organization_id
        ):
            raise InvitationInvalid("The link code is invalid.")

        existing_for_account = await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_account_id == account.id,
                PatientRecordLink.organization_id == invitation.organization_id,
                PatientRecordLink.status.in_(ACTIVE_LINK_STATUSES),
            )
        )
        if existing_for_account is not None:
            if (
                existing_for_account.status == "verified"
                and existing_for_account.patient_id == invitation.patient_id
            ):
                await db.execute(
                    update(PatientLinkInvitation)
                    .where(
                        PatientLinkInvitation.id == invitation.id,
                        PatientLinkInvitation.status == "pending",
                    )
                    .values(
                        status="accepted",
                        accepted_at=now,
                        accepted_by_account_id=account.id,
                        accepted_link_id=existing_for_account.id,
                    )
                )
                await db.commit()
                return existing_for_account
            raise LinkAlreadyExists("A record link already exists for this clinic.")

        existing_for_patient = await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_id == invitation.patient_id,
                PatientRecordLink.status.in_(ACTIVE_LINK_STATUSES),
            )
        )
        if existing_for_patient is not None:
            raise LinkAlreadyExists("A record link already exists for this patient.")

        claimed = await db.execute(
            update(PatientLinkInvitation)
            .where(
                PatientLinkInvitation.id == invitation.id,
                PatientLinkInvitation.status == "pending",
            )
            .values(status="accepted")
        )
        if not claimed.rowcount:
            await db.rollback()
            current = await db.get(PatientLinkInvitation, invitation.id)
            if (
                current is not None
                and current.status == "accepted"
                and current.accepted_by_account_id == account.id
                and current.accepted_link_id
            ):
                return await db.get(PatientRecordLink, current.accepted_link_id)
            raise InvitationInvalid("The link code is invalid.")

        link = PatientRecordLink(
            patient_account_id=account.id,
            organization_id=invitation.organization_id,
            facility_id=invitation.facility_id,
            patient_id=invitation.patient_id,
            status="verified",
            request_kind="clinic_invitation",
            verification_source="clinic_invitation",
            reviewed_by_user_id=invitation.created_by_user_id,
            reviewed_at=now,
        )
        db.add(link)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise LinkAlreadyExists("A record link already exists for this patient.") from exc
        invitation.accepted_at = now
        invitation.accepted_by_account_id = account.id
        invitation.accepted_link_id = link.id
        await record_audit(
            db,
            organization_id=invitation.organization_id,
            facility_id=invitation.facility_id,
            actor_user_id=None,
            action="patient.link_verified",
            resource_type="patient_record_link",
            resource_id=link.id,
            patient_id=link.patient_id,
            details={"source": "clinic_invitation", "invitation_id": str(invitation.id)},
        )
        await db.commit()
        await db.refresh(link)
        return link

    async def request_link(
        self,
        db: AsyncSession,
        *,
        account: PatientPortalAccount,
        organization: Organization,
        facility_id: UUID | None,
        claimed_name: str | None = None,
        claimed_date_of_birth: str | None = None,
    ) -> PatientRecordLink:
        if not organization.is_active or not organization.portal_enabled:
            raise ClinicNotAvailable("This clinic is not available in the patient portal.")
        existing = await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_account_id == account.id,
                PatientRecordLink.organization_id == organization.id,
                PatientRecordLink.status.in_(ACTIVE_LINK_STATUSES),
            )
        )
        if existing is not None:
            if existing.status == "pending":
                return existing
            raise LinkAlreadyVerified("This clinic is already linked to your account.")
        kind = "clinic_assisted" if (claimed_name or claimed_date_of_birth) else "self_request"
        link = PatientRecordLink(
            patient_account_id=account.id,
            organization_id=organization.id,
            facility_id=facility_id,
            patient_id=None,
            status="pending",
            request_kind=kind,
            claimed_name=claimed_name[:255] if claimed_name else None,
            claimed_date_of_birth=claimed_date_of_birth[:10] if claimed_date_of_birth else None,
        )
        db.add(link)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise LinkAlreadyExists("A record link request already exists.") from exc
        await record_audit(
            db,
            organization_id=organization.id,
            facility_id=facility_id,
            actor_user_id=None,
            action="patient.link_requested",
            resource_type="patient_record_link",
            resource_id=link.id,
            details={"kind": kind},
        )
        await db.commit()
        await db.refresh(link)
        return link

    async def approve_request(
        self,
        db: AsyncSession,
        *,
        link: PatientRecordLink,
        patient: Patient,
        actor_user_id: UUID,
        facility_id: UUID | None = None,
        review_reason: str | None = None,
    ) -> PatientRecordLink:
        if link.status != "pending":
            raise LinkNotPending("This link request is no longer pending.")
        if patient.organization_id != link.organization_id or not patient.is_active:
            raise ClinicalPatientInvalid("The clinical patient is not valid for this organization.")
        other = await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_id == patient.id,
                PatientRecordLink.status.in_(ACTIVE_LINK_STATUSES),
                PatientRecordLink.id != link.id,
            )
        )
        if other is not None:
            raise LinkAlreadyExists("A record link already exists for this patient.")
        now = datetime.now(UTC)
        source = "clinic_assisted" if link.request_kind == "clinic_assisted" else "clinic_review"
        result = await db.execute(
            update(PatientRecordLink)
            .where(
                PatientRecordLink.id == link.id,
                PatientRecordLink.status == "pending",
            )
            .values(
                patient_id=patient.id,
                facility_id=facility_id if facility_id is not None else link.facility_id,
                status="verified",
                verification_source=source,
                reviewed_by_user_id=actor_user_id,
                reviewed_at=now,
                review_reason=review_reason[:255] if review_reason else None,
                updated_at=now,
            )
        )
        if not result.rowcount:
            await db.rollback()
            raise LinkNotPending("This link request is no longer pending.")
        await record_audit(
            db,
            organization_id=link.organization_id,
            facility_id=facility_id if facility_id is not None else link.facility_id,
            actor_user_id=actor_user_id,
            action="patient.link_verified",
            resource_type="patient_record_link",
            resource_id=link.id,
            patient_id=patient.id,
            details={"source": source},
        )
        try:
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise LinkAlreadyExists("A record link already exists for this patient.") from exc
        await db.refresh(link)
        return link

    async def reject_request(
        self,
        db: AsyncSession,
        *,
        link: PatientRecordLink,
        actor_user_id: UUID,
        reason: str | None = None,
    ) -> PatientRecordLink:
        if link.status != "pending":
            raise LinkNotPending("This link request is no longer pending.")
        now = datetime.now(UTC)
        result = await db.execute(
            update(PatientRecordLink)
            .where(
                PatientRecordLink.id == link.id,
                PatientRecordLink.status == "pending",
            )
            .values(
                status="rejected",
                reviewed_by_user_id=actor_user_id,
                reviewed_at=now,
                review_reason=reason[:255] if reason else None,
                updated_at=now,
            )
        )
        if not result.rowcount:
            await db.rollback()
            raise LinkNotPending("This link request is no longer pending.")
        await record_audit(
            db,
            organization_id=link.organization_id,
            facility_id=link.facility_id,
            actor_user_id=actor_user_id,
            action="patient.link_rejected",
            resource_type="patient_record_link",
            resource_id=link.id,
            details={"reason": reason[:255] if reason else None},
        )
        await db.commit()
        await db.refresh(link)
        return link

    async def revoke_link(
        self,
        db: AsyncSession,
        *,
        link: PatientRecordLink,
        actor_user_id: UUID,
        reason: str | None = None,
    ) -> PatientRecordLink:
        if link.status != "verified":
            raise LinkNotPending("Only a verified link can be revoked.")
        now = datetime.now(UTC)
        result = await db.execute(
            update(PatientRecordLink)
            .where(
                PatientRecordLink.id == link.id,
                PatientRecordLink.status == "verified",
            )
            .values(
                status="revoked",
                revoked_at=now,
                revoked_by_user_id=actor_user_id,
                revoke_reason=reason[:255] if reason else None,
                updated_at=now,
            )
        )
        if not result.rowcount:
            await db.rollback()
            raise LinkNotPending("Only a verified link can be revoked.")
        await record_audit(
            db,
            organization_id=link.organization_id,
            facility_id=link.facility_id,
            actor_user_id=actor_user_id,
            action="patient.link_revoked",
            resource_type="patient_record_link",
            resource_id=link.id,
            patient_id=link.patient_id,
            details={"reason": reason[:255] if reason else None},
        )
        await db.commit()
        await db.refresh(link)
        return link

    async def list_links_for_account(
        self, db: AsyncSession, *, account_id: UUID
    ) -> list[PatientRecordLink]:
        return list(
            (
                await db.scalars(
                    select(PatientRecordLink)
                    .where(PatientRecordLink.patient_account_id == account_id)
                    .order_by(PatientRecordLink.created_at.desc())
                )
            ).all()
        )

    async def list_links_for_organization(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        status: str | None = None,
        patient_id: UUID | None = None,
        facility_id: UUID | None = None,
        facility_ids: list[UUID] | None = None,
    ) -> list[PatientRecordLink]:
        query = select(PatientRecordLink).where(
            PatientRecordLink.organization_id == organization_id
        )
        if status:
            query = query.where(PatientRecordLink.status == status)
        if patient_id:
            query = query.where(PatientRecordLink.patient_id == patient_id)
        if facility_id:
            query = query.where(PatientRecordLink.facility_id == facility_id)
        if facility_ids is not None:
            query = query.where(PatientRecordLink.facility_id.in_(facility_ids))
        return list(
            (await db.scalars(query.order_by(PatientRecordLink.created_at.desc()))).all()
        )

    async def verified_links(
        self, db: AsyncSession, *, account_id: UUID
    ) -> list[PatientRecordLink]:
        return list(
            (
                await db.scalars(
                    select(PatientRecordLink).where(
                        PatientRecordLink.patient_account_id == account_id,
                        PatientRecordLink.status == "verified",
                    )
                )
            ).all()
        )

    async def verified_link_for_organization(
        self,
        db: AsyncSession,
        *,
        account_id: UUID,
        organization_id: UUID,
    ) -> PatientRecordLink | None:
        return await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_account_id == account_id,
                PatientRecordLink.organization_id == organization_id,
                PatientRecordLink.status == "verified",
            )
        )

    async def verified_link_for_patient(
        self,
        db: AsyncSession,
        *,
        account_id: UUID,
        patient_id: UUID,
    ) -> PatientRecordLink | None:
        return await db.scalar(
            select(PatientRecordLink).where(
                PatientRecordLink.patient_account_id == account_id,
                PatientRecordLink.patient_id == patient_id,
                PatientRecordLink.status == "verified",
            )
        )

    @staticmethod
    def facility_permitted(link: PatientRecordLink, facility_id: UUID | None) -> bool:
        """A null link facility permits the whole clinic; a set value is exclusive."""
        if facility_id is None:
            return True
        if link.facility_id is None:
            return True
        return link.facility_id == facility_id

    async def link_status(self, db: AsyncSession, *, account_id: UUID) -> str:
        rows = (
            await db.scalars(
                select(PatientRecordLink.status).where(
                    PatientRecordLink.patient_account_id == account_id
                )
            )
        ).all()
        values = set(rows)
        if "verified" in values:
            return "verified"
        if "pending" in values:
            return "pending"
        if "revoked" in values or "rejected" in values:
            return "revoked"
        return "unlinked"
