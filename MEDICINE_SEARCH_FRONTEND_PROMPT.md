# Medicine search for prescriptions and administration

Implement medicine selection using the existing HealthOS components and styles. Do not add a UI dependency or redesign the prescription screen.

## Prescription screen

1. Keep `Add Medication Row`, but make each new row start with a searchable Medicine combobox instead of an unrestricted medicine-name input.
2. Start searching after 3 characters, debounce by 250–300 ms, cancel stale requests, and call authenticated `GET /api/v1/masters/medicines?q=<text>&limit=20`.
3. Render `data.items` with the product/brand `name` first, `generic_name` second, and manufacturer plus pack size as muted metadata. Show loading, no-results, and retry states inside the menu. Support keyboard navigation, Escape, Enter, visible focus, and a real accessible label.
4. When selected, store `uuid` as `medicine_id`; set `medicine_name` and `brand` from `name`. Show `generic_name` and `pack_size_label` as read-only context. Do not infer dose, route, frequency, duration, timing, or instructions from this community dataset—the clinician must enter them.
5. Preserve selected values when navigating between encounter steps and when reopening a draft. Existing prescriptions without `medicine_id` must continue to display their saved text.
6. Submit each prescription item with the existing fields plus `medicine_id`. Do not send dataset price as a prescription value.

## Admin medicine master

1. Add `Admin > Masters > Medicines` using the existing HealthOS admin layout, permissions, fields, buttons, validation, and table styling.
2. Provide the same debounced search for finding existing medicines before addition.
3. Add a form that calls authenticated `POST /api/v1/masters/medicines` with `name`, `manufacturer_name`, `medicine_type`, `pack_size_label`, `composition1`, `composition2`, optional `price`, `is_discontinued`, and `is_active`.
4. Load a selected record with authenticated `GET /api/v1/masters/medicines/{uuid}` and save edits with `PATCH /api/v1/masters/medicines/{uuid}` using any subset of the same editable fields. Never send `source` or `source_id`.
5. Only organization administrators may see or submit add/edit forms. Handle `403`, `404`, `409`, and validation responses inline. On success, refresh search results and show the saved medicine.
6. Label imported records as `Indian Medicine Dataset` and manually created records as `Manual`. Do not expose source editing.

Acceptance check: type `Aug` → select `Augmentin 625 Duo Tablet` → enter dosage/frequency/duration → save draft → reopen → the row retains both `medicine_id` and the saved medicine text. An administrator can add a missing medicine, and a non-admin cannot.
