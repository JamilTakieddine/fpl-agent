# Phase 3 part 5: transfer recommendations - the agreed thresholds (free, capped, hits), the quick
# lineup value against brute force, and planted squads (a clear upgrade, a buy only a second sale
# can fund, the club limit, a marginal gain that only the 5-transfer cap makes worth taking);
# synthetic simulations, no network. The flag-duration rules for later weeks are tested too.

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pytest

from fpl_agent.data.injuries import as_of, return_date
from fpl_agent.data.models import Bootstrap, Player, PositionCode
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.lineup import legal_xis
from fpl_agent.optimize.rules import Limits, SquadPlayer
from fpl_agent.optimize.transfers import (
    Move,
    quick_value,
    recommend_transfers,
    required_gain,
)

POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}
# Constant points per player (everyone always plays): GK 5/1, DEF 3, MID 4 (12 scores 3), FWD 3/2/1.
BASE_POINTS = {1: 5, 2: 1, **dict.fromkeys(range(3, 8), 3), **dict.fromkeys(range(8, 12), 4)}
BASE_POINTS |= {12: 3, 13: 1, 14: 3, 15: 2}


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def samples(points: dict[int, int], n: int = 20) -> PointsSamples:
    ids = sorted(points)
    return PointsSamples(
        player=np.array(ids, dtype=np.int64),
        total=np.array([[points[p]] * n for p in ids], dtype=np.int32),
        played=np.ones((len(ids), n), dtype=np.bool_),
        breakdown={},
    )


def squad(teams: dict[int, int] | None = None) -> list[SquadPlayer]:
    teams = teams or {}
    return [SquadPlayer(p, pos, teams.get(p, p), 50) for p, pos in POS.items()]


Target = tuple[int, PositionCode, int, int, int]  # id, position, team, price, points


def plan_for(
    targets: list[Target],
    limits: Limits,
    free: int,
    bank: int = 0,
    teams: dict[int, int] | None = None,
) -> Any:
    mine = squad(teams)
    pool = [*mine, *(SquadPlayer(i, pos, team, price) for i, pos, team, price, _ in targets)]
    positions = {**POS, **{i: pos for i, pos, *_ in targets}}
    week = samples(BASE_POINTS | {i: pts for i, _, _, _, pts in targets})
    return recommend_transfers(mine, bank, free, pool, [week], positions, limits)


# --- thresholds ----------------------------------------------------------------------------------


def test_thresholds_for_free_transfers_and_hits(limits: Limits) -> None:
    assert required_gain(0, 1, limits) == 0
    assert required_gain(1, 1, limits) == 1.5
    assert required_gain(2, 1, limits) == 1.5 + 4 + 2  # the second is a hit
    assert required_gain(2, 2, limits) == 3.0
    # At the 5-transfer cap, rolling would lose one: the first costs nothing to use.
    assert required_gain(1, 5, limits) == 0
    assert required_gain(3, 5, limits) == 3.0


# --- the quick lineup value ----------------------------------------------------------------------


def test_quick_value_matches_brute_force_over_legal_xis(limits: Limits) -> None:
    rng = np.random.default_rng(0)
    for _ in range(20):
        xp = {p: float(v) for p, v in zip(POS, rng.uniform(0, 8, size=15), strict=True)}
        brute = max(
            sum(xp[p] for p in starters) + max(xp[p] for p in starters)
            for starters, _, _ in legal_xis(tuple(POS), POS, limits)
        )
        assert quick_value(xp, tuple(POS), POS, limits) == pytest.approx(brute)


# --- planted squads ------------------------------------------------------------------------------


def test_a_clear_upgrade_is_recommended(limits: Limits) -> None:
    upgrade: list[Target] = [(100, "FWD", 100, 50, 7)]
    plan = plan_for(upgrade, limits, free=1)
    (move,) = plan.best.moves
    assert move.buy == 100 and POS[move.sell] == "FWD"
    assert plan.best.gain > 1.5 and plan.best.hit == 0 and plan.best.bank == 0


def test_a_second_sale_can_fund_a_dearer_buy(limits: Limits) -> None:
    """Forward 101 costs 7.0m with nothing in the bank: only selling a 5.0m midfielder for a
    3.0m one (who also scores more) pays for him."""
    targets: list[Target] = [(101, "FWD", 101, 70, 9), (102, "MID", 102, 30, 5)]
    plan = plan_for(targets, limits, free=2)
    assert {m.buy for m in plan.best.moves} == {101, 102}
    assert plan.best.bank == 0 and plan.best.hit == 0


def test_the_club_limit_blocks_a_fourth_player(limits: Limits) -> None:
    three_from_7 = dict.fromkeys((3, 4, 5), 7)
    fourth: list[Target] = [(103, "FWD", 7, 50, 9)]
    plan = plan_for(fourth, limits, free=1, teams=three_from_7)
    assert plan.best.moves == ()


def test_a_marginal_gain_needs_the_cap_to_be_worth_a_transfer(limits: Limits) -> None:
    """Forward 104 scores 4 against the XI forward's 3: +1 point, short of the 1.5 a free transfer
    must earn - unless rolling would lose it to the 5-transfer cap."""
    target: list[Target] = [(104, "FWD", 104, 50, 4)]
    assert plan_for(target, limits, free=1).best.moves == ()
    capped = plan_for(target, limits, free=5).best
    # Selling the bench forward (13) or the XI one (14) gains the same; either is fine.
    assert capped.moves in ((Move(13, 104),), (Move(14, 104),))
    assert capped.gain == pytest.approx(1.0)


def test_a_hit_needs_six_points(limits: Limits) -> None:
    """Two upgrades of about +3 each with one free transfer: the second would be a hit, and +3
    doesn't cover 4 + 2."""
    targets: list[Target] = [(105, "FWD", 105, 50, 6), (106, "MID", 106, 50, 7)]
    best = plan_for(targets, limits, free=1).best
    assert len(best.moves) == 1 and best.hit == 0


# --- how long a flag lasts (data/injuries.py) ----------------------------------------------------

NEXT = datetime(2026, 10, 10, 10, tzinfo=UTC)
LATER = datetime(2026, 10, 24, 10, tzinfo=UTC)
TODAY = date(2026, 9, 30)


def player(bootstrap_json: Any, status: str, chance: int | None, news: str) -> Player:
    p = Bootstrap.model_validate(bootstrap_json).elements[0]
    return p.model_copy(
        update={"status": status, "chance_of_playing_next_round": chance, "news": news}
    )


def test_return_dates_are_read_from_the_news() -> None:
    assert return_date("Hamstring injury - Expected back 10 Oct", TODAY) == date(2026, 10, 10)
    assert return_date("Suspended until 03 Jan", TODAY) == date(2027, 1, 3)  # across New Year
    assert return_date("Has joined Oxford United on loan until January", TODAY) is None
    assert return_date("Knee injury - Unknown return date", TODAY) is None


def test_flags_in_later_weeks(bootstrap_json: Any) -> None:
    knock = player(bootstrap_json, "d", 75, "Knee injury - 75% chance of playing")
    assert as_of(knock, NEXT, NEXT, TODAY) is knock  # the next gameweek: as flagged
    assert as_of(knock, LATER, NEXT, TODAY).chance_of_playing_next_round is None  # then fit
    dated = player(bootstrap_json, "i", 0, "Hamstring injury - Expected back 30 Oct")
    assert as_of(dated, LATER, NEXT, TODAY).status == "i"  # not back yet
    back = datetime(2026, 10, 31, 10, tzinfo=UTC)
    assert as_of(dated, back, NEXT, TODAY).status == "a"
    unknown = player(bootstrap_json, "i", 0, "Knee injury - Unknown return date")
    assert as_of(unknown, LATER, NEXT, TODAY).status == "i"  # cautious: out all horizon
    gone = player(bootstrap_json, "u", 0, "Has joined Oxford United on loan until January")
    assert as_of(gone, LATER, NEXT, TODAY).status == "u"


def test_with_hits_off_no_free_transfer_means_no_transfer(limits: Limits) -> None:
    """A +6 upgrade would clear the 4 + 2 bar for a hit, but with hits off (D37) and no free
    transfer left the planner rolls."""
    mine = squad()
    target = SquadPlayer(107, "FWD", 107, 50)
    week = samples(BASE_POINTS | {107: 9})
    with_hits = recommend_transfers(
        mine, 0, 0, [*mine, target], [week], {**POS, 107: "FWD"}, limits
    )
    assert with_hits.best.moves and with_hits.best.hit == 4
    no_hits = recommend_transfers(
        mine, 0, 0, [*mine, target], [week], {**POS, 107: "FWD"}, limits, allow_hits=False
    )
    assert no_hits.best.moves == ()
