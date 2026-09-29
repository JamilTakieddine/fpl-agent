"""Phase 2 step 3: who scores and who assists each simulated goal.

For every simulated team goal (docs/decisions.md D20):
1. Minute: uniform on [0, 90). FPL doesn't publish goal minutes; real goals lean slightly late.
2. Own goal with probability own_goal_share (season data): nobody on the scoring team is credited.
   The -2 for the opponent who scored it isn't simulated (~0.02 points per player per season).
3. Scorer: among the team's players ON THE PITCH at that minute (step 2), weighted by expected
   goals per 90. Penalty xG is included, so penalty takers are covered.
4. Assist with probability assist_rate (season data; FPL is generous: ~97%): a teammate on the
   pitch other than the scorer, weighted by position-adjusted expected assists per 90.

Per-player rates are shrunk towards their position's average by PRIOR_MINUTES, so a player with
one chance in a 20-minute cameo isn't treated as elite. xA is multiplied by a per-position factor
(FPL assists / xA, season to date): FPL awards assists xA doesn't count (rebounds from saves,
penalties won). In 2026/27 so far: FWD x3.08, MID x1.30, DEF x1.22 (GK x3.03 rests on one assist
and is noise, but keepers' xA is ~0). Only the RELATIVE factors matter for who assists, since
assist_rate fixes how many assists happen; forwards come out ~x2.4 relative to midfielders.
Goals use plain xG: the goals-vs-xG gaps look like finishing noise, while the assist gap comes
from FPL's rules.

Vectorised over simulations; the Python loop runs over "goal number g" per team (a handful).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import Fixture, Player
from fpl_agent.model.minutes import FULL_MATCH, MinutesSamples
from fpl_agent.model.scoreline import ScoreSamples

PRIOR_MINUTES = 270.0  # three full matches of position-average output
DEFAULT_OWN_GOAL_SHARE = 0.05  # before any match has been played
DEFAULT_ASSIST_RATE = 0.9


@dataclass(frozen=True)
class AttackRates:
    """Per-player weights (per 90 minutes) and league-wide event rates."""

    xg90: dict[int, float]
    xa90: dict[int, float]  # already position-adjusted
    own_goal_share: float  # own goals / all team goals
    assist_rate: float  # FPL assists / goals that aren't own goals
    xa_factor: dict[int, float]  # element_type -> FPL assists / xA


def _per_90(total: float, minutes: int, position_mean: float) -> float:
    """Shrink a player's per-90 rate towards his position mean by PRIOR_MINUTES."""
    return (total + position_mean * PRIOR_MINUTES / 90) / ((minutes + PRIOR_MINUTES) / 90)


def attack_rates(players: list[Player], fixtures: list[Fixture]) -> AttackRates:
    by_pos: dict[int, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.element_type, []).append(p)

    def pos_mean(ps: list[Player], field: str) -> float:
        minutes = sum(p.minutes for p in ps)
        return sum(float(getattr(p, field)) for p in ps) / (minutes / 90) if minutes else 0.0

    xg_mean = {et: pos_mean(ps, "expected_goals") for et, ps in by_pos.items()}
    xa_mean = {et: pos_mean(ps, "expected_assists") for et, ps in by_pos.items()}
    xa_factor = {}
    for et, ps in by_pos.items():
        xa = sum(p.expected_assists for p in ps)
        xa_factor[et] = sum(p.assists for p in ps) / xa if xa > 0 else 1.0

    team_goals = sum((f.team_h_score or 0) + (f.team_a_score or 0) for f in fixtures if f.finished)
    own = sum(p.own_goals for p in players)
    assists = sum(p.assists for p in players)
    if team_goals > 0:
        own_share = own / team_goals
        assist_rate = min(assists / max(team_goals - own, 1), 1.0)
    else:
        own_share, assist_rate = DEFAULT_OWN_GOAL_SHARE, DEFAULT_ASSIST_RATE

    return AttackRates(
        xg90={p.id: _per_90(p.expected_goals, p.minutes, xg_mean[p.element_type]) for p in players},
        xa90={
            p.id: _per_90(p.expected_assists, p.minutes, xa_mean[p.element_type])
            * xa_factor[p.element_type]
            for p in players
        },
        own_goal_share=own_share,
        assist_rate=assist_rate,
        xa_factor=xa_factor,
    )


@dataclass(frozen=True)
class AttackSamples:
    """Goals and assists, aligned with MinutesSamples rows: arrays are (rows, n_sims)."""

    goals: NDArray[np.int64]
    assists: NDArray[np.int64]


def _categorical(weights: NDArray[np.float64], rng: np.random.Generator) -> NDArray[np.int64]:
    """For each column, draw a row index with probability proportional to its weight; -1 where a
    column's weights are all zero (nobody eligible)."""
    cum = np.cumsum(weights, axis=0)
    total = cum[-1]
    u = rng.random(weights.shape[1]) * total
    idx: NDArray[np.int64] = (cum > u[None, :]).argmax(axis=0).astype(np.int64)
    idx[total <= 0] = -1
    return idx


def simulate_attack(
    scores: Mapping[int, ScoreSamples],
    minutes: MinutesSamples,
    teams: Mapping[int, int],
    fixtures: Mapping[int, Fixture],
    rates: AttackRates,
    rng: np.random.Generator,
) -> AttackSamples:
    """Assign every simulated goal a scorer (or own goal) and maybe an assister.

    `teams` maps player id -> team id. Fixtures are processed in id order, home side first, so a
    seed gives identical results.
    """
    n_rows, n_sims = minutes.minutes.shape
    goals = np.zeros((n_rows, n_sims), dtype=np.int64)
    assists = np.zeros((n_rows, n_sims), dtype=np.int64)
    sims = np.arange(n_sims)
    row_team = np.array([teams[int(p)] for p in minutes.player], dtype=np.int64)

    for fid in sorted(scores):
        f = fixtures[fid]
        for team, team_goals in ((f.team_h, scores[fid].home), (f.team_a, scores[fid].away)):
            rows = np.flatnonzero((minutes.fixture == fid) & (row_team == team))
            max_goals = int(team_goals.max()) if team_goals.size else 0
            if rows.size == 0 or max_goals == 0:
                continue
            on_from = minutes.on_from[rows]
            off_at = on_from + minutes.minutes[rows]
            w_goal = np.array([rates.xg90[int(p)] for p in minutes.player[rows]])[:, None]
            w_assist = np.array([rates.xa90[int(p)] for p in minutes.player[rows]])[:, None]

            for g in range(max_goals):
                happened = team_goals > g
                t = rng.uniform(0, FULL_MATCH, n_sims)
                on_pitch = (on_from <= t) & (t < off_at)
                own_goal = rng.random(n_sims) < rates.own_goal_share
                scorer = _categorical(w_goal * on_pitch, rng)
                credited = happened & ~own_goal & (scorer >= 0)
                np.add.at(goals, (rows[scorer[credited]], sims[credited]), 1)

                gets_assist = rng.random(n_sims) < rates.assist_rate
                helpers = w_assist * on_pitch
                has_scorer = scorer >= 0
                helpers[scorer[has_scorer], sims[has_scorer]] = 0.0  # can't assist yourself
                assister = _categorical(helpers, rng)
                assisted = credited & gets_assist & (assister >= 0)
                np.add.at(assists, (rows[assister[assisted]], sims[assisted]), 1)

    return AttackSamples(goals=goals, assists=assists)
