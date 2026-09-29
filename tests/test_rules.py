# Phase 3 part 1: the FPL rules engine - squad and formation validity, auto-subs, captaincy, team
# points, transfers, selling price and chips - including replays of real FPL team-gameweeks
# (anonymised) that must reproduce FPL's own auto-subs and official points; no network.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, Chip, ChipDefinition, PositionCode
from fpl_agent.optimize.rules import (
    Limits,
    Lineup,
    SquadPlayer,
    auto_subs,
    chip_playable,
    chip_window,
    chips_expiring,
    formation_violations,
    lineup_violations,
    multipliers,
    next_free_transfers,
    selling_price,
    squad_violations,
    team_points,
    transfer_cost,
)
from tests.conftest import load_fixture

CODE: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def test_limits_come_from_the_api(limits: Limits) -> None:
    assert dict(limits.squad_counts) == {"GKP": 2, "DEF": 5, "MID": 5, "FWD": 3}
    assert (limits.min_play["DEF"], limits.max_play["DEF"]) == (3, 5)
    assert (limits.club_limit, limits.squad_size, limits.starters) == (3, 15, 11)
    assert limits.max_free_transfers == 5


# --- a legal squad to build on: 2 GK (1,2), 5 DEF (3-7), 5 MID (8-12), 3 FWD (13-15) -----------

POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}


def squad(teams: dict[int, int] | None = None) -> list[SquadPlayer]:
    teams = teams or {}
    return [SquadPlayer(pid, pos, teams.get(pid, pid), 50) for pid, pos in POS.items()]


# A 4-4-2: GK 1; DEF 3-6; MID 8-11; FWD 13-14. Bench: GK 2, then 7 (DEF), 12 (MID), 15 (FWD).
LINEUP = Lineup(
    starters=(1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14),
    bench=(2, 7, 12, 15),
    captain=13,
    vice_captain=8,
)
ALL_PLAYED = dict.fromkeys(POS, True)


def test_legal_squad_and_lineup(limits: Limits) -> None:
    assert squad_violations(squad(), limits) == []
    assert lineup_violations(LINEUP, POS, limits) == []


def test_club_limit_and_counts(limits: Limits) -> None:
    four_from_one = squad(dict.fromkeys((3, 4, 5, 8), 99))
    assert squad_violations(four_from_one, limits) == ["4 players from team 99 (max 3)"]
    short = squad_violations(squad()[:14], limits)
    assert "14 players (need 15)" in short and "2 FWD (need 3)" in short
    assert squad_violations(squad(), limits, bank=-5) == ["over budget by 0.5m"]


def shape(gk: int, d: int, m: int, f: int) -> list[PositionCode]:
    counts: dict[PositionCode, int] = {"GKP": gk, "DEF": d, "MID": m, "FWD": f}
    return [pos for pos, n in counts.items() for _ in range(n)]


def test_formations(limits: Limits) -> None:
    assert formation_violations(shape(1, 3, 5, 2), limits) == []
    assert formation_violations(shape(1, 2, 5, 3), limits)  # too few defenders
    assert formation_violations(shape(2, 4, 4, 1), limits)  # two goalkeepers
    assert formation_violations(shape(1, 5, 5, 0), limits)  # no forward


def test_lineup_violations(limits: Limits) -> None:
    bad = Lineup(LINEUP.starters, (7, 2, 12, 15), captain=13, vice_captain=13)
    problems = lineup_violations(bad, POS, limits)
    assert "the first bench slot must be the substitute goalkeeper" in problems
    assert "captain and vice-captain must differ" in problems


# --- auto-subs (rule 2) --------------------------------------------------------------------------


def test_everyone_played_means_no_subs(limits: Limits) -> None:
    xi, subs = auto_subs(LINEUP, ALL_PLAYED, POS, limits)
    assert xi == LINEUP.starters and subs == []


def test_goalkeeper_swap_is_separate(limits: Limits) -> None:
    played = ALL_PLAYED | {1: False}
    xi, subs = auto_subs(LINEUP, played, POS, limits)
    assert subs == [(1, 2)] and 2 in xi
    # An outfield bench player never replaces the goalkeeper, even if the sub keeper didn't play.
    _, subs = auto_subs(LINEUP, played | {2: False}, POS, limits)
    assert subs == []


def test_bench_priority_order(limits: Limits) -> None:
    # Midfielder 8 misses: the first bench outfielder (DEF 7) comes on, legal as 5-3-2.
    _, subs = auto_subs(LINEUP, ALL_PLAYED | {8: False}, POS, limits)
    assert subs == [(8, 7)]


def test_formation_blocked_sub_skips_to_next_bench_player(limits: Limits) -> None:
    # A 3-4-3 loses defender 3. Midfielder 12 is first on the bench, but bringing him on would
    # leave 2 defenders, so he is skipped for the next bench defender (6).
    three_four_three = Lineup((1, 3, 4, 5, 8, 9, 10, 11, 13, 14, 15), (2, 12, 6, 7), 13, 8)
    _, subs = auto_subs(three_four_three, ALL_PLAYED | {3: False}, POS, limits)
    assert subs == [(3, 6)]


def test_formation_counts_the_final_team(limits: Limits) -> None:
    # Both forwards of a 3-5-2 miss with a DEF, DEF, FWD bench: the first defender legally
    # replaces one forward (4-5-1), then the forward replaces the other; FPL does the same.
    three_five_two = Lineup((1, 3, 4, 5, 8, 9, 10, 11, 12, 13, 14), (2, 6, 7, 15), 14, 8)
    _, subs = auto_subs(three_five_two, ALL_PLAYED | {13: False, 14: False}, POS, limits)
    assert subs == [(13, 6), (14, 15)]


def test_subs_that_did_not_play_are_skipped(limits: Limits) -> None:
    _, subs = auto_subs(LINEUP, ALL_PLAYED | {8: False, 7: False}, POS, limits)
    assert subs == [(8, 12)]


# --- captaincy (rule 4) and points ---------------------------------------------------------------


def test_armband_goes_to_the_vice_only_if_the_captain_did_not_play() -> None:
    assert multipliers(LINEUP, ALL_PLAYED)[13] == 2
    no_captain = multipliers(LINEUP, ALL_PLAYED | {13: False})
    assert no_captain[13] == 1 and no_captain[8] == 2
    assert max(multipliers(LINEUP, ALL_PLAYED | {13: False, 8: False}).values()) == 1
    assert multipliers(LINEUP, ALL_PLAYED, chip="3xc")[13] == 3


def test_team_points_with_chips(limits: Limits) -> None:
    points = dict.fromkeys(POS, 2) | {13: 10, 2: 6}
    base = 2 * 10 + 10 * 2  # captain doubled + ten other starters
    assert team_points(LINEUP, points, ALL_PLAYED, POS, limits) == base
    assert team_points(LINEUP, points, ALL_PLAYED, POS, limits, chip="3xc") == base + 10
    bench = 6 + 2 + 2 + 2
    assert team_points(LINEUP, points, ALL_PLAYED, POS, limits, chip="bboost") == base + bench


@pytest.mark.parametrize("case", load_fixture("autosub_cases"), ids=lambda c: "+".join(c["covers"]))
def test_real_fpl_team_gameweeks_are_reproduced(case: dict[str, Any], limits: Limits) -> None:
    """Replays of real H2H-league team-gameweeks: FPL returns the lineup AFTER its auto-subs, so
    undo them to recover the original team sheet, then our rules must re-derive exactly FPL's
    substitutions and its official points (captaincy, vice-captain and chips included)."""
    players = {int(k): v for k, v in case["players"].items()}
    pos = {pid: CODE[p["element_type"]] for pid, p in players.items()}
    order = [p["element"] for p in sorted(case["picks"], key=lambda p: p["position"])]
    for s in reversed(case["automatic_subs"]):
        i, j = order.index(s["element_in"]), order.index(s["element_out"])
        order[i], order[j] = order[j], order[i]
    flags = {p["element"]: p for p in case["picks"]}
    lineup = Lineup(
        tuple(order[:11]),
        tuple(order[11:]),
        next(e for e in order if flags[e]["is_captain"]),
        next(e for e in order if flags[e]["is_vice_captain"]),
    )
    played = {pid: p["minutes"] > 0 for pid, p in players.items()}
    points = {pid: p["points"] for pid, p in players.items()}
    chip = case["active_chip"]
    if chip != "bboost":
        _, subs = auto_subs(lineup, played, pos, limits)
        expected = [(s["element_out"], s["element_in"]) for s in case["automatic_subs"]]
        assert sorted(subs) == sorted(expected)
    assert team_points(lineup, points, played, pos, limits, chip) == case["official_points"]


# --- transfers (rules 1 and 5) --------------------------------------------------------------------


def test_transfer_cost() -> None:
    assert transfer_cost(1, free=1) == 0
    assert transfer_cost(3, free=1) == 8
    assert transfer_cost(5, free=1, chip="wildcard") == 0
    assert transfer_cost(2, free=1, chip="freehit") == 0


def test_free_transfers_bank_up_to_five(limits: Limits) -> None:
    assert next_free_transfers(1, 0, limits) == 2
    assert next_free_transfers(5, 0, limits) == 5  # capped
    assert next_free_transfers(3, 2, limits) == 2
    assert next_free_transfers(1, 3, limits) == 1  # took hits: back to one
    assert next_free_transfers(4, 9, limits, chip="wildcard") == 4


@pytest.mark.parametrize(
    ("purchase", "current", "expected"),
    [(50, 53, 51), (50, 54, 52), (50, 51, 50), (50, 48, 48), (50, 50, 50)],
)
def test_selling_price_keeps_half_the_rise_rounded_down(
    purchase: int, current: int, expected: int
) -> None:
    assert selling_price(purchase, current) == expected


# --- chips (rule 3) -------------------------------------------------------------------------------


def chip(name: str, status: str, start: int, stop: int) -> Chip:
    return Chip(
        id=1,
        name=name,
        status_for_entry=status,
        start_event=start,
        stop_event=stop,
        is_pending=False,
    )


def test_chip_windows_and_status() -> None:
    mine = [chip("bboost", "available", 1, 19), chip("wildcard", "played", 2, 19)]
    assert chip_playable("bboost", 6, mine)
    assert not chip_playable("bboost", 20, mine)  # the first set is over
    assert not chip_playable("wildcard", 6, mine)  # already played


def test_chips_expiring_before_the_gw19_deadline() -> None:
    mine = [
        chip("bboost", "available", 1, 19),
        chip("3xc", "available", 1, 19),
        chip("freehit", "played", 2, 19),
    ]
    assert chips_expiring(19, mine) == ["3xc", "bboost"]
    assert chips_expiring(15, mine, within=4) == ["3xc", "bboost"]
    assert chips_expiring(6, mine, within=4) == []


def test_chip_window_from_definitions(bootstrap_json: Any) -> None:
    defs = [ChipDefinition.model_validate(c) for c in bootstrap_json["chips"]]
    first, second = chip_window("wildcard", 6, defs), chip_window("wildcard", 25, defs)
    assert first is not None and (first.start_event, first.stop_event) == (2, 19)
    assert second is not None and second.start_event == 20
