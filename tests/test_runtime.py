from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mm_agent.config import Settings
from mm_agent.domain import VenueId
from mm_agent.main import build_runtime


def test_full_runtime_serves_ui_and_stops(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'runtime.db'}",
        enabled_venues_raw="fake",
        active_venue=VenueId.FAKE,
        auto_propose=False,
        admin_password="test-password",
        session_secret="test-session-secret-long-value",
        llm_base_url="",
        llm_model="",
    )
    runtime = build_runtime(settings)
    with TestClient(runtime.app) as client:
        assert client.get("/health").status_code == 200
        page = client.get("/")
        assert page.status_code == 200
        assert "Multi-Market Agent" in page.text
        login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
        assert login.status_code == 200, login.text
        status = client.get("/v1/status")
        assert status.status_code == 200, status.text
        assert status.json()["badge"].startswith("FAKE")
        report = client.get("/v1/reports/daily")
        assert report.status_code == 200
        assert "daily" in report.json()["text"].lower() or "Daily" in report.json()["text"]
