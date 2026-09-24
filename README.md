# fpl-agent

An autonomous Fantasy Premier League manager. It runs before each gameweek deadline, simulates the week,
and sets the lineup, captain, bench, transfers and chips to maximize head-to-head win probability.

Status: Phase 0 (auth and write-access spike).

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"   # runtime deps + ruff, mypy, pytest, pre-commit
playwright install chromium
pre-commit install        # ruff + mypy + hygiene checks on every commit
cp .env.example .env      # fill in your IDs
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

Unofficial project. Not affiliated with the Premier League or Fantasy Premier League.
