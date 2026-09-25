#!/usr/bin/env python3
"""Local/dev capacity probe for the HealthOS communication API.

Safety-first: defaults to a local target, refuses production hosts, and only
makes plain authenticated HTTP calls. It does **not** open Ably/FCM connections
and must be pointed at an environment whose provider sends are disabled or
synthetic. The >1,000-user Ably connection profile is a separate, budgeted
browser test (see docs/communication-delivery/TEST_MATRIX.md).

Examples::

    # preview the plan only (no requests)
    venv/bin/python scripts/capacity/communication_capacity.py --plan-only

    # local smoke: 10 concurrent GETs of /health for 30s
    venv/bin/python scripts/capacity/communication_capacity.py \
        --base-url http://localhost:8000/api/v1 --path /health \
        --concurrency 10 --duration 30 --i-understand-load

    # local synthetic notification list with a bearer token
    venv/bin/python scripts/capacity/communication_capacity.py \
        --path /communication/notifications --token "$TOKEN" \
        --concurrency 5 --duration 60 --i-understand-load
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from urllib.parse import urlparse

import httpx

PRODUCTION_HOSTS = {"api.medikai.in", "healthos.medikai.in"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
TARGETS = {
    "online_notification_p95_ms": 2000,
    "chat_persist_ack_p95_ms": 1000,
}


def _guard_target(base_url: str, allow_nonlocal: bool) -> str:
    host = (urlparse(base_url).hostname or "").lower()
    if host in PRODUCTION_HOSTS:
        sys.exit("Refusing to target a production host. Use a staging/local URL.")
    if host not in LOCAL_HOSTS and not allow_nonlocal:
        sys.exit(
            f"Host '{host}' is not local. Pass --allow-nonlocal for an authorized "
            "staging environment (never production)."
        )
    return host


async def _login(client: httpx.AsyncClient, base_url: str, email: str, password: str) -> str:
    response = await client.post(
        f"{base_url}/auth/local/login", json={"email": email, "password": password}
    )
    response.raise_for_status()
    return response.json()["data"]["access_token"]


async def _run_worker(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    path: str,
    method: str,
    token: str | None,
    deadline: float,
    latencies: list[float],
    statuses: dict[str, int],
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base_url}{path}"
    while time.perf_counter() < deadline:
        started = time.perf_counter()
        try:
            response = await client.request(method, url, headers=headers)
            statuses[str(response.status_code)] = statuses.get(str(response.status_code), 0) + 1
        except httpx.HTTPError as exc:
            statuses[type(exc).__name__] = statuses.get(type(exc).__name__, 0) + 1
        latencies.append((time.perf_counter() - started) * 1000)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1)))
    return ordered[index]


async def _run(args: argparse.Namespace) -> dict:
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.timeout)
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        token = args.token
        if not token and args.login_email:
            token = await _login(client, args.base_url, args.login_email, args.login_password)
        # warmup
        await client.request(args.method, f"{args.base_url}{args.path}")
        deadline = time.perf_counter() + args.duration
        await asyncio.gather(
            *(
                _run_worker(
                    client,
                    base_url=args.base_url,
                    path=args.path,
                    method=args.method,
                    token=token,
                    deadline=deadline,
                    latencies=latencies,
                    statuses=statuses,
                )
                for _ in range(args.concurrency)
            )
        )
    errors = sum(count for key, count in statuses.items() if not key.startswith("2"))
    return {
        "requests": len(latencies),
        "errors": errors,
        "statuses": statuses,
        "p50_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_ms": round(_percentile(latencies, 95), 2),
        "p99_ms": round(_percentile(latencies, 99), 2),
        "rps": round(len(latencies) / args.duration, 2) if args.duration else 0.0,
    }


def _print_plan(args: argparse.Namespace) -> None:
    print("Capacity probe plan (no requests sent)")
    print(f"  target base_url : {args.base_url}")
    print(f"  path/method     : {args.method} {args.path}")
    print(f"  concurrency     : {args.concurrency}")
    print(f"  duration        : {args.duration}s")
    print(f"  auth            : {'bearer token' if args.token else 'password login' if args.login_email else 'anonymous'}")
    print("  provider sends  : disabled by this script (HTTP only; no Ably/FCM connections)")
    print("  measured targets (not promises):")
    for name, value in TARGETS.items():
        print(f"    {name}: <= {value} ms")
    print("  >1,000-user Ably profile: run separately on a budgeted plan per TEST_MATRIX.md")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000/api/v1")
    parser.add_argument("--path", default="/health")
    parser.add_argument("--method", default="GET", choices=["GET", "POST", "PATCH", "PUT"])
    parser.add_argument("--token", default=None, help="bearer access token (preferred)")
    parser.add_argument("--login-email", default=None)
    parser.add_argument("--login-password", default=None)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--allow-nonlocal", action="store_true")
    parser.add_argument(
        "--i-understand-load",
        action="store_true",
        help="required to actually send traffic",
    )
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()

    _guard_target(args.base_url, args.allow_nonlocal)
    if args.concurrency < 1 or args.duration < 1:
        sys.exit("--concurrency and --duration must be >= 1")
    if args.login_email and not args.login_password:
        sys.exit("--login-password is required with --login-email")

    if args.plan_only or not args.i_understand_load:
        _print_plan(args)
        if not args.plan_only:
            print("\nRefusing to send traffic without --i-understand-load.")
        return 0

    result = asyncio.run(_run(args))
    print("Measured results (this run only; not a capacity claim):")
    print(json.dumps(result, indent=2))
    print("\nTargets (separate from measured numbers):")
    print(json.dumps(TARGETS, indent=2))
    return 0 if result["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
