"""Download and idempotently import the MIT Indian Medicine Dataset.

Validate source only:
    python src/scripts/import_medicines.py

Import/update all records:
    python src/scripts/import_medicines.py --apply

Source: https://github.com/junioralive/Indian-Medicine-Dataset (MIT, 2024 JuniorAlive)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.core.db.database import local_session
from app.models.masters import Medicine

DEFAULT_URL = "https://raw.githubusercontent.com/junioralive/Indian-Medicine-Dataset/main/DATA/indian_medicine_data.csv"
SOURCE = "indian_medicine_dataset"
REQUIRED = {
    "id", "name", "price(₹)", "Is_discontinued", "manufacturer_name",
    "type", "pack_size_label", "short_composition1", "short_composition2",
}


def _text(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def normalize_record(row: dict[str, str], line: int) -> dict[str, object]:
    source_id, name = _text(row.get("id")), _text(row.get("name"))
    if not source_id or not name:
        raise ValueError(f"line {line}: id and name are required")
    raw_price = _text(row.get("price(₹)"))
    try:
        price = Decimal(raw_price) if raw_price and raw_price.casefold() != "nan" else None
    except InvalidOperation:
        raise ValueError(f"line {line}: invalid price {raw_price!r}") from None
    discontinued = (row.get("Is_discontinued") or "").strip().casefold()
    if discontinued not in {"true", "false"}:
        raise ValueError(f"line {line}: Is_discontinued must be TRUE or FALSE")
    if price is not None and price < 0:
        raise ValueError(f"line {line}: price cannot be negative")
    record = {
        "id": uuid5(NAMESPACE_URL, f"healthos:medicine:{SOURCE}:{source_id}"),
        "source": SOURCE,
        "source_id": source_id,
        "name": name,
        "price": price,
        "is_discontinued": discontinued == "true",
        "manufacturer_name": _text(row.get("manufacturer_name")),
        "medicine_type": _text(row.get("type")),
        "pack_size_label": _text(row.get("pack_size_label")),
        "composition1": _text(row.get("short_composition1")),
        "composition2": _text(row.get("short_composition2")),
        "is_active": True,
    }
    for field, limit in {
        "source_id": 128, "name": 500, "manufacturer_name": 500,
        "medicine_type": 64, "pack_size_label": 255,
        "composition1": 500, "composition2": 500,
    }.items():
        if record[field] is not None and len(str(record[field])) > limit:
            raise ValueError(f"line {line}: {field} exceeds {limit} characters")
    return record


def source_rows(url: str):
    request = Request(url, headers={"User-Agent": "HealthOS medicine importer"})
    with urlopen(request, timeout=60) as response, io.TextIOWrapper(response, encoding="utf-8-sig", newline="") as text:
        reader = csv.DictReader(text)
        missing = REQUIRED - set(reader.fieldnames or ())
        if missing:
            raise SystemExit(f"Source is missing columns: {', '.join(sorted(missing))}")
        for line, row in enumerate(reader, 2):
            yield normalize_record(row, line)


async def run(url: str, batch_size: int, apply: bool) -> None:
    total = 0
    if not apply:
        for _ in source_rows(url):
            total += 1
        print(f"Validated {total} medicine records; rerun with --apply to import")
        return

    async with local_session() as db:
        batch: dict[str, dict[str, object]] = {}
        for record in source_rows(url):
            batch[str(record["source_id"])] = record
            total += 1
            if len(batch) < batch_size:
                continue
            await _upsert(db, list(batch.values()))
            # ponytail: commit per batch permits partial progress; deterministic upserts make reruns the recovery path.
            await db.commit()
            print(f"Imported {total} records")
            batch.clear()
        if batch:
            await _upsert(db, list(batch.values()))
            await db.commit()
    print(f"Medicine import complete: {total} source records")


async def _upsert(db, records: list[dict[str, object]]) -> None:
    statement = insert(Medicine).values(records)
    excluded = statement.excluded
    await db.execute(statement.on_conflict_do_update(
        constraint="uq_platform_medicine_source_id",
        set_={
            "name": excluded.name,
            "price": excluded.price,
            "is_discontinued": excluded.is_discontinued,
            "manufacturer_name": excluded.manufacturer_name,
            "medicine_type": excluded.medicine_type,
            "pack_size_label": excluded.pack_size_label,
            "composition1": excluded.composition1,
            "composition2": excluded.composition2,
            "is_active": True,
            "updated_at": func.now(),
        },
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be positive")
    asyncio.run(run(args.url, args.batch_size, args.apply))


if __name__ == "__main__":
    main()
