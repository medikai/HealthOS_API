from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from ...models.care import AuditLog


async def record_audit(db: AsyncSession, *, organization_id: UUID, actor_user_id: UUID | None, action: str, resource_type: str, resource_id: UUID | None = None, facility_id: UUID | None = None, patient_id: UUID | None = None) -> None:
    db.add(AuditLog(organization_id=organization_id, actor_user_id=actor_user_id, action=action, resource_type=resource_type, resource_id=str(resource_id) if resource_id else None, facility_id=facility_id, patient_id=str(patient_id) if patient_id else None))
