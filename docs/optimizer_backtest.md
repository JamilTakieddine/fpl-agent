# Optimizer back-test

*Generated 2026-10-04 by `python -m fpl_agent.validation.optimizer`. Method and caveats: D35 in `decisions.md`. An interactive version is published as an HTML page.*

Every manager in the H2H league, GW2-5: **44 team-weeks**. Same squad, three lineups, each scored on real results (real minutes, the manager's own chip and hits): what the manager played, the agent's recommendation, and the highest-expected-points lineup.

## Real points per team-week

| Comparison | Mean difference |
|---|---|
| Agent vs manager | **-2.18 (95% -4.7 to +0.4)** |
| Highest expected points vs manager | -1.80 (95% -4.2 to +0.6) |
| Agent vs highest expected points | -0.39 (95% -0.9 to +0.1) |
| What the model expected the agent to gain | +1.08 (95% +0.8 to +1.4) |

## H2H results against the real opponent score

| Lineup | W-D-L |
|---|---|
| Manager's own | 21-0-23 (63 league pts) |
| Agent | **19-0-25 (57 league pts)** |
| Highest expected points | 18-2-24 (56 league pts) |

The agent's lineup changed the result in 6 team-weeks: 2 better, 4 worse.

## Captain and lineup choices

- Same captain as the manager: 52%; same starting XI: 2%.
- Armband points, agent vs manager: -0.59 (95% -1.8 to +0.6).

## Were the win chances honest?

- Predicted P(win) for the agent's lineups averaged **54.9%**; they actually won **43.2%**.
- Brier score 0.260 (lower is better; always guessing 50% scores 0.250).

## Why the agent differs

The agent changed 2.0 starters per team-week on average.

| Starters only one side picked | n | Played 0 minutes | Out two weeks running | Real points | When they played |
|---|---|---|---|---|---|
| Agent's picks | 87 | 24% | 18 | 2.44 | 3.21 |
| Manager's picks | 87 | 11% | 5 | 4.00 | 4.52 |

"Out two weeks running": 0 minutes that gameweek and the one before, almost always an injury FPL had flagged, which managers saw and the rebuilt weeks don't.

Clean comparison (only team-weeks where every swapped player played the gameweek before): agent vs manager **-1.86 (95% -5.8 to +2.1)** over 21 team-weeks.

## A humbler agent

Keep the manager's lineup unless the agent's expected gain is at least T points (T = 0 is today's agent).

| T | Switches | Agent vs manager (95%) | W-D-L |
|---|---|---|---|
| 0.0 | 43 | -2.11 (95% -4.7 to +0.4) | 19-0-25 |
| 0.5 | 27 | -0.89 (95% -2.9 to +1.2) | 21-0-23 |
| 1.0 | 17 | -0.95 (95% -2.7 to +0.8) | 20-0-24 |
| 1.5 | 15 | -0.52 (95% -2.1 to +1.1) | 21-0-23 |
| 2.0 | 10 | -0.18 (95% -1.2 to +0.9) | 20-0-24 |
| 3.0 | 2 | +0.18 (95% -0.1 to +0.5) | 21-0-23 |

## By gameweek

| GW | Team-weeks | Manager | Agent | Highest xP | Agent W-D-L | Manager W-D-L |
|---|---|---|---|---|---|---|
| 2 | 11 | 79.2 | 72.1 | 73.0 | 3-0-8 | 5-0-6 |
| 3 | 11 | 49.9 | 50.5 | 50.5 | 5-0-6 | 5-0-6 |
| 4 | 11 | 60.2 | 60.2 | 60.7 | 6-0-5 | 5-0-6 |
| 5 | 11 | 49.5 | 47.3 | 47.4 | 5-0-6 | 6-0-5 |

## Your team

| GW | You | Agent | Opponent | You | Agent |
|---|---|---|---|---|---|
| 2 | 113 | 106 | 60 | W | W |
| 3 | 58 | 49 | 60 | L | L |
| 4 | 72 | 88 | 75 | L | W |
| 5 | 40 | 41 | 66 | L | L |

## Caveats

- 44 team-weeks is a small sample: a difference inside the 95% interval could be luck. Rerun every ~5 gameweeks as the sample grows.
- The opponent captain model was fitted on these same GW2-5 picks (D30), which slightly flatters the H2H part.
- No injury flags exist before GW6, so the rebuilt weeks see everyone as fit.
- AVERAGE weeks use today's ownership (the only one available for GW2-5).
- Check: the managers' own team sheets, scored by our rules, reproduce FPL's official score in 44 of 44 team-weeks.
