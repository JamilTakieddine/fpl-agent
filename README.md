# fpl-agent

An autonomous Fantasy Premier League manager. It runs before each gameweek deadline, simulates the week,
and sets the lineup, captain, bench, transfers and chips to maximize head-to-head win probability.

Status:
- **Phase 1 (data layer): complete.** Typed FPL API client with auth and token refresh, fixture calendar
  with double/blank gameweek detection, H2H opponent inputs, predicted-lineup baseline with
  availability-flag snapshots, and Kalshi match odds converted to expected goals.
- **Phase 2 (Monte Carlo simulations): complete.** Scorelines from odds (live Kalshi → saved Kalshi → xG
  ratings, Dixon-Coles from the market's draw price), minutes, attacking and defensive events, bonus, and
  FPL points, validated against 2025/26 and early 2026/27: it beats form-based benchmarks on accuracy
  and ranking, with honest probabilities (`docs/validation.md`).
- **Phase 3 (optimizer): complete.** An FPL rules engine that reproduces FPL's own scores; a vectorized
  scorer (a lineup over 10,000 simulations in about 1 ms); the H2H opponent model; a lineup and captain
  optimizer that maximizes the chance of beating this week's opponent by 3+; 5-week transfer
  recommendations; saving the lineup behind dry-run safety checks; and a back-test against the league's
  real lineups (`docs/optimizer_backtest.md`; no proven edge over careful managers yet, fair rerun at
  GW10–11).
- **Phase 4 (cloud): complete.** A self-scheduling Cloud Run job saves the lineup before every deadline
  (`deploy/README.md`).
- **Phase 5 (planner): in progress.** 5a: the agent makes free transfers itself. 5b: a multi-week transfer
  plan solved exactly with HiGHS, checked by the simulations. Next: chips (5c), planner back-test (5d).

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,login,cloud]"   # + tooling, the browser login, Google Cloud libs
playwright install chromium
pre-commit install        # ruff + mypy + hygiene checks on every commit
cp .env.example .env      # fill in FPL_ENTRY_ID and FPL_H2H_LEAGUE_ID
```

## Usage
```bash
python -m fpl_agent.model         # simulate the next gameweek: your squad's xPts and haul chances
python -m fpl_agent.optimize      # DRY RUN: recommended lineup/captain vs this week's H2H opponent,
                                  # the save payload, plus transfer advice over the next 5 gameweeks
python -m fpl_agent.optimize --live  # ...and SAVE the recommended lineup to FPL (after the checks)
python -m fpl_agent.optimize --live --transfers  # ...and also MAKE the planned transfers (the cloud's save run does this)
python -m fpl_agent.data          # live check + records snapshots/odds: deadlines, next 6 GWs, H2H opponent,
                                  # Kalshi odds + expected goals, your team with predicted minutes
pytest                            # tests (no network; uses tests/fixtures/)
python scripts/record_fixtures.py # rebuild test fixtures from data/cache/ (e.g. new season)
python scripts/fetch_history.py   # download last season's archives for back-testing (gitignored)
python scripts/check_history.py   # verify the rebuilt season is faithful before back-testing
python scripts/fetch_kalshi_history.py  # this season's Kalshi price history (held-out back-test)
python scripts/fit_opponent.py    # refit the opponent captain model and AVERAGE scale on league data
python -m fpl_agent.validation.optimizer  # back-test the optimizer vs the league's real lineups
python -m fpl_agent.validation --report  # back-test both seasons; writes docs/validation.md
```

## Phase 0
```bash
python spikes/phase0_auth.py          # read-only checks; opens a browser to log in only if needed
python spikes/phase0_auth.py --write  # save your current lineup back unchanged
python spikes/phase0_auth.py --fresh  # ignore the saved session and log in again
python spikes/phase0_refresh.py       # swap the saved refresh token for new tokens, no browser
```
The first run opens a clean Chromium window for you to log in. The session is saved to `.secrets/`
(gitignored), and later runs reuse it without a browser until it expires.

Why it works this way, and every other design decision: [`docs/decisions.md`](docs/decisions.md).
What to re-check and how often: [`docs/maintenance.md`](docs/maintenance.md).
How well the simulation predicts real gameweeks: [`docs/validation.md`](docs/validation.md).

Unofficial project. Not affiliated with the Premier League or Fantasy Premier League.
