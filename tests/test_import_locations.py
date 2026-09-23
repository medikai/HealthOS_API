"""Small offline check for LGD parsing and existing-UUID preservation."""

from copy import deepcopy
from io import BytesIO
from json import loads
from pathlib import Path
from types import SimpleNamespace

from src.scripts import import_locations
from src.scripts.import_locations import (
    discover,
    load_stage,
    make_plan,
    normalize,
    prepare,
)


def test_release_listing_selects_newest_common_snapshot(tmp_path: Path, monkeypatch):
    def asset(name, day, tag):
        filename = f"{name}.{day}.csv.7z"
        return f"{filename},10,https://github.com/ramSeraph/opendata/releases/download/{tag}/{filename}\n"

    listing = "name,size,url\n"
    for day in ("21Sep2026", "22Sep2026"):
        for name in ("states", "districts", "urban_local_bodies", "statewise_ulbs_coverage"):
            listing += asset(name, day, "lgd-latest-extra1")
    monkeypatch.setattr(import_locations, "request", lambda *_args, **_kwargs: BytesIO(listing.encode()))
    day, selected = discover(None, tmp_path / "listing_files.csv")
    assert day.isoformat() == "2026-09-22"
    assert set(selected) == {"states", "districts", "urban_local_bodies", "statewise_ulbs_coverage"}
    assert discover("2026-09-21", tmp_path / "listing_files.csv")[0].isoformat() == "2026-09-21"


def test_lgd_stage_and_plan(tmp_path: Path, capsys):
    files = {}
    contents = {
        "states": "State Code;State Name(In English)\n27;MAHARASHTRA\n29;KARNATAKA\n",
        "districts": "District Code;District Name(In English);State Code\n519;MUMBAI\n;27\n520;PUNE;27\n521;BENGALURU;29\n",
        "urban_local_bodies": 'Local Body\nCode;Local Body Name\n(In English);State Code\n900;Mumbai;27\n901;Pune;27\n902;Twin City;27\n',
        "statewise_ulbs_coverage": "Localbody;District Code;State Name\nLocalbody;District Code;State Name\n900;519;MAHARASHTRA\n900;519;MAHARASHTRA\n901;520;MAHARASHTRA\n902;519;MAHARASHTRA\n902;520;MAHARASHTRA\n",
    }
    # Quoted multiline source headings and semicolon delimiters are accepted.
    contents["urban_local_bodies"] = contents["urban_local_bodies"].replace("Local Body\nCode", '"Local Body\nCode"').replace(
        "Local Body Name\n(In English)", '"Local Body Name\n(In English)"')
    contents["districts"] = contents["districts"].replace("519;MUMBAI\n;27", "519;MUMBAI;27")
    for name, content in contents.items():
        files[name] = tmp_path / f"{name}.csv"
        files[name].write_text(content)
    staged, report = normalize(files)
    assert report["staged_rows"] == {"states": 2, "districts": 3, "cities": 4}
    output = tmp_path / "prepared"
    prepare(SimpleNamespace(output=output, offline=tmp_path, snapshot="2026-09-20"))
    printed = capsys.readouterr()
    assert "[geography] Parsing states" in printed.err
    assert "Wrote staged data" in printed.err
    assert loads(printed.out)["rows"] == report["staged_rows"]
    assert load_stage(output)[0] == staged
    current = {
        "country": {"id": "10000000-0000-0000-0000-000000000000", "active": True},
        "states": [
            {"id": "20000000-0000-0000-0000-000000000000", "code": "MH", "name": "Maharashtra", "external_code": "IN-MH", "active": True},
            {"id": "30000000-0000-0000-0000-000000000000", "code": "KA", "name": "Karnataka", "external_code": "IN-KA", "active": True},
        ],
        "districts": [],
        "cities": [
            {"id": "40000000-0000-0000-0000-000000000000", "state_id": "20000000-0000-0000-0000-000000000000",
             "district_id": None, "name": "Mumbai", "normalized_name": "mumbai", "external_code": None,
             "is_user_added": True, "active": True},
        ],
    }
    plan = make_plan(staged, current, {}, "full")
    assert plan["state_map"]["27"] == current["states"][0]["id"]
    assert plan["counts"]["inserted"] == {"district": 3, "city": 3}
    assert plan["counts"]["updated"] == {"city": 1}
    assert next(op for op in plan["operations"] if op["action"] == "update")["id"] == current["cities"][0]["id"]
    assert not plan["conflicts"]
    assert not make_plan(staged, current, {}, "districts")["counts"]["updated"]
    after = deepcopy(current)
    for operation in plan["operations"]:
        if operation["action"] == "insert":
            row = {"id": operation["id"], "active": True, **operation["after"]}
            if operation["table"] == "city":
                row["is_user_added"] = False
            after["cities" if operation["table"] == "city" else "districts"].append(row)
        else:
            next(c for c in after["cities"] if c["id"] == operation["id"]).update(operation["after"])
    rerun = make_plan(staged, after, {}, "full")
    assert not rerun["operations"] and not rerun["conflicts"]
    missing = make_plan(staged, {**current, "states": current["states"][:1]}, {}, "districts")
    assert any(c["type"] == "state_mapping" for c in missing["conflicts"])
    ambiguous = deepcopy(current)
    ambiguous["cities"].append({**ambiguous["cities"][0], "id": "50000000-0000-0000-0000-000000000000"})
    assert any(c["type"] == "city_ambiguous" for c in make_plan(staged, ambiguous, {}, "full")["conflicts"])
    staged["cities"].append({"code": "999", "name": "Pune", "state_code": "27", "district_code": "520"})
    assert any(c["type"] == "source_city_collision" for c in make_plan(staged, current, {}, "full")["conflicts"])
    separate = make_plan(staged, current, {}, "full", disambiguate_cities=True)
    assert not separate["conflicts"] and separate["counts"]["disambiguated_cities"] == 2
    assert {c["display_name"] for c in separate["disambiguated_source_cities"]} == {"Pune (LGD 901)", "Pune (LGD 999)"}
    resolved = make_plan(staged, current, {"city_source_aliases": {"999:520": "901:520"}}, "full")
    assert not resolved["conflicts"] and resolved["counts"]["aliased_cities"] == 1
    assert resolved["resolved_source_aliases"] == [{"source": "999:520", "canonical": "901:520"}]
    invalid = make_plan(staged, current, {"city_source_aliases": {"999:520": "900:519"}}, "full")
    assert any(c["type"] == "invalid_city_source_alias" for c in invalid["conflicts"])
    staged["cities"][-1] = {"code": "999", "name": "Karjat", "state_code": "27", "district_code": "519"}
    staged["cities"].append({"code": "998", "name": "Karjat", "state_code": "27", "district_code": "520"})
    existing = {**current["cities"][0], "id": "60000000-0000-0000-0000-000000000000",
                "name": "Karjat", "normalized_name": "karjat"}
    ambiguous_geography = make_plan(staged, {**current, "cities": current["cities"] + [existing]}, {}, "full")
    assert not any(op["id"] == existing["id"] for op in ambiguous_geography["operations"])
