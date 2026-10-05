# Maintenance schedule

What needs re-checking, how often, and why. Most things are read fresh on every run and need no
manual check. This list is only for the numbers and assumptions baked into the code.

## Automatic (every run, nothing to do)

| What | Why it's automatic |
|---|---|
| Deadlines, double/blank gameweeks | Recomputed from `/fixtures/` + `deadline_time` each run (D10). |
| Scoring values, chip windows | Read from `bootstrap-static` each run (rule 8, rule 3). |
| Player availability flags and ownership | Read each run and saved as a snapshot (D14, D30). |
| Access-token refresh | Handled by `TokenManager` (D5, D8, D12). |

## Periodic checks

| What | How often | How | Why |
|---|---|---|---|
| `SURPRISE_NON_START` (currently **0.10**) | **Every 5 gameweeks**: GW11, 16, 21, 26, 31. Also after heavy rotation periods (festive GW17–20, European knockouts ~GW25–30). | Share of players who were unflagged in the pre-deadline snapshot and started all their team's matches in the previous 4 gameweeks, but didn't start the next one. | Measured on one gameweek (n = 130, about ±3 points). The sample grows by roughly 130 per gameweek, so by GW11 it's about 650 (about ±1.2 points). From GW6 onwards, snapshots let us use the real flag *at the time* instead of today's flag as a stand-in. Build the script at GW11. |
| Surprise non-start **per position** | With the GW11 re-measurement. | Same measurement as above, split by GK / DEF / MID / FWD. | The 10% is pooled across positions, and goalkeepers are probably lower. The starter top-up already hands a regular keeper back some of it (D19). |
| `WINDOW_GWS` (6) and `PRIOR_MATCHES` (1) | Once, in the Phase 2 backtest; then once a season. | Compare prediction accuracy for other values. | Reasonable starting values, not yet tested. |
| Attacking-event assumptions (D20) | In the Phase 2 validation step; then once a season. | Compare simulated vs actual goal and assist shares by position; check whether late-goal timing or team-specific assist rates improve accuracy. The xA factors and the assist and own-goal rates rebuild themselves from season data every run. | Uniform goal timing and season-wide rates are simplifications. |
| Scoring thresholds (`scoring_rules.py`): DEFCON 10/12, saves per 3, conceded per 2, clean sheet 60 min | **Start of each season.** | Re-run the D21 verification against `explain` in `/event/{gw}/live/` after GW1–2. | The API exposes point values but not thresholds; FPL changes rules between seasons. |
| Every shrinkage strength and window (attack 270, saves 5,000, cards/penalty saves 270, DEFCON 45; lineup window 2, prior 1) and `DEFCON_DISPERSION` 1.55 | **Once a season**, and after any model change. | `python scripts/sweep_parameters.py` (+ `--followup`), then confirm on held-out gameweeks before adopting (D27). | Tuned on 2025/26 in D27; rosters and managers' habits drift between seasons. |
| Bonus model (D23): BPS fit quality, keep factor | Phase 2 validation; then every ~5 gameweeks. | Out-of-sample base-BPS error by keep factor (fit on earlier gameweeks, predict the next); bonus handed out per match and by position vs actual. | One gameweek of evidence so far; the error curve is flat between 0.25 and 0.5. |
| DEFCON vs opponent strength | Phase 2 validation. | Check whether defensive-action counts rise against stronger opponents; if so, scale the rate. | Plausible but unmeasured. |
| Back-testing archives (D24) | **Each summer**, once the finished season is archived. | `python scripts/fetch_history.py <season>` then `python scripts/check_history.py <season>`: expect OK (scoring reproduced 100%, scores reconcile, all matches priced, no leaks). | Archives can have duplicates or gaps (2025/26 had 10 duplicate rows); rules can change between seasons. |
| Back-test (D25) | **Every ~5 gameweeks** in season (fetch new Kalshi history, refresh live cache), and after any model change. | `python scripts/fetch_kalshi_history.py`, then `python -m fpl_agent.validation --report` (updates `docs/validation.md`; republish the HTML page). Compare with D25/D26: bias near 0, beats both benchmarks, flat PIT. | A regression shows up as bias, a lost benchmark lead, or a bent PIT. |
| Rules engine vs FPL (D28) | **Start of each season**, after GW2–3. | Fetch league picks into `data/cache/picks/`, then replay them as in D28: auto-subs and official points must match 100%. `scripts/record_fixtures.py` refreshes the 9 regression cases. | FPL can change auto-sub, chip or captaincy rules between seasons. |
| Opponent captain spread (habit 0.85, temperature 1.0) and AVERAGE scale (0.9) (D30) | **Every ~5 gameweeks** (GW11, 16, ...), after refreshing the back-test for the new gameweeks. | `python scripts/fit_opponent.py`: compare the fit with the values in use. Change a constant only if the fit moves clearly, and record it (Q6). The back-test's current-season range (`fpl_agent/validation/__main__.py`, GW2–5 today) must be extended first, or new gameweeks have no expected points. | 44 decisions and 4 average-score weeks so far; each gameweek adds 10 decisions and one ownership-snapshotted week. |
| Opponent chip chances (30–80%, D30) | **After each double gameweek, and GW18–19**; rethink before GW20. | For each league opponent: did they hold Triple Captain / Bench Boost, and did they play it? Compare with the predicted chance. | Starting values, reasoned rather than fitted (Q6). |
| Doubtful-then-absent excusal (D33) | **GW11**, with the `SURPRISE_NON_START` re-measurement. | From snapshots: of players doubtful at a deadline who then played 0 minutes, how many started the next match? If many didn't, those were rotation, not injury. | Approved as a reasoned change; the back-test can't measure it (no historical flags). |
| Flag-duration rules for later weeks (D32: dated returns, a knock lasts one gameweek, undated injuries last the horizon) | **Every ~5 gameweeks**, with the snapshot re-measurements. | From consecutive snapshots: how many doubtful players were fit a week later, and did players return on their "Expected back" date? | Reasoned, not fitted; they decide which injured players the transfer advice sells. |
| Chip keep-values (D39, Q10): BB/TC 1.25 × a typical week, Wildcard 15, Free Hit 12 | **About GW12**, before the GW15–19 run-in when first-set chips must be used. | Read the weekly "keep it / play it" lines in the summary emails. Did the agent hold chips through weeks where the simulations say they'd have gained more than the keep-value? Adjust the values in `fpl_agent/optimize/planner.py`. | Starting points, not fitted. |
| Hits switch for automatic transfers (D37) | **GW10–11 review**, with the Q9 rerun. | Decide whether to set `FPL_ALLOW_HITS=1` in the job (`gcloud run jobs update fpl-agent --update-env-vars FPL_ALLOW_HITS=1`). | Free transfers only until the agent's decisions are proven. |
| Optimizer back-test vs the league's managers (D35, Q9) | **GW10–11**, then every ~5 gameweeks. | Refresh `data/cache/` (league picks, H2H results, live gameweeks, extend the GW range in `fpl_agent/validation/optimizer.py`), then `python -m fpl_agent.validation.optimizer` and republish the HTML page. | GW2–5 was confounded by missing flags; from GW6 the comparison is fair. |
| Test data (`tests/fixtures/`) | **Start of each season**, and whenever the live check fails with a pydantic validation error. | `python scripts/record_fixtures.py` after refreshing `data/cache/`. | FPL changes its API between seasons. A validation error on arrival is the signal. |
| Auth behaviour: access token 1h, refresh token 180 days (extends with use), reuse detection | **Once per season.** | Phase 0 spikes. `phase1_reuse_detection.py --delay 180` revokes your login on purpose, so only run it when you're at your Mac. | The login provider can change these, and D5/D12 rely on them. |
| Kalshi team-name table (`KALSHI_TO_FPL_NAME`) | **Start of each season** (promoted teams bring new names), and whenever the live check reports `unknown team name`. | Add the new Kalshi name → FPL name pair. | Mapping is explicit on purpose: unknown names are skipped, not guessed (D15). |
| Kalshi quality gate (`MAX_SPREAD` 0.06, `MIN_VOLUME` 1000, `TOTALS_PRICE_RANGE` 0.03–0.97) | Once in the Phase 2 calibration; then when coverage near deadline looks low. | Compare priced-fixture coverage and odds accuracy at other thresholds. | Starting values chosen from one day's data. |
| Poisson draw bias (`draw_gap` in `MatchOdds`) | Once ~5 gameweeks of totals-based odds exist (Phase 2 calibration). | Average `draw_gap` over matches with `total_source="totals"`. Consistently negative means Poisson underrates draws, so consider Dixon-Coles. | Decides whether the simple model needs a correction. |
| Flag snapshots and saved odds are being written | **Weekly** until Phase 4; then the agent checks this itself and alerts. | `ls data/snapshots/ data/odds/`: one file per gameweek in each. | A missed gameweek's flags can't be recorded afterwards, and saved odds are the fallback for missing markets. |

## Rules of thumb

- **Prefer alerts over calendar reminders.** A validation error, a `RateLimitedError`, or a missing snapshot
  should raise an alert (Phase 4 emails), rather than relying on someone remembering to look.
- **Re-measure, don't hard-cap.** If a measured constant moves, find out why (rotation, fixture congestion)
  before changing it.
