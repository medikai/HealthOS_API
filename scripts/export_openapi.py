"""Export the actual mounted FastAPI OpenAPI document to JSON.

Usage (from the repository root):
    ./venv/bin/python scripts/export_openapi.py [output_path]

The export reflects only routes actually mounted in ``src.app.main.app``; it is
the authoritative machine-readable surface for frontend integration. It does
not connect to or modify any database.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.app.main import app


def main() -> None:
    output = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else REPO_ROOT / "docs" / "patient-app" / "handoffs" / "openapi.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    schema = app.openapi()
    output.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n")
    paths = schema.get("paths", {})
    patient = [p for p in paths if "/patient" in p]
    print(f"wrote {output} ({len(paths)} paths, {len(patient)} patient paths)")


if __name__ == "__main__":
    main()
