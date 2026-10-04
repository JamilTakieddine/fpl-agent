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
- **Phase 3 (optimizer): in progress.** Part 1 done: the FPL rules engine, which reproduces FPL's own
  auto-subs and official points for every real team-gameweek tested. Part 2 done: a vectorized scorer that
  gives a lineup's points in all 10,000 simulations in about 1 ms, matching the rules engine exactly.
  Part 3 done: the H2H opponent (last week's team sheet, a captain spread fitted on the league's real
  picks, chip chances, and FPL's overall average for "AVERAGE" weeks) scored in the same simulations.
  Part 4 done: the lineup optimizer (XI, bench order, captain, vice-captain) that maximizes the chance of
  beating this week's opponent by 3+ points without giving up more than 1 expected point; dry run only.
  Part 5 done: transfer recommendations over the next 5 gameweeks (budget, club limit, free-transfer and
  hit thresholds), recommendation only. Part 6 done: saving the lineup, DRY RUN unless `--live`, with
  checks against a fresh read of the team (deadline, squad, rules) and a read-back after saving.
  Part 7 done: back-testing the optimizer against the league's real GW2–5 lineups
  (`docs/optimizer_backtest.md`). There's no evidence yet that it beats a careful manager; the fair test
  needs GW6+ (injury flags). Next: Phase 4, deploying to the cloud.

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
python -m fpl_agent.model         # simulate the next gameweek: your squad's xPts and haul chances
python -m fpl_agent.optimize      # DRY RUN: recommended lineup/captain vs this week's H2H opponent,
                                  # the save payload, plus transfer advice over the next 5 gameweeks
python -m fpl_agent.optimize --live  # ...and SAVE the recommended lineup to FPL (after the checks)
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
