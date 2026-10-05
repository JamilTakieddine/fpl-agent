# Planner back-test

*2025-26, GW2-38, by `python -m fpl_agent.validation.planner_backtest`. Method: D40 in `decisions.md`.*

Four policies from the same realistic GW1 squad (the most-owned legal 15 within the budget), deciding each week from what was known before its deadline and scored on real results. Free transfers only, at most 3 a week, the same lineup picker for all.

| Policy | Real points | Transfers | Chips played |
|---|---|---|---|
| Agent (planner + chips) | **2157** | 33 | wildcard GW2, 3xc GW6, bboost GW15, freehit GW18, wildcard GW22, 3xc GW26, freehit GW33, bboost GW36 |
| Agent without chips | **2064** | 37 | - |
| Part 5 one-week logic | **2025** | 37 | - |
| Hold (no transfers) | **1536** | 0 | - |

| Comparison | Points over the season (± 1 s.e.) |
|---|---|
| Agent vs hold | +621 (± 141) |
| Agent vs part 5 | +132 (± 81) |
| Chips' contribution (agent vs agent without chips) | +93 (± 86) |
| Part 5 vs hold | +489 (± 126) |

## Caveats

- One season, one starting squad: a single path, so luck matters. The s.e. is from the weekly differences.
- No hits, no price-change strategy, prices as the archive recorded them.
- No injury flags in the archive: the rebuilt weeks see everyone as fit (as in D35).
- Later weeks use the xG-ratings fallback, as live; that week uses closing odds.
