from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.middleware.client_cache_middleware import ClientCacheMiddleware


def test_api_responses_are_not_cached() -> None:
    app = FastAPI()
    app.add_middleware(ClientCacheMiddleware)
    app.get("/patient")(lambda: {"name": "Private"})

    assert TestClient(app).get("/patient").headers["cache-control"] == "no-store"
