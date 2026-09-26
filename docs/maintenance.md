# Maintenance schedule

What needs re-checking, how often, and why. Most things are read fresh on every run and need no
manual check. This list is only for the numbers and assumptions baked into the code.

## Automatic (every run, nothing to do)

| What | Why it's automatic |
|---|---|
| Deadlines, double/blank gameweeks | Recomputed from `/fixtures/` + `deadline_time` each run (D10). |
| Scoring values, chip windows | Read from `bootstrap-static` each run (rule 8, rule 3). |
| Player availability flags | Read each run and saved as a snapshot (D14). |
| Access-token refresh | Handled by `TokenManager` (D5, D8, D12). |

## Periodic checks

| What | How often | How | Why |
|---|---|---|---|
| `SURPRISE_NON_START` (currently **0.10**) | **Every 5 gameweeks**: GW11, 16, 21, 26, 31. Also after heavy rotation periods (festive GW17–20, European knockouts ~GW25–30). | Share of players who were unflagged in the pre-deadline snapshot and started all their team's matches in the previous 4 gameweeks, but didn't start the next one. | Measured on one gameweek (n = 130, about ±3 points). The sample grows by roughly 130 per gameweek, so by GW11 it's about 650 (about ±1.2 points). From GW6 onwards, snapshots let us use the real flag *at the time* instead of today's flag as a stand-in. Build the script at GW11. |
| `WINDOW_GWS` (6) and `PRIOR_MATCHES` (1) | Once, in the Phase 2 backtest; then once a season. | Compare prediction accuracy for other values. | Reasonable starting values, not yet tested. |
| Test data (`tests/fixtures/`) | **Start of each season**, and whenever the live check fails with a pydantic validation error. | `python scripts/record_fixtures.py` after refreshing `data/cache/`. | FPL changes its API between seasons. A validation error on arrival is the signal. |
| Auth behaviour: access token 1h, refresh token 180 days (extends with use), reuse detection | **Once per season.** | Phase 0 spikes. `phase1_reuse_detection.py --delay 180` revokes your login on purpose, so only run it when you're at your Mac. | The login provider can change these, and D5/D12 rely on them. |
| Kalshi team-name table (`KALSHI_TO_FPL_NAME`) | **Start of each season** (promoted teams bring new names), and whenever the live check reports `unknown team name`. | Add the new Kalshi name → FPL name pair. | Mapping is explicit on purpose: unknown names are skipped, not guessed (D15). |
| Kalshi quality gate (`MAX_SPREAD` 0.06, `MIN_VOLUME` 1000, `TOTALS_PRICE_RANGE` 0.03–0.97) | Once in the Phase 2 calibration; then when coverage near deadline looks low. | Compare priced-fixture coverage and odds accuracy at other thresholds. | Starting values chosen from one day's data. |
| Poisson draw bias (`draw_gap` in `MatchOdds`) | Once ~5 gameweeks of totals-based odds exist (Phase 2 calibration). | Average `draw_gap` over matches with `total_source="totals"`. Consistently negative means Poisson underrates draws, so consider Dixon-Coles. | Decides whether the simple model needs a correction. |
| Flag snapshots are being written | **Weekly** until Phase 4; then the agent checks this itself and alerts. | `ls data/snapshots/`: there should be one file per gameweek. | A missed gameweek can't be recorded afterwards. |

## Rules of thumb

- **Prefer alerts over calendar reminders.** A validation error, a `RateLimitedError`, or a missing snapshot
  should raise an alert (Phase 4 emails), rather than relying on someone remembering to look.
- **Re-measure, don't hard-cap.** If a measured constant moves, find out why (rotation, fixture congestion)
  before changing it.
