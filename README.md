# fpl-agent

An autonomous Fantasy Premier League manager. It runs before each gameweek deadline, simulates the week,
and sets the lineup, captain, bench, transfers and chips to maximize head-to-head win probability.

Status: Phase 1 (data layer) complete: typed FPL API client with auth and token refresh, fixture
calendar with double/blank gameweek detection, H2H opponent inputs, predicted-lineup baseline with
availability-flag snapshots, and Kalshi match odds converted to expected goals.
Phase 2 (Monte Carlo simulations) in progress: step 1, scorelines from odds (live Kalshi → saved
Kalshi → xG-ratings fallback). Next: the minutes simulation.

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # runtime deps + ruff, mypy, pytest, pre-commit
playwright install chromium
pre-commit install        # ruff + mypy + hygiene checks on every commit
cp .env.example .env      # fill in FPL_ENTRY_ID and FPL_H2H_LEAGUE_ID
```

## Usage
```bash
python -m fpl_agent.data          # live read-only check: deadlines, next 6 GWs, H2H opponent,
                                  # Kalshi odds + expected goals, your team with predicted minutes
pytest                            # tests (no network; uses tests/fixtures/)
python scripts/record_fixtures.py # rebuild test fixtures from data/cache/ (e.g. new season)
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

Unofficial project. Not affiliated with the Premier League or Fantasy Premier League.
