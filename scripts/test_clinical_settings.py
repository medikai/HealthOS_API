#!/usr/bin/env python3
"""
Test script for Clinical Documentation & SOAP Settings API.

Usage:
    python scripts/test_clinical_settings.py
    python scripts/test_clinical_settings.py --base-url https://api.medikai.in --token <JWT_TOKEN>
    python scripts/test_clinical_settings.py --email user@example.com --password secret
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
    parser = argparse.ArgumentParser(description="Test HealthOS Clinical Documentation Settings APIs")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL of HealthOS API (default: http://localhost:8000)")
    parser.add_argument("--email", default="vikram.sen@citycareclinic.demo", help="Login email")
    parser.add_argument("--password", default="HealthOS@2026", help="Login password")
    parser.add_argument("--token", default=None, help="Bearer access token (bypasses login)")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api_base = f"{base}/api/v1"
    token = args.token

    print("=" * 70)
    print("HealthOS Clinical & SOAP Documentation Settings API Test")
    print(f"Target API: {api_base}")
    print("=" * 70)

    # 1. Authentication
    if not token:
        print(f"\n[1/5] Authenticating as {args.email}...")
        status, resp = make_request(
            f"{api_base}/auth/local/login",
            method="POST",
            body={"email": args.email, "password": args.password},
        )
        if status != 200 or not resp.get("success"):
            print(f"[-] Login failed (Status {status}): {json.dumps(resp, indent=2)}")
            print("[!] Please provide a valid --token or check credentials.")
            sys.exit(1)
        token = resp["data"]["access_token"]
        print(f"[+] Authenticated successfully. Token obtained.")
    else:
        print(f"\n[1/5] Using provided token.")

    auth_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    # 2. GET /clinical/documentation-settings
    print("\n[2/5] Testing GET /clinical/documentation-settings...")
    status, resp = make_request(
        f"{api_base}/clinical/documentation-settings",
        method="GET",
        headers=auth_headers,
    )
    if status != 200 or not resp.get("success"):
        print(f"[-] GET failed (Status {status}): {json.dumps(resp, indent=2)}")
        sys.exit(1)

    data = resp["data"]
    print(f"[+] GET succeeded! Envelope verified.")
    print(f"    - Triage default expanded: {data.get('triage_expanded_default')}")
    print(f"    - Active modifications count: {data.get('active_modifications_count')}")
    print(f"    - Active customizations:")
    for item in data.get("summary", []):
        print(f"      • {item}")

    # 3. PUT /clinical/documentation-settings
    print("\n[3/5] Testing PUT /clinical/documentation-settings (Toggling RR ON, adding custom section)...")
    vitals_cfg = data.get("vitals_config", {})
    if "respiratory_rate" in vitals_cfg:
        vitals_cfg["respiratory_rate"]["visible"] = True
    else:
        vitals_cfg["respiratory_rate"] = {"visible": True}

    soap_cfg = data.get("soap_config", {})
    custom_sections = list(data.get("custom_sections", []))
    custom_sections.append({
        "key": "test_pre_op_notes",
        "label": "Pre-Op Checklist",
        "group": "objective",
        "helper_text": "Surgical clearance and consent verification",
        "required": True,
        "expanded": True,
    })

    update_payload = {
        "triage_expanded_default": False,
        "vitals_config": vitals_cfg,
        "soap_config": soap_cfg,
        "custom_sections": custom_sections,
    }

    status, resp = make_request(
        f"{api_base}/clinical/documentation-settings",
        method="PUT",
        headers=auth_headers,
        body=update_payload,
    )
    if status != 200 or not resp.get("success"):
        print(f"[-] PUT failed (Status {status}): {json.dumps(resp, indent=2)}")
        sys.exit(1)

    put_data = resp["data"]
    print(f"[+] PUT succeeded! Settings updated.")
    print(f"    - New Triage default expanded: {put_data.get('triage_expanded_default')}")
    print(f"    - New Custom sections count: {len(put_data.get('custom_sections', []))}")

    # 4. Verify GET reflects PUT changes
    print("\n[4/5] Verifying persistence via GET /clinical/documentation-settings...")
    status, resp = make_request(
        f"{api_base}/clinical/documentation-settings",
        method="GET",
        headers=auth_headers,
    )
    assert status == 200 and resp.get("success")
    fetched = resp["data"]
    assert fetched.get("triage_expanded_default") is False, "Triage default expansion did not persist"
    assert any(cs.get("key") == "test_pre_op_notes" for cs in fetched.get("custom_sections", [])), "Custom section was not found"
    print("[+] Persistence verified! Changes were saved correctly.")

    # 5. POST /clinical/documentation-settings/reset
    print("\n[5/5] Testing POST /clinical/documentation-settings/reset...")
    status, resp = make_request(
        f"{api_base}/clinical/documentation-settings/reset",
        method="POST",
        headers=auth_headers,
    )
    if status != 200 or not resp.get("success"):
        print(f"[-] Reset failed (Status {status}): {json.dumps(resp, indent=2)}")
        sys.exit(1)

    reset_data = resp["data"]
    print(f"[+] Reset succeeded! Restored baseline defaults.")
    print(f"    - Triage default expanded: {reset_data.get('triage_expanded_default')}")
    print(f"    - Active modifications count: {reset_data.get('active_modifications_count')}")
    print(f"    - Restored summary:")
    for item in reset_data.get("summary", []):
        print(f"      • {item}")

    print("\n" + "=" * 70)
    print("ALL CLINICAL DOCUMENTATION SETTINGS API CHECKS PASSED! [OK]")
    print("=" * 70)


if __name__ == "__main__":
    main()

