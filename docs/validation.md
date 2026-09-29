# Validation report

*Generated 2026-09-29 by `python -m fpl_agent.validation --report`. Decisions and method: D24–D26 in `decisions.md`. An interactive version with charts is published as an HTML page.*

## Verdict

The simulation beats both "just use form" benchmarks in both seasons, on accuracy and on ranking players within a gameweek, and its probabilities match how often things actually happen. Three calibration gaps are left for 7d: draws, high-scoring teams and DEFCON.

## Expected points vs benchmarks

Same player-gameweeks for all three. Error = mean absolute error (lower is better); ranking = Spearman correlation within each gameweek, averaged (higher is better). Cells read model / points per game / last-3 form.

| Slice | n | Error | Ranking | Model bias |
|---|---|---|---|---|
| 2025/26, all players with history | 17,164 | **1.64** / 2.14 / 2.21 | **0.540** / 0.385 / 0.346 | -0.03 |
| 2025/26, likely starters | 7,824 | **2.31** / 2.57 / 2.75 | **0.288** / 0.176 / 0.152 | +0.09 |
| 2025/26, GW2–6 | 1,834 | **1.92** / 2.29 / 2.29 | **0.398** / 0.327 / 0.320 | +0.00 |
| 2025/26, GW7+ | 15,330 | **1.60** / 2.13 / 2.20 | **0.562** / 0.394 / 0.350 | -0.03 |
| 2026/27 held out, likely starters | 874 | **2.46** / 2.89 / 2.88 | **0.231** / 0.175 / 0.174 | +0.04 |

Likely starters: given at least a 50% chance to start before the gameweek.

## Picks and captaincy (2025/26, likely starters with history)

- **Top 10:** the model's top 10 averaged **5.19** actual points each, points-per-game's top 10 4.10; the model's was better in 28 of 37 gameweeks.
- **Captain (doubled):** model 12.70 per gameweek vs 11.95; **+28 points** over the season.

## Event probabilities (2025/26)

| Event | Predicted | Actual | Brier | Skill vs base rate |
|---|---|---|---|---|
| start | 0.280 | 0.280 | 0.0974 | +0.517 |
| 60+ minutes | 0.262 | 0.262 | 0.0980 | +0.493 |
| goal | 0.031 | 0.031 | 0.0269 | +0.097 |
| assist | 0.030 | 0.030 | 0.0275 | +0.053 |
| clean sheet (GK/DEF) | 0.076 | 0.075 | 0.0590 | +0.155 |
| DEFCON (outfield) | 0.049 | 0.054 | 0.0422 | +0.176 |
| 6+ points | 0.071 | 0.070 | 0.0557 | +0.143 |
| 10+ points | 0.019 | 0.018 | 0.0168 | +0.053 |

## Distribution honesty (PIT, 10 bins; honest = 0.10 each)

- 2025/26: 0.10 0.10 0.10 0.09 0.10 0.10 0.10 0.10 0.11 0.11
- 2026/27 held out: 0.10 0.11 0.10 0.09 0.08 0.09 0.09 0.09 0.11 0.14

## Scorelines (2025/26)

- Win/draw/loss Brier 0.612 (coin flip 0.667).
- Goals predicted 1060 vs actual 1021.
- **Draws predicted 0.230 vs actual 0.273** (370 matches).
- Team goals by expected goals (expected → scored): 0.78 → 0.62 (n 64), 1.06 → 1.20 (n 180), 1.34 → 1.31 (n 211), 1.64 → 1.40 (n 137), 1.99 → 1.92 (n 111), 2.46 → 2.24 (n 37)

## By position (2025/26, likely starters)

| Position | n | Error | Ranking | Model bias |
|---|---|---|---|---|
| GKP | 714 | **2.13** / 2.40 / 2.51 | **0.291** / 0.135 / 0.148 | +0.06 |
| DEF | 3,020 | **2.41** / 2.72 / 2.91 | **0.307** / 0.151 / 0.115 | +0.16 |
| MID | 3,343 | **2.21** / 2.42 / 2.63 | **0.270** / 0.208 / 0.180 | +0.08 |
| FWD | 747 | **2.50** / 2.77 / 2.91 | **0.242** / 0.180 / 0.204 | -0.15 |

## To tune in 7d

1. **Draws:** 23.0% predicted vs 27.3% actual; a Dixon-Coles correction.
2. **High-scoring teams:** teams expected to score 1.6+ scored about 0.2 fewer.
3. **DEFCON:** 4.9% predicted vs 5.4% actual.

## Limitations

- No historical injury flags: everyone counts as available, which understates the live agent.
- 2025/26 closing odds are set slightly after FPL's deadline.
- 2026/27 is 4 gameweeks: indicative only.
