"""Run the PWA's JS unit tests (reducer, stream client, layouts) with node's built-in test runner.

The EventViews they render are built here by the real Python builders, so the JS is tested against the
same shapes the server sends. Skipped when node is not installed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_eventview import pm, soccer

ROOT = Path(__file__).parent


def _node() -> str | None:
    return shutil.which("node")


@pytest.mark.skipif(_node() is None, reason="node is not installed")
def test_js_unit_tests(tmp_path: Path) -> None:
    views = {
        "soccer": soccer(),
        "nba": pm("nba_game"),
        "bracket": pm("bracket_anthropic_ipo"),
        "candidates": pm("candidates"),
        "updown": pm("crypto_updown_5m"),
        "binary": pm("binary"),
    }
    for name, view in views.items():
        (tmp_path / f"{name}.json").write_text(json.dumps(view), encoding="utf-8")
    node = _node()
    assert node is not None
    run = subprocess.run(
        [node, "--test", *sorted(str(p) for p in (ROOT / "js").glob("*.test.mjs"))],
        env={"EVENTVIEW_FIXTURES": str(tmp_path), "PATH": "/usr/bin:/bin:/usr/local/bin"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert run.returncode == 0, run.stdout[-3000:] + run.stderr[-2000:]


def test_static_assets_are_packaged_and_served() -> None:
    static = ROOT.parent / "src" / "mm_agent" / "static"
    for rel in ("css/live.css", "js/main.js", "js/live.js", "js/view.js", "js/chart.js", "js/views/game.js"):
        assert (static / rel).is_file(), rel
    sw = (static / "sw.js").read_text(encoding="utf-8")
    index = (static / "index.html").read_text(encoding="utf-8")
    assert "/static/js/main.js" in index and "/static/css/live.css" in index
    for rel in ("css/live.css", "js/main.js", "js/live.js", "js/view.js", "js/chart.js"):
        assert "/static/" + rel in sw, rel
