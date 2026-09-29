# Validation report

*Generated 2026-09-29 by `python -m fpl_agent.validation --report`. Decisions and method: D24–D26 in `decisions.md`. An interactive version with charts is published as an HTML page.*

## Verdict

The simulation beats both "just use form" benchmarks in both seasons, on accuracy and on ranking players within a gameweek, and its probabilities match how often things actually happen. The tuning pass (7d) fixed the draw and DEFCON gaps and sharpened the lineup model; what remains is on the watch list below.

## Expected points vs benchmarks

Same player-gameweeks for all three. Error = mean absolute error (lower is better); ranking = Spearman correlation within each gameweek, averaged (higher is better). Cells read model / points per game / last-3 form.

| Slice | n | Error | Ranking | Model bias |
|---|---|---|---|---|
| 2025/26, all players with history | 17,164 | **1.58** / 2.14 / 2.21 | **0.585** / 0.385 / 0.346 | -0.06 |
| 2025/26, likely starters | 7,775 | **2.30** / 2.54 / 2.74 | **0.262** / 0.156 / 0.137 | -0.00 |
| 2025/26, GW2–6 | 1,834 | **1.89** / 2.29 / 2.29 | **0.420** / 0.327 / 0.320 | -0.00 |
| 2025/26, GW7+ | 15,330 | **1.54** / 2.13 / 2.20 | **0.611** / 0.394 / 0.350 | -0.07 |
| 2026/27 held out, likely starters | 869 | **2.46** / 2.89 / 2.87 | **0.240** / 0.175 / 0.175 | +0.09 |

Likely starters: given at least a 50% chance to start before the gameweek.

## Picks and captaincy (2025/26, likely starters with history)

- **Top 10:** the model's top 10 averaged **5.03** actual points each, points-per-game's top 10 4.29; the model's was better in 25 of 37 gameweeks.
- **Captain (doubled):** model 13.89 per gameweek vs 11.95; **+72 points** over the season.

## Event probabilities (2025/26)

| Event | Predicted | Actual | Brier | Skill vs base rate |
|---|---|---|---|---|
| start | 0.280 | 0.280 | 0.0868 | +0.569 |
| 60+ minutes | 0.262 | 0.262 | 0.0889 | +0.540 |
| goal | 0.031 | 0.031 | 0.0267 | +0.101 |
| assist | 0.030 | 0.030 | 0.0275 | +0.054 |
| clean sheet (GK/DEF) | 0.076 | 0.075 | 0.0586 | +0.161 |
| DEFCON (outfield) | 0.055 | 0.054 | 0.0413 | +0.194 |
| 6+ points | 0.072 | 0.070 | 0.0553 | +0.149 |
| 10+ points | 0.020 | 0.018 | 0.0168 | +0.057 |

## Distribution honesty (PIT, 10 bins; honest = 0.10 each)

- 2025/26: 0.09 0.10 0.10 0.10 0.10 0.10 0.10 0.10 0.11 0.11
- 2026/27 held out: 0.10 0.11 0.10 0.08 0.08 0.09 0.10 0.09 0.11 0.13

## Scorelines (2025/26)

- Win/draw/loss Brier 0.610 (coin flip 0.667).
- Goals predicted 1060 vs actual 1021.
- **Draws predicted 0.248 vs actual 0.273** (370 matches).
- Team goals by expected goals (expected → scored): 0.78 → 0.62 (n 64), 1.06 → 1.20 (n 180), 1.34 → 1.31 (n 211), 1.64 → 1.40 (n 137), 1.99 → 1.92 (n 111), 2.46 → 2.24 (n 37)

## By position (2025/26, likely starters)

| Position | n | Error | Ranking | Model bias |
|---|---|---|---|---|
| GKP | 722 | **2.08** / 2.37 / 2.49 | **0.231** / 0.071 / 0.093 | -0.15 |
| DEF | 2,979 | **2.43** / 2.70 / 2.89 | **0.285** / 0.142 / 0.108 | +0.07 |
| MID | 3,315 | **2.19** / 2.39 / 2.62 | **0.262** / 0.179 / 0.163 | +0.01 |
| FWD | 759 | **2.48** / 2.71 / 2.90 | **0.213** / 0.188 / 0.195 | -0.20 |

## Changed in 7d (D27)

- **Draws:** Dixon-Coles, with rho set per match from the market's draw price. Predicted draws 24.8% (the market's level) vs 27.3% actual; the remaining gap is about one standard error, so the market isn't second-guessed.
- **DEFCON:** negative-binomial counts (dispersion measured at 1.55): 5.5% predicted vs 5.4% actual.
- **Lineups:** a 2-gameweek window instead of 6 (start Brier 0.0868); fits still use 6 gameweeks of data.
- **Saves:** heavier shrinkage (5,000 minutes); attacking shrinkage stays at 270 (flat).

## Watch list

- **Goals:** 1060 predicted vs 1021 actual (about one standard error; not acted on).
- **Held-out clean sheets:** 9.9% predicted vs 11.0% actual on four gameweeks.
- **Likely-starter slices** depend on the model's own start probabilities, so they shift when the lineup model changes; compare same-row numbers across versions.

## Limitations

- No historical injury flags: everyone counts as available, which understates the live agent.
- 2025/26 closing odds are set slightly after FPL's deadline.
- 2026/27 is 4 gameweeks: indicative only.
