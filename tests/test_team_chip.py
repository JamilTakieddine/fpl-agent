# The weekly team-chip decision (D43): Triple Captain / Bench Boost are checked exactly when the
# plan plays them this week OR doesn't use them anywhere in its horizon (GW7: the plan's Wildcard
# was rejected and Triple Captain was never checked); a chip saved for a later horizon week, an
# unplayable chip and a failed planner are left alone; one chip a week, the biggest surplus.

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from fpl_agent.agent import Advice, decide_team_chip
from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.planner import Plan
from fpl_agent.optimize.rules import Limits, Lineup
from fpl_agent.optimize.scoring import SquadSims

POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}
# Everyone plays; fixed points. Captain 13 scores 8, so Triple Captain adds exactly 8. The bench
# (2, 6, 7, 12) scores 2 + 3 + 3 + 2, so Bench Boost adds exactly 10.
POINTS = {
    1: 4,
    2: 2,
    3: 3,
    4: 3,
    5: 3,
    6: 3,
    7: 3,
    8: 4,
    9: 4,
    10: 4,
    11: 4,
    12: 2,
    13: 8,
    14: 5,
    15: 5,
}
LINEUP = Lineup((1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15), (2, 6, 7, 12), captain=13, vice_captain=14)
SIMS = SquadSims(
    tuple(POINTS),
    np.array([[v] * 50 for v in POINTS.values()], dtype=np.int32),
    np.ones((len(POINTS), 50), dtype=bool),
)
GW7_VALUES = {
    "3xc": 7.5,
    "bboost": 11.7,
    "wildcard": 15.0,
    "freehit": 12.0,
}  # the real GW7 keep-values
BOTH = frozenset({"3xc", "bboost"})


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def decide(
    limits: Limits,
    chips: tuple[str | None, ...] | None,
    values: dict[str, float] = GW7_VALUES,
    playable: frozenset[str] = BOTH,
    last_chance: frozenset[str] = frozenset(),
) -> tuple[str | None, str]:
    multi = None if chips is None else Plan((), (), 0.0, True, 0.0, 0, chips=chips)
    stub: Any = None
    advice = Advice(stub, {}, multi, values, [], [], last_chance)
    said: list[str] = []
    chip = decide_team_chip(
        lambda *a: said.append(" ".join(map(str, a))), advice, LINEUP, SIMS, POS, limits, playable
    )
    return chip, "".join(said)


def test_gw7_a_rejected_wildcard_no_longer_hides_triple_captain(limits: Limits) -> None:
    chip, said = decide(limits, ("wildcard", None, None, None, None))
    assert chip == "3xc"  # +8.0 against 7.5 to keep
    assert "Triple Captain this week (not in the plan's weeks): +8.0" in said and "PLAY IT" in said
    assert "Bench Boost this week (not in the plan's weeks): +10.0" in said and "keep it" in said


def test_a_chip_the_plan_saves_for_a_later_week_is_left_for_it(limits: Limits) -> None:
    chip, said = decide(limits, (None, None, "3xc", None, None))
    assert chip is None and "Triple Captain" not in said  # saved for GW+2 (a double, say)
    assert "Bench Boost" in said  # not planned anywhere: still checked (10 < 11.7: keep)


def test_a_rejected_bench_boost_no_longer_hides_triple_captain(limits: Limits) -> None:
    chip, said = decide(limits, ("bboost", None, None, None, None))
    assert chip == "3xc" and "Bench Boost this week: +10.0" in said  # planned now, rejected


def test_the_plans_own_chip_this_week_is_still_played(limits: Limits) -> None:
    chip, _ = decide(limits, ("bboost", None, None, None, None), {**GW7_VALUES, "bboost": 9.0})
    assert chip == "bboost"


def test_one_chip_a_week_the_biggest_surplus(limits: Limits) -> None:
    values = {**GW7_VALUES, "3xc": 7.0, "bboost": 9.5}  # surpluses 1.0 and 0.5
    assert decide(limits, (None,) * 5, values)[0] == "3xc"
    values = {**GW7_VALUES, "3xc": 7.9, "bboost": 8.0}  # surpluses 0.1 and 2.0
    assert decide(limits, (None,) * 5, values)[0] == "bboost"


def test_unplayable_chips_and_no_plan_are_left_alone(limits: Limits) -> None:
    assert decide(limits, (None,) * 5, playable=frozenset())[0] is None  # used / window closed
    assert decide(limits, (None,) * 5, values={"bboost": 11.7})[0] is None  # no keep-value: skip
    assert decide(limits, None)[0] is None  # the planner failed: as before, no team chip
    assert decide(limits, ())[0] is None  # planned without chips


def test_a_last_chance_chip_is_played_unless_it_loses_points(limits: Limits) -> None:
    chip, _ = decide(
        limits,
        (None,) * 5,
        {**GW7_VALUES, "3xc": 50.0, "bboost": 50.0},
        last_chance=frozenset({"bboost"}),
    )
    assert chip == "bboost"
