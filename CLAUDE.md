# FPL Agent

An agent that manages my Fantasy Premier League team on its own: it runs shortly before each
gameweek deadline, sets the lineup, captain, bench order and transfers, plays chips, and emails me a summary.
Public repo. I'm building it to learn, so explain non-obvious choices briefly as you go.

## Goals (in priority order)
1. Never miss a deadline.
2. Maximize win probability in my head-to-head league each week, while protecting overall rank.
3. Be a clean, readable project other FPL players could reuse.

## Fixed facts
- Team ID and league ID come from `.env` (`FPL_ENTRY_ID`, `FPL_H2H_LEAGUE_ID`). Never hardcode them.
- Deadline = `deadline_time` from `/api/bootstrap-static/` `events[]`. Never derive it from kickoff times.
- The agent runs ~15 min before the deadline. Confirmed lineups are never available by then;
  all minutes decisions use predicted lineups.
- An opponent's current-GW picks are hidden until the deadline. Model the opponent from last GW's squad
  plus a probability spread over their likely captain. Chips only if they have one left and it's a DGW.

## FPL rules the code must enforce
1. Any transfer beyond the free ones costs -4 and must beat that in expected gain. Max 5 banked free transfers.
2. Bench auto-subs go in bench slot order (not by points). The GK sub is a separate swap.
   Subs must keep a legal formation: 1 GK, at least 3 DEF, 2 MID, 1 FWD.
3. Two chip sets: first set (WC, FH, BB, TC) valid GW1-19, second set GW20-38. One chip per GW.
   Unused first-set chips are FORFEITED at the GW19 deadline. The planner must schedule them, not just flag them.
4. Captain and vice captain are chosen as a pair (the VC takes over if the captain gets 0 minutes).
5. Use per-player `selling_price` (from `/my-team/`), not purchase or current price. Sell-on fee is 50% of the rise, rounded down.
6. Max 3 players per club, hard constraint.
7. Track double and blank gameweeks from `/api/fixtures/`. DGWs are the windows for BB/TC; BGWs are the window for FH.
8. Scoring includes defensive contributions (+2 points): DEF at 10 CBIT, MID/FWD at 12 CBIRT.
   Read scoring values from `game_config.scoring` in bootstrap-static rather than hardcoding them.

## Architecture (phases)
- Phase 0 `spikes/`: throwaway tests. Auth + saving a lineup, opponent visibility, timing.
- Phase 1 `fpl_agent/data/`: FPL API client, fixture calendar (DGW/BGW), opponent history, odds, predicted lineups.
- Phase 2 `fpl_agent/model/`: Monte Carlo sims: team goals from odds, player events given the scoreline,
  minutes model (a mix of: no minutes / cameo / full match), DEFCON, bonus.
- Phase 3 `fpl_agent/optimize/`: rules engine + optimizer (lineup, C/VC, bench order, transfers, chips).
  Objective: maximize H2H win probability subject to not giving up expected points / overall rank.
- Phase 4 `deploy/`: Cloud Run job + Cloud Scheduler (the job re-points its own schedule to the next deadline),
  secrets in Secret Manager, email summary via Gmail.
- Phase 5: multi-GW transfer + chip planner (horizon 5-6 GWs, chip schedule up to GW19).

LLMs are only used at the edges (turning injury news text into flags, writing the email).
All decisions come from deterministic code that can be tested.

## Conventions
- Python 3.12, `venv` + `requirements.txt`. Type hints. Small pure functions for rules so they're unit-testable.
- `pytest` for tests. Every rule above gets a test before the optimizer uses it.
- Anything that writes to FPL defaults to DRY RUN. A real submission needs an explicit `--live` flag
  (or env `FPL_LIVE=1` in the cloud). Log the exact payload before sending it.
- Rate-limit politely: cache bootstrap-static per run, no tight loops against the FPL API.
- Record every design/architecture/technical decision that had real alternatives in `docs/decisions.md`
  (context, decision, alternatives rejected and why, consequences) in the same change that makes it.
  Update "Open questions" there when a question is answered or a new one appears.

## Security (public repo)
- Never commit `.env`, `.secrets/`, tokens, cookies, or my email/password. Check `git status` before committing.
- Credentials come from env vars locally and Secret Manager in the cloud.
