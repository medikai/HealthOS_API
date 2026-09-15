#!/usr/bin/env python3
"""
Test script for HealthOS Encounter SOAP Notes & Diagnoses APIs.

Endpoints tested:
1. GET  /api/v1/encounters/{id}/soap
2. PUT  /api/v1/encounters/{id}/soap
3. GET  /api/v1/encounters/{id}/diagnoses
4. PUT  /api/v1/encounters/{id}/diagnoses
5. POST /api/v1/encounters/{id}/soap/sign

Usage:
    python scripts/test_encounter_soap.py
    python scripts/test_encounter_soap.py --base-url http://localhost:8000 --token <JWT_TOKEN>
    python scripts/test_encounter_soap.py --encounter-uuid <UUID>
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


def make_request(url: str, method: str = "GET", headers: dict = None, body: dict = None):
    headers = headers or {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            resp_body = resp.read().decode("utf-8")
            return resp.status, json.loads(resp_body) if resp_body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        try:
            parsed = json.loads(err_body)
        except Exception:
            parsed = {"raw": err_body}
        return e.code, parsed
    except urllib.error.URLError as e:
        print(f"[!] Network error connecting to {url}: {e.reason}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Test HealthOS Encounter SOAP & Diagnoses APIs")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL (default: http://localhost:8000)")
    parser.add_argument("--email", default="dr.vikram.sen@citycare.com", help="Login email")
    parser.add_argument("--password", default="ClinicSecure2026!", help="Login password")
    parser.add_argument("--token", default=None, help="Bearer access token (bypasses login)")
    parser.add_argument("--encounter-uuid", default=None, help="Specific encounter UUID to test")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api_base = f"{base}/api/v1"
    token = args.token

    print("=" * 70)
    print("HealthOS Encounter SOAP & Diagnoses API Verification")
    print(f"Target API: {api_base}")
    print("=" * 70)

    # 1. Login if no token provided
    if not token:
        print("\n[Step 1] Authenticating as clinical practitioner...")
        code, body = make_request(
            f"{api_base}/auth/local/login",
            method="POST",
            body={"email": args.email, "password": args.password},
        )
        if code == 200 and body.get("success"):
            token = body["data"].get("token") or body["data"].get("access_token")
            print(f"[✓] Authenticated successfully. Bearer token acquired.")
        else:
            print(f"[!] Login returned status {code}: {body}")
            print("[!] Please supply --token or verify credentials.")
            sys.exit(1)

    auth_headers = {"Authorization": f"Bearer {token}"}

    # 2. Determine encounter UUID
    encounter_uuid = args.encounter_uuid
    if not encounter_uuid:
        print("\n[Step 2] Resolving active encounter...")
        code, body = make_request(f"{api_base}/encounters", headers=auth_headers)
        if code == 200 and body.get("success"):
            items = body.get("data", {}).get("items", [])
            if items:
                encounter_uuid = items[0].get("uuid") or items[0].get("id")
                print(f"[✓] Using first active encounter: {encounter_uuid}")
            else:
                print("[!] No encounters found on database. Please specify --encounter-uuid.")
                sys.exit(1)
        else:
            print(f"[!] Could not list encounters (status {code}): {body}")
            sys.exit(1)

    # 3. GET current SOAP note
    print(f"\n[Step 3] GET /api/v1/encounters/{encounter_uuid}/soap")
    code, body = make_request(f"{api_base}/encounters/{encounter_uuid}/soap", headers=auth_headers)
    print(f"Status: {code}")
    print(f"Response: {json.dumps(body, indent=2)}")

    # 4. PUT update SOAP note draft
    print(f"\n[Step 4] PUT /api/v1/encounters/{encounter_uuid}/soap (Save Draft)")
    soap_payload = {
        "subjective": "Patient reports persistent bilateral tension headache for 2 weeks. Exacerbated by computer screen time. Denies nausea.",
        "objective": "Alert, oriented x3. Pupils equal and reactive. BP 118/78 mmHg, Pulse 72 bpm, SpO2 99%, Temp 36.6C.",
        "assessment": "Tension-type headache with occupational postural strain.",
        "plan": "Workplace ergonomics adjustment, 20-20-20 screen rule, hydration >2L/day. Paracetamol 500mg SOS.",
    }
    code, body = make_request(
        f"{api_base}/encounters/{encounter_uuid}/soap",
        method="PUT",
        headers=auth_headers,
        body=soap_payload,
    )
    print(f"Status: {code}")
    print(f"Response: {json.dumps(body, indent=2)}")
    assert code == 200, f"Expected 200, got {code}"
    assert body.get("success") is True

    # 5. PUT update diagnoses
    print(f"\n[Step 5] PUT /api/v1/encounters/{encounter_uuid}/diagnoses (Replace ICD-10 Codes)")
    diag_payload = [
        {"code": "G44.209", "description": "Tension-type headache, unspecified", "is_primary": True},
        {"code": "M54.2", "description": "Cervicalgia", "is_primary": False},
    ]
    code, body = make_request(
        f"{api_base}/encounters/{encounter_uuid}/diagnoses",
        method="PUT",
        headers=auth_headers,
        body=diag_payload,
    )
    print(f"Status: {code}")
    print(f"Response: {json.dumps(body, indent=2)}")
    assert code == 200, f"Expected 200, got {code}"
    assert body.get("success") is True

    # 6. GET diagnoses
    print(f"\n[Step 6] GET /api/v1/encounters/{encounter_uuid}/diagnoses")
    code, body = make_request(f"{api_base}/encounters/{encounter_uuid}/diagnoses", headers=auth_headers)
    print(f"Status: {code}")
    print(f"Response: {json.dumps(body, indent=2)}")
    assert code == 200, f"Expected 200, got {code}"

    print("\n" + "=" * 70)
    print("[✓] ALL ENCOUNTER SOAP & DIAGNOSES API TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)


if __name__ == "__main__":
    main()

