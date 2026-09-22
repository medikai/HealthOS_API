import unittest
import uuid as uuid_pkg
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from fastapi import HTTPException

from src.app.api.v1.masters import (
    FALLBACK_DESIGNATIONS,
    FALLBACK_SPECIALTIES,
    FALLBACK_SUB_SPECIALTIES,
    create_city,
    create_specialty,
    create_staff_designation,
    create_sub_specialty,
    get_medicine,
    get_specialty,
    list_medical_councils,
    list_prescription_options,
    list_specialties,
    list_staff_designations,
    list_sub_specialties,
    normalize_city_name,
    search_medicines,
    update_medicine,
)
from src.app.api.v1.scheduling import (
    create_practitioner,
    practitioners,
    update_practitioner,
)
from src.app.domains.organization.service import access_service
from src.app.models.care import PractitionerAvailabilityRule
from src.app.models.organization import FacilitySchedule
from src.app.schemas.masters import (
    CityCreate,
    MedicineUpdate,
    PractitionerCreate,
    PractitionerUpdate,
    SpecialtyCreate,
    StaffDesignationCreate,
    SubSpecialtyCreate,
)
from src.scripts.import_medicines import normalize_record


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _MockDB:
    def __init__(self, rows=(), staff_org=None, entities=None, schedules=()):
        self.rows = rows
        self.staff_org = staff_org
        self.entities = entities or {}
        self.schedules = schedules
        self.added = []
        self.committed = False
        self.refreshed = False

    async def execute(self, query):
        if self.staff_org and "staff_member" in str(query).lower():
            return _Result([self.staff_org])
        return _Result(self.rows)

    async def scalars(self, query):
        if query.column_descriptions[0].get("entity") is FacilitySchedule:
            return _Result(self.schedules)
        return _Result(self.rows)

    async def scalar(self, query):
        if self.rows:
            return self.rows[0]
        return None

    async def get(self, entity, ident):
        return self.entities.get(ident)

    def add(self, obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid_pkg.uuid4()
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        self.refreshed = True


class MasterEndpointsUnitTests(unittest.IsolatedAsyncioTestCase):
    def test_medicine_import_normalizes_source_record(self):
        record = normalize_record({
            "id": "1", "name": "Augmentin 625 Duo Tablet", "price(₹)": "223.42",
            "Is_discontinued": "FALSE", "manufacturer_name": "GSK",
            "type": "allopathy", "pack_size_label": "strip of 10 tablets",
            "short_composition1": "Amoxycillin (500mg)",
            "short_composition2": "Clavulanic Acid (125mg)",
        }, 2)
        self.assertEqual(record["price"], Decimal("223.42"))
        self.assertFalse(record["is_discontinued"])
        self.assertEqual(record["source_id"], "1")

    async def test_medicine_search_returns_prescription_fields(self):
        row = SimpleNamespace(
            id=UUID(int=77), name="Augmentin 625 Duo Tablet",
            composition1="Amoxycillin (500mg)", composition2="Clavulanic Acid (125mg)",
            manufacturer_name="GSK", medicine_type="allopathy",
            pack_size_label="strip of 10 tablets", price=Decimal("223.42"),
            source="indian_medicine_dataset", source_id="1",
            is_discontinued=False, is_active=True,
        )
        result = await search_medicines(
            q="aug", db=_MockDB([row]), _account=SimpleNamespace(id=UUID(int=1)), limit=20,
        )
        self.assertEqual(result["data"]["items"][0]["uuid"], str(row.id))
        self.assertIn("Clavulanic", result["data"]["items"][0]["generic_name"])

    async def test_get_and_admin_update_medicine(self):
        medicine_id = UUID(int=78)
        row = SimpleNamespace(
            id=medicine_id, name="Old name", composition1=None, composition2=None,
            manufacturer_name="Maker", medicine_type="allopathy", pack_size_label=None,
            price=None, source="manual", source_id=None, is_discontinued=False,
            is_active=True, updated_at=None,
        )
        account = SimpleNamespace(id=UUID(int=1))
        db = _MockDB(entities={medicine_id: row})
        detail = await get_medicine(medicine_id=medicine_id, db=db, _account=account)
        self.assertEqual(detail["data"]["uuid"], str(medicine_id))

        db.scalar = AsyncMock(side_effect=[UUID(int=2), None])
        updated = await update_medicine(
            medicine_id=medicine_id,
            payload=MedicineUpdate(name="  New   name ", is_discontinued=True),
            db=db,
            account=account,
        )
        self.assertEqual(updated["data"]["name"], "New name")
        self.assertTrue(updated["data"]["is_discontinued"])
        self.assertTrue(db.committed)

    async def test_prescription_options_are_grouped_for_dropdowns(self):
        rows = [
            SimpleNamespace(
                id=UUID(int=80), country_code="IN", category="frequency", code="od",
                label="Once daily (OD)", value="Once daily", description="OD",
                sort_order=10, is_active=True,
            ),
            SimpleNamespace(
                id=UUID(int=81), country_code="IN", category="route", code="oral",
                label="Oral", value="Oral", description=None, sort_order=10, is_active=True,
            ),
        ]
        result = await list_prescription_options(
            db=_MockDB(rows), _account=SimpleNamespace(id=UUID(int=1)),
            category=None, country_code="in",
        )
        self.assertEqual(result["data"]["groups"]["frequency"][0]["value"], "Once daily")
        self.assertEqual(result["data"]["groups"]["route"][0]["code"], "oral")

    def test_city_name_normalization(self):
        self.assertEqual(normalize_city_name("  Navi   MUMBAI "), "navi mumbai")

    async def test_authenticated_user_can_add_missing_city(self):
        state_id = UUID("01955b20-4444-7000-8000-000000000001")
        db = _MockDB([], entities={state_id: SimpleNamespace(id=state_id, is_active=True)})
        res = await create_city(
            payload=CityCreate(state_id=state_id, name="  Navi   Mumbai "),
            db=db,
            _account=SimpleNamespace(id=UUID(int=1)),
        )
        self.assertEqual(res["data"]["name"], "Navi Mumbai")
        self.assertTrue(res["data"]["is_user_added"])
        self.assertTrue(db.committed)

    async def test_facility_location_must_follow_country_state_city_hierarchy(self):
        country_id, state_id, district_id, city_id = UUID(int=11), UUID(int=12), UUID(int=13), UUID(int=14)
        db = _MockDB(entities={
            country_id: SimpleNamespace(id=country_id, is_active=True),
            state_id: SimpleNamespace(id=state_id, country_id=country_id, is_active=True),
            district_id: SimpleNamespace(id=district_id, state_id=state_id, is_active=True),
            city_id: SimpleNamespace(id=city_id, state_id=state_id, district_id=district_id, is_active=True),
        })
        await access_service._validate_location(db, country_id, state_id, district_id, city_id)
        with self.assertRaises(HTTPException):
            await access_service._validate_location(db, country_id, state_id, UUID(int=99), city_id)

    async def test_list_medical_councils_fallback(self):
        res = await list_medical_councils(db=_MockDB([]), is_active=True)
        self.assertEqual(res["meta"]["count"], 30)
        self.assertIn("mmc", {item["code"] for item in res["data"]["items"]})

    async def test_list_specialties_fallback(self):
        db = _MockDB([])
        res = await list_specialties(db=db, is_active=True)
        self.assertTrue(res["success"])
        self.assertGreaterEqual(len(res["data"]["items"]), len(FALLBACK_SPECIALTIES))
        codes = [item["code"] for item in res["data"]["items"]]
        self.assertIn("general_practice", codes)
        self.assertIn("cardiology", codes)

    async def test_list_specialties_from_db(self):
        mock_spec = SimpleNamespace(
            id=UUID("01955b20-1111-7000-8000-000000000001"),
            code="cardiology",
            name="Cardiology",
            description="Heart and vascular care",
            is_active=True,
        )
        db = _MockDB([mock_spec])
        res = await list_specialties(db=db, is_active=True)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)
        self.assertEqual(res["data"]["items"][0]["code"], "cardiology")
        self.assertEqual(res["data"]["items"][0]["name"], "Cardiology")

    async def test_create_specialty(self):
        db = _MockDB([])
        payload = SpecialtyCreate(
            code="neurology",
            name="Neurology",
            description="Brain and nerve care",
            is_active=True,
        )
        res = await create_specialty(payload=payload, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["code"], "neurology")
        self.assertEqual(res["data"]["name"], "Neurology")
        self.assertTrue(db.committed)

    async def test_list_sub_specialties_fallback(self):
        db = _MockDB([])
        res = await list_sub_specialties(db=db, is_active=True)
        self.assertTrue(res["success"])
        self.assertGreaterEqual(len(res["data"]["items"]), len(FALLBACK_SUB_SPECIALTIES))

    async def test_create_sub_specialty(self):
        spec_id = UUID("01955b20-1111-7000-8000-000000000001")
        mock_parent = SimpleNamespace(id=spec_id, name="Cardiology")
        db = _MockDB([], entities={spec_id: mock_parent})
        payload = SubSpecialtyCreate(
            specialty_id=spec_id,
            code="interventional_cardio",
            name="Interventional Cardiology",
            description="Catheter-based therapies",
        )
        res = await create_sub_specialty(payload=payload, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["code"], "interventional_cardio")
        self.assertEqual(res["data"]["specialty_id"], str(spec_id))
        self.assertTrue(db.committed)

    async def test_list_staff_designations_fallback(self):
        db = _MockDB([])
        res = await list_staff_designations(db=db, is_active=True)
        self.assertTrue(res["success"])
        self.assertGreaterEqual(len(res["data"]["items"]), len(FALLBACK_DESIGNATIONS))
        codes = [item["code"] for item in res["data"]["items"]]
        self.assertIn("consultant_physician", codes)
        self.assertIn("senior_gynecologist", codes)

    async def test_create_staff_designation(self):
        db = _MockDB([])
        payload = StaffDesignationCreate(
            code="head_of_surgery",
            name="Head of Surgery",
            category="clinical",
            description="Chief surgical consultant",
        )
        res = await create_staff_designation(payload=payload, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["code"], "head_of_surgery")
        self.assertEqual(res["data"]["category"], "clinical")
        self.assertTrue(db.committed)

    async def test_get_specialty_fallback(self):
        spec_uuid = UUID("01a0b95d-0001-7000-8000-000000000001")
        db = _MockDB([])
        res = await get_specialty(specialty_uuid=spec_uuid, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["uuid"], str(spec_uuid))
        self.assertIn("sub_specialties", res["data"])


class PractitionerMastersMappingTests(unittest.IsolatedAsyncioTestCase):
    account = SimpleNamespace(
        id=UUID(int=1),
        organization_id=UUID(int=10),
    )

    async def test_practitioners_list_returns_master_fields(self):
        spec_id = UUID("01955b20-1111-7000-8000-000000000001")
        sub_spec_id = UUID("01955b20-2222-7000-8000-000000000001")
        desig_id = UUID("01955b20-3333-7000-8000-000000000001")

        mock_p = SimpleNamespace(
            id=UUID(int=100),
            organization_id=self.account.organization_id,
            person_name="Dr. Nitin Shukla",
            specialty="Cardiology",
            specialty_id=spec_id,
            sub_specialty_id=sub_spec_id,
            designation_id=desig_id,
            medical_council_reg_no="MCI-2014-987654",
            prescription_authority_status="ACTIVE",
            has_prescription_authority=True,
            user_account_id=UUID(int=1),
            is_active=True,
        )
        spec_name = "Cardiology"
        sub_spec_name = "Interventional Cardiology"
        desig_name = "Chief Medical Officer"

        # Row tuple simulating joined query: (Practitioner, Specialty.name, SubSpecialty.name, StaffDesignation.name)
        row = (mock_p, spec_name, sub_spec_name, desig_name)
        db = _MockDB([row])

        res = await practitioners(account=self.account, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(len(res["data"]["items"]), 1)

        item = res["data"]["items"][0]
        self.assertEqual(item["name"], "Dr. Nitin Shukla")
        self.assertEqual(item["specialty_id"], str(spec_id))
        self.assertEqual(item["specialty_name"], "Cardiology")
        self.assertEqual(item["sub_specialty_id"], str(sub_spec_id))
        self.assertEqual(item["sub_specialty_name"], "Interventional Cardiology")
        self.assertEqual(item["designation_id"], str(desig_id))
        self.assertEqual(item["designation_name"], "Chief Medical Officer")
        self.assertEqual(item["medical_council_reg_no"], "MCI-2014-987654")
        self.assertEqual(item["prescription_authority_status"], "ACTIVE")
        self.assertTrue(item["has_prescription_authority"])

    async def test_create_practitioner_with_masters(self):
        spec_id = UUID("01955b20-1111-7000-8000-000000000001")
        sub_spec_id = UUID("01955b20-2222-7000-8000-000000000001")
        desig_id = UUID("01955b20-3333-7000-8000-000000000001")

        mock_spec = SimpleNamespace(code="cardio", name="Cardiology")
        mock_sub = SimpleNamespace(code="interventional_cardio", name="Interventional Cardiology")
        mock_desig = SimpleNamespace(code="cmo", name="Chief Medical Officer")
        mock_staff = SimpleNamespace(id=UUID(int=50), organization_id=self.account.organization_id)
        mock_org = SimpleNamespace(id=self.account.organization_id, is_active=True)

        entities = {
            spec_id: mock_spec,
            sub_spec_id: mock_sub,
            desig_id: mock_desig,
        }

        db = _MockDB([], staff_org=(mock_staff, mock_org), entities=entities)

        payload = PractitionerCreate(
            person_name="Dr. Anita Roy",
            specialty="Cardiology",
            specialty_id=spec_id,
            sub_specialty_id=sub_spec_id,
            designation_id=desig_id,
            medical_council_reg_no="WB-MC-8877",
            prescription_authority_status="ACTIVE",
            has_prescription_authority=True,
        )

        res = await create_practitioner(payload=payload, account=self.account, db=db)
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["name"], "Dr. Anita Roy")
        self.assertEqual(res["data"]["specialty_id"], str(spec_id))
        self.assertEqual(res["data"]["sub_specialty_id"], str(sub_spec_id))
        self.assertEqual(res["data"]["designation_id"], str(desig_id))
        self.assertEqual(res["data"]["medical_council_reg_no"], "WB-MC-8877")
        self.assertTrue(res["data"]["has_prescription_authority"])
        self.assertTrue(db.committed)

    async def test_create_practitioner_copies_facility_hours(self):
        facility_id = UUID(int=60)
        schedule = SimpleNamespace(
            facility_id=facility_id,
            timezone="Asia/Kolkata",
            days_of_week='["monday", "tuesday"]',
            operating_start="08:00",
            operating_end="20:00",
            slot_interval_minutes=30,
        )
        mock_staff = SimpleNamespace(id=UUID(int=50), organization_id=self.account.organization_id)
        mock_org = SimpleNamespace(id=self.account.organization_id, is_active=True)
        db = _MockDB(staff_org=(mock_staff, mock_org), schedules=[schedule])

        await create_practitioner(
            payload=PractitionerCreate(person_name="Dr. New", facility_ids=[facility_id]),
            account=self.account,
            db=db,
        )

        rules = [value for value in db.added if isinstance(value, PractitionerAvailabilityRule)]
        self.assertEqual([rule.weekday for rule in rules], [0, 1])
        self.assertTrue(all(rule.facility_id == facility_id for rule in rules))

    async def test_update_practitioner_with_masters(self):
        prac_id = UUID("01955b20-4444-7000-8000-000000000001")
        spec_id = UUID("01955b20-1111-7000-8000-000000000001")

        existing_p = SimpleNamespace(
            id=prac_id,
            organization_id=self.account.organization_id,
            person_name="Dr. Nitin Shukla",
            specialty="General Practice",
            specialty_id=None,
            sub_specialty_id=None,
            designation_id=None,
            medical_council_reg_no=None,
            prescription_authority_status="authorized",
            has_prescription_authority=True,
        )

        mock_spec = SimpleNamespace(code="cardio", name="Cardiology")
        mock_staff = SimpleNamespace(id=UUID(int=50), organization_id=self.account.organization_id)
        mock_org = SimpleNamespace(id=self.account.organization_id, is_active=True)

        entities = {
            prac_id: existing_p,
            spec_id: mock_spec,
        }

        db = _MockDB([existing_p], staff_org=(mock_staff, mock_org), entities=entities)

        update_payload = PractitionerUpdate(
            specialty_id=spec_id,
            medical_council_reg_no="MCI-UPDATED-12345",
            prescription_authority_status="RESTRICTED",
            has_prescription_authority=False,
        )

        res = await update_practitioner(
            practitioner_uuid=prac_id,
            payload=update_payload,
            account=self.account,
            db=db,
        )
        self.assertTrue(res["success"])
        self.assertEqual(res["data"]["specialty_id"], str(spec_id))
        self.assertEqual(res["data"]["specialty_name"], "Cardiology")
        self.assertEqual(res["data"]["medical_council_reg_no"], "MCI-UPDATED-12345")
        self.assertEqual(res["data"]["prescription_authority_status"], "RESTRICTED")
        self.assertFalse(res["data"]["has_prescription_authority"])
        self.assertTrue(db.committed)
