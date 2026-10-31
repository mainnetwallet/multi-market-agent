from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

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


def test_session_refresh_renews_and_is_capped(harness: Harness) -> None:
    client = make_client(harness)
    assert make_client(harness).post("/v1/auth/refresh").status_code == 401
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    token = login.json()["token"]
    refreshed = client.post("/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert refreshed.status_code == 200, refreshed.text
    fresh = refreshed.json()["token"]
    assert fresh
    ok = make_client(harness).get("/v1/status", headers={"Authorization": f"Bearer {fresh}"})
    assert ok.status_code == 200, ok.text
    # A session that was first signed in more than 90 days ago cannot be renewed.
    signer = URLSafeTimedSerializer(harness.settings.session_secret, salt="mm-session")
    old = signer.dumps({"u": "operator", "v": "1", "o": int(time.time()) - 91 * 24 * 3600})
    refused = make_client(harness).post("/v1/auth/refresh", headers={"Authorization": f"Bearer {old}"})
    assert refused.status_code == 401


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


def test_discover_routes_are_venue_aware(harness: Harness) -> None:
    client = make_client(harness)
    login = client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    assert login.status_code == 200, login.text
    assert client.get("/v1/discover/tabs").json()["venue"] == "polymarket"  # fake venue active: old default
    kalshi = client.get("/v1/discover/tabs", params={"venue": "kalshi"}).json()
    assert kalshi["venue"] == "kalshi" and {"trending", "sports"} <= {t["id"] for t in kalshi["tabs"]}
    assert client.get("/v1/discover", params={"venue": "fake"}).status_code == 404
    assert client.get("/v1/discover", params={"venue": "kalshi", "tab": "perps"}).status_code == 404
    # Tab is valid but the venue is not enabled on this server.
    assert client.get("/v1/discover", params={"venue": "limitless", "tab": "new"}).status_code == 409


def _sse(body: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


class _ScriptedLLM:
    """Stands in for the LLM: one tool round, then a streamed answer."""

    enabled = True

    def __init__(self, tool: str, on_call: object = None) -> None:
        self.tool = tool
        self.on_call = on_call
        self.rounds = 0

    async def chat(self, messages, tools=None, on_delta=None):  # type: ignore[no-untyped-def]
        from mm_agent.llm import LLMReply

        self.rounds += 1
        if callable(self.on_call):
            self.on_call()
        if self.rounds == 1:
            return LLMReply(content="", tool_calls=({"id": "c1", "name": self.tool, "arguments": "{}"},))
        if on_delta is not None:
            await on_delta("All ")
            await on_delta("good.")
        return LLMReply(content="All good.")


def test_chat_stream_slash_command_events(harness: Harness) -> None:
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    events = _sse(client.get("/v1/chat/stream", params={"message": "/status"}).text)
    names = [name for name, _ in events]
    assert names[0] == "status" and names[-2:] == ["message", "done"]
    assert "FAKE" in str(events[-2][1]["reply"])


def test_chat_stream_reports_tool_progress_and_tokens(harness: Harness) -> None:
    harness.chat.llm = _ScriptedLLM("get_status")  # type: ignore[assignment]
    client = make_client(harness)
    client.post("/v1/auth/login", json={"username": "operator", "password": "test-password"})
    events = _sse(client.get("/v1/chat/stream", params={"message": "how are we doing"}).text)
    tools = [(p["name"], p["state"]) for name, p in events if name == "tool"]
    assert tools == [("get_status", "running"), ("get_status", "done")]
    assert "".join(str(p["text"]) for name, p in events if name == "delta") == "All good."
    assert events[-2] == ("message", events[-2][1]) and events[-2][1]["reply"] == "All good."


async def test_stop_prevents_a_new_tool_from_starting(harness: Harness) -> None:
    stop = {"now": False}
    # The operator presses Stop while the model is deciding to pause the agent.
    harness.chat.llm = _ScriptedLLM("pause_agent", on_call=lambda: stop.update(now=True))  # type: ignore[assignment]
    result = await harness.chat.handle(
        "pause everything", actor="web:operator", on_event=_noop, should_stop=lambda: stop["now"]
    )
    assert result["data"]["stopped"] is True
    assert harness.agent.status()["paused"] is False


async def _noop(kind: str, payload: dict[str, object]) -> None:
    return None
