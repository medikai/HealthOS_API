"""Idempotently import country/state/city masters from a normalized CSV.

Required headers: country_code,country_name,state_code,state_name,city_name
Optional headers: district_code,district_name,external_code

Validate only:
    python src/scripts/import_locations.py locations.csv
Import:
    python src/scripts/import_locations.py locations.csv --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects.postgresql import insert

from app.api.v1.masters import normalize_city_name
from app.core.db.database import local_session
from app.models.masters import City, Country, District, State

REQUIRED = {"country_code", "country_name", "state_code", "state_name", "city_name"}


def stable_id(kind: str, *values: str):
    return uuid5(NAMESPACE_URL, ":".join(("healthos", kind, *values)))


def load(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise SystemExit(f"Missing CSV headers: {', '.join(sorted(missing))}")
        rows = []
        seen = set()
        for line, raw in enumerate(reader, 2):
            row = {key: (value or "").strip() for key, value in raw.items()}
            if any(not row[field] for field in REQUIRED):
                raise SystemExit(f"Line {line}: required value is blank")
            row["country_code"] = row["country_code"].upper()
            row["state_code"] = row["state_code"].upper()
            if len(row["country_code"]) != 2:
                raise SystemExit(f"Line {line}: country_code must be ISO alpha-2")
            if bool(row.get("district_code")) != bool(row.get("district_name")):
                raise SystemExit(f"Line {line}: district_code and district_name must be provided together")
            key = row["country_code"], row["state_code"], row.get("district_code", ""), normalize_city_name(row["city_name"])
            if key not in seen:
                seen.add(key)
                rows.append(row)
    if not rows:
        raise SystemExit("CSV contains no location rows")
    return rows


async def import_rows(rows: list[dict[str, str]]) -> None:
    countries = {(row["country_code"], row["country_name"]) for row in rows}
    states = {(row["country_code"], row["state_code"], row["state_name"]) for row in rows}
    districts = {
        (row["country_code"], row["state_code"], row["district_code"], row["district_name"])
        for row in rows if row.get("district_code")
    }
    async with local_session() as db:
        for code, name in countries:
            statement = insert(Country).values(id=stable_id("country", code), code=code, name=name, is_active=True)
            await db.execute(statement.on_conflict_do_update(index_elements=[Country.code], set_={"name": name, "is_active": True}))
        for country_code, code, name in states:
            country_id = stable_id("country", country_code)
            statement = insert(State).values(
                id=stable_id("state", country_code, code), country_id=country_id,
                code=code, name=name, external_code=f"{country_code}-{code}", is_active=True,
            )
            await db.execute(statement.on_conflict_do_update(
                constraint="uq_platform_state_country_code",
                set_={"name": name, "external_code": f"{country_code}-{code}", "is_active": True},
            ))
        for country_code, state_code, code, name in districts:
            statement = insert(District).values(
                id=stable_id("district", country_code, state_code, code),
                state_id=stable_id("state", country_code, state_code),
                code=code, name=name, is_active=True,
            )
            await db.execute(statement.on_conflict_do_update(
                constraint="uq_platform_district_state_code",
                set_={"name": name, "is_active": True},
            ))
        for row in rows:
            normalized = normalize_city_name(row["city_name"])
            state_id = stable_id("state", row["country_code"], row["state_code"])
            district_code = row.get("district_code") or ""
            district_id = stable_id("district", row["country_code"], row["state_code"], district_code) if district_code else None
            city_id_parts = [row["country_code"], row["state_code"]]
            if district_code:
                city_id_parts.append(district_code)
            city_id_parts.append(normalized)
            statement = insert(City).values(
                id=stable_id("city", *city_id_parts),
                state_id=state_id, district_id=district_id, name=row["city_name"], normalized_name=normalized,
                external_code=row.get("external_code") or None, is_user_added=False, is_active=True,
            )
            await db.execute(statement.on_conflict_do_update(
                index_elements=[City.id],
                set_={"district_id": district_id, "name": row["city_name"], "external_code": row.get("external_code") or None, "is_user_added": False, "is_active": True},
            ))
        await db.commit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    rows = load(args.csv_path)
    print(f"Validated {len(rows)} unique cities")
    if args.apply:
        asyncio.run(import_rows(rows))
        print("Location masters imported")


if __name__ == "__main__":
    main()
