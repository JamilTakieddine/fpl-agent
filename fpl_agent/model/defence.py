"""Phase 2 step 4: defensive events per player per match.

(docs/decisions.md D21; thresholds verified in scoring_rules.py)
- Goals conceded: the opponent's goals (own goals included) whose minute falls inside the
  player's time on the pitch, from step 3's goal minutes. Exact to the minute, so a defender
  subbed off at 70 while leading 1-0 keeps his clean sheet if his team concedes at 80.
- Clean sheet: 0 conceded while on the pitch and CLEAN_SHEET_MIN_MINUTES+ minutes.
- DEFCON: count ~ negative binomial (mean = player's CBIT/CBIRT per 90 x minutes / 90, variance
  = DEFCON_DISPERSION x mean; D27 replaced Poisson); awarded once at the
  position's threshold. Player-specific because the differences are BETWEEN players (a ball-winning
  centre-back vs an attacking full-back); within a player the counts look roughly Poisson.
  Shrinkage is LIGHT (DEFCON_PRIOR_MINUTES = 45): out of sample (rates from GW1-4 predicting GW5
  threshold hits, 118 appearances) less shrinkage was strictly better, because defensive-action
  rate is a stable trait of a player's role and the position mean mixes very different roles.
  45 minutes is nearly as good as none and still guards against cameo-only samples. Not scaled by
  the opponent yet: plausible, but unmeasured (Phase 2 validation).
- Saves (goalkeepers): ~ Poisson(keeper's shrunk saves per 90 x opponent expected goals / league
  mean x minutes / 90). Drawn independently of goals conceded (correlation only +0.10 in GW1-5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import Fixture, Player, PositionCode
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.minutes import FULL_MATCH, MinutesSamples
from fpl_agent.model.scoreline import FixtureRates
from fpl_agent.model.scoring_rules import CLEAN_SHEET_MIN_MINUTES, DEFCON_THRESHOLD

DEFAULT_GOALS_PER_TEAM = 1.35  # before any match: typical Premier League scoring per team
DEFCON_PRIOR_MINUTES = 45.0  # light: chosen out of sample (see module docstring and D21)
# Tuned out of sample (D27): saves error on the 2025/26 back-test fell steadily with heavier
# shrinkage (1.534 at 90 minutes, 1.518 at 270, 1.478 at 5000) and levelled off; confirmed on the
# held-out 2026/27. A keeper's saves depend mainly on the shots his defence allows, which the
# opponent-attack factor already covers, so his own save history adds little.
SAVES_PRIOR_MINUTES = 5000.0
# DEFCON counts vary more than Poisson: measured on the 2025/26 back-test (4,532 full-90 outfield
# appearances) as mean((actual - rate)^2 / rate) = 1.55 with unbiased rates (8.10 vs 8.13); the
# held-out 2026/27 GW2-5 gives 1.70. Hits live in the tail (10+/12+), so Poisson under-counted
# them by ~10%. Counts are negative binomial with this variance/mean ratio (D27).
DEFCON_DISPERSION = 1.55


@dataclass(frozen=True)
class DefenceRates:
    dc90: dict[int, float]  # DEFCON actions per 90 (CBIT for DEF, CBIRT for MID/FWD), shrunk
    saves90: dict[int, float]  # goalkeepers only, shrunk
    goals_per_team: float  # league mean goals per team per match (the opponent-attack baseline)


def _shrunk_per_90(total: float, minutes: int, mean_per_90: float, prior_minutes: float) -> float:
    return (total + mean_per_90 * prior_minutes / 90) / ((minutes + prior_minutes) / 90)


def defence_rates(
    players: list[Player], fixtures: list[Fixture], goalkeeper: int = 1
) -> DefenceRates:
    by_pos: dict[int, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.element_type, []).append(p)

    def mean_per_90(ps: list[Player], field: str) -> float:
        minutes = sum(p.minutes for p in ps)
        return sum(getattr(p, field) for p in ps) / (minutes / 90) if minutes else 0.0

    dc_mean = {et: mean_per_90(ps, "defensive_contribution") for et, ps in by_pos.items()}
    saves_mean = mean_per_90(by_pos.get(goalkeeper, []), "saves")

    played = [f for f in fixtures if f.finished and f.team_h_score is not None]
    goals_per_team = (
        sum((f.team_h_score or 0) + (f.team_a_score or 0) for f in played) / (2 * len(played))
        if played
        else DEFAULT_GOALS_PER_TEAM
    )
    return DefenceRates(
        dc90={
            p.id: _shrunk_per_90(
                p.defensive_contribution, p.minutes, dc_mean[p.element_type], DEFCON_PRIOR_MINUTES
            )
            for p in players
        },
        saves90={
            p.id: _shrunk_per_90(p.saves, p.minutes, saves_mean, SAVES_PRIOR_MINUTES)
            for p in players
            if p.element_type == goalkeeper
        },
        goals_per_team=goals_per_team,
    )


@dataclass(frozen=True)
class DefenceSamples:
    """Aligned with MinutesSamples rows; arrays are (rows, n_sims)."""

    goals_conceded: NDArray[np.int64]  # while on the pitch
    clean_sheet: NDArray[np.bool_]
    defcon_count: NDArray[np.int64]
    defcon_award: NDArray[np.bool_]  # reached the position's threshold (at most once per match)
    saves: NDArray[np.int64]  # 0 for outfield players


def overdispersed_counts(
    mean: NDArray[np.float64], dispersion: float, rng: np.random.Generator
) -> NDArray[np.int64]:
    """Counts with the given mean and variance = dispersion x mean (negative binomial).

    dispersion == 1 is Poisson. NB(n, p) with n = mean / (dispersion - 1), p = 1 / dispersion has
    exactly that mean and variance; zero means give zero counts.
    """
    if dispersion <= 1.0:
        return rng.poisson(mean).astype(np.int64)
    safe = np.where(mean > 0, mean, 1.0)
    counts = rng.negative_binomial(safe / (dispersion - 1.0), 1.0 / dispersion)
    return np.where(mean > 0, counts, 0).astype(np.int64)


def simulate_defence(
    attack: AttackSamples,
    minutes: MinutesSamples,
    teams: Mapping[int, int],
    positions: Mapping[int, PositionCode],
    fixtures: Mapping[int, Fixture],
    rates: Mapping[int, FixtureRates],
    defence: DefenceRates,
    rng: np.random.Generator,
) -> DefenceSamples:
    n_rows, n_sims = int(minutes.minutes.shape[0]), int(minutes.minutes.shape[1])
    conceded = np.zeros((n_rows, n_sims), dtype=np.int64)
    on_from = minutes.on_from
    off_at = on_from + minutes.minutes
    opp_attack = np.zeros(n_rows)  # opponent expected goals / league mean, per row

    for r in range(n_rows):
        pid, fid = int(minutes.player[r]), int(minutes.fixture[r])
        f = fixtures[fid]
        opponent = f.team_a if teams[pid] == f.team_h else f.team_h
        times = attack.goal_minutes.get((fid, opponent))
        if times is not None and times.shape[0] > 0:
            inside = (times >= on_from[r][None, :]) & (times < off_at[r][None, :])
            conceded[r] = inside.sum(axis=0)
        rate = rates[fid]
        opp_lambda = rate.lambda_away if teams[pid] == f.team_h else rate.lambda_home
        opp_attack[r] = opp_lambda / defence.goals_per_team

    played_share = minutes.minutes / FULL_MATCH
    clean_sheet = (conceded == 0) & (minutes.minutes >= CLEAN_SHEET_MIN_MINUTES)

    dc_rate = np.array([defence.dc90.get(int(p), 0.0) for p in minutes.player])[:, None]
    defcon_count = overdispersed_counts(dc_rate * played_share, DEFCON_DISPERSION, rng)
    thresholds = np.array(
        [DEFCON_THRESHOLD[positions[int(p)]] or np.iinfo(np.int64).max for p in minutes.player]
    )[:, None]
    defcon_award = defcon_count >= thresholds

    save_rate = np.array([defence.saves90.get(int(p), 0.0) for p in minutes.player])[:, None]
    saves = rng.poisson(save_rate * opp_attack[:, None] * played_share).astype(np.int64)

    return DefenceSamples(
        goals_conceded=conceded,
        clean_sheet=clean_sheet,
        defcon_count=defcon_count,
        defcon_award=defcon_award,
        saves=saves,
    )
