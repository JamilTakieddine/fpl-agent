"""Phase 2 entry point: simulate a whole gameweek from Phase 1 inputs to per-player points.

Runs steps 1-6 in a FIXED order from one seeded Generator, so the same inputs and seed always
give the same samples, and Phase 3 can compare lineups on identical simulated gameweeks.
Takes already-fetched data (no API client), so it's pure simulation: testable offline and
cheap to call repeatedly.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from fpl_agent.data.calendar import Calendar
from fpl_agent.data.lineups import LineupPrediction
from fpl_agent.data.models import Bootstrap, EventLive, Fixture, PositionCode
from fpl_agent.data.odds import MatchOdds
from fpl_agent.data.odds_store import SavedOdds
from fpl_agent.model.attack import attack_rates, simulate_attack
from fpl_agent.model.bonus import fit_bps_model, simulate_bonus
from fpl_agent.model.defence import defence_rates, simulate_defence
from fpl_agent.model.discipline import discipline_rates, simulate_discipline
from fpl_agent.model.minutes import minutes_distributions, simulate_minutes
from fpl_agent.model.points import PointsSamples, compute_points
from fpl_agent.model.scoreline import (
    DEFAULT_SIMS,
    FixtureRates,
    fit_ratings,
    fixture_rates,
    simulate_scores,
)


@dataclass(frozen=True)
class GameweekSimulation:
    event: int
    rates: dict[int, FixtureRates]  # per fixture: expected goals and where they came from
    points: PointsSamples


def simulate_gameweek(
    bootstrap: Bootstrap,
    calendar: Calendar,
    all_fixtures: list[Fixture],
    event: int,
    predictions: Mapping[int, LineupPrediction],
    lives: Mapping[int, EventLive],
    odds: Mapping[int, MatchOdds],
    saved_odds: Mapping[int, SavedOdds] | None = None,
    n_sims: int = DEFAULT_SIMS,
    seed: int = 0,
) -> GameweekSimulation:
    players = bootstrap.elements
    positions: dict[int, PositionCode] = {
        p.id: bootstrap.position_code(p.element_type) for p in players
    }
    teams = {p.id: p.team for p in players}
    gw_fixtures = list(calendar.get(event).fixtures)
    by_id = {f.id: f for f in gw_fixtures}
    team_fixtures: dict[int, list[int]] = {}
    for f in gw_fixtures:
        team_fixtures.setdefault(f.team_h, []).append(f.id)
        team_fixtures.setdefault(f.team_a, []).append(f.id)

    fallback = fit_ratings(all_fixtures, players, [t.id for t in bootstrap.teams])
    rates = fixture_rates(gw_fixtures, dict(odds), fallback, dict(saved_odds or {}))

    rng = np.random.default_rng(seed)
    scores = simulate_scores(rates, n_sims, rng)  # 1
    minutes = simulate_minutes(  # 2
        predictions,
        positions,
        teams,
        team_fixtures,
        minutes_distributions(lives, positions),
        n_sims,
        rng,
    )
    attack = simulate_attack(
        scores, minutes, teams, by_id, attack_rates(players, all_fixtures), rng
    )  # 3
    defence = simulate_defence(  # 4
        attack, minutes, teams, positions, by_id, rates, defence_rates(players, all_fixtures), rng
    )
    discipline = simulate_discipline(minutes, discipline_rates(players), rng)  # 5
    element_types = {p.id: p.element_type for p in players}
    bonus = simulate_bonus(  # 6
        minutes,
        attack,
        defence,
        discipline,
        element_types,
        fit_bps_model(lives, element_types),
        rng,
    )
    points = compute_points(bootstrap, positions, minutes, attack, defence, discipline, bonus)
    return GameweekSimulation(event=event, rates=rates, points=points)
