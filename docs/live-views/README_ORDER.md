# How to run these prompts
Copy this folder into the repo as `docs/live-views/` (commit it), then give the agent ONE launcher line per session, in this order:

1. `Read docs/live-views/00_CONTEXT.md and docs/live-views/P0_probe.md and execute P0 now. Do not ask questions. Stop when P0 is done.`
2. same with `P1_eventview.md`
3. same with `P2a_hub_polymarket.md`
4. same with `P2b_hub_kalshi_limitless.md`
5. same with `P3_pwa.md`
6. same with `P4_android.md`
7. same with `P5_hardening.md`

Start a fresh session for each one: state is carried by `WORKLOG.md` and git, so nothing is lost. If your limit runs out mid-phase, the next session continues from the Handoff in `WORKLOG.md`
(launcher: `Read docs/live-views/00_CONTEXT.md and WORKLOG.md and continue the unfinished phase.`).
Best value first: P0 -> P1 -> P2a -> P3 gives live soccer/bracket views on the web. P2b (Kalshi/Limitless) and P4 (Android) can wait.
