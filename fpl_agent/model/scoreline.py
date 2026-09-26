"""Phase 2 step 1: each fixture's expected goals, and simulated scorelines.

Rates come, in order of preference, from: live Kalshi odds; the most recent SAVED Kalshi price
for that fixture (fpl_agent/data/odds_store.py, D17); otherwise an xG-based fallback
(docs/decisions.md D16, answering Q3). FPL's own attack/defence strength ratings are all 0 this
season, and ratings from actual goals were measurably worse than xG against Kalshi (mean abs
error 0.47 vs 0.39 goals, on the 8 team-rates available).

Fallback: lambda_home = home_avg * attack[home] * weakness[away], and likewise for away.
- home_avg / away_avg: this season's mean goals per match at home and away (league-wide, so
  already stable after ~50 matches).
- attack: the team's xG per match (sum of its players' xG) / league mean.
- weakness: xG conceded per 90 by the team's most-used goalkeeper / league mean.
- Both are shrunk towards 1.0 with SHRINK_MATCHES pseudo-matches of an average team.
One rating per team (splitting ~5 matches into home and away is too thin).

Scorelines are independent Poisson draws (consistent with how Phase 1 fitted the odds), from a
seeded numpy Generator so runs are reproducible and Phase 3 can compare lineups on the SAME
simulated gameweeks (common random numbers).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import Fixture, Player
from fpl_agent.data.odds import MatchOdds
from fpl_agent.data.odds_store import SavedOdds

SHRINK_MATCHES = 5.0
GOALKEEPER = 1  # element_type
DEFAULT_SIMS = 10_000


@dataclass(frozen=True)
class FixtureRates:
    fixture_id: int
    team_h: int
    team_a: int
    lambda_home: float
    lambda_away: float
    source: str  # "kalshi-{totals|draw}", "kalshi-saved-{totals|draw}" or "xg-ratings"
    odds_as_of: datetime | None = None  # when a saved price was taken (None for live / fallback)


@dataclass(frozen=True)
class RatingsModel:
    """Team ratings used when a fixture has no usable odds. 1.0 = league average."""

    home_avg: float
    away_avg: float
    attack: dict[int, float]
    weakness: dict[int, float]

    def rates(self, fixture: Fixture) -> tuple[float, float]:
        lam_h = self.home_avg * self.attack[fixture.team_h] * self.weakness[fixture.team_a]
        lam_a = self.away_avg * self.attack[fixture.team_a] * self.weakness[fixture.team_h]
        return lam_h, lam_a


def _shrunk(per_match: dict[int, float], games: dict[int, int], shrink: float) -> dict[int, float]:
    """Rate / league mean, pulled towards 1.0 by `shrink` pseudo-matches of an average team."""
    mean = sum(per_match.values()) / len(per_match)
    if mean <= 0:
        return dict.fromkeys(per_match, 1.0)
    return {
        t: (per_match[t] * games[t] + shrink * mean) / ((games[t] + shrink) * mean)
        for t in per_match
    }


def fit_ratings(
    fixtures: list[Fixture],
    players: list[Player],
    team_ids: list[int],
    shrink: float = SHRINK_MATCHES,
) -> RatingsModel:
    played = [
        f
        for f in fixtures
        if f.finished and f.team_h_score is not None and f.team_a_score is not None
    ]
    if not played:
        # Before GW1: no information, every team average (typical Premier League scoring).
        return RatingsModel(1.5, 1.2, dict.fromkeys(team_ids, 1.0), dict.fromkeys(team_ids, 1.0))

    home_avg = sum(f.team_h_score or 0 for f in played) / len(played)
    away_avg = sum(f.team_a_score or 0 for f in played) / len(played)
    games = dict.fromkeys(team_ids, 0)
    for f in played:
        games[f.team_h] += 1
        games[f.team_a] += 1

    xg = dict.fromkeys(team_ids, 0.0)
    keeper: dict[int, Player] = {}
    for p in players:
        if p.team not in xg:
            continue
        xg[p.team] += p.expected_goals
        if p.element_type == GOALKEEPER and p.minutes > (
            keeper[p.team].minutes if p.team in keeper else 0
        ):
            keeper[p.team] = p

    xg_per_match = {t: xg[t] / games[t] if games[t] else 0.0 for t in team_ids}
    xga_per_90 = {
        t: keeper[t].expected_goals_conceded / (keeper[t].minutes / 90) if t in keeper else 0.0
        for t in team_ids
    }
    return RatingsModel(
        home_avg=home_avg,
        away_avg=away_avg,
        attack=_shrunk(xg_per_match, games, shrink),
        weakness=_shrunk(xga_per_90, games, shrink),
    )


def fixture_rates(
    fixtures: list[Fixture],
    odds: dict[int, MatchOdds],
    fallback: RatingsModel,
    saved: dict[int, SavedOdds] | None = None,
) -> dict[int, FixtureRates]:
    """Expected goals for every fixture: live Kalshi, else saved Kalshi, else xG ratings.

    `saved` should already be filtered with odds_store.usable_saved_odds (kickoff unchanged).
    """
    rates = {}
    for f in fixtures:
        live, stored = odds.get(f.id), (saved or {}).get(f.id)
        as_of = None
        if live is not None:
            lam_h, lam_a, source = live.lambda_home, live.lambda_away, f"kalshi-{live.total_source}"
        elif stored is not None:
            lam_h, lam_a = stored.lambda_home, stored.lambda_away
            source, as_of = f"kalshi-saved-{stored.total_source}", stored.taken_at
        else:
            lam_h, lam_a = fallback.rates(f)
            source = "xg-ratings"
        rates[f.id] = FixtureRates(f.id, f.team_h, f.team_a, lam_h, lam_a, source, as_of)
    return rates


@dataclass(frozen=True)
class ScoreSamples:
    """Simulated goals: home[i] and away[i] are simulation i's score for this fixture."""

    home: NDArray[np.int64]
    away: NDArray[np.int64]


def simulate_scores(
    rates: dict[int, FixtureRates], n_sims: int, rng: np.random.Generator
) -> dict[int, ScoreSamples]:
    """Independent Poisson scorelines for each fixture. Fixtures are drawn in id order, so the
    same seed always gives the same scorelines regardless of dict ordering."""
    return {
        fid: ScoreSamples(
            home=rng.poisson(rates[fid].lambda_home, n_sims),
            away=rng.poisson(rates[fid].lambda_away, n_sims),
        )
        for fid in sorted(rates)
    }
