import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from src.app.api.v1.staff import accept_invitation
from src.app.models.care import Practitioner, PractitionerAvailabilityRule
from src.app.models.identity import UserAccount
from src.app.models.organization import (
    FacilitySchedule,
    StaffAssignment,
    StaffInvitation,
    StaffMember,
)
from src.app.schemas.staff import StaffInviteAccept


class _InvitationDb:
    def __init__(self, invitation):
        self.invitation = invitation
        self.added = []
        self.add = Mock(side_effect=self.added.append)
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def scalar(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is StaffInvitation:
            return self.invitation
        if entity is FacilitySchedule:
            return SimpleNamespace(
                timezone="Asia/Kolkata",
                days_of_week='["monday", "tuesday", "wednesday"]',
                operating_start="08:00",
                operating_end="20:00",
                slot_interval_minutes=30,
            )
        if entity in {UserAccount, StaffMember, StaffAssignment, Practitioner, PractitionerAvailabilityRule}:
            return None
        raise AssertionError(f"Unexpected query: {statement}")


class StaffInvitationTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepting_practitioner_copies_facility_hours(self):
        invitation = StaffInvitation(
            organization_id=uuid4(),
            facility_id=uuid4(),
            email="new.doctor@example.com",
            full_name="Dr New",
            role_code="practitioner",
            token="token",
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
        db = _InvitationDb(invitation)

        with (
            patch("src.app.api.v1.staff.get_password_hash", return_value="hash"),
            patch("src.app.api.v1.staff.record_audit", AsyncMock()),
            patch("src.app.api.v1.staff.create_access_token", AsyncMock(return_value="access")),
        ):
            await accept_invitation(StaffInviteAccept(token="token", password="password"), db)

        rules = [value for value in db.added if isinstance(value, PractitionerAvailabilityRule)]
        self.assertEqual([rule.weekday for rule in rules], [0, 1, 2])
        self.assertTrue(all(rule.facility_id == invitation.facility_id for rule in rules))
        self.assertTrue(all(rule.start_time.isoformat() == "08:00:00" for rule in rules))
        self.assertEqual(invitation.status, "accepted")


if __name__ == "__main__":
    unittest.main()
