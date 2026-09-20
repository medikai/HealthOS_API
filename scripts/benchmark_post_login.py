import argparse
import asyncio
from pathlib import Path
import statistics
import sys
import time
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from jose import jwt

from src.app.core.security import ALGORITHM, SECRET_KEY


class _MockResult:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def all(self):
        return self.rows

    def first(self):
        return self.rows[0] if self.rows else None


class _MockDatabase:
    def __init__(self):
        self.query_count = 0

    async def execute(self, query):
        self.query_count += 1
        sql = str(query).lower()
        if "accessible" in sql or "appointment_counts" in sql or "duty_counts" in sql:
            # /facilities query
            facility_rows = [
                SimpleNamespace(
                    facility_id=UUID(int=i + 10),
                    organization_id=UUID(int=3),
                    code=f"FAC-{i}",
                    name=f"Main Facility {i}",
                    timezone="Asia/Kolkata",
                    booked=5,
                    queue=3,
                    duty=2,
                )
                for i in range(10)
            ]
            return _MockResult(facility_rows)
        if "appointment" in sql:
            # /appointments query
            return _MockResult([])
        if "queue_entry" in sql or "queue" in sql:
            # /queue query
            return _MockResult([])
        if "staff_member" in sql and "practitioner" in sql:
            # /me query
            staff = SimpleNamespace(
                id=UUID(int=2),
                designation="Chief Doctor",
                status="active",
            )
            org = SimpleNamespace(
                id=UUID(int=3),
                name="Apollo Clinic",
                plan_code="ENTERPRISE",
            )
            practitioner = SimpleNamespace(
                id=UUID(int=4),
                person_name="Dr. Sharma",
                specialty="Cardiology",
            )
            return _MockResult([(staff, org, practitioner, "doctor")])
        return _MockResult([])

    async def scalars(self, query):
        self.query_count += 1
        return _MockResult([])

    async def scalar(self, query):
        self.query_count += 1
        return None


async def run_benchmark(client: httpx.AsyncClient, paths: list[str], requests: int, concurrency: int) -> dict[str, dict[str, float]]:
    results = {}
    for path in paths:
        semaphore = asyncio.Semaphore(concurrency)

        async def sample(path: str = path) -> float:
            async with semaphore:
                started_at = time.perf_counter()
                response = await client.get(path)
                response.raise_for_status()
                return (time.perf_counter() - started_at) * 1000

        values = sorted(await asyncio.gather(*(sample() for _ in range(requests))))
        p50 = statistics.median(values)
        percentiles = statistics.quantiles(values, n=100, method="inclusive")
        p95 = percentiles[94]
        p99 = percentiles[98]
        results[path] = {
            "p50": round(p50, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "min": round(values[0], 2),
            "max": round(values[-1], 2),
        }
        print(
            f"{path:40s}: n={len(values):3d}  p50={p50:6.2f}ms  "
            f"p95={p95:6.2f}ms  p99={p99:6.2f}ms"
        )
    return results


async def main() -> None:
    parser = argparse.ArgumentParser(description="HealthOS Post-Login Endpoints Benchmark")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/api/v1")
    parser.add_argument("--account-id", default=str(UUID(int=1)))
    parser.add_argument("--requests", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--in-memory", action="store_true", help="Run benchmark in-memory against FastAPI ASGI app")
    args = parser.parse_args()

    token = jwt.encode(
        {"sub": args.account_id, "exp": datetime.now(UTC) + timedelta(minutes=10)},
        SECRET_KEY.get_secret_value(),
        algorithm=ALGORITHM,
    )
    headers = {"Authorization": f"Bearer {token}"}

    paths = [
        "/me",
        "/facilities",
        "/appointments?facility_uuid=all",
        "/queue?facility_uuid=all",
        "/practitioners?facility_uuid=all",
    ]

    print(f"Running benchmark ({args.requests} requests/endpoint, concurrency {args.concurrency})...\n")

    if args.in_memory:
        from src.app.core.db.database import async_get_db
        from src.app.main import app
        from src.app.models.identity import UserAccount

        mock_db = _MockDatabase()
        account_uuid = UUID(args.account_id)
        mock_account = SimpleNamespace(
            id=account_uuid,
            logto_user_id="auth_test_sub",
            email="dr.sharma@apollo.test",
            display_name="Dr. Sharma",
            is_active=True,
        )

        async def override_db():
            yield mock_db

        async def override_identity():
            return mock_account

        from src.app.api.dependencies import get_current_identity_account

        app.dependency_overrides[async_get_db] = override_db
        app.dependency_overrides[get_current_identity_account] = override_identity

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver/api/v1", headers=headers
        ) as client:
            await run_benchmark(client, paths, args.requests, args.concurrency)
        app.dependency_overrides.clear()
    else:
        async with httpx.AsyncClient(
            base_url=args.base_url, headers=headers, timeout=60
        ) as client:
            await run_benchmark(client, paths, args.requests, args.concurrency)


if __name__ == "__main__":
    asyncio.run(main())
