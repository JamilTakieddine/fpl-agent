# Phase 5d: the planner back-test's pure parts - the template starting squad (legal, the most
# owned), real results summed over a double gameweek's matches, and the summary and report on
# synthetic seasons; no simulation, no network.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.rules import Limits, SquadPlayer, squad_violations
from fpl_agent.validation.planner_backtest import (
    State,
    real_results,
    render_markdown,
    summarize,
    template_squad,
)
from fpl_agent.validation.pointintime import Season


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def test_the_template_squad_is_legal_and_the_most_owned(limits: Limits) -> None:
    shape: tuple[PositionCode, ...] = ("GKP",) * 3 + ("DEF",) * 7 + ("MID",) * 7 + ("FWD",) * 5
    players = [SquadPlayer(i, pos, i % 10, 50) for i, pos in enumerate(shape, 1)]
    selected = {q.id: 1000 * q.id for q in players}  # higher ids are more owned
    squad = template_squad(players, selected, limits)
    chosen = [q for q in players if q.id in squad]
    assert squad_violations(chosen, limits, limits.budget - sum(q.price for q in chosen)) == []
    for pos, n in limits.squad_counts.items():  # within each position: the n most owned
        at = sorted((q.id for q in players if q.position == pos), reverse=True)
        assert sorted(q.id for q in chosen if q.position == pos) == sorted(at[:n])


class Row:
    def __init__(self, element: int, points: int, minutes: int) -> None:
        self.element, self.total_points, self.minutes = element, points, minutes


def test_real_results_sum_a_double_gameweek() -> None:
    season = Season(
        name="x",
        rows_by_gw={5: [Row(1, 6, 90), Row(1, 2, 0), Row(2, 0, 0)]},  # type: ignore[list-item]
        teams=[],
        fixtures=[],
        player_types={},
        odds_source=lambda *a: ({}, []),
        template=None,  # type: ignore[arg-type]
    )
    assert real_results(season, 5) == {1: (8, True), 2: (0, False)}
    assert real_results(season, 6) == {}


def state(points: dict[int, int], chips: dict[int, str] | None = None) -> State:
    return State(
        squad=[],
        paid={},
        bank=0,
        points=points,
        transfers=dict.fromkeys(points, 1),
        chips_played=chips or {},
    )


def test_summary_and_report() -> None:
    states = {
        "agent": state({2: 60, 3: 70}, {2: "wildcard"}),
        "agent_no_chips": state({2: 55, 3: 66}),
        "part5": state({2: 50, 3: 65}),
        "hold": state({2: 40, 3: 50}),
    }
    s = summarize(states)
    assert s["policies"]["agent"]["total"] == 130 and s["policies"]["hold"]["total"] == 90
    assert s["agent_vs_hold"]["total"] == 40 and s["agent_vs_part5"]["total"] == 15
    assert s["policies"]["agent"]["chips"] == {"2": "wildcard"}
    md = render_markdown(s)
    assert "| Agent (planner + chips) | **130** | 2 | wildcard GW2 |" in md
    assert "Agent vs hold | +40" in md


def test_a_checkpoint_round_trips(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from fpl_agent.validation import planner_backtest as pb

    monkeypatch.setattr(pb, "CHECKPOINT", tmp_path / "checkpoint.json")
    states = {
        "agent": State(
            squad=[1, 2],
            paid={1: 50, 2: 45},
            bank=5,
            free=2,
            chips=[("bboost", 1, 19)],
            points={2: 60},
            transfers={2: 1},
            chips_played={2: "wildcard"},
        )
    }
    pb.save_checkpoint(7, states, {1: 51}, {1: 3}, {1: "MID"})
    loaded = pb.load_checkpoint()
    assert loaded is not None
    done, back, memory, teams, positions = loaded
    assert done == 7 and back == states
    assert memory == {1: 51} and teams == {1: 3} and positions == {1: "MID"}
