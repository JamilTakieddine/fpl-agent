# Phase 2 step 6: bonus points (FPL tie rules via competition ranking, eligibility, per-match
# ranking in doubles), exact event BPS, and fitting BPS weights plus shrunk base BPS from live
# data; synthetic data, no network.

from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from fpl_agent.data.models import (
    EventLive,
    ExplainFixture,
    ExplainStat,
    LiveElement,
    LiveStats,
)
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.bonus import (
    DEFAULT_WEIGHTS,
    FEATURES,
    BpsModel,
    bonus_from_bps,
    fit_bps_model,
    simulate_bonus,
    simulated_features,
)
from fpl_agent.model.defence import DefenceSamples
from fpl_agent.model.discipline import DisciplineSamples
from fpl_agent.model.minutes import MinutesSamples


def ranks(bps: list[int], eligible: list[bool] | None = None) -> list[int]:
    b = np.array(bps, dtype=np.int64)[:, None]
    e = np.array(eligible if eligible is not None else [True] * len(bps))[:, None]
    return [int(v) for v in bonus_from_bps(b, e)[:, 0]]


@pytest.mark.parametrize(
    ("bps", "bonus"),
    [
        ([30, 20, 10, 5], [3, 2, 1, 0]),  # no ties
        ([30, 30, 10, 5], [3, 3, 1, 0]),  # tie for 1st: both 3, next gets 1
        ([30, 30, 30, 5], [3, 3, 3, 0]),  # three tied 1st: all 3, nobody else
        ([30, 20, 20, 5], [3, 2, 2, 0]),  # tie for 2nd: both 2, nobody gets 1
        ([30, 20, 10, 10], [3, 2, 1, 1]),  # tie for 3rd: all tied get 1
    ],
)
def test_fpl_tie_rules(bps: list[int], bonus: list[int]) -> None:
    assert ranks(bps) == bonus


def test_only_players_who_played_are_ranked() -> None:
    # The top BPS belongs to someone who didn't play (impossible in reality; guards the mask).
    assert ranks([50, 30, 20, 10], [False, True, True, True]) == [0, 3, 2, 1]


N = 4000


def samples(
    rows: list[dict[str, int]],
) -> tuple[MinutesSamples, AttackSamples, DefenceSamples, DisciplineSamples]:
    def col(key: str, default: int = 0) -> NDArray[np.int64]:
        return np.array([[r.get(key, default)] * N for r in rows], dtype=np.int64)

    minutes = MinutesSamples(
        player=np.array([r["pid"] for r in rows], dtype=np.int64),
        fixture=np.array([r.get("fid", 1) for r in rows], dtype=np.int64),
        minutes=col("min", 90),
        on_from=np.zeros((len(rows), N), dtype=np.int64),
        started=np.ones((len(rows), N), dtype=bool),
    )
    attack = AttackSamples(goals=col("g"), assists=col("a"), goal_minutes={})
    defence = DefenceSamples(
        goals_conceded=col("gc"),
        clean_sheet=col("cs").astype(bool),
        defcon_count=col("dc"),
        defcon_award=np.zeros((len(rows), N), dtype=bool),
        saves=col("sv"),
    )
    discipline = DisciplineSamples(
        yellow=col("y").astype(bool), red=col("r").astype(bool), pens_saved=col("ps")
    )
    return minutes, attack, defence, discipline


def test_event_bps_features_are_exact() -> None:
    ms, at, de, di = samples([{"pid": 1, "g": 2, "cs": 1, "sv": 3, "dc": 11}])
    f = simulated_features(np.array([2]), ms, at, de, di)  # a defender
    got = {k: float(v[0, 0]) for k, v in f.items()}
    assert got["goal_gk_def"] == 2 and got["goal_mid"] == 0 and got["goal_fwd"] == 0
    assert got["clean_sheet_gk_def"] == 1 and got["play_60"] == 1 and got["play_1_59"] == 0
    assert got["save"] == 3 and got["defcon_action"] == 11


def model(base: dict[int, float], noise: float = 3.0) -> BpsModel:
    return BpsModel(dict(DEFAULT_WEIGHTS), base, noise, {})


def test_scorer_with_high_base_nearly_always_gets_three() -> None:
    ms, at, de, di = samples([{"pid": 1, "g": 2}, {"pid": 2}, {"pid": 3}, {"pid": 4, "min": 0}])
    types = {1: 4, 2: 3, 3: 3, 4: 3}
    b = simulate_bonus(ms, at, de, di, types, model({1: 5.0}), np.random.default_rng(0))
    assert (b.bonus[0] == 3).mean() > 0.99
    assert (b.bonus[3] == 0).all()  # no minutes, no bonus


def test_double_gameweek_matches_are_ranked_separately() -> None:
    ms, at, de, di = samples(
        [
            {"pid": 1, "fid": 1, "g": 1},
            {"pid": 2, "fid": 1},
            {"pid": 1, "fid": 2, "g": 1},
            {"pid": 3, "fid": 2},
        ]
    )
    b = simulate_bonus(
        ms, at, de, di, {1: 4, 2: 3, 3: 3}, model({}, noise=0.1), np.random.default_rng(1)
    )
    assert (b.bonus[0] == 3).all() and (b.bonus[2] == 3).all()  # tops both matches


def test_seeded_bonus_is_reproducible() -> None:
    ms, at, de, di = samples([{"pid": 1}, {"pid": 2}, {"pid": 3}, {"pid": 4}])
    types = dict.fromkeys(range(1, 5), 3)
    a = simulate_bonus(ms, at, de, di, types, model({}), np.random.default_rng(5))
    c = simulate_bonus(ms, at, de, di, types, model({}), np.random.default_rng(5))
    assert np.array_equal(a.bps, c.bps)


# --- fitting ----------------------------------------------------------------------------------


def synthetic_lives(
    true_base: dict[int, float], n_matches: int, seed: int = 0
) -> dict[int, EventLive]:
    """Midfielders with random events; bps = DEFAULT_WEIGHTS . features + base + small noise."""
    rng = np.random.default_rng(seed)
    lives = {}
    for gw in range(1, n_matches + 1):
        elements = []
        for pid, base in true_base.items():
            goals, assists, dc = int(rng.poisson(0.3)), int(rng.poisson(0.3)), int(rng.poisson(8))
            bps = (
                DEFAULT_WEIGHTS["play_60"]
                + DEFAULT_WEIGHTS["goal_mid"] * goals
                + DEFAULT_WEIGHTS["assist"] * assists
                + DEFAULT_WEIGHTS["defcon_action"] * dc
                + base
                + rng.normal(0, 0.5)
            )
            elements.append(
                LiveElement(
                    id=pid,
                    stats=LiveStats(
                        minutes=90,
                        starts=1,
                        goals_scored=goals,
                        assists=assists,
                        defensive_contribution=dc,
                        bps=round(bps),
                    ),
                    explain=[
                        ExplainFixture(
                            fixture=gw, stats=[ExplainStat(identifier="minutes", value=90)]
                        )
                    ],
                )
            )
        lives[gw] = EventLive(elements=elements)
    return lives


def test_too_little_data_uses_default_weights() -> None:
    m = fit_bps_model(synthetic_lives({1: 0.0}, 3), {1: 3})
    assert m.weights == DEFAULT_WEIGHTS and m.base == {}


def test_fit_recovers_weights_and_trusts_clearly_different_players() -> None:
    base = {pid: (6.0 if pid % 2 else -6.0) for pid in range(1, 121)}  # two clear groups
    m = fit_bps_model(synthetic_lives(base, 6), dict.fromkeys(base, 3))
    assert m.weights["goal_mid"] == pytest.approx(DEFAULT_WEIGHTS["goal_mid"], abs=1.0)
    assert m.weights["assist"] == pytest.approx(DEFAULT_WEIGHTS["assist"], abs=1.0)
    assert m.base[1] > 4 and m.base[2] < -4  # kept close to their own averages
    assert min(m.keep_factor.values()) > 0.9


def test_identical_players_are_shrunk_to_the_position_mean() -> None:
    base = dict.fromkeys(range(1, 121), 0.0)  # nobody differs: any spread is noise
    m = fit_bps_model(synthetic_lives(base, 6, seed=3), dict.fromkeys(base, 3))
    spread = np.std([m.base[p] for p in base])
    assert spread < 0.3


def test_features_cover_every_weight() -> None:
    assert set(FEATURES) == set(DEFAULT_WEIGHTS)
