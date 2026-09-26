# Phase 2 step 2: minutes simulation (role frequencies, position-level starter minutes, cameo
# timing, one availability draw per gameweek in doubles, blanks) and building the empirical
# distributions from live data; synthetic inputs, no network.

from __future__ import annotations

import numpy as np
import pytest

from fpl_agent.data.lineups import LineupPrediction, RoleHistory
from fpl_agent.data.models import (
    EventLive,
    ExplainFixture,
    ExplainStat,
    LiveElement,
    LiveStats,
    PositionCode,
)
from fpl_agent.model.minutes import (
    FULL_MATCH,
    MIN_SAMPLE,
    MinutesDistributions,
    MinutesSamples,
    minutes_distributions,
    simulate_minutes,
)

N = 20_000


def pred(pid: int, avail: float, start: float, cameo: float) -> LineupPrediction:
    return LineupPrediction(pid, avail, start, cameo, RoleHistory(5, 0, 0))


DISTS = MinutesDistributions(
    starter={
        "GKP": np.array([90]),
        "DEF": np.array([90, 90, 90, 75]),
        "MID": np.array([90, 70]),
        "FWD": np.array([90, 65]),
    },
    cameo=np.array([10, 20]),
)


def run(
    preds: dict[int, LineupPrediction],
    positions: dict[int, PositionCode],
    team_fixtures: dict[int, list[int]] | None = None,
    seed: int = 0,
) -> MinutesSamples:
    teams = dict.fromkeys(preds, 1)
    return simulate_minutes(
        preds,
        positions,
        teams,
        {1: [100]} if team_fixtures is None else team_fixtures,
        DISTS,
        N,
        np.random.default_rng(seed),
    )


def test_role_frequencies_match_the_predictions() -> None:
    ms = run({1: pred(1, 1.0, 0.6, 0.3)}, {1: "MID"})
    started = ms.started[0]
    played = ms.minutes[0] > 0
    assert started.mean() == pytest.approx(0.6, abs=0.01)
    assert (played & ~started).mean() == pytest.approx(0.3, abs=0.01)
    assert (~played).mean() == pytest.approx(0.1, abs=0.01)


def test_availability_scales_roles() -> None:
    # p_start and p_cameo already include availability (Phase 1): 0.5 * 0.8 and 0.5 * 0.2.
    ms = run({1: pred(1, 0.5, 0.4, 0.1)}, {1: "DEF"})
    assert ms.started[0].mean() == pytest.approx(0.4, abs=0.01)


def test_starter_minutes_follow_the_position_distribution() -> None:
    ms = run({1: pred(1, 1.0, 1.0, 0.0), 2: pred(2, 1.0, 1.0, 0.0)}, {1: "GKP", 2: "MID"})
    gk, mid = ms.minutes[0], ms.minutes[1]
    assert (gk == FULL_MATCH).all()
    assert set(np.unique(mid)) == {70, 90}
    assert (mid == 70).mean() == pytest.approx(0.5, abs=0.01)
    assert (ms.on_from[0] == 0).all()


def test_cameo_comes_on_late_and_ends_at_full_time() -> None:
    ms = run({1: pred(1, 1.0, 0.0, 1.0)}, {1: "FWD"})
    mins, on = ms.minutes[0], ms.on_from[0]
    assert set(np.unique(mins)) == {10, 20}
    assert ((on + mins) == FULL_MATCH).all()


def test_unused_player_has_no_minutes() -> None:
    ms = run({1: pred(1, 0.0, 0.0, 0.0)}, {1: "DEF"})
    assert (ms.minutes[0] == 0).all() and (ms.on_from[0] == FULL_MATCH).all()


def test_double_gameweek_shares_one_availability_draw() -> None:
    # 50% available, then always starts when available: both matches or neither.
    ms = run({1: pred(1, 0.5, 0.5, 0.0)}, {1: "DEF"}, {1: [100, 200]})
    first, second = ms.started[0], ms.started[1]
    assert list(ms.fixture) == [100, 200]
    assert (first == second).all()
    assert first.mean() == pytest.approx(0.5, abs=0.01)


def test_double_gameweek_roles_are_drawn_per_match() -> None:
    # Always available, 50% start per match: starting one match says nothing about the other.
    ms = run({1: pred(1, 1.0, 0.5, 0.0)}, {1: "MID"}, {1: [100, 200]})
    first, second = ms.started[0], ms.started[1]
    assert (first & second).mean() == pytest.approx(0.25, abs=0.01)


def test_blank_gameweek_player_has_no_rows() -> None:
    ms = run({1: pred(1, 1.0, 1.0, 0.0)}, {1: "DEF"}, {})
    assert ms.minutes.shape == (0, N)


def test_seeded_runs_are_reproducible() -> None:
    preds = {1: pred(1, 0.8, 0.5, 0.2)}
    positions: dict[int, PositionCode] = {1: "MID"}
    a, b = run(preds, positions, seed=5), run(preds, positions, seed=5)
    assert np.array_equal(a.minutes, b.minutes)


def live_starter(pid: int, minutes: int, starts: int = 1) -> LiveElement:
    return LiveElement(
        id=pid,
        stats=LiveStats(minutes=minutes, starts=starts),
        explain=[
            ExplainFixture(fixture=1, stats=[ExplainStat(identifier="minutes", value=minutes)])
        ],
    )


def test_distributions_from_live_data_with_fallbacks() -> None:
    elements = [live_starter(i, 80) for i in range(MIN_SAMPLE)]  # 30 MID starts at 80'
    elements += [live_starter(100 + i, 15, starts=0) for i in range(MIN_SAMPLE)]  # 30 cameos
    positions: dict[int, PositionCode] = {i: "MID" for i in range(MIN_SAMPLE)}
    positions |= {100 + i: "DEF" for i in range(MIN_SAMPLE)}
    dists = minutes_distributions({1: EventLive(elements=elements)}, positions)
    assert set(dists.starter["MID"]) == {80}
    assert set(dists.starter["DEF"]) == {80}  # too few DEF starts: pooled outfield
    assert set(dists.starter["GKP"]) == {FULL_MATCH}  # no keeper data: keepers play 90
    assert set(dists.cameo) == {15}


def test_double_gameweek_appearances_are_excluded_from_distributions() -> None:
    dgw = LiveElement(
        id=1,
        stats=LiveStats(minutes=150, starts=2),
        explain=[
            ExplainFixture(fixture=1, stats=[ExplainStat(identifier="minutes", value=90)]),
            ExplainFixture(fixture=2, stats=[ExplainStat(identifier="minutes", value=60)]),
        ],
    )
    dists = minutes_distributions({1: EventLive(elements=[dgw])}, {1: "MID"})
    assert 150 not in dists.starter["MID"]
