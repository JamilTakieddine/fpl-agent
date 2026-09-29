# Phase 2 step 3: attacking events (scorers on the pitch weighted by xG, assists by adjusted xA,
# own goals, goal timing vs substitutions) and the per-90 rates with shrinkage and position
# factors; synthetic matches, no network.

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from fpl_agent.data.models import Bootstrap, Fixture, Player
from fpl_agent.model.attack import (
    PRIOR_MINUTES,
    AttackRates,
    AttackSamples,
    attack_rates,
    simulate_attack,
)
from fpl_agent.model.minutes import MinutesSamples
from fpl_agent.model.scoreline import ScoreSamples
from tests.conftest import fx

N = 20_000
FIX = fx(1, 1, 1, 2)  # team 1 at home to team 2


def minutes_rows(rows: list[tuple[int, int, int]]) -> MinutesSamples:
    """(player id, on_from, minutes) for fixture 1, identical in every simulation."""
    return MinutesSamples(
        player=np.array([r[0] for r in rows], dtype=np.int64),
        fixture=np.ones(len(rows), dtype=np.int64),
        minutes=np.array([[r[2]] * N for r in rows], dtype=np.int64),
        on_from=np.array([[r[1]] * N for r in rows], dtype=np.int64),
        started=np.array([[r[1] == 0] * N for r in rows]),
    )


def rates(
    xg: dict[int, float], xa: dict[int, float], og: float = 0.0, ar: float = 1.0
) -> AttackRates:
    return AttackRates(xg90=xg, xa90=xa, own_goal_share=og, assist_rate=ar, xa_factor={})


def run(
    ms: MinutesSamples, home_goals: int, r: AttackRates, teams: dict[int, int], seed: int = 0
) -> AttackSamples:
    scores = {1: ScoreSamples(home=np.full(N, home_goals), away=np.zeros(N, dtype=np.int64))}
    return simulate_attack(scores, ms, teams, {1: FIX}, r, np.random.default_rng(seed))


TEAMS = {1: 1, 2: 1, 3: 1, 9: 2}


def test_goals_add_up_to_the_scoreline_without_own_goals() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 90), (9, 0, 90)])
    att = run(ms, 3, rates({1: 0.5, 2: 0.5, 9: 1.0}, {1: 0.1, 2: 0.1, 9: 0.1}), TEAMS)
    assert (att.goals[:2].sum(axis=0) == 3).all()
    assert (att.goals[2] == 0).all()  # the other team's player never scores these


def test_scorer_frequencies_follow_xg() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 90)])
    att = run(ms, 1, rates({1: 0.75, 2: 0.25}, {1: 0.1, 2: 0.1}), TEAMS)
    assert att.goals[0].mean() == pytest.approx(0.75, abs=0.01)


def test_only_players_on_the_pitch_score() -> None:
    # Player 2 comes on at minute 60: he can only score the ~1/3 of goals after that.
    ms = minutes_rows([(1, 0, 90), (2, 60, 30)])
    att = run(ms, 1, rates({1: 1.0, 2: 1.0}, {1: 0.0, 2: 0.0}), TEAMS)
    # Before 60 only player 1 is on (2/3 of goals); after, they split evenly (1/6 each).
    assert att.goals[1].mean() == pytest.approx(1 / 6, abs=0.01)


def test_unused_player_never_scores() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 90, 0)])
    att = run(ms, 2, rates({1: 0.1, 2: 5.0}, {1: 0.1, 2: 0.1}), TEAMS)
    assert att.goals[1].sum() == 0


def test_nobody_assists_their_own_goal() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 90)])
    att = run(ms, 1, rates({1: 1.0, 2: 0.0}, {1: 5.0, 2: 1.0}), TEAMS)
    assert att.assists[0].sum() == 0  # player 1 scores every goal, so only 2 can assist
    assert (att.assists[1] == 1).all()


def test_assist_rate_is_respected() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 90)])
    att = run(ms, 1, rates({1: 1.0, 2: 1.0}, {1: 1.0, 2: 1.0}, ar=0.6), TEAMS)
    assert att.assists.sum() / att.goals.sum() == pytest.approx(0.6, abs=0.01)


def test_own_goals_credit_nobody() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 90)])
    att = run(ms, 1, rates({1: 1.0, 2: 1.0}, {1: 1.0, 2: 1.0}, og=0.2), TEAMS)
    assert att.goals.sum() / N == pytest.approx(0.8, abs=0.01)
    assert att.assists.sum() <= att.goals.sum()


def test_seeded_runs_are_reproducible() -> None:
    ms = minutes_rows([(1, 0, 90), (2, 0, 70), (3, 70, 20)])
    r = rates({1: 0.5, 2: 0.3, 3: 0.2}, {1: 0.2, 2: 0.2, 3: 0.2}, og=0.05, ar=0.9)
    a, b = run(ms, 2, r, TEAMS, seed=3), run(ms, 2, r, TEAMS, seed=3)
    assert np.array_equal(a.goals, b.goals) and np.array_equal(a.assists, b.assists)


# --- rates ------------------------------------------------------------------------------------


def make(
    base: Player, pid: int, et: int, mins: int, xg: float, xa: float, a: int, og: int = 0
) -> Player:
    return base.model_copy(
        update={
            "id": pid,
            "element_type": et,
            "minutes": mins,
            "expected_goals": xg,
            "expected_assists": xa,
            "assists": a,
            "own_goals": og,
        }
    )


@pytest.fixture
def base(bootstrap_json: Any) -> Player:
    return Bootstrap.model_validate(bootstrap_json).elements[0]


def played(goals_h: int, goals_a: int) -> Fixture:
    return fx(5, 1, 1, 2, finished=True).model_copy(
        update={"team_h_score": goals_h, "team_a_score": goals_a}
    )


def test_small_samples_are_shrunk_towards_the_position(base: Player) -> None:
    regular = make(base, 1, 4, 900, 5.0, 1.0, 1)  # 0.5 xG per 90 over 10 full matches
    cameo = make(base, 2, 4, 20, 0.3, 0.0, 0)  # 1.35 per 90 over 20 minutes
    r = attack_rates([regular, cameo], [played(2, 1)])
    raw_cameo = 0.3 / (20 / 90)
    assert r.xg90[2] < raw_cameo / 2  # pulled strongly towards the position mean
    assert r.xg90[1] == pytest.approx(
        (5.0 + (5.3 / (920 / 90)) * PRIOR_MINUTES / 90) / ((900 + PRIOR_MINUTES) / 90)
    )


def test_xa_is_scaled_by_the_positions_fpl_assist_ratio(base: Player) -> None:
    fwd = make(base, 1, 4, 900, 0.0, 1.0, 3)  # 3 FPL assists from 1.0 xA
    mid = make(base, 2, 3, 900, 0.0, 2.0, 2)  # 2 from 2.0
    r = attack_rates([fwd, mid], [played(3, 2)])
    assert r.xa_factor == {4: pytest.approx(3.0), 3: pytest.approx(1.0)}
    assert r.xa90[1] / r.xa90[2] == pytest.approx(3.0 * 1.0 / (1.0 * 2.0))


def test_event_rates_from_season_data(base: Player) -> None:
    ps = [make(base, 1, 4, 900, 1.0, 1.0, a=3, og=1)]
    r = attack_rates(ps, [played(3, 1)])  # 4 team goals, 1 own goal, 3 assists
    assert r.own_goal_share == pytest.approx(0.25)
    assert r.assist_rate == pytest.approx(1.0)


def test_defaults_before_any_match(base: Player) -> None:
    r = attack_rates([make(base, 1, 4, 0, 0.0, 0.0, 0)], [])
    assert (r.own_goal_share, r.assist_rate) == (0.05, 0.9)
