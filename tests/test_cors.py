from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from src.app.core.config import CORSSettings
from src.app.core.setup import create_application

PRODUCTION_ORIGIN = "https://healthos.medikai.in"
EVIL_ORIGIN = "https://evil.example.com"
LOCALHOST_ORIGIN = "http://localhost:5173"
PATIENT_ORIGIN = "http://localhost:5174"
PATIENT_IP_ORIGIN = "http://127.0.0.1:5174"


def _cors_app(settings: CORSSettings) -> FastAPI:
    app = create_application(
        router=APIRouter(),
        settings=settings,
        create_tables_on_start=False,
    )

    @app.post("/api/v1/auth/login")
    async def login() -> dict[str, bool]:
        return {"success": True}

    return app


def _preflight(app: FastAPI, origin: str):
    with TestClient(app) as client:
        return client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization",
            },
        )


def test_default_origins_include_production_and_localhost(monkeypatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)

    origins = CORSSettings().CORS_ORIGINS

    assert PRODUCTION_ORIGIN in origins
    assert LOCALHOST_ORIGIN in origins
    assert "http://127.0.0.1:5173" in origins
    assert PATIENT_ORIGIN in origins
    assert PATIENT_IP_ORIGIN in origins


def test_patient_origin_preflight_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f"{LOCALHOST_ORIGIN},{PATIENT_ORIGIN}")

    response = _preflight(_cors_app(CORSSettings()), PATIENT_ORIGIN)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PATIENT_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_json_env_origins_are_trimmed_and_de_slashed(monkeypatch) -> None:
    monkeypatch.setenv(
        "CORS_ORIGINS",
        '["https://healthos.medikai.in/", " http://localhost:5173 "]',
    )

    assert CORSSettings().CORS_ORIGINS == [PRODUCTION_ORIGIN, LOCALHOST_ORIGIN]


def test_comma_separated_env_origins_are_accepted(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f"{PRODUCTION_ORIGIN},{LOCALHOST_ORIGIN}")

    assert CORSSettings().CORS_ORIGINS == [PRODUCTION_ORIGIN, LOCALHOST_ORIGIN]


def test_single_origin_env_value_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", PRODUCTION_ORIGIN)

    assert CORSSettings().CORS_ORIGINS == [PRODUCTION_ORIGIN]


def test_production_preflight_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f"{PRODUCTION_ORIGIN},{LOCALHOST_ORIGIN}")

    response = _preflight(_cors_app(CORSSettings()), PRODUCTION_ORIGIN)

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == PRODUCTION_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "POST" in response.headers["access-control-allow-methods"]


def test_arbitrary_origin_is_not_allowed(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", f"{PRODUCTION_ORIGIN},{LOCALHOST_ORIGIN}")

    response = _preflight(_cors_app(CORSSettings()), EVIL_ORIGIN)

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
