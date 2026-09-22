# HealthOS prescription dropdowns — frontend implementation prompt

Update the existing Prescription step; do not redesign the page or add a UI dependency. Reuse the HealthOS field, focus-ring, error, menu, and button styles.

## Data

1. After authentication, fetch all India options once with `GET /api/v1/masters/prescription-options?country_code=IN`.
2. Read options from `data.groups.dosage`, `route`, `frequency`, `timing`, `duration`, and `instructions`. Render `label`; save `value` into the existing prescription item field. Do not submit the option UUID.
3. Cache the response for the current session. Show a compact retry message if it fails, but retain editable text fields so prescription entry is never blocked.

## Medication row

1. Replace the free-text Dosage, Route, Frequency, Timing, and Duration controls with the existing HealthOS Select component. Prefer a styled native `<select>` if the existing component is not fully keyboard accessible.
2. Every select must start with `Select …`, contain the master options, and end with `Other / type custom…`. Choosing Other swaps only that control to a text input with a `Back to options` action.
3. Do not preselect, infer, or recommend any dose, route, frequency, timing, duration, or instruction from the medicine. The prescriber must make the clinical choice.
4. For Specific Instructions, show a `Quick instruction` select above the existing editable input/textarea. Choosing an option copies its `value` into the editable field; the clinician can modify it or add more detail.
5. When reopening older prescriptions, preserve any saved text. If it does not match a current master value, show it as `Current: <saved value>` and keep it selected until the clinician changes it.
6. Keep the submitted payload unchanged: `dosage`, `route`, `frequency`, `timing`, `duration`, and `instructions` remain strings.

## Keyboard and accessibility acceptance criteria

1. A clinician can complete the entire row without a mouse. Tab order is Medicine → Dosage → Route → Frequency → Timing → Duration → Quick instruction → Instruction details → row actions.
2. On each select, Space/Enter opens, Up/Down moves, Enter selects, Escape closes, and typing letters jumps to a matching option. Do not override native behaviour when using `<select>`.
3. Each control has a persistent accessible label—not placeholder-only text—and a visible HealthOS focus ring. Use `aria-describedby` for help/error text and focus the first invalid field after submit.
4. After `Add Medication Row`, move focus to the new row’s Medicine search. After medicine selection, move focus to Dosage. Do not unexpectedly move focus after any other selection.
5. Show expanded wording in the UI, for example `Twice daily (BD/BID)`, never an abbreviation alone. Maintain at least a 44px interactive target and do not encode state by colour alone.

## Administration

Add `Administration > Prescription Options` with category and country filters.

- List: `GET /api/v1/masters/prescription-options?country_code=IN&category=<category>&include_inactive=true`
- Add: `POST /api/v1/masters/prescription-options`
- Edit/deactivate: `PATCH /api/v1/masters/prescription-options/{uuid}`
- Add/edit is visible only to organization administrators. Handle `403`, `404`, `409`, and validation errors inline.

Example create body:

```json
{
  "country_code": "IN",
  "category": "frequency",
  "code": "od",
  "label": "Once daily (OD)",
  "value": "Once daily",
  "description": "OD",
  "sort_order": 10,
  "is_active": true
}
```

Acceptance check: using only the keyboard, add a medicine, choose `1 tablet`, `Oral`, `Twice daily (BD/BID)`, `After meals`, `5 days`, and `Complete the full course`; save, reopen, and confirm the same text values remain selected.
