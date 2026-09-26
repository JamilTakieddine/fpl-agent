# Phase 2 step 1: xG-based fallback team ratings (shrinkage, formula), choosing Kalshi vs fallback
# rates per fixture, and seeded Poisson scoreline simulation; synthetic teams and players only.

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from fpl_agent.data.models import Bootstrap, Fixture, Player
from fpl_agent.data.odds import MatchOdds
from fpl_agent.model.scoreline import (
    FixtureRates,
    RatingsModel,
    fit_ratings,
    fixture_rates,
    simulate_scores,
)
from tests.conftest import fx


def played(fid: int, gw: int, home: int, away: int, hs: int, as_: int) -> Fixture:
    return fx(fid, gw, home, away, finished=True).model_copy(
        update={"team_h_score": hs, "team_a_score": as_}
    )


def make_player(
    base: Player, pid: int, team: int, pos: int, xg: float, xgc: float, mins: int
) -> Player:
    return base.model_copy(
        update={
            "id": pid,
            "team": team,
            "element_type": pos,
            "expected_goals": xg,
            "expected_goals_conceded": xgc,
            "minutes": mins,
        }
    )


@pytest.fixture
def base_player(bootstrap_json: Any) -> Player:
    return Bootstrap.model_validate(bootstrap_json).elements[0]


def two_team_season(base: Player, xg1: float, xg2: float, xgc1: float, xgc2: float) -> list[Player]:
    """Team 1 and 2 each played 2 matches (180 keeper minutes)."""
    return [
        make_player(base, 1, 1, 1, 0.0, xgc1, 180),  # team 1 keeper
        make_player(base, 2, 1, 4, xg1, 0.0, 180),  # team 1 striker
        make_player(base, 3, 2, 1, 0.0, xgc2, 180),
        make_player(base, 4, 2, 4, xg2, 0.0, 180),
    ]


SEASON = [played(1, 1, 1, 2, 2, 1), played(2, 2, 2, 1, 0, 0)]


def test_more_xg_means_stronger_attack(base_player: Player) -> None:
    model = fit_ratings(SEASON, two_team_season(base_player, 4.0, 1.0, 2.0, 2.0), [1, 2])
    assert model.attack[1] > 1.0 > model.attack[2]


def test_more_xg_conceded_means_weaker_defence(base_player: Player) -> None:
    model = fit_ratings(SEASON, two_team_season(base_player, 2.0, 2.0, 4.0, 1.0), [1, 2])
    assert model.weakness[1] > 1.0 > model.weakness[2]


def test_shrinkage_pulls_ratings_towards_average(base_player: Player) -> None:
    players = two_team_season(base_player, 4.0, 1.0, 2.0, 2.0)
    loose = fit_ratings(SEASON, players, [1, 2], shrink=0.0)
    tight = fit_ratings(SEASON, players, [1, 2], shrink=50.0)
    assert abs(tight.attack[1] - 1) < abs(loose.attack[1] - 1)
    assert tight.attack[1] == pytest.approx(1.0, abs=0.05)


def test_league_averages_come_from_results(base_player: Player) -> None:
    model = fit_ratings(SEASON, two_team_season(base_player, 2, 2, 2, 2), [1, 2])
    assert (model.home_avg, model.away_avg) == pytest.approx((1.0, 0.5))


def test_before_any_match_every_team_is_average(base_player: Player) -> None:
    model = fit_ratings([], two_team_season(base_player, 2, 2, 2, 2), [1, 2])
    assert model.attack == {1: 1.0, 2: 1.0} and model.weakness == {1: 1.0, 2: 1.0}


def test_rates_formula() -> None:
    model = RatingsModel(1.5, 1.2, {1: 1.2, 2: 0.8}, {1: 0.9, 2: 1.1})
    lam_h, lam_a = model.rates(fx(9, 3, 1, 2))
    assert lam_h == pytest.approx(1.5 * 1.2 * 1.1)
    assert lam_a == pytest.approx(1.2 * 0.8 * 0.9)


def odds_for(fid: int) -> MatchOdds:
    return MatchOdds(fid, 0.5, 0.25, 0.25, 1.9, 1.0, 0.0, 5000, 0.02, "totals", 3, -0.01)


def test_kalshi_rates_win_and_fallback_fills_gaps() -> None:
    model = RatingsModel(1.5, 1.2, {1: 1.0, 2: 1.0}, {1: 1.0, 2: 1.0})
    rates = fixture_rates([fx(1, 3, 1, 2), fx(2, 3, 2, 1)], {1: odds_for(1)}, model)
    assert (rates[1].lambda_home, rates[1].source) == (1.9, "kalshi-totals")
    assert (rates[2].lambda_home, rates[2].source) == (1.5, "xg-ratings")


def rates(lam_h: float, lam_a: float, fid: int = 1) -> dict[int, FixtureRates]:
    return {fid: FixtureRates(fid, 1, 2, lam_h, lam_a, "test")}


def test_simulation_is_reproducible_with_a_seed() -> None:
    a = simulate_scores(rates(1.6, 1.1), 1000, np.random.default_rng(42))
    b = simulate_scores(rates(1.6, 1.1), 1000, np.random.default_rng(42))
    c = simulate_scores(rates(1.6, 1.1), 1000, np.random.default_rng(7))
    assert np.array_equal(a[1].home, b[1].home) and np.array_equal(a[1].away, b[1].away)
    assert not np.array_equal(a[1].home, c[1].home)


def test_simulated_goals_average_to_the_rates() -> None:
    s = simulate_scores(rates(1.9, 0.7), 200_000, np.random.default_rng(0))
    assert s[1].home.mean() == pytest.approx(1.9, abs=0.02)
    assert s[1].away.mean() == pytest.approx(0.7, abs=0.02)
    assert s[1].home.min() >= 0


def test_draw_order_does_not_depend_on_dict_order() -> None:
    r = {**rates(1.2, 1.0, fid=2), **rates(2.0, 0.5, fid=1)}
    reordered = {1: r[1], 2: r[2]}
    a = simulate_scores(r, 500, np.random.default_rng(3))
    b = simulate_scores(reordered, 500, np.random.default_rng(3))
    assert np.array_equal(a[1].home, b[1].home) and np.array_equal(a[2].away, b[2].away)
