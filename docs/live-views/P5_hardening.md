# P5 - Hardening, load test, docs
Follow `00_CONTEXT.md` session protocol. Needs P1-P4 (whatever is done). Do **only** this phase, then STOP.
Read only: `WORKLOG.md` and the `live/` package.

## Do
- Load test script (about 200 simulated clients on 50 events) with CPU and memory numbers; verify coalescing, queue limits and idle eviction under load.
- Count REST calls per minute for a typical session before vs after (old pollers vs stream) and write the numbers in `WORKLOG.md`.
- Failure drills with fakes: feed silent, feed reconnect storm, venue 429 with `Retry-After`, malformed frames; confirm stale flags and recovery, no crash, no unbounded memory.
- Security review of new routes: auth required, URL allow-list, no secrets in payloads/logs, per-client caps.
- Update `README.md` (venue capability table, live-data section, new endpoints, corrected Limitless history claim, `LIVE_VIEWS_ENABLED`) and `DOCS_FINDINGS.md`.
- Full gate. Final report: what was verified live vs only from docs or synthetic fixtures (and what the owner must still run once), measured latency per feed (venue event -> client paint), REST counts before/after, known gaps.
  Honest note: "no delay" means push-based and sub-second from the venue feed; venue data itself can lag, and the UI shows source and age.

## Done when
Numbers recorded, docs updated, gate green, Handoff written. STOP.
