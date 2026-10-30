#!/usr/bin/env python
"""Run the venue doctor from a source checkout: python scripts/doctor.py --venue kalshi."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mm_agent.doctor import main

if __name__ == "__main__":
    raise SystemExit(main())
