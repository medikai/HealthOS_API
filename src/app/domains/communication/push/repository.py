"""SQL for push device bindings."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from ....models.communication import PushDevice


class PushRepository:
    async def get(self, db: AsyncSession, device_id: UUID) -> PushDevice | None:
        return await db.get(PushDevice, device_id)

    async def get_by_token(
        self, db: AsyncSession, token: str, *, for_update: bool = False
    ) -> PushDevice | None:
        stmt = select(PushDevice).where(PushDevice.token == token)
        if for_update:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt)

    async def get_by_installation(
        self,
        db: AsyncSession,
        *,
        organization_id: UUID,
        staff_member_id: UUID,
        installation_id: str,
        for_update: bool = False,
    ) -> PushDevice | None:
        stmt = select(PushDevice).where(
            PushDevice.organization_id == organization_id,
            PushDevice.staff_member_id == staff_member_id,
            PushDevice.installation_id == installation_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return await db.scalar(stmt)

    async def list_for_staff(
        self, db: AsyncSession, staff_member_id: UUID, *, active_only: bool = True
    ) -> list[PushDevice]:
        stmt = select(PushDevice).where(PushDevice.staff_member_id == staff_member_id)
        if active_only:
            stmt = stmt.where(PushDevice.is_active.is_(True))
        return list((await db.scalars(stmt.order_by(PushDevice.created_at))).all())

    async def active_devices_for_staff_ids(
        self, db: AsyncSession, staff_member_ids: list[UUID]
    ) -> list[PushDevice]:
        if not staff_member_ids:
            return []
        stmt = select(PushDevice).where(
            PushDevice.staff_member_id.in_(staff_member_ids),
            PushDevice.is_active.is_(True),
            PushDevice.revoked_at.is_(None),
        )
        return list((await db.scalars(stmt)).all())

    async def revoke(
        self, db: AsyncSession, device: PushDevice, *, reason: str, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(UTC)
        device.is_active = False
        device.revoked_at = now
        device.revoked_reason = reason
        device.updated_at = now
        await db.flush()

    async def revoke_for_account(
        self,
        db: AsyncSession,
        user_account_id: UUID,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        result = await db.execute(
            update(PushDevice)
            .where(
                PushDevice.user_account_id == user_account_id,
                PushDevice.is_active.is_(True),
            )
            .values(is_active=False, revoked_at=now, revoked_reason=reason, updated_at=now)
        )
        return int(result.rowcount or 0)

    async def create(self, db: AsyncSession, **values) -> PushDevice:
        values.setdefault("id", uuid7())
        device = PushDevice(**values)
        db.add(device)
        await db.flush()
        return device
