# WORKLOG - live category views (branch feat/live-category-views)

## Baseline (before P0)
ruff check, ruff format --check, mypy src: clean. pytest: all pass (161 tests). Python 3.12.3.

## Handoff - P0 (probe)
Done:
- Plan copied to `docs/live-views/`.
- `scripts/probe_live_shapes.py`: read-only (GET only), public endpoints, 10 s timeout, scrubs credential-like keys,
  saves to `tests/fixtures/live/<venue>/<n>.json`; `--synthetic` or automatic fallback when no venue is reachable.
- `tests/test_probe_live_shapes.py`: scrub, synthetic marking, GET-only, unreachable-venue behaviour.
- `DOCS_FINDINGS.md` section 7; two outdated Limitless history claims marked superseded.
Not done / **LIVE_VERIFIED=false**: no venue reachable from the sandbox, so every fixture is synthetic and every
[INFERRED] item is unconfirmed. Owner must run `python scripts/probe_live_shapes.py` once on a real machine and commit
the result.
Decisions: synthetic soccer fixture lists the away team first to exercise score orientation; README Limitless-history
claim left for P2b/P5 (docs only).
Gotchas: no secrets written anywhere; the leaked GitHub token must be revoked.
Next step: P1 (`docs/live-views/P1_eventview.md`), against synthetic fixtures unless the owner supplies live ones.
