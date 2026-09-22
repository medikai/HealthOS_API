# Clinic registration location dropdowns

Update `/registration/clinic` with the existing HealthOS form/select components and styles. Do not add a UI dependency or redesign the screen.

1. Replace Country, State / Province, District, and City free-text fields with dependent searchable selects:
   - `GET /api/v1/masters/countries`
   - `GET /api/v1/masters/states?country_id=<country_uuid>`
   - `GET /api/v1/masters/districts?state_id=<state_uuid>`
   - `GET /api/v1/masters/cities?state_id=<state_uuid>&district_id=<district_uuid>&q=<search>&limit=50`
   Responses use `data.items`; each option has `uuid` and `name`.
2. Default Country to India when present. Clear all descendants when a parent changes: Country clears State/District/City; State clears District/City; District clears City. Disable child selects until their parent is selected.
3. If city search has no exact case-insensitive match, show `Add "<entered city>"`. On confirmation call authenticated `POST /api/v1/masters/cities` with `{ "state_id": "...", "district_id": "...", "name": "..." }`, then select the returned `data` item. For `409`, refetch and select the existing match.
4. Submit UUIDs, not labels, in the organization registration payload: `country_id`, `state_id`, `district_id`, `city_id`. Also submit the existing screen values as `clinic_name`, `classification`, `street_address`, `postal_code`, `phone`, and `timezone`.
5. Make Classification, Country, State, City, and Timezone use the same HealthOS select styling: existing height, radius, border/focus teal, typography, required/error states, keyboard navigation, visible labels, and loading/empty states. Preserve the current two-column responsive layout and collapse to one column on mobile.
6. Debounce city search by 250–300 ms and cancel stale requests. Do not preload every city.
7. Keep the current values when navigating Back to Doctor and restore labels from the loaded options.

Acceptance check: India → Maharashtra → select a district → search a city → Continue to Review → return Back → all values remain selected; the final organization request contains all four location UUIDs and all clinic address fields. Verify same-named cities in different districts remain distinct.
