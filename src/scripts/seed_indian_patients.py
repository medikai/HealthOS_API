"""Seed 100 Indian demo patients for New Light Care Medical Center.

Run without ``--apply`` to validate the dataset, or with ``--apply`` to write it.
The stable UUIDs and MRNs make repeated runs safe.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.db.database import local_session
from app.models.identity import Patient, Person
from app.models.organization import Facility, Organization

ORGANIZATION_ID = uuid.UUID("01a0a022-b2aa-7509-aecb-3166583df954")
FACILITY_ID = uuid.UUID("01a0a022-b2c1-76e3-a91d-49f5568eca0d")
NAMESPACE = uuid.UUID("793cc4e7-5f18-49f8-b7a5-a65c264f5ff1")

# Broad regional mix, kept explicit so demo data stays stable and reviewable.
NAMES = [
    ("Aarav", "Sharma", "male"),
    ("Aadhya", "Patel", "female"),
    ("Vivaan", "Reddy", "male"),
    ("Ananya", "Iyer", "female"),
    ("Aditya", "Deshmukh", "male"),
    ("Diya", "Nair", "female"),
    ("Arjun", "Mehta", "male"),
    ("Ishita", "Banerjee", "female"),
    ("Reyansh", "Kulkarni", "male"),
    ("Meera", "Joshi", "female"),
    ("Kabir", "Malhotra", "male"),
    ("Saanvi", "Pillai", "female"),
    ("Rohan", "Chatterjee", "male"),
    ("Kavya", "Shetty", "female"),
    ("Vihaan", "Gupta", "male"),
    ("Nisha", "Menon", "female"),
    ("Dhruv", "Agarwal", "male"),
    ("Pooja", "Jadhav", "female"),
    ("Karan", "Bhat", "male"),
    ("Riya", "Chauhan", "female"),
    ("Siddharth", "Rao", "male"),
    ("Sneha", "Naidu", "female"),
    ("Rahul", "Verma", "male"),
    ("Tanvi", "Gokhale", "female"),
    ("Nikhil", "Saxena", "male"),
    ("Aditi", "Mishra", "female"),
    ("Manish", "Yadav", "male"),
    ("Priya", "Kumari", "female"),
    ("Akash", "Thakur", "male"),
    ("Neha", "Kapoor", "female"),
    ("Varun", "Bose", "male"),
    ("Shreya", "Srinivasan", "female"),
    ("Abhishek", "Tiwari", "male"),
    ("Nandini", "Acharya", "female"),
    ("Pranav", "Mane", "male"),
    ("Swati", "Kamble", "female"),
    ("Harsh", "Bhatt", "male"),
    ("Divya", "Prasad", "female"),
    ("Sameer", "Qureshi", "male"),
    ("Zoya", "Khan", "female"),
    ("Imran", "Shaikh", "male"),
    ("Ayesha", "Siddiqui", "female"),
    ("Gurpreet", "Singh", "male"),
    ("Simran", "Kaur", "female"),
    ("Jaspreet", "Gill", "male"),
    ("Harleen", "Sandhu", "female"),
    ("Tenzin", "Dorjee", "male"),
    ("Sonam", "Bhutia", "female"),
    ("Lalrin", "Pachuau", "male"),
    ("Malsawmi", "Chhangte", "female"),
    ("Anirban", "Das", "male"),
    ("Moumita", "Saha", "female"),
    ("Sourav", "Dutta", "male"),
    ("Rituparna", "Ghosh", "female"),
    ("Debojit", "Borah", "male"),
    ("Junali", "Gogoi", "female"),
    ("Ningthoujam", "Meitei", "male"),
    ("Thoibi", "Devi", "female"),
    ("Roshan", "Lakra", "male"),
    ("Anjali", "Toppo", "female"),
    ("Suresh", "Munda", "male"),
    ("Sunita", "Tirkey", "female"),
    ("Ganesh", "Pawar", "male"),
    ("Madhuri", "Shinde", "female"),
    ("Sachin", "More", "male"),
    ("Vaishali", "Gaikwad", "female"),
    ("Karthik", "Subramanian", "male"),
    ("Lakshmi", "Krishnan", "female"),
    ("Aravind", "Raman", "male"),
    ("Keerthana", "Murugan", "female"),
    ("Venkatesh", "Nayak", "male"),
    ("Deepa", "Hegde", "female"),
    ("Mahesh", "Gowda", "male"),
    ("Rashmi", "Pai", "female"),
    ("Sandeep", "Kumar", "male"),
    ("Bhavana", "Raj", "female"),
    ("Rakesh", "Soni", "male"),
    ("Komal", "Solanki", "female"),
    ("Mohit", "Choudhary", "male"),
    ("Payal", "Jain", "female"),
    ("Hemant", "Parmar", "male"),
    ("Krupa", "Trivedi", "female"),
    ("Ashwin", "Vyas", "male"),
    ("Hetal", "Dave", "female"),
    ("Pradeep", "Mohanty", "male"),
    ("Suchitra", "Patnaik", "female"),
    ("Alok", "Tripathi", "male"),
    ("Garima", "Srivastava", "female"),
    ("Mukul", "Chandra", "male"),
    ("Preeti", "Rawat", "female"),
    ("Naveen", "Negi", "male"),
    ("Jyoti", "Bisht", "female"),
    ("Faizan", "Ansari", "male"),
    ("Sana", "Mirza", "female"),
    ("Joel", "D'Souza", "male"),
    ("Maria", "Fernandes", "female"),
    ("Thomas", "Varghese", "male"),
    ("Anu", "Mathew", "female"),
    ("Ritwik", "Sen", "male"),
    ("Leena", "George", "female"),
]


def records() -> list[dict[str, object]]:
    result = []
    for number, (first_name, last_name, gender) in enumerate(NAMES, 1):
        key = f"indian-patient-{number:03d}"
        result.append(
            {
                "patient_id": uuid.uuid5(NAMESPACE, f"patient:{key}"),
                "person_id": uuid.uuid5(NAMESPACE, f"person:{key}"),
                "first_name": first_name,
                "last_name": last_name,
                "gender": gender,
                "date_of_birth": date(
                    1948 + (number * 7) % 58,
                    1 + (number * 5) % 12,
                    1 + (number * 11) % 28,
                ).isoformat(),
                "phone": f"9{100_000_000 + number:09d}",
                "email": f"{first_name}.{last_name}.{number}@example.com".lower().replace(
                    "'", ""
                ),
                "mrn": f"MRN-IND-{number:04d}",
            }
        )
    return result


def validate(dataset: list[dict[str, object]]) -> None:
    assert len(dataset) == 100
    for field in ("patient_id", "person_id", "phone", "email", "mrn"):
        assert len({row[field] for row in dataset}) == 100, f"duplicate {field}"
    assert all(len(str(row["phone"])) == 10 for row in dataset)


async def seed() -> None:
    dataset = records()
    validate(dataset)
    async with local_session() as db:
        organization = await db.get(Organization, ORGANIZATION_ID)
        facility = await db.get(Facility, FACILITY_ID)
        if organization is None:
            raise SystemExit(f"Organization not found: {ORGANIZATION_ID}")
        if facility is None or facility.organization_id != ORGANIZATION_ID:
            raise SystemExit(f"Facility not found in organization: {FACILITY_ID}")

        inserted = 0
        for row in dataset:
            person = await db.get(Person, row["person_id"])
            if person is None:
                phone_owner = await db.scalar(
                    select(Person).where(
                        Person.organization_id == ORGANIZATION_ID,
                        Person.phone == row["phone"],
                    )
                )
                if phone_owner is not None:
                    raise SystemExit(
                        f"Phone already belongs to another person: {row['phone']}"
                    )
                person = Person(
                    organization_id=ORGANIZATION_ID,
                    first_name=str(row["first_name"]),
                    last_name=str(row["last_name"]),
                    phone=str(row["phone"]),
                    email=str(row["email"]),
                    date_of_birth=str(row["date_of_birth"]),
                    gender=str(row["gender"]),
                )
                person.id = row["person_id"]
                db.add(person)

            patient = await db.get(Patient, row["patient_id"])
            if patient is None:
                mrn_owner = await db.scalar(
                    select(Patient).where(
                        Patient.organization_id == ORGANIZATION_ID,
                        Patient.mrn == row["mrn"],
                    )
                )
                if mrn_owner is not None:
                    raise SystemExit(
                        f"MRN already belongs to another patient: {row['mrn']}"
                    )
                patient = Patient(
                    organization_id=ORGANIZATION_ID,
                    person_id=person.id,
                    mrn=str(row["mrn"]),
                )
                patient.id = row["patient_id"]
                db.add(patient)
                inserted += 1

        await db.commit()
        seeded = await db.scalar(
            select(func.count())
            .select_from(Patient)
            .where(
                Patient.organization_id == ORGANIZATION_ID,
                Patient.mrn.like("MRN-IND-%"),
            )
        )
        print(
            f"Seeded patients: {seeded}/100 ({inserted} inserted this run) for {organization.name}."
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="write the validated dataset to PostgreSQL"
    )
    args = parser.parse_args()
    validate(records())
    if args.apply:
        asyncio.run(seed())
    else:
        print("Validated 100 Indian demo patients. Re-run with --apply to insert them.")


if __name__ == "__main__":
    main()
