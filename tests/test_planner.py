# Phase 5b: the multi-week transfer planner (HiGHS) on synthetic squads - a clear upgrade, waiting
# for a player's good run, rolling a transfer to make two next week, hits off, budget, club
# limit, the 5-transfer cap, and every planned week's squad legal under the rules engine.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.planner import pair_moves, plan_transfers
from fpl_agent.optimize.rules import Limits, SquadPlayer, squad_violations
from fpl_agent.optimize.transfers import Move

POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}
BASE = {
    1: 5,
    2: 1,
    **dict.fromkeys(range(3, 8), 3),
    **dict.fromkeys(range(8, 13), 4),
    13: 1,
    14: 3,
    15: 2,
}
Targets = dict[int, tuple[PositionCode, int, int, list[float]]]  # id -> pos, club, price, xp/week


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def squad(teams: dict[int, int] | None = None) -> list[SquadPlayer]:
    teams = teams or {}
    return [SquadPlayer(p, pos, teams.get(p, p), 50) for p, pos in POS.items()]


def plan(
    targets: Targets,
    limits: Limits,
    free: int,
    weeks: int = 3,
    bank: int = 0,
    teams: dict[int, int] | None = None,
    **kw: Any,
) -> Any:
    mine = squad(teams)
    pool = [
        *mine,
        *(SquadPlayer(i, pos, team, price) for i, (pos, team, price, _) in targets.items()),
    ]
    xps = [
        {**{p: float(v) for p, v in BASE.items()}, **{i: t[3][w] for i, t in targets.items()}}
        for w in range(weeks)
    ]
    return plan_transfers(mine, bank, free, pool, xps, limits, **kw), mine, pool


def test_a_clear_upgrade_is_bought_now(limits: Limits) -> None:
    upgrade: Targets = {100: ("FWD", 100, 50, [8, 8, 8])}
    result, _, _ = plan(upgrade, limits, free=1)
    assert result.optimal
    assert [m.buy for m in result.weeks[0]] == [100]


def test_it_waits_for_a_players_good_run(limits: Limits) -> None:
    """Player 101 blanks this week, then scores 8 a week: buy him next week, not now."""
    later: Targets = {101: ("FWD", 101, 50, [0, 8, 8])}
    result, _, _ = plan(later, limits, free=1)
    assert result.weeks[0] == ()
    assert [m.buy for m in result.weeks[1]] == [101]


def test_roll_one_now_to_make_two_next_week(limits: Limits) -> None:
    """Two upgrades that only start paying next week, with one free transfer: roll it, then
    make both next week (no hits)."""
    targets: Targets = {102: ("FWD", 102, 50, [0, 9, 9]), 103: ("MID", 103, 50, [0, 9, 9])}
    result, _, _ = plan(targets, limits, free=1)
    assert result.weeks[0] == ()
    assert sorted(m.buy for m in result.weeks[1]) == [102, 103]
    assert result.free_transfers[:2] == (1, 2)


def test_hits_off_means_no_transfer_without_a_free_one(limits: Limits) -> None:
    target: Targets = {104: ("FWD", 104, 50, [12, 12, 12])}
    assert plan(target, limits, free=0)[0].weeks[0] == ()
    with_hits = plan(target, limits, free=0, allow_hits=True)[0]
    assert [m.buy for m in with_hits.weeks[0]] == [104]


def test_budget_and_club_limit(limits: Limits) -> None:
    pricey: Targets = {105: ("FWD", 105, 90, [12, 12, 12])}  # 9.0m with 5.0m to sell and no bank
    assert all(105 not in [m.buy for m in wk] for wk in plan(pricey, limits, free=1)[0].weeks)
    three_from_99 = dict.fromkeys((3, 4, 5), 99)
    fourth: Targets = {106: ("FWD", 99, 50, [12, 12, 12])}
    result = plan(fourth, limits, free=1, teams=three_from_99)[0]
    assert all(106 not in [m.buy for m in wk] for wk in result.weeks)


def test_a_marginal_upgrade_only_when_the_cap_would_waste_the_transfer(limits: Limits) -> None:
    """+0.5 points (one week) is less than a banked free transfer's 1.5, unless rolling it would
    exceed the 5-transfer cap anyway."""
    small: Targets = {107: ("FWD", 107, 50, [3.5])}  # forward 14 in the XI scores 3
    assert plan(small, limits, free=1, weeks=1)[0].weeks[0] == ()
    assert [m.buy for m in plan(small, limits, free=5, weeks=1)[0].weeks[0]] == [107]


def test_every_planned_week_is_a_legal_squad(limits: Limits) -> None:
    targets: Targets = {
        110: ("FWD", 110, 55, [7, 7, 7]),
        111: ("MID", 111, 45, [6, 6, 6]),
        112: ("DEF", 112, 40, [5, 5, 5]),
        113: ("GKP", 113, 45, [6, 6, 6]),
    }
    result, mine, pool = plan(targets, limits, free=2, bank=10)
    by_id = {q.id: q for q in pool}
    current = {s.id: s for s in mine}
    bank = 10
    for week in result.weeks:
        for m in week:
            bank += current[m.sell].price - by_id[m.buy].price
            del current[m.sell]
            current[m.buy] = by_id[m.buy]
        assert squad_violations(list(current.values()), limits, bank) == []


def test_moves_pair_by_position() -> None:
    position: dict[int, PositionCode] = {1: "DEF", 2: "MID", 3: "MID", 4: "DEF"}
    assert pair_moves([1, 2], [3, 4], position) == (Move(1, 4), Move(2, 3))
