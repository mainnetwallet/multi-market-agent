from __future__ import annotations

from fastapi.testclient import TestClient

from mm_agent.control_api import create_app
from tests.conftest import Harness


def make_client(harness: Harness) -> TestClient:
    app = create_app(harness.settings, harness.agent, harness.scanner, harness.registry, harness.chat, harness.bus)
    return TestClient(app)


def test_health_head_and_auth(harness: Harness) -> None:
    client = make_client(harness)
    assert client.get("/health").status_code == 200
    assert client.head("/health").status_code == 200
    assert client.get("/v1/status").status_code == 401
    assert client.post("/v1/auth/login", json={"username": "operator", "password": "wrong"}).status_code == 401
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    assert login.status_code == 200, login.text
    status = client.get("/v1/status")
    assert status.status_code == 200, status.text
    assert status.json()["active_venue"] == "fake"
    assert "PAPER" in status.json()["badge"]


def test_panic_needs_confirm(harness: Harness) -> None:
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    first = client.post("/v1/panic", json={"confirm": False})
    assert first.status_code == 200
    assert first.json()["confirm_required"] is True
    second = client.post("/v1/panic", json={"confirm": True})
    assert second.status_code == 200
    assert second.json()["paused"] is True


def test_chat_help_and_status(harness: Harness) -> None:
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    help_response = client.post("/v1/chat", json={"message": "/help"})
    assert help_response.status_code == 200
    assert "/approve" in help_response.json()["reply"]
    status_response = client.post("/v1/chat", json={"message": "/status"})
    assert status_response.status_code == 200
    assert "FAKE" in status_response.json()["reply"]


def test_no_market_mutation_without_auth(harness: Harness) -> None:
    client = make_client(harness)
    response = client.post("/v1/venues/active", json={"venue": "fake"})
    assert response.status_code == 401


def test_bengali_summary_is_appended(harness: Harness) -> None:
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    response = client.post("/v1/chat", json={"message": "/status দেখুন"})
    assert response.status_code == 200
    assert "[বাংলা সারসংক্ষেপ]" in response.json()["reply"]
