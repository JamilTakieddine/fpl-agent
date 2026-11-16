# Optimizer back-test

*Generated 2026-11-16 by `python -m fpl_agent.validation.optimizer`. Method and caveats: D35 in `decisions.md`. An interactive version is published as an HTML page.*

Every manager in the H2H league, GW6-10: **55 team-weeks**. Same squad, three lineups, each scored on real results (real minutes, the manager's own chip and hits): what the manager played, the agent's recommendation, and the highest-expected-points lineup.

## Real points per team-week

| Comparison | Mean difference |
|---|---|
| Agent vs manager | **+1.20 (95% -0.3 to +2.7)** |
| Highest expected points vs manager | +1.15 (95% -0.4 to +2.7) |
| Agent vs highest expected points | +0.05 (95% -0.6 to +0.8) |
| What the model expected the agent to gain | +0.93 (95% +0.7 to +1.2) |

## H2H results against the real opponent score

| Lineup | W-D-L |
|---|---|
| Manager's own | 28-0-27 (84 league pts) |
| Agent | **31-0-24 (93 league pts)** |
| Highest expected points | 30-0-25 (90 league pts) |

The agent's lineup changed the result in 3 team-weeks: 3 better, 0 worse.

## Captain and lineup choices

- Same captain as the manager: 47%; same starting XI: 9%.
- Armband points, agent vs manager: +1.00 (95% -0.3 to +2.3).

## Were the win chances honest?

- Predicted P(win) for the agent's lineups averaged **54.1%**; they actually won **56.4%**.
- Brier score 0.167 (lower is better; always guessing 50% scores 0.250).

## Why the agent differs

The agent changed 2.3 starters per team-week on average.

| Starters only one side picked | n | Played 0 minutes | Out two weeks running | Real points | When they played |
|---|---|---|---|---|---|
| Agent's picks | 124 | 44% | 43 | 2.21 | 3.97 |
| Manager's picks | 124 | 7% | 1 | 3.31 | 3.57 |

"Out two weeks running": 0 minutes that gameweek and the one before, almost always an injury FPL had flagged. From GW6 the rebuilt weeks see the flags too: the agent starts some flagged players ON PURPOSE when the bench covers them, since FPL's auto-sub brings the bench player in (scored on real minutes). When the bench can't cover them all, the slot scores 0 (Q14).

Clean comparison (only team-weeks where every swapped player played the gameweek before): agent vs manager **-0.43 (95% -2.4 to +1.5)** over 21 team-weeks.

## A humbler agent

Keep the manager's lineup unless the agent's expected gain is at least T points (T = 0 is today's agent).

| T | Switches | Agent vs manager (95%) | W-D-L |
|---|---|---|---|
| 0.0 | 53 | +1.25 (95% -0.3 to +2.8) | 31-0-24 |
| 0.5 | 33 | +1.31 (95% -0.1 to +2.7) | 30-0-25 |
| 1.0 | 22 | +1.02 (95% -0.1 to +2.2) | 30-0-25 |
| 1.5 | 12 | +0.53 (95% -0.4 to +1.4) | 29-0-26 |
| 2.0 | 9 | +0.07 (95% -0.4 to +0.6) | 29-0-26 |
| 3.0 | 1 | -0.11 (95% -0.3 to +0.1) | 28-0-27 |

## By gameweek

| GW | Team-weeks | Manager | Agent | Highest xP | Agent W-D-L | Manager W-D-L |
|---|---|---|---|---|---|---|
| 6 | 11 | 59.0 | 57.1 | 55.7 | 6-0-5 | 6-0-5 |
| 7 | 11 | 61.3 | 63.1 | 62.9 | 6-0-5 | 5-0-6 |
| 8 | 11 | 48.0 | 50.5 | 51.1 | 6-0-5 | 6-0-5 |
| 9 | 11 | 44.9 | 43.8 | 43.3 | 5-0-6 | 5-0-6 |
| 10 | 11 | 55.2 | 59.9 | 61.1 | 8-0-3 | 6-0-5 |

## Your team

| GW | You | Agent | Opponent | You | Agent |
|---|---|---|---|---|---|
| 6 | 59 | 64 | 56 | W | W |
| 7 | 81 | 81 | 74 | W | W |
| 8 | 47 | 47 | 35 | W | W |
| 9 | 45 | 45 | 39 | W | W |
| 10 (3xc) | 94 | 94 | 55 | W | W |

## Caveats

- 55 team-weeks is a small sample: a difference inside the 95% interval could be luck. Rerun every ~5 gameweeks as the sample grows.
- The opponent captain model was fitted on GW2-5 picks (D30), not these weeks.
- From GW6 the rebuilt weeks use the agent's own saved flags, odds and ownership (D47); for GW2-5 none existed, so those weeks saw everyone as fit.
- In your own team-weeks from GW7 the manager IS the agent (live, with fresher news), so the two lineups should mostly agree.
- Check: the managers' own team sheets, scored by our rules, reproduce FPL's official score in 55 of 55 team-weeks.
