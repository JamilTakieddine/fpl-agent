# Phase 5b: the multi-week transfer planner (HiGHS) on synthetic squads - a clear upgrade, waiting
# for a player's good run, rolling a transfer to make two next week, hits off, budget, club
# limit, the 5-transfer cap, and every planned week's squad legal under the rules engine.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.planner import chip_inputs, current_chips, pair_moves, plan_transfers
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


# --- chips (5c) ----------------------------------------------------------------------------------

UPGRADES: tuple[PositionCode, ...] = ("DEF", "DEF", "MID", "FWD", "FWD")
WHOLE_SQUAD: tuple[PositionCode, ...] = ("GKP",) * 2 + ("DEF",) * 5 + ("MID",) * 5 + ("FWD",) * 3


def chip_plan(
    limits: Limits,
    week_xp: list[dict[int, float]],
    chips: dict[str, list[int]],
    values: dict[str, float],
    targets: Targets | None = None,
    free: int = 1,
) -> Any:
    targets = targets or {}
    mine = squad()
    pool = [
        *mine,
        *(SquadPlayer(i, pos, team, price) for i, (pos, team, price, _) in targets.items()),
    ]
    xps = [
        {**{p: float(v) for p, v in BASE.items()}, **wx, **{i: t[3][w] for i, t in targets.items()}}
        for w, wx in enumerate(week_xp)
    ]
    return plan_transfers(mine, 0, free, pool, xps, limits, chips=chips, chip_values=values), mine


def test_triple_captain_goes_where_the_captain_hauls(limits: Limits) -> None:
    weeks = [{}, {14: 20.0}, {}]  # forward 14 expects 20 in week 1 (a double gameweek, say)
    played = chip_plan(limits, weeks, {"3xc": [0, 1, 2]}, {"3xc": 8.0})[0].chips
    assert played == (None, "3xc", None)
    # Worth more kept for later than the +20 x 0.9 here: not played.
    assert chip_plan(limits, weeks, {"3xc": [0, 1, 2]}, {"3xc": 30.0})[0].chips == (None,) * 3


def test_bench_boost_goes_where_the_bench_is_strongest(limits: Limits) -> None:
    strong_bench = {2: 6.0, 7: 6.0, 12: 6.0, 15: 6.0}  # the four bench players, all at 6
    weeks = [{}, {}, {**strong_bench, 1: 9.0, 3: 9.0, 8: 9.0, 14: 9.0}]
    plan, _ = chip_plan(limits, weeks, {"bboost": [0, 1, 2]}, {"bboost": 5.0})
    assert plan.chips[2] == "bboost"


def test_a_chip_whose_window_ends_inside_the_horizon_is_used(limits: Limits) -> None:
    """Use it or lose it: kept beyond the window it's worth nothing, so it goes in the best week."""
    weeks = [{14: 6.0}, {14: 7.0}, {14: 5.0}]
    plan, _ = chip_plan(limits, weeks, {"3xc": [0, 1, 2]}, {"3xc": 0.0})
    assert plan.chips == (None, "3xc", None)


def test_a_wildcard_for_many_upgrades_keeps_the_free_transfers(limits: Limits) -> None:
    targets: Targets = {
        200 + i: (pos, 200 + i, 50, [8.0, 8.0, 8.0]) for i, pos in enumerate(UPGRADES)
    }
    plan, _ = chip_plan(limits, [{}, {}, {}], {"wildcard": [0, 1, 2]}, {"wildcard": 5.0}, targets)
    assert plan.chips[0] == "wildcard" and len(plan.weeks[0]) >= 4
    assert plan.free_transfers[1] == 1  # kept, not used up


def test_a_free_hit_for_a_blank_week_leaves_the_squad_alone(limits: Limits) -> None:
    blank = dict.fromkeys(POS, 0.0)  # the whole squad blanks in week 1
    targets: Targets = {
        300 + i: (pos, 300 + i, 50, [0.0, 6.0, 0.0]) for i, pos in enumerate(WHOLE_SQUAD)
    }
    plan, mine = chip_plan(
        limits, [{}, blank, {}], {"freehit": [0, 1, 2]}, {"freehit": 5.0}, targets
    )
    assert plan.chips == (None, "freehit", None)
    assert plan.weeks == ((), (), ())  # the regular squad isn't touched
    fh = plan.free_hit[1]
    assert fh is not None and len(fh) == 15 and all(p >= 300 for p in fh)


def test_chip_inputs_value_keeping_and_use_it_or_lose_it(limits: Limits) -> None:
    xps = [{p: float(v) for p, v in BASE.items()} for _ in range(5)]
    squad_ids = list(POS)
    available = [("3xc", 1, 19), ("bboost", 1, 19), ("wildcard", 2, 19), ("freehit", 2, 19)]
    weeks, values, expiring = chip_inputs(available, 6, xps, squad_ids, POS, limits)
    assert expiring == set()
    assert weeks["3xc"] == [0, 1, 2, 3, 4]
    assert values["3xc"] == pytest.approx(1.25 * 5)  # the squad's best player (keeper 1) scores 5
    # Bench: 15-man total minus the best XI. Keeper 2 (1), and the three lowest outfielders.
    assert values["bboost"] == pytest.approx(1.25 * (1 + 3 + 2 + 1))
    assert values["wildcard"] == 15.0 and values["freehit"] == 12.0
    # At GW16 the GW19 window ends inside the 5-week horizon: keeping is worth nothing.
    weeks, values, expiring = chip_inputs(available, 16, xps, squad_ids, POS, limits)
    assert expiring == {"3xc", "bboost", "wildcard", "freehit"}
    assert values == dict.fromkeys(("3xc", "bboost", "wildcard", "freehit"), 0.0)
    assert weeks["3xc"] == [0, 1, 2, 3]  # GW16-19 only


def test_expiring_chips_are_played_even_when_the_plan_is_indifferent(limits: Limits) -> None:
    """A Free Hit that gains nothing (the squad is already the best) and a Bench Boost about to
    expire: with must_play the plan still uses both, one per week, rather than losing them."""
    plan = plan_transfers(
        squad(),
        0,
        1,
        squad(),
        [{p: float(v) for p, v in BASE.items()} for _ in range(2)],
        limits,
        chips={"freehit": [0, 1], "bboost": [0, 1]},
        chip_values={"freehit": 0.0, "bboost": 0.0},
        must_play={"freehit", "bboost"},
    )
    assert sorted(c for c in plan.chips if c) == ["bboost", "freehit"]


def test_more_expiring_chips_than_weeks_uses_every_week(limits: Limits) -> None:
    """Three chips expire with one week left: one chip a week, so one is played (not infeasible)."""
    plan = plan_transfers(
        squad(),
        0,
        1,
        squad(),
        [{p: float(v) for p, v in BASE.items()}],
        limits,
        chips={"freehit": [0], "bboost": [0], "3xc": [0]},
        chip_values=dict.fromkeys(("freehit", "bboost", "3xc"), 0.0),
        must_play={"freehit", "bboost", "3xc"},
    )
    assert len([c for c in plan.chips if c]) == 1


def test_two_chip_sets_with_the_same_names_keep_the_current_one(limits: Limits) -> None:
    """Both sets are called "bboost" etc. At GW16 the first set (GW1-19) is the one in play and
    expiring; the second (GW20-38) must not overwrite its weeks (the bug the 5d back-test found)."""
    xps = [{p: float(v) for p, v in BASE.items()} for _ in range(5)]  # GW16-20
    both = [("bboost", 1, 19), ("bboost", 20, 38), ("freehit", 2, 19), ("freehit", 20, 38)]
    assert current_chips(both, 16) == [("bboost", 1, 19), ("freehit", 2, 19)]
    assert current_chips(both, 20) == [("bboost", 20, 38), ("freehit", 20, 38)]
    weeks, values, expiring = chip_inputs(both, 16, xps, list(POS), POS, limits)
    assert weeks == {"bboost": [0, 1, 2, 3], "freehit": [0, 1, 2, 3]}  # GW16-19, not GW20
    assert expiring == {"bboost", "freehit"}
