# Phase 2 step 5: exact FPL points from hand-built events (per position, rounding, the 60-minute
# boundary, doubles, config-driven values), card/penalty-save sampling, and the seeded whole-
# gameweek simulation; recorded scoring config, no network.

from __future__ import annotations

import copy
from typing import Any

import numpy as np
import pytest

from fpl_agent.data.calendar import build_calendar
from fpl_agent.data.lineups import predict_all
from fpl_agent.data.models import Bootstrap, EventLive, Fixture, PositionCode
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.bonus import BonusSamples
from fpl_agent.model.defence import DefenceSamples
from fpl_agent.model.discipline import DisciplineRates, DisciplineSamples, simulate_discipline
from fpl_agent.model.gameweek import simulate_gameweek
from fpl_agent.model.minutes import MinutesSamples
from fpl_agent.model.points import COMPONENTS, PointsSamples, compute_points
from tests.conftest import load_fixture


def one(values: list[int]) -> Any:
    return np.array([[v] for v in values], dtype=np.int64)


def score(
    bootstrap_json: Any,
    rows: list[dict[str, Any]],
    positions: dict[int, PositionCode],
) -> PointsSamples:
    """One simulation; each row dict describes one player-match."""

    def col(key: str, default: int = 0) -> Any:
        return one([r.get(key, default) for r in rows])

    minutes = MinutesSamples(
        player=np.array([r["pid"] for r in rows], dtype=np.int64),
        fixture=np.array([r.get("fid", 1) for r in rows], dtype=np.int64),
        minutes=col("min", 90),
        on_from=np.zeros((len(rows), 1), dtype=np.int64),
        started=np.ones((len(rows), 1), dtype=bool),
    )
    attack = AttackSamples(goals=col("g"), assists=col("a"), goal_minutes={})
    defence = DefenceSamples(
        goals_conceded=col("gc"),
        clean_sheet=col("cs").astype(bool),
        defcon_count=col("dc"),
        defcon_award=col("dca").astype(bool),
        saves=col("sv"),
    )
    discipline = DisciplineSamples(
        yellow=col("y").astype(bool), red=col("r").astype(bool), pens_saved=col("ps")
    )
    bonus = BonusSamples(bps=col("bps"), bonus=col("b"))
    boot = Bootstrap.model_validate(bootstrap_json)
    return compute_points(boot, positions, minutes, attack, defence, discipline, bonus)


POS: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


def pts(bootstrap_json: Any, **row: Any) -> int:
    p = score(bootstrap_json, [row], POS)
    return int(p.total[0, 0])


def test_defender_two_goals_and_a_clean_sheet(bootstrap_json: Any) -> None:
    assert pts(bootstrap_json, pid=2, g=2, cs=1) == 2 + 2 * 6 + 4


@pytest.mark.parametrize(
    ("pid", "goal", "clean_sheet"), [(1, 10, 4), (2, 6, 4), (3, 5, 1), (4, 4, 0)]
)
def test_position_values_come_from_config(
    bootstrap_json: Any, pid: int, goal: int, clean_sheet: int
) -> None:
    assert pts(bootstrap_json, pid=pid, g=1) == 2 + goal
    assert pts(bootstrap_json, pid=pid, cs=1) == 2 + clean_sheet


def test_goals_conceded_per_two_rounded_down(bootstrap_json: Any) -> None:
    assert pts(bootstrap_json, pid=2, gc=1) == 2
    assert pts(bootstrap_json, pid=2, gc=3) == 2 - 1
    assert pts(bootstrap_json, pid=2, gc=4) == 2 - 2
    assert pts(bootstrap_json, pid=3, gc=4) == 2  # midfielders don't lose points


def test_saves_per_three_rounded_down(bootstrap_json: Any) -> None:
    assert pts(bootstrap_json, pid=1, sv=5) == 2 + 1
    assert pts(bootstrap_json, pid=1, sv=6) == 2 + 2


def test_appearance_boundary_at_sixty(bootstrap_json: Any) -> None:
    assert pts(bootstrap_json, pid=3, min=59) == 1
    assert pts(bootstrap_json, pid=3, min=60) == 2
    assert pts(bootstrap_json, pid=3, min=0) == 0


def test_defcon_cards_assists_and_penalty_saves(bootstrap_json: Any) -> None:
    assert pts(bootstrap_json, pid=2, dca=1) == 2 + 2
    assert pts(bootstrap_json, pid=3, a=1, y=1) == 2 + 3 - 1
    assert pts(bootstrap_json, pid=4, r=1) == 2 - 3
    assert pts(bootstrap_json, pid=1, ps=1) == 2 + 5
    assert pts(bootstrap_json, pid=4, b=3) == 2 + 3


def test_config_change_changes_points(bootstrap_json: Any) -> None:
    changed = copy.deepcopy(bootstrap_json)
    changed["game_config"]["scoring"]["goals_scored"]["DEF"] = 7
    assert pts(changed, pid=2, g=1) == 2 + 7


def test_double_gameweek_sums_and_played_flag(bootstrap_json: Any) -> None:
    p = score(
        bootstrap_json,
        [
            {"pid": 2, "fid": 1, "g": 1},
            {"pid": 2, "fid": 2, "cs": 1},
            {"pid": 3, "fid": 1, "min": 0},
        ],
        POS,
    )
    assert list(p.player) == [2, 3]
    assert int(p.total[p.index(2), 0]) == (2 + 6) + (2 + 4)
    assert bool(p.played[p.index(2), 0]) and not bool(p.played[p.index(3), 0])


def test_breakdown_adds_up_to_expected_points(bootstrap_json: Any) -> None:
    p = score(bootstrap_json, [{"pid": 2, "g": 1, "cs": 1, "y": 1, "dca": 1}], POS)
    assert set(p.breakdown) == set(COMPONENTS)
    assert sum(float(v[0]) for v in p.breakdown.values()) == pytest.approx(float(p.expected()[0]))


# --- discipline -------------------------------------------------------------------------------


def minutes_for(n: int, mins: int) -> MinutesSamples:
    return MinutesSamples(
        player=np.array([1, 2], dtype=np.int64),
        fixture=np.array([1, 1], dtype=np.int64),
        minutes=np.full((2, n), mins, dtype=np.int64),
        on_from=np.zeros((2, n), dtype=np.int64),
        started=np.ones((2, n), dtype=bool),
    )


def test_card_probability_scales_with_minutes() -> None:
    rates = DisciplineRates(yellow90={1: 0.2, 2: 0.2}, red90={}, pens_saved90={})
    full = simulate_discipline(minutes_for(50_000, 90), rates, np.random.default_rng(0))
    half = simulate_discipline(minutes_for(50_000, 45), rates, np.random.default_rng(0))
    assert full.yellow[0].mean() == pytest.approx(1 - np.exp(-0.2), abs=0.005)
    assert half.yellow[0].mean() == pytest.approx(1 - np.exp(-0.1), abs=0.005)


def test_red_card_clears_the_yellow() -> None:
    rates = DisciplineRates(yellow90={1: 5.0}, red90={1: 5.0}, pens_saved90={})
    d = simulate_discipline(minutes_for(5_000, 90), rates, np.random.default_rng(1))
    assert not (d.yellow & d.red).any()


def test_penalty_saves_only_for_listed_goalkeepers() -> None:
    rates = DisciplineRates(yellow90={}, red90={}, pens_saved90={1: 0.5})
    d = simulate_discipline(minutes_for(20_000, 90), rates, np.random.default_rng(2))
    assert d.pens_saved[0].mean() == pytest.approx(0.5, abs=0.02)
    assert (d.pens_saved[1] == 0).all()


# --- whole gameweek ---------------------------------------------------------------------------


def gameweek_inputs(bootstrap_json: Any) -> tuple[Any, ...]:
    boot = Bootstrap.model_validate(bootstrap_json)
    fixtures = [Fixture.model_validate(f) for f in load_fixture("fixtures")]
    cal = build_calendar(boot, fixtures)
    lives = {5: EventLive.model_validate(load_fixture("live_gw5"))}
    preds = predict_all(boot.elements, cal, lives, 6)
    return boot, cal, fixtures, preds, lives


def test_simulate_gameweek_is_reproducible(bootstrap_json: Any) -> None:
    boot, cal, fixtures, preds, lives = gameweek_inputs(bootstrap_json)
    a = simulate_gameweek(boot, cal, fixtures, 6, preds, lives, {}, None, n_sims=300, seed=9)
    b = simulate_gameweek(boot, cal, fixtures, 6, preds, lives, {}, None, n_sims=300, seed=9)
    c = simulate_gameweek(boot, cal, fixtures, 6, preds, lives, {}, None, n_sims=300, seed=10)
    assert np.array_equal(a.points.total, b.points.total)
    assert not np.array_equal(a.points.total, c.points.total)
    assert a.points.total.shape == (len(a.points.player), 300)
    assert {r.source for r in a.rates.values()} == {"xg-ratings"}  # no odds passed in
