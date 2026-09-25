"""Export the focused communication contract.

Writes ``docs/communication-delivery/contracts/`` containing only the
implemented ``/api/v1/communication`` OpenAPI fragment plus the event-envelope
JSON schema. Run from the repository root::

    venv/bin/python -m src.scripts.export_communication_contract
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from ..app.main import app

PREFIX = "/api/v1/communication"
OUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "communication-delivery" / "contracts"


def _collect_refs(node, into: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                into.add(value.rsplit("/", 1)[-1])
            else:
                _collect_refs(value, into)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, into)


def _transitive_schemas(schema: dict, names: set[str]) -> dict:
    resolved: set[str] = set()
    stack = list(names)
    while stack:
        name = stack.pop()
        if name in resolved:
            continue
        resolved.add(name)
        body = schema.get("components", {}).get("schemas", {}).get(name)
        if body is None:
            continue
        nested: set[str] = set()
        _collect_refs(body, nested)
        stack.extend(nested - resolved)
    return {name: schema["components"]["schemas"][name] for name in sorted(resolved)}


def event_envelope_schema() -> dict:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "healthos://communication/event-envelope.schema.json",
        "title": "HealthOS communication event envelope",
        "type": "object",
        "required": [
            "event_id",
            "event_type",
            "schema_version",
            "occurred_at",
            "entity_id",
        ],
        "properties": {
            "event_id": {"type": "string", "description": "Stable delivery event id (not a provider id or cursor)."},
            "event_type": {"type": "string", "examples": ["notification.created", "message.created", "conversation.read"]},
            "schema_version": {"type": "integer", "const": 1},
            "occurred_at": {"type": "string", "format": "date-time"},
            "entity_id": {"type": ["string", "null"]},
            "entity_version": {"type": ["integer", "null"]},
            "organization_id": {"type": ["string", "null"]},
            "facility_id": {"type": ["string", "null"]},
            "recipient_staff_id": {"type": ["string", "null"]},
            "data": {"type": "object", "description": "Minimal, non-secret metadata only."},
        },
        "additionalProperties": False,
    }


def export() -> None:
    schema = app.openapi()
    paths = {p: item for p, item in schema.get("paths", {}).items() if p.startswith(PREFIX)}
    refs: set[str] = set()
    _collect_refs(paths, refs)
    fragment = {
        "openapi": schema.get("openapi", "3.1.0"),
        "info": {
            **schema.get("info", {}),
            "title": "HealthOS communication contract (implemented subset)",
            "description": "Focused export: only implemented /api/v1/communication endpoints plus referenced schemas.",
        },
        "paths": paths,
        "components": {"schemas": _transitive_schemas(schema, refs)},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "communication.openapi.json").write_text(json.dumps(fragment, indent=2) + "\n")
    (OUT_DIR / "event-envelope.schema.json").write_text(
        json.dumps(event_envelope_schema(), indent=2) + "\n"
    )
    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "implemented_paths": sorted(paths),
        "source": "src/scripts/export_communication_contract.py",
    }
    (OUT_DIR / "generated.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"exported {len(paths)} paths to {OUT_DIR}")


if __name__ == "__main__":
    export()
