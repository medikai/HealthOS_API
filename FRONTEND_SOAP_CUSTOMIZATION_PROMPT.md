# Frontend implementation prompt: configurable clinical documentation

Implement the production integration for configurable clinical documentation in the sibling `healthos-frontend` repository. Read its `AGENTS.md` first. Reuse the existing React/Tailwind implementation; do not add a dependency or build a second settings screen.

## Existing code to reuse

- Settings UI: `src/features/clinical-configuration-preview/pages/DocumentationSettingsPreview.tsx`
- Configured encounter reference: `src/features/clinical-configuration-preview/pages/ConfiguredEncounterPreview.tsx`
- Production encounter: `src/pages/encounter/EncounterView.tsx`
- API client/types: `src/lib/api.ts`
- Active facility: `useAppContext()`

Promote/refactor the preview settings UI into the authenticated application and add a `Documentation Settings` navigation entry. Preserve the existing design and the production encounter's five-step behavior. Do not copy the page into a second large component; share or move the existing component.

## Backend contract

All responses use the existing API client's unwrapped `data` value.

- `GET /clinical/documentation-settings?facility_uuid=<uuid>` returns the effective configuration. `source` is `facility`, `organization`, or `default`.
- `PUT /clinical/documentation-settings` saves a complete configuration. Always include `facility_uuid` from the selected active facility when saving from a doctor/clinic screen.
- `POST /clinical/documentation-settings/reset?facility_uuid=<uuid>` removes the facility override and returns the newly effective inherited configuration.
- `GET|PUT /encounters/:encounterUuid/soap` now includes `custom_fields: Record<string, string | null>`.

Settings shape:

```ts
type FieldState = {
  visible: boolean;
  mandatory: boolean;
  default_state: 'expanded' | 'collapsed';
};

type ClinicalDocumentationSettings = {
  id: string | null;
  organization_id: string;
  facility_id: string | null;
  source: 'facility' | 'organization' | 'default';
  triage_expanded_default: boolean;
  vitals_config: Record<
    'blood_pressure' | 'pulse_rate' | 'spo2' | 'respiratory_rate' |
    'temperature' | 'height' | 'weight' | 'bmi',
    { visible: boolean; label?: string; unit?: string }
  >;
  soap_config: {
    subjective: Record<'general_notes' | 'chief_complaints' | 'hpi' | 'family_social', FieldState>;
    objective: Record<'general_exam' | 'systemic_exam' | 'additional_obs', FieldState>;
    assessment: Record<'diagnoses', FieldState>;
    plan: Record<'general_plan' | 'prescription_summary', FieldState>;
  };
  custom_sections: Array<{
    key: string;
    label: string;
    group: 'subjective' | 'objective' | 'assessment' | 'plan';
    helper_text?: string | null;
    input_type: 'multiline_text';
    visible: boolean;
    required: boolean;
    expanded: boolean;
  }>;
  active_modifications_count: number;
  summary: string[];
};
```

The API rejects hidden+required sections, duplicate/reserved custom keys, unsupported keys, and BMI enabled while height or weight is explicitly disabled. Surface the backend error next to the save action.

## Settings screen

1. Load using the active facility UUID. Show the existing page skeleton/loading state while fetching.
2. Save with the active `facility_uuid`; disable Save/Reset while their request is running.
3. Apply empty arrays from the API too. In particular, `custom_sections: []` must clear the current UI list; do not keep demo entries because the array is empty. Do the same for `summary: []`.
4. Generate custom keys with the existing lowercase/underscore rule. Prevent duplicates in the client and allow deletion of a custom section.
5. Use `source` to show a small `Facility override` or `Inherited organization default` status. Reset must immediately re-render the returned inherited configuration.
6. Keep authorization server-driven. A 403 should show a read-only/error state, not silently fall back to mock data.
7. Remove demo-only names, hardcoded modification counts, fake notifications, and hardcoded facility/doctor copy from the production route. Keep the design-preview route if it is still useful.

## Production encounter

1. After loading the encounter, fetch settings with `encounter.facility.uuid` (not whichever facility happens to be active in global context).
2. If settings fail to load, keep every existing field visible and show a non-blocking warning. Never hide clinical inputs because of a network error.
3. Render each vital card only when its `vitals_config[key].visible !== false`. Keep BMI calculation unchanged. The current vitals save payload must also send `respiratory_rate: vitals.respRate` and `spo2: vitals.spo2`; it currently omits both.
4. Preserve the four existing SOAP values and API fields: `subjective`, `objective`, `assessment`, and `plan`. Use the corresponding general-section configuration to control their visibility/required/default-expanded state:
   - `subjective.general_notes` -> `subjective`
   - `objective.general_exam` -> `objective`
   - `assessment.diagnoses` -> assessment/diagnosis area
   - `plan.general_plan` -> `plan`
5. Render enabled built-in subsections such as `chief_complaints`, `hpi`, `family_social`, `systemic_exam`, `additional_obs`, and `prescription_summary` inside their SOAP group. Persist their text in `soap.custom_fields` under the subsection key.
6. Render each visible custom section in its configured group using the existing textarea styling. Bind its value to `soap.custom_fields[section.key]`. Respect `required` and `expanded`.
7. Hiding a section must never erase its local or saved answer. Keep hidden values in `custom_fields`, include non-empty previously recorded hidden values in Note Review with an `Archived/hidden by current template` label, and submit them unchanged on draft saves.
8. Required fields should block Step 2 -> Step 3 and signing, focus the first invalid field, and show a concise inline error. Draft saves remain allowed.
9. Note Review, signed-note modal, and completion/discharge views must use the same resolved configuration and include visible custom sections plus non-empty archived answers.
10. Do not let configuration edits mutate already signed SOAP notes; the existing backend conflict response remains authoritative.

## API client changes

- Replace `Record<string, any>` with the concrete types above.
- Add `custom_fields` to `SoapNoteData` and `saveEncounterSoap`.
- URL-encode every facility/encounter ID as already done elsewhere.
- Keep the existing cookie/CSRF behavior in the shared `api()` function; do not add direct `fetch` calls.

## Minimum checks

- Settings load/save/reset for an active facility.
- Empty custom-section reset clears demo sections.
- Respiratory Rate OFF removes only that card and does not erase previously recorded data.
- Custom section text survives save, refresh, Note Review, and Completion.
- Hidden required configuration is rejected/displayed cleanly.
- Settings-fetch failure leaves the current encounter UI fully usable.
- Run the repository's existing lint, tests, and production build. Add focused tests only for the configuration mapping and non-destructive hidden-answer behavior.
