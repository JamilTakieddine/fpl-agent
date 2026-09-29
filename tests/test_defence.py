# Phase 2 step 4: defensive events (goals conceded while on the pitch, clean sheets incl. the
# subbed-off-before-the-goal case, DEFCON counts and thresholds, goalkeeper saves) and their
# rates; synthetic matches with exact goal minutes, no network.

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from fpl_agent.data.models import Bootstrap, Fixture, Player, PositionCode
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.defence import (
    DEFCON_DISPERSION,
    DEFCON_PRIOR_MINUTES,
    DefenceRates,
    DefenceSamples,
    defence_rates,
    overdispersed_counts,
    simulate_defence,
)
from fpl_agent.model.minutes import MinutesSamples
from fpl_agent.model.scoreline import FixtureRates
from tests.conftest import fx

N = 20_000
FIX = fx(1, 1, 1, 2)  # team 1 (home) v team 2
TEAMS = {1: 1, 2: 1, 3: 1, 9: 2}


def minutes_rows(rows: list[tuple[int, int, int, int]]) -> MinutesSamples:
    """(player, fixture, on_from, minutes), identical in every simulation."""
    return MinutesSamples(
        player=np.array([r[0] for r in rows], dtype=np.int64),
        fixture=np.array([r[1] for r in rows], dtype=np.int64),
        minutes=np.array([[r[3]] * N for r in rows], dtype=np.int64),
        on_from=np.array([[r[2]] * N for r in rows], dtype=np.int64),
        started=np.array([[r[2] == 0] * N for r in rows]),
    )


def goals_at(minutes: list[float], fid: int = 1, team: int = 2) -> dict[tuple[int, int], Any]:
    """Opponent (team 2) scores at these minutes in every simulation."""
    arr = np.array([[m] * N for m in minutes], dtype=np.float64).reshape(len(minutes), N)
    return {(fid, team): arr}


def run(
    ms: MinutesSamples,
    goal_minutes: dict[tuple[int, int], Any],
    positions: dict[int, PositionCode],
    rates: DefenceRates | None = None,
    fixtures: dict[int, Fixture] | None = None,
    seed: int = 0,
) -> DefenceSamples:
    attack = AttackSamples(
        goals=np.zeros_like(ms.minutes),
        assists=np.zeros_like(ms.minutes),
        goal_minutes=goal_minutes,
    )
    fixtures = fixtures or {1: FIX}
    fixture_rates = {
        fid: FixtureRates(fid, f.team_h, f.team_a, 1.5, 1.2, "test") for fid, f in fixtures.items()
    }
    return simulate_defence(
        attack,
        ms,
        TEAMS,
        positions,
        fixtures,
        fixture_rates,
        rates or DefenceRates({}, {}, 1.2),
        np.random.default_rng(seed),
    )


DEF: dict[int, PositionCode] = {1: "DEF", 2: "DEF", 3: "GKP", 9: "FWD"}


def test_subbed_off_before_the_goal_keeps_the_clean_sheet() -> None:
    ms = minutes_rows([(1, 1, 0, 70), (2, 1, 0, 90)])
    d = run(ms, goals_at([80.0]), DEF)
    assert (d.goals_conceded[0] == 0).all() and d.clean_sheet[0].all()  # off at 70
    assert (d.goals_conceded[1] == 1).all() and not d.clean_sheet[1].any()  # on at 80


def test_cameo_under_60_minutes_gets_no_clean_sheet() -> None:
    ms = minutes_rows([(1, 1, 70, 20)])
    d = run(ms, goals_at([]), DEF)
    assert (d.goals_conceded[0] == 0).all()
    assert not d.clean_sheet[0].any()


def test_only_goals_while_on_the_pitch_count() -> None:
    ms = minutes_rows([(1, 1, 0, 70), (2, 1, 75, 15)])
    d = run(ms, goals_at([10.0, 50.0, 80.0]), DEF)
    assert (d.goals_conceded[0] == 2).all()  # 10' and 50'
    assert (d.goals_conceded[1] == 1).all()  # 80'


def test_goals_that_did_not_happen_are_ignored() -> None:
    ms = minutes_rows([(1, 1, 0, 90)])
    d = run(ms, goals_at([np.inf, np.inf]), DEF)
    assert (d.goals_conceded[0] == 0).all() and d.clean_sheet[0].all()


def test_double_gameweek_scored_per_match() -> None:
    fixtures = {1: FIX, 2: fx(2, 1, 2, 1)}  # second match: team 2 at home to team 1
    ms = minutes_rows([(1, 1, 0, 90), (1, 2, 0, 90)])
    d = run(ms, goals_at([30.0], fid=2, team=2), DEF, fixtures=fixtures)
    assert d.clean_sheet[0].all() and not d.clean_sheet[1].any()


def nb_at_least(k: int, mean: float, dispersion: float = DEFCON_DISPERSION) -> float:
    """P(X >= k) for a negative binomial with this mean and variance = dispersion x mean."""
    n, p = mean / (dispersion - 1), 1 / dispersion
    pmf = p**n  # P(X = 0)
    below = 0.0
    for i in range(k):
        below += pmf
        pmf *= (i + n) / (i + 1) * (1 - p)
    return 1 - below


def test_defcon_hit_rate_matches_negative_binomial_and_scales_with_minutes() -> None:
    rates = DefenceRates(dc90={1: 9.0, 2: 9.0, 3: 20.0}, saves90={}, goals_per_team=1.2)
    ms = minutes_rows([(1, 1, 0, 90), (2, 1, 0, 45), (3, 1, 0, 90)])
    d = run(ms, goals_at([]), DEF, rates)
    assert d.defcon_award[0].mean() == pytest.approx(nb_at_least(10, 9.0), abs=0.01)
    assert d.defcon_count[1].mean() == pytest.approx(4.5, abs=0.05)  # half the minutes
    assert not d.defcon_award[2].any()  # goalkeepers can't earn DEFCON


def test_forward_threshold_is_twelve() -> None:
    rates = DefenceRates(dc90={9: 12.0}, saves90={}, goals_per_team=1.2)
    d = run(minutes_rows([(9, 1, 0, 90)]), goals_at([]), DEF, rates)
    assert d.defcon_award[0].mean() == pytest.approx(nb_at_least(12, 12.0), abs=0.01)


def test_saves_scale_with_opponent_attack_and_minutes() -> None:
    rates = DefenceRates(dc90={}, saves90={3: 3.0}, goals_per_team=1.2)
    ms = minutes_rows([(3, 1, 0, 90), (1, 1, 0, 90)])
    d = run(ms, goals_at([]), DEF, rates)
    # Keeper of team 1 faces team 2's lambda (1.2 in run()): factor 1.2 / 1.2 = 1.
    assert d.saves[0].mean() == pytest.approx(3.0, abs=0.05)
    assert (d.saves[1] == 0).all()  # outfield players make no saves


def test_seeded_runs_are_reproducible() -> None:
    rates = DefenceRates(dc90={1: 8.0}, saves90={3: 3.0}, goals_per_team=1.2)
    ms = minutes_rows([(1, 1, 0, 90), (3, 1, 0, 90)])
    a, b = (
        run(ms, goals_at([20.0]), DEF, rates, seed=4),
        run(ms, goals_at([20.0]), DEF, rates, seed=4),
    )
    assert np.array_equal(a.defcon_count, b.defcon_count) and np.array_equal(a.saves, b.saves)


# --- rates ------------------------------------------------------------------------------------


@pytest.fixture
def base(bootstrap_json: Any) -> Player:
    return Bootstrap.model_validate(bootstrap_json).elements[0]


def make(base: Player, pid: int, et: int, mins: int, dc: int, saves: int = 0) -> Player:
    return base.model_copy(
        update={
            "id": pid,
            "element_type": et,
            "minutes": mins,
            "defensive_contribution": dc,
            "saves": saves,
        }
    )


def test_defcon_shrinkage_is_light(base: Player) -> None:
    cb = make(base, 1, 2, 450, 60)  # 12 per 90
    fb = make(base, 2, 2, 450, 20)  # 4 per 90
    r = defence_rates([cb, fb], [])
    mean = 80 / 10  # 8 per 90 across the position
    expected = (60 + mean * DEFCON_PRIOR_MINUTES / 90) / ((450 + DEFCON_PRIOR_MINUTES) / 90)
    assert r.dc90[1] == pytest.approx(expected)
    assert r.dc90[1] > 11.0  # stays close to his own 12 per 90


def test_saves_only_for_goalkeepers_and_league_goal_rate(base: Player) -> None:
    gk = make(base, 1, 1, 450, 0, saves=15)
    df = make(base, 2, 2, 450, 30)
    played = fx(5, 1, 1, 2, finished=True).model_copy(update={"team_h_score": 2, "team_a_score": 1})
    r = defence_rates([gk, df], [played])
    assert set(r.saves90) == {1}
    assert r.goals_per_team == pytest.approx(1.5)


def test_overdispersed_counts_have_the_intended_mean_and_spread() -> None:
    rng = np.random.default_rng(0)
    mean = np.full((1, 200_000), 8.0)
    counts = overdispersed_counts(mean, 1.55, rng)
    assert counts.mean() == pytest.approx(8.0, rel=0.01)
    assert counts.var() / counts.mean() == pytest.approx(1.55, rel=0.03)
    poisson = overdispersed_counts(mean, 1.0, rng)  # dispersion 1 is plain Poisson
    assert poisson.var() / poisson.mean() == pytest.approx(1.0, rel=0.03)
    zeros = overdispersed_counts(np.zeros((1, 10)), 1.55, rng)
    assert (zeros == 0).all()
