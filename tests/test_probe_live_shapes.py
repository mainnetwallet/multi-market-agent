from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import httpx

_SPEC = importlib.util.spec_from_file_location(
    "probe_live_shapes", Path(__file__).resolve().parents[1] / "scripts" / "probe_live_shapes.py"
)
assert _SPEC is not None and _SPEC.loader is not None
probe: ModuleType = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)


def test_scrub_drops_credential_keys_recursively() -> None:
    out = probe.scrub({"a": 1, "apiKey": "x", "n": [{"secret": "y", "ok": 2}], "Authorization": "z"})
    assert out == {"a": 1, "n": [{"ok": 2}]}


def test_synthetic_fixtures_marked_and_clean(tmp_path: Path) -> None:
    paths = probe.write_synthetic(tmp_path)
    assert len(paths) >= 10
    for path in paths:
        body = json.loads(path.read_text(encoding="utf-8"))
        assert body["synthetic"] is True
        assert "ghp_" not in path.read_text(encoding="utf-8")


def test_synthetic_soccer_has_away_team_listed_first() -> None:
    soccer = probe.synthetic_fixtures()["polymarket/soccer_main"]
    assert soccer["teams"][0]["ordering"] == "away"
    assert len(soccer["markets"]) == 3


def test_probe_is_read_only_and_survives_unreachable_venues(tmp_path: Path) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if "gamma-api" in request.url.host and request.url.path == "/sports":
            return httpx.Response(200, json=[{"sport": "nba", "tags": "1,745"}])
        return httpx.Response(503)

    async def go() -> object:
        probe.PAUSE = 0
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe.run(tmp_path, client)

    prober = asyncio.run(go())
    assert set(methods) <= {"GET"}  # no POST, so no order can leave this script
    assert (tmp_path / "polymarket" / "sports.json").exists()
    assert prober.failures
