# India geography ETL

The current [ramSeraph/opendata LGD mirror](https://github.com/ramSeraph/opendata) hosts extracts of India's Local Government Directory. It is a community mirror, not a government-operated service. The mirror's authored code is [UNLICENSE](https://github.com/ramSeraph/opendata/blob/master/UNLICENSE); this does not assert a license for the upstream data. The source [documents](https://ramseraph.github.io/opendata/lgd/) 7-Zip archives. We inspect its [parser](https://github.com/ramSeraph/opendata/blob/master/lgd/parse.py) as format documentation and never execute it.

The ETL uses one common snapshot date for `states`, `districts`, `urban_local_bodies`, and `statewise_ulbs_coverage`. It reads the release's [file listing](https://github.com/ramSeraph/opendata/releases/download/lgd-latest/listing_files.csv) in one request; that listing includes supplementary release URLs. `prepare` prints snapshot age and writes a manifest, staged records, and validation report. Downloaded files and reports default to the gitignored `data/lgd/` directory. The listing provides asset URLs and sizes; the ETL computes SHA-256 for every downloaded file. It records an asset ID or published digest only if the source supplies one. Have `bsdtar` or `7z` installed for archive extraction.

Each command prints live `[geography]` progress to stderr, including listing retrieval, concurrent downloads/cache reuse, parsing, reconciliation, database writes, and verification. The final machine-readable summary remains on stdout.

For the verified 2026-09-22 snapshot, the four direct files are [states](https://github.com/ramSeraph/opendata/releases/download/lgd-latest-extra1/states.22Sep2026.csv.7z), [districts](https://github.com/ramSeraph/opendata/releases/download/lgd-latest-extra1/districts.22Sep2026.csv.7z), [urban local bodies](https://github.com/ramSeraph/opendata/releases/download/lgd-latest-extra1/urban_local_bodies.22Sep2026.csv.7z), and [urban-body district coverage](https://github.com/ramSeraph/opendata/releases/download/lgd-latest-extra1/statewise_ulbs_coverage.22Sep2026.csv.7z). If automatic download is unavailable, place all four files together under `data/lgd/cache/` and run `./venv/bin/python src/scripts/import_locations.py prepare --offline data/lgd/cache --snapshot 2026-09-22`.

Run these from the repository root using the configured `src/.env` only after verifying its database target and confirming migration `20260922_30` (the `platform.district` table and city district FK) is present. Apply pending Alembic migrations separately to the chosen local/dev database if needed. **Only `apply` writes application tables, and it must be run manually.**

```sh
./venv/bin/python src/scripts/import_locations.py prepare
./venv/bin/python src/scripts/import_locations.py plan --scope full --overrides src/scripts/india_geography_overrides.json --disambiguate-cities
```

Review `data/lgd/plan-full.json`, then run the manual import:

```sh
./venv/bin/python src/scripts/import_locations.py apply --scope full --overrides src/scripts/india_geography_overrides.json --disambiguate-cities
./venv/bin/python src/scripts/import_locations.py verify --overrides src/scripts/india_geography_overrides.json --disambiguate-cities
```

Review `manifest.json`, `validation.json`, and the selected plan before apply. If the plan contains conflicts, apply refuses it. The full scope inserts districts and cities together. If you apply `--scope districts` first, regenerate the full plan before applying it because the previous plan becomes stale. Verify reports rerun inserts, updates, conflicts, source rejects, and database counts; a complete import should have zero rerun inserts and updates. `--snapshot YYYY-MM-DD` pins preparation to an explicit common date. `--offline /path/to/files` accepts the four component `.csv` or `.csv.7z` files, with optional `--snapshot` to record a known date; offline files with no date are labeled unverified.

The reviewed `src/scripts/india_geography_overrides.json` maps LGD state 38's name to the existing state “Dadra and Nagar Haveli and Daman and Diu.” The read-only district plan has zero conflicts and proposes 784 inserts. Four distinct source identities share a district and name: Itanagar (249701/276621), Pasighat (249707/276622), Chapar Tc (300489/300490), and Sonai (277152/289552). `--disambiguate-cities` keeps both rows and appends their LGD codes to those display names; the plan records all eight display changes in `disambiguated_source_cities`. With this option, the current full plan has zero conflicts and proposes 784 districts, 4,661 new cities, and 415 existing-city district updates. Review these names before apply. If a reviewed pair represents one place instead, use `city_source_aliases` to choose a canonical code and omit the disambiguation flag. Four existing districtless Maharashtra cities—Greater Mumbai, Malegaon, Malkapur, and Karjat—remain unresolved rather than being moved without reliable district evidence. The current database has no facility rows linked to a city, so this import does not need a facility district backfill.

## Identity and reconciliation

Existing state UUIDs are selected only under `country.code='IN'`. State `code` and `external_code` retain their existing HealthOS meanings; LGD state codes are matched by exact case-folded name or reviewed overrides. New districts use `code='LGD:<source code>'` and UUIDv5 of `healthos:lgd-district:<source code>`. New cities use `external_code='LGD:<urban body code>'` and UUIDv5 of `healthos:lgd-city:<urban body code>:<district code>`. District UUIDs and city UUIDs already in HealthOS remain unchanged when matched. No existing row is deleted, merged, reactivated, or moved between states or districts.

Urban local bodies with multiple covered districts create one city per district. Repeated coverage rows are deduplicated. Bodies with no valid district are reported and omitted. An existing districtless city is linked only if its state and normalized name match exactly and the urban body has one district. Its UUID, `is_user_added`, name, and external code remain unchanged. Other districtless cities stay unresolved. The import cannot claim to include every Indian locality.

When names differ or records are ambiguous, create a reviewed JSON override file, then pass `--overrides /path/overrides.json` to both plan and apply:

```json
{
  "states": {"<LGD state code>": "<existing HealthOS state UUID>"},
  "state_aliases": {"<LGD state code>": "<exact existing HealthOS state name>"},
  "districts": {"<LGD district code>": "<existing HealthOS district UUID>"},
  "cities": {"<LGD urban body code>:<LGD district code>": "<existing HealthOS city UUID>"},
  "city_source_aliases": {"<secondary urban body code>:<district code>": "<canonical urban body code>:<same district code>"}
}
```

Pass the same override file to verify. Overrides preserve existing names and external codes. A city alias may differ in display name, but must remain in the same state and either the covered district or a null district. The plan records before-images of updates and inserted IDs. Apply checks the plan against the current database, source hash, snapshot, overrides hash, and database name under a PostgreSQL advisory transaction lock; integrity failures roll back. `recovery-{scope}.json` records pending or committed operations. Review it before manual reversal; no automated destructive undo is provided.

## Checks after manual import

The seeded state UUID `0e38a9f8-7227-52c4-b4f1-2e3d24f9f945` is **Maharashtra**. Seeded Karnataka is `3726d980-fac2-53b0-a810-6fe66c39e4af`. Confirm these against the selected database:

```sql
SELECT s.id, s.code, s.name FROM platform.state s
JOIN platform.country c ON c.id=s.country_id WHERE c.code='IN' AND s.code IN ('MH','KA');
SELECT s.code, count(d.id) FROM platform.state s LEFT JOIN platform.district d ON d.state_id=s.id
WHERE s.code IN ('MH','KA') GROUP BY s.code;
SELECT d.name, count(ci.id) FROM platform.district d LEFT JOIN platform.city ci ON ci.district_id=d.id
WHERE d.state_id='0e38a9f8-7227-52c4-b4f1-2e3d24f9f945' GROUP BY d.id ORDER BY d.name;
SELECT count(*) FROM platform.city ci JOIN platform.district d ON d.id=ci.district_id
WHERE ci.state_id<>d.state_id;
```

Read-only API smoke checks:

```sh
curl -sS 'http://localhost:8000/api/v1/masters/districts?state_id=0e38a9f8-7227-52c4-b4f1-2e3d24f9f945'
curl -sS 'http://localhost:8000/api/v1/masters/districts?state_id=3726d980-fac2-53b0-a810-6fe66c39e4af'
curl -sS 'http://localhost:8000/api/v1/masters/cities?state_id=<STATE_UUID>&district_id=<DISTRICT_UUID>&limit=100'
```

The masters API reads active `platform` rows directly, returns `{"success":true,"data":{"items":[...]},"meta":{"count":N}}`, and does not cache these endpoints; the app's client cache middleware sets `Cache-Control: no-store`. Invalid UUID query values return 422. State, district, and city lists filter inactive rows. The city endpoint has a 100-row maximum per request; a crowded district may need a `q` search for smoke checks.
