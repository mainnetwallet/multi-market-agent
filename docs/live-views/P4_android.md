# P4 - Android views
Follow `00_CONTEXT.md` session protocol. Needs P2a. Do **only** this phase, then STOP. If too big: `GameScreen` + `BracketScreen` first, log the rest, commit, stop.
Read only: `WORKLOG.md`, `android/README.md`, `data/LiveSocket.kt`, `data/ApiClient.kt`, `ui/DiscoverScreen.kt`, `ui/TradeScreen.kt`, `data/TradeSheetViewModel.kt`.

## Build
- `data/LiveStream.kt`: OkHttp WebSocket to `/v1/stream` (Bearer), same reconnect/backoff pattern as `LiveSocket.kt`, snapshot resync after reconnect.
- `EventViewModel` + a pure reducer applying `snapshot`/`patch` frames; `ui/event/{GameScreen,BracketScreen,CandidatesScreen,UpDownScreen,GenericScreen}.kt`; crests/logos via an image cache (e.g. Coil) with initials fallback;
  LIVE/DELAYED/OFFLINE indicator; row Yes/No opens the existing trade sheet pre-set. Keep `AUTO_REFRESH_MS` only as a safety net. Open from a pasted venue URL via `/v1/event/view`.
- Unit-test the reducer (patches, seq gap -> snapshot, stale flag).
- If the Android SDK is not available in the sandbox, still write code and tests and state clearly that it was **not compiled**.

## Done when
Code and reducer tests written (and compiled if the toolchain exists), gate for the Python side still green, Handoff written. STOP.
