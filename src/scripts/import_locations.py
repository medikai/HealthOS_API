"""Reviewable LGD geography ETL. No database write without explicit apply."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import http.client
import io
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COMPONENTS = ("states", "districts", "urban_local_bodies", "statewise_ulbs_coverage")
LISTING_URL = "https://github.com/ramSeraph/opendata/releases/download/lgd-latest/listing_files.csv"
ASSET = re.compile(r"^(states|districts|urban_local_bodies|statewise_ulbs_coverage)\.(\d{2}[A-Za-z]{3}\d{4})\.csv\.7z$")


def status(message: str) -> None:
    print(f"[geography] {message}", file=sys.stderr, flush=True)


def key(value: str) -> str:
    return " ".join(value.casefold().split())


def identifier(kind: str, *parts: str) -> UUID:
    return uuid5(NAMESPACE_URL, ":".join(("healthos", kind, *parts)))


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")
    temp.replace(path)


def request(url: str, *, accept: str = "application/vnd.github+json"):
    headers = {"Accept": accept, "User-Agent": "healthos-geography-etl"}
    for attempt in range(4):
        try:
            return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=45)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429) and exc.headers.get("X-RateLimit-Remaining") == "0":
                raise RuntimeError("GitHub download rate limit reached; use the direct file links and --offline") from exc
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise RuntimeError(f"GitHub request failed: HTTP {exc.code}, {url}") from exc
        except (TimeoutError, urllib.error.URLError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Cannot reach GitHub: {exc.reason}") from exc
        status(f"GitHub request failed; retrying ({attempt + 1}/4)")
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def discover(snapshot: str | None, listing_path: Path) -> tuple[date, dict[str, dict]]:
    status("Reading the LGD release file listing (one GitHub download)")
    with request(LISTING_URL, accept="text/csv") as response:
        listing = response.read()
    listing_path.write_bytes(listing)
    reader = csv.DictReader(io.StringIO(listing.decode("utf-8-sig")))
    if set(reader.fieldnames or ()) != {"name", "size", "url"}:
        raise ValueError(f"Unexpected LGD listing headers: {reader.fieldnames}")
    available: dict[date, dict[str, dict]] = defaultdict(dict)
    for row in reader:
        match = ASSET.fullmatch(row["name"])
        if not match:
            continue
        url = urlparse(row["url"])
        if url.hostname != "github.com" or not url.path.startswith("/ramSeraph/opendata/releases/download/") or not url.path.endswith("/" + row["name"]):
            raise ValueError(f"Unexpected LGD asset URL for {row['name']}")
        day = datetime.strptime(match.group(2), "%d%b%Y").replace(tzinfo=UTC).date()
        available[day].setdefault(match.group(1), {"name": row["name"], "size": int(row["size"]),
                                                     "browser_download_url": row["url"], "id": None, "digest": None})
    common = {day: assets for day, assets in available.items() if set(assets) == set(COMPONENTS)}
    if snapshot:
        selected = date.fromisoformat(snapshot)
        if selected not in common:
            raise ValueError(f"No complete snapshot for {selected}; newest: {max(common) if common else 'none'}")
    elif common:
        selected = max(common)
    else:
        raise ValueError("No common snapshot with all four required components")
    status(f"Selected common snapshot {selected} ({(datetime.now(UTC).date() - selected).days} days old)")
    return selected, common[selected]


def fetch(asset: dict, cache: Path) -> dict:
    target = cache / asset["name"]
    if not target.exists() or target.stat().st_size != asset["size"]:
        status(f"Downloading {asset['name']} ({asset['size'] / 1048576:.1f} MiB)")
        temp = target.with_suffix(target.suffix + ".tmp")
        for attempt in range(3):
            try:
                last_report = time.monotonic()
                with request(asset["browser_download_url"], accept="application/octet-stream") as response, temp.open("wb") as output:
                    for chunk in iter(lambda: response.read(1024 * 1024), b""):
                        output.write(chunk)
                        if time.monotonic() - last_report >= 5:
                            status(f"{asset['name']}: {output.tell() / 1048576:.1f}/{asset['size'] / 1048576:.1f} MiB")
                            last_report = time.monotonic()
                if temp.stat().st_size != asset["size"]:
                    raise ValueError(f"Size mismatch for {asset['name']}")
                temp.replace(target)
                break
            except (TimeoutError, urllib.error.URLError, http.client.IncompleteRead, ValueError):
                temp.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                status(f"Download interrupted; retrying {asset['name']} ({attempt + 1}/3)")
                time.sleep(2 ** attempt)
    else:
        status(f"Using cached {asset['name']}")
    status(f"Checking SHA-256 for {asset['name']}")
    digest = sha(target)
    if asset.get("digest") and asset["digest"] != f"sha256:{digest}":
        raise ValueError(f"Published digest mismatch for {asset['name']}")
    return {"path": str(target), "url": asset["browser_download_url"], "asset_id": asset["id"],
            "size": target.stat().st_size, "sha256": digest, "published_digest": asset.get("digest"),
            "retrieved_at": datetime.fromtimestamp(target.stat().st_mtime, UTC).isoformat()}


def extract(path: Path) -> str:
    if path.suffix == ".csv":
        return path.read_text(encoding="utf-8-sig")
    if path.suffix != ".7z":
        raise ValueError(f"Expected CSV or 7z: {path}")
    try:
        if shutil.which("bsdtar"):
            names = subprocess.run(["bsdtar", "-tf", str(path)], capture_output=True, check=True).stdout.decode().splitlines()
            if len(names) != 1 or Path(names[0]).name != path.name.removesuffix(".7z"):
                raise ValueError(f"Unexpected archive contents in {path.name}: {names}")
            command = ["bsdtar", "-xOf", str(path), names[0]]
        elif shutil.which("7z"):
            command = ["7z", "e", "-so", str(path)]
        else:
            raise RuntimeError("bsdtar or 7z is required for LGD .csv.7z assets")
        # stdout extraction avoids archive path traversal.
        result = subprocess.run(command, capture_output=True, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Archive extraction failed on {path.name}: {exc.stderr.decode(errors='replace')[-300:]}") from exc
    return result.stdout.decode("utf-8-sig")


def field(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.casefold())


def rows(path: Path, required: set[str]) -> list[dict[str, str]]:
    content = extract(path)
    for delimiter in (";", ","):
        reader = csv.reader(io.StringIO(content, newline=""), delimiter=delimiter)
        try:
            headers = [field(item) for item in next(reader)]
        except StopIteration as exc:
            raise ValueError(f"Empty source: {path}") from exc
        if required.issubset(headers) and len(headers) == len(set(headers)):
            break
    else:
        raise ValueError(f"Unexpected headers in {path.name}: {headers}")
    output = []
    for line, values in enumerate(reader, 2):
        if not any(value.strip() for value in values) or [field(v) for v in values] == headers:
            continue
        if len(values) != len(headers):
            raise ValueError(f"Malformed {path.name} record near row {line}")
        row = dict(zip(headers, (value.strip() for value in values), strict=True))
        if any(not row[name] for name in required):
            raise ValueError(f"Missing required field in {path.name} near row {line}")
        output.append(row)
    if not output:
        raise ValueError(f"No data records in {path.name}")
    return output


def normalize(files: dict[str, Path]) -> tuple[dict, dict]:
    required = {"states": {"statecode", "statenameinenglish"},
                "districts": {"districtcode", "districtnameinenglish", "statecode"},
                "urban_local_bodies": {"localbodycode", "localbodynameinenglish", "statecode"},
                "statewise_ulbs_coverage": {"districtcode"}}
    source = {}
    for name in COMPONENTS:
        status(f"Parsing {name}")
        source[name] = rows(files[name], required[name])
        status(f"Parsed {len(source[name])} {name} rows")
    states = {r["statecode"]: r["statenameinenglish"] for r in source["states"]}
    if len(states) != len(source["states"]):
        raise ValueError("Repeated LGD state code")
    districts = {}
    for r in source["districts"]:
        code = r["districtcode"]
        if code in districts or r["statecode"] not in states:
            raise ValueError(f"Duplicate district or missing state: {code}")
        districts[code] = {"code": code, "name": r["districtnameinenglish"], "state_code": r["statecode"]}
    bodies = {}
    for r in source["urban_local_bodies"]:
        code = r["localbodycode"]
        if code in bodies or r["statecode"] not in states:
            raise ValueError(f"Duplicate urban body or missing state: {code}")
        bodies[code] = {"code": code, "name": r["localbodynameinenglish"], "state_code": r["statecode"]}
    associations: dict[str, set[str]] = defaultdict(set)
    rejected = []
    state_names = {key(name): code for code, name in states.items()}
    for index, r in enumerate(source["statewise_ulbs_coverage"], 1):
        if index % 10000 == 0:
            status(f"Mapped coverage rows {index}/{len(source['statewise_ulbs_coverage'])}")
        body, district = r.get("localbody") or r.get("localbodycode", ""), r["districtcode"]
        if body not in bodies or district not in districts or bodies[body]["state_code"] != districts[district]["state_code"]:
            rejected.append({"body": body, "district": district, "reason": "unknown or cross-state identity"})
            continue
        coverage_state = r.get("statename") or r.get("statenameinenglish")
        if coverage_state and state_names.get(key(coverage_state)) != bodies[body]["state_code"]:
            rejected.append({"body": body, "district": district, "reason": "coverage state mismatch"})
            continue
        associations[body].add(district)
    cities = [{"code": body, "name": info["name"], "state_code": info["state_code"], "district_code": district}
              for body, info in bodies.items() for district in sorted(associations[body])]
    staged = {"states": [{"code": c, "name": n} for c, n in sorted(states.items())],
              "districts": sorted(districts.values(), key=lambda x: x["code"]), "cities": cities}
    report = {"source_rows": {name: len(value) for name, value in source.items()},
              "staged_rows": {name: len(value) for name, value in staged.items()},
              "coverage_rejected": rejected, "bodies_without_district": sorted(set(bodies) - set(associations)),
              "cities_per_state": dict(Counter(c["state_code"] for c in cities)),
              "districts_per_state": dict(Counter(d["state_code"] for d in districts.values()))}
    return staged, report


def prepare(args) -> None:
    status("Preparing India geography source files")
    target = args.output.resolve()
    target.mkdir(parents=True, exist_ok=True)
    if args.offline:
        status(f"Using offline files from {args.offline}")
        dated: dict[date, dict[str, Path]] = defaultdict(dict)
        for path in args.offline.iterdir():
            match = ASSET.fullmatch(path.name)
            if match:
                day = datetime.strptime(match.group(2), "%d%b%Y").replace(tzinfo=UTC).date()
                dated[day][match.group(1)] = path
        common = {day: paths for day, paths in dated.items() if set(paths) == set(COMPONENTS)}
        if common:
            snapshot = date.fromisoformat(args.snapshot) if args.snapshot else max(common)
            if snapshot not in common:
                raise ValueError(f"No complete offline snapshot for {snapshot}")
            source_paths = common[snapshot]
        else:
            source_paths = {name: next((path for path in (args.offline / f"{name}.csv", args.offline / f"{name}.csv.7z")
                                        if path.exists()), args.offline / f"{name}.csv") for name in COMPONENTS}
            snapshot = date.fromisoformat(args.snapshot) if args.snapshot else None
        for path in source_paths.values():
            if not path.exists():
                raise ValueError(f"Missing offline file: {path}")
        assets = {name: {"path": str(path.resolve()), "sha256": sha(path), "size": path.stat().st_size,
                         "url": None, "asset_id": None, "retrieved_at": None} for name, path in source_paths.items()}
    else:
        snapshot, selected = discover(args.snapshot, target / "listing_files.csv")
        cache = target / "cache"
        cache.mkdir(exist_ok=True)
        status("Downloading the four components concurrently")
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {name: pool.submit(fetch, asset, cache) for name, asset in selected.items()}
            assets = {name: future.result() for name, future in futures.items()}
        source_paths = {name: Path(asset["path"]) for name, asset in assets.items()}
    staged, report = normalize(source_paths)
    report["snapshot_date"] = snapshot.isoformat() if snapshot else "offline-unverified"
    report["snapshot_age_days"] = (datetime.now(UTC).date() - snapshot).days if snapshot else None
    manifest = {"source": "ramSeraph/opendata community-hosted extract of official LGD data",
                "source_url": "https://github.com/ramSeraph/opendata",
                "attribution": "Local Government Directory (LGD), India; mirror maintained by ramSeraph",
                "license_note": "Mirror repository's authored code is UNLICENSE; upstream data license not asserted",
                "snapshot_date": report["snapshot_date"],
                "assets": assets}
    write_json(target / "staged.json", staged)
    manifest["staged_sha256"] = sha(target / "staged.json")
    write_json(target / "manifest.json", manifest)
    write_json(target / "validation.json", report)
    status(f"Wrote staged data, manifest and validation to {target}")
    print(json.dumps({"snapshot": report["snapshot_date"], "age_days": report["snapshot_age_days"],
                      "rows": report["staged_rows"], "coverage_rejected": len(report["coverage_rejected"]),
                      "bodies_without_district": len(report["bodies_without_district"])}))


def load_stage(output: Path) -> tuple[dict, dict]:
    if not (output / "manifest.json").exists() or not (output / "staged.json").exists():
        raise ValueError(f"No prepared dataset in {output}; run prepare before plan, apply or verify")
    manifest = json.loads((output / "manifest.json").read_text())
    if sha(output / "staged.json") != manifest["staged_sha256"]:
        raise ValueError("Staged records changed since preparation")
    return json.loads((output / "staged.json").read_text()), manifest


def load_overrides(path: Path | None) -> dict:
    value = json.loads(path.read_text()) if path else {}
    allowed = {"states", "state_aliases", "districts", "cities", "city_source_aliases"}
    if not isinstance(value, dict) or set(value) - allowed or any(not isinstance(v, dict) for v in value.values()):
        raise ValueError("Override JSON must contain only states, state_aliases, districts, cities, city_source_aliases maps")
    if any(not isinstance(k, str) or not isinstance(v, str) for mapping in value.values() for k, v in mapping.items()):
        raise ValueError("Override map keys and values must be strings")
    return value


async def database_rows(db) -> dict:
    from sqlalchemy import select

    from app.models.masters import City, Country, District, State

    country = (await db.scalars(select(Country).where(Country.code == "IN"))).all()
    if len(country) != 1:
        raise ValueError("Expected exactly one country with code IN")
    country_id = country[0].id
    states = (await db.scalars(select(State).where(State.country_id == country_id).order_by(State.id))).all()
    status(f"Loaded {len(states)} India states")
    state_ids = {s.id for s in states}
    districts = (await db.scalars(select(District).order_by(District.id))).all()
    status(f"Loaded {len(districts)} districts")
    cities = (await db.scalars(select(City).order_by(City.id))).all()
    status(f"Loaded {len(cities)} cities")
    return {"country": {"id": str(country_id), "active": country[0].is_active},
            "states": [{"id": str(s.id), "code": s.code, "name": s.name,
                        "external_code": s.external_code, "active": s.is_active} for s in states],
            "districts": [{"id": str(d.id), "state_id": str(d.state_id), "code": d.code,
                           "name": d.name, "active": d.is_active} for d in districts if d.state_id in state_ids],
            "cities": [{"id": str(c.id), "state_id": str(c.state_id), "district_id": str(c.district_id) if c.district_id else None,
                        "name": c.name, "normalized_name": c.normalized_name, "external_code": c.external_code,
                        "is_user_added": c.is_user_added, "active": c.is_active} for c in cities if c.state_id in state_ids]}


def fingerprint(value: dict) -> str:
    ordered = {name: sorted(items, key=lambda r: r["id"]) if isinstance(items, list) else items
               for name, items in value.items()}
    return hashlib.sha256(json.dumps(ordered, sort_keys=True).encode()).hexdigest()


def make_plan(staged: dict, current: dict, overrides: dict, scope: str, *, disambiguate_cities: bool = False) -> dict:
    if not current["country"]["active"]:
        raise ValueError("IN country is inactive")
    conflicts = []
    operations = []
    states_by_name: dict[str, list[dict]] = defaultdict(list)
    states_by_id = {r["id"]: r for r in current["states"]}
    for row in current["states"]:
        states_by_name[key(row["name"])].append(row)
    state_map = {}
    for source in staged["states"]:
        code = source["code"]
        explicit = overrides.get("states", {}).get(code)
        alias = overrides.get("state_aliases", {}).get(code, source["name"])
        matches = [states_by_id[explicit]] if explicit in states_by_id else states_by_name[key(alias)]
        if explicit and explicit not in states_by_id or len(matches) != 1 or not matches[0]["active"]:
            conflicts.append({"type": "state_mapping", "source": source, "candidates": [m["id"] for m in matches]})
        else:
            state_map[code] = matches[0]["id"]
    if len(set(state_map.values())) != len(state_map):
        conflicts.append({"type": "duplicate_state_mapping"})

    existing_districts = {(d["state_id"], d["code"]): d for d in current["districts"]}
    district_ids = {}
    planned_district_names: dict[tuple[str, str], str] = {}
    for index, source in enumerate(staged["districts"], 1):
        if index % 250 == 0:
            status(f"Reconciled {index}/{len(staged['districts'])} districts")
        state_id = state_map.get(source["state_code"])
        if not state_id:
            continue
        code = f"LGD:{source['code']}"
        explicit_id = overrides.get("districts", {}).get(source["code"])
        existing = next((d for d in current["districts"] if d["id"] == explicit_id), None) if explicit_id else existing_districts.get((state_id, code))
        if explicit_id and not existing:
            conflicts.append({"type": "district_override_missing", "source": source, "id": explicit_id})
            continue
        if existing and (existing["state_id"] != state_id or not existing["active"]):
            conflicts.append({"type": "district_state_or_active", "source": source, "existing": existing})
            continue
        coded = existing_districts.get((state_id, code))
        if existing and coded and coded["id"] != existing["id"]:
            conflicts.append({"type": "district_code_collision", "source": source, "existing": coded})
            continue
        same_name = [d for d in current["districts"] if d["state_id"] == state_id and key(d["name"]) == key(source["name"])
                     and (not existing or d["id"] != existing["id"])]
        if same_name:
            conflicts.append({"type": "district_name_collision", "source": source, "existing": same_name})
            continue
        if existing and key(existing["name"]) != key(source["name"]) and not explicit_id:
            conflicts.append({"type": "district_rename", "source": source, "existing": existing})
            continue
        district_id = existing["id"] if existing else str(identifier("lgd-district", source["code"]))
        district_ids[source["code"]] = district_id
        name_key = (state_id, key(source["name"]))
        if name_key in planned_district_names and planned_district_names[name_key] != source["code"]:
            conflicts.append({"type": "source_district_name_collision", "source": source})
        planned_district_names[name_key] = source["code"]
        if existing:
            continue
        operations.append({"table": "district", "action": "insert", "id": district_id, "before": None,
                           "after": {"state_id": state_id, "code": code, "name": source["name"]}})

    if scope == "full":
        city_keys: dict[tuple[str, str], str] = {}
        city_districts: dict[str, set[str]] = defaultdict(set)
        name_sources: dict[tuple[str, str], set[str]] = defaultdict(set)
        source_by_id = {f"{s['code']}:{s['district_code']}": s for s in staged["cities"]}
        source_aliases = overrides.get("city_source_aliases", {})
        resolved_source_aliases = []
        disambiguated = []
        source_names: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for source in staged["cities"]:
            identity = f"{source['code']}:{source['district_code']}"
            if identity not in source_aliases:
                source_names[source["state_code"], source["district_code"], key(source["name"])].add(identity)
        for source_id, canonical_id in source_aliases.items():
            source = source_by_id.get(source_id)
            canonical = source_by_id.get(canonical_id)
            if (not source or not canonical or source_id == canonical_id or canonical_id in source_aliases
                    or (source["state_code"], source["district_code"], key(source["name"]))
                    != (canonical["state_code"], canonical["district_code"], key(canonical["name"]))):
                conflicts.append({"type": "invalid_city_source_alias", "source": source_id, "canonical": canonical_id})
        for source in staged["cities"]:
            city_districts[source["code"]].add(source["district_code"])
            source_id = f"{source['code']}:{source['district_code']}"
            canonical_id = source_aliases.get(source_id, source_id)
            name_sources[source["state_code"], key(source["name"])].add(canonical_id)
        unchanged_cities = 0
        for index, source in enumerate(staged["cities"], 1):
            if index % 500 == 0:
                status(f"Reconciled {index}/{len(staged['cities'])} cities")
            state_id = state_map.get(source["state_code"])
            district_id = district_ids.get(source["district_code"])
            if not state_id or not district_id:
                continue
            name = source["name"]
            if disambiguate_cities and len(source_names[source["state_code"], source["district_code"], key(name)]) > 1:
                name = f"{name} (LGD {source['code']})"
                disambiguated.append({"source": f"{source['code']}:{source['district_code']}", "display_name": name})
            normalized = key(name)
            natural = (district_id, normalized)
            source_identity = f"{source['code']}:{source['district_code']}"
            if source_identity in source_aliases:
                resolved_source_aliases.append({"source": source_identity, "canonical": source_aliases[source_identity]})
                continue
            if natural in city_keys and city_keys[natural] != source_identity:
                conflicts.append({"type": "source_city_collision", "source": source, "other": city_keys[natural]})
                continue
            city_keys[natural] = source_identity
            explicit_id = overrides.get("cities", {}).get(source_identity)
            exact = [c for c in current["cities"] if c["district_id"] == district_id and c["normalized_name"] == normalized]
            by_code = [c for c in current["cities"] if c["state_id"] == state_id and c["external_code"] == f"LGD:{source['code']}"
                       and c["district_id"] == district_id]
            null_match = [c for c in current["cities"] if c["state_id"] == state_id and c["district_id"] is None
                          and c["normalized_name"] == normalized]
            if len(city_districts[source["code"]]) != 1 or len(name_sources[source["state_code"], normalized]) != 1:
                null_match = []  # A districtless city needs one supported source and district.
            chosen = [c for c in current["cities"] if c["id"] == explicit_id] if explicit_id else list({c["id"]: c for c in exact + by_code + null_match}.values())
            if len(chosen) > 1 or (explicit_id and not chosen):
                conflicts.append({"type": "city_ambiguous", "source": source, "candidates": [c["id"] for c in chosen]})
                continue
            city = chosen[0] if chosen else None
            if city and city["external_code"] and city["external_code"].startswith("LGD:") and city["external_code"] != f"LGD:{source['code']}":
                conflicts.append({"type": "city_source_identity_collision", "source": source, "existing": city})
                continue
            if city and (city["state_id"] != state_id or city["district_id"] not in (None, district_id)
                         or (city["normalized_name"] != normalized and not explicit_id) or not city["active"]):
                conflicts.append({"type": "city_identity_or_active", "source": source, "existing": city})
                continue
            if city:
                if city["district_id"] is None:
                    if exact:
                        conflicts.append({"type": "city_unique_collision", "source": source, "existing": exact})
                    else:
                        operations.append({"table": "city", "action": "update", "id": city["id"], "before": city,
                                           "after": {"district_id": district_id}})
                else:
                    unchanged_cities += 1
                continue
            if exact:
                conflicts.append({"type": "city_unique_collision", "source": source, "existing": exact})
                continue
            operations.append({"table": "city", "action": "insert",
                               "id": str(identifier("lgd-city", source["code"], source["district_code"])),
                               "before": None, "after": {"state_id": state_id, "district_id": district_id,
                                                         "name": name, "normalized_name": normalized,
                                                         "external_code": f"LGD:{source['code']}"}})
    inserted = Counter(op["table"] for op in operations if op["action"] == "insert")
    updated = Counter(op["table"] for op in operations if op["action"] == "update")
    unresolved = [c for c in current["cities"] if c["district_id"] is None]
    result = {"scope": scope, "disambiguate_cities": disambiguate_cities,
              "database_fingerprint": fingerprint(current), "state_map": state_map,
              "operations": operations, "conflicts": conflicts, "unresolved_existing_cities": unresolved,
              "resolved_source_aliases": resolved_source_aliases if scope == "full" else [],
              "disambiguated_source_cities": disambiguated if scope == "full" else [],
              "before_counts": {"states": len(current["states"]), "districts": len(current["districts"]), "cities": len(current["cities"])},
              "counts": {"inserted": dict(inserted), "updated": dict(updated),
                         "unchanged_districts": len(district_ids) - inserted["district"],
                         "skipped_districts": len(staged["districts"]) - len(district_ids),
                         "unchanged_cities": unchanged_cities if scope == "full" else 0,
                         "aliased_cities": len(resolved_source_aliases) if scope == "full" else 0,
                         "disambiguated_cities": len(disambiguated) if scope == "full" else 0,
                         "skipped_cities": len(staged["cities"]) - inserted["city"] - updated["city"] - unchanged_cities - len(resolved_source_aliases) if scope == "full" else 0}}
    return result


async def db_plan(output: Path, scope: str, override_path: Path | None, *,
                  apply: bool = False, disambiguate_cities: bool = False) -> None:
    from sqlalchemy import text

    from app.core.db.database import local_session
    from app.models.masters import City, District

    mode = "apply" if apply else "plan"
    status(f"Starting {mode} for {scope} scope")
    staged, manifest = load_stage(output)
    overrides = load_overrides(override_path)
    plan_path = output / f"plan-{scope}.json"
    status("Connecting to the configured PostgreSQL database")
    async with local_session() as db:
        async with db.begin():
            if apply:
                status("Waiting for the geography import lock")
                await db.execute(text("SELECT pg_advisory_xact_lock(610778425, 1)"))
                status("Geography import lock acquired")
            db_name = (await db.execute(text("SELECT current_database()"))).scalar_one()
            status(f"Reading existing India geography from database {db_name}")
            current = await database_rows(db)
            status(f"Read {len(current['states'])} states, {len(current['districts'])} districts and {len(current['cities'])} cities")
            status("Reconciling staged records with existing IDs")
            fresh = make_plan(staged, current, overrides, scope, disambiguate_cities=disambiguate_cities)
            fresh["snapshot_date"] = manifest["snapshot_date"]
            fresh["staged_sha256"] = manifest["staged_sha256"]
            fresh["database"] = db_name
            fresh["overrides_sha256"] = sha(override_path) if override_path else None
            if not apply:
                write_json(plan_path, fresh)
                status(f"Plan saved to {plan_path}; {len(fresh['conflicts'])} blocking conflicts")
                print(json.dumps({"plan": str(plan_path), "snapshot": fresh["snapshot_date"],
                                  "database": db_name, "counts": fresh["counts"],
                                  "conflicts": len(fresh["conflicts"]),
                                  "unresolved_existing_cities": len(fresh["unresolved_existing_cities"])}))
                return
            reviewed = json.loads(plan_path.read_text())
            if fresh != reviewed:
                raise ValueError("Plan is stale or changed; generate and review a new plan")
            if fresh["conflicts"]:
                raise ValueError(f"{len(fresh['conflicts'])} blocking conflicts; review the plan and overrides")
            status(f"Reviewed plan matches current database; {len(fresh['operations'])} changes to apply")
            recovery = output / f"recovery-{scope}.json"
            write_json(recovery, {"status": "pending", "database": db_name,
                                  "snapshot_date": fresh["snapshot_date"], "operations": fresh["operations"]})
            district_ops = [op for op in fresh["operations"] if op["table"] == "district"]
            city_ops = [op for op in fresh["operations"] if op["table"] == "city"]
            status(f"Writing {len(district_ops)} district changes")
            for index, operation in enumerate(district_ops, 1):
                values = operation["after"]
                district = District(state_id=UUID(values["state_id"]), code=values["code"], name=values["name"])
                district.id = UUID(operation["id"])
                db.add(district)
                if index % 250 == 0:
                    await db.flush()
                    status(f"Saved {index}/{len(district_ops)} districts in the open transaction")
            await db.flush()
            status(f"Writing {len(city_ops)} city changes")
            for index, operation in enumerate(city_ops, 1):
                values = operation["after"]
                if operation["action"] == "insert":
                    city = City(state_id=UUID(values["state_id"]), district_id=UUID(values["district_id"]),
                                name=values["name"], normalized_name=values["normalized_name"],
                                external_code=values["external_code"], is_user_added=False)
                    city.id = UUID(operation["id"])
                    db.add(city)
                else:
                    city = await db.get(City, UUID(operation["id"]))
                    city.district_id = UUID(values["district_id"])
                if index % 250 == 0:
                    await db.flush()
                    status(f"Saved {index}/{len(city_ops)} cities in the open transaction")
            await db.flush()
            status("Checking post-import counts and city/district links")
            after = await database_rows(db)
            for table in ("district", "city"):
                plural = "cities" if table == "city" else "districts"
                expected = fresh["before_counts"][plural] + fresh["counts"]["inserted"].get(table, 0)
                if len(after[plural]) != expected:
                    raise ValueError(f"Post-import {plural} count mismatch: expected {expected}, found {len(after[plural])}")
            after_cities = {c["id"]: c for c in after["cities"]}
            after_districts = {d["id"]: d for d in after["districts"]}
            for operation in fresh["operations"]:
                if operation["table"] != "city":
                    continue
                city = after_cities[operation["id"]]
                district = after_districts[city["district_id"]]
                if city["state_id"] != district["state_id"]:
                    raise ValueError(f"City/district state mismatch for {operation['id']}")
            status("Checks passed; committing transaction")
        write_json(recovery, {"status": "committed", "database": db_name,
                              "snapshot_date": fresh["snapshot_date"], "operations": fresh["operations"]})
        status("Transaction committed; recovery record marked committed")
    print(json.dumps({"applied": scope, "snapshot": fresh["snapshot_date"],
                      "database": db_name, "counts": fresh["counts"], "recovery": str(recovery)}))


async def verify(output: Path, override_path: Path | None, *, disambiguate_cities: bool = False) -> None:
    from sqlalchemy import text

    from app.core.db.database import local_session

    status("Loading staged source and checking its manifest")
    staged, manifest = load_stage(output)
    status("Connecting to the configured PostgreSQL database")
    async with local_session() as db:
        status("Reading existing India geography")
        current = await database_rows(db)
        result = {}
        queries = {
            "districts_per_state": """SELECT s.code, count(d.id) FROM platform.state s
                JOIN platform.country c ON c.id=s.country_id AND c.code='IN'
                LEFT JOIN platform.district d ON d.state_id=s.id GROUP BY s.code ORDER BY s.code""",
            "cities_per_state": """SELECT s.code, count(ci.id) FROM platform.state s
                JOIN platform.country c ON c.id=s.country_id AND c.code='IN'
                LEFT JOIN platform.city ci ON ci.state_id=s.id AND ci.district_id IS NOT NULL
                GROUP BY s.code ORDER BY s.code""",
            "mismatches": """SELECT count(*) FROM platform.city ci JOIN platform.district d ON d.id=ci.district_id
                WHERE ci.state_id<>d.state_id""",
            "unresolved_existing_cities": """SELECT count(*) FROM platform.city ci
                JOIN platform.state s ON s.id=ci.state_id JOIN platform.country c ON c.id=s.country_id
                WHERE c.code='IN' AND ci.district_id IS NULL""",
            "districts_without_cities": """SELECT count(*) FROM platform.district d
                JOIN platform.state s ON s.id=d.state_id JOIN platform.country c ON c.id=s.country_id
                WHERE c.code='IN' AND NOT EXISTS (SELECT 1 FROM platform.city ci WHERE ci.district_id=d.id)""",
        }
        for name, query in queries.items():
            status(f"Checking {name.replace('_', ' ')}")
            rows_found = (await db.execute(text(query))).all()
            result[name] = dict(rows_found) if name.endswith("per_state") else rows_found[0][0]
        result["snapshot"] = manifest["snapshot_date"]
        result["staged_rows"] = {name: len(rows_) for name, rows_ in staged.items()}
        validation = json.loads((output / "validation.json").read_text())
        result["source_rejected"] = len(validation["coverage_rejected"])
        result["bodies_without_district"] = len(validation["bodies_without_district"])
        status("Checking whether a second import would change rows")
        rerun = make_plan(staged, current, load_overrides(override_path), "full", disambiguate_cities=disambiguate_cities)
        result["rerun_counts"] = rerun["counts"]
        result["rerun_conflicts"] = len(rerun["conflicts"])
        status("Verification complete")
        print(json.dumps(result, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "plan", "apply", "verify"))
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "lgd")
    parser.add_argument("--snapshot", help="LGD snapshot date YYYY-MM-DD; prepare only")
    parser.add_argument("--offline", type=Path, help="Directory with the four component CSV or dated CSV.7z files; prepare only")
    parser.add_argument("--scope", choices=("districts", "full"), default="full")
    parser.add_argument("--overrides", type=Path, help="Reviewed JSON mappings; plan and apply")
    parser.add_argument("--disambiguate-cities", action="store_true",
                        help="Keep colliding LGD city identities as separate rows with their LGD codes in the display name")
    args = parser.parse_args()
    try:
        root = Path(__file__).resolve().parents[2]
        if args.output.resolve().is_relative_to(root) and not args.output.resolve().is_relative_to(root / "data" / "lgd"):
            raise ValueError("Choose --output outside the Git checkout or under the ignored data/lgd directory")
        if args.mode == "prepare":
            prepare(args)
        elif args.mode == "verify":
            asyncio.run(verify(args.output, args.overrides, disambiguate_cities=args.disambiguate_cities))
        else:
            asyncio.run(db_plan(args.output, args.scope, args.overrides, apply=args.mode == "apply",
                                disambiguate_cities=args.disambiguate_cities))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    main()
