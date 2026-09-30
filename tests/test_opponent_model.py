# Phase 3 part 3: the opponent model - team sheet recovered from FPL's post-auto-sub picks (real
# cases), the captain spread and its maximum-likelihood fit, chip chances, per-simulation draws,
# and the FPL-wide "AVERAGE" opponent; synthetic simulations, no network.

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from fpl_agent.data.calendar import Calendar, Gameweek
from fpl_agent.data.models import (
    AutomaticSub,
    Bootstrap,
    ChipDefinition,
    EntryPicks,
    PositionCode,
    PublicPick,
)
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.opponent import (
    CHIP_CAP,
    CHIP_FLOOR,
    CaptainCase,
    OpponentModel,
    average_points,
    build_opponent_model,
    captain_probabilities,
    chip_probabilities,
    fit_average_scale,
    fit_captain,
    opponent_points,
    team_sheet,
)
from fpl_agent.optimize.rules import Limits, Lineup, auto_subs
from fpl_agent.optimize.scoring import SquadSims, score_lineup
from tests.conftest import load_fixture

CODE: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}
# A 4-4-2 (GK 1; DEF 3-6; MID 8-11; FWD 13-14), bench GK 2, then 7, 12, 15.
SHEET = Lineup((1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14), (2, 7, 12, 15), captain=13, vice_captain=8)


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


# --- team sheet ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [c for c in load_fixture("autosub_cases") if c["active_chip"] != "bboost"],
    ids=lambda c: "+".join(c["covers"]),
)
def test_team_sheet_undoes_fpls_auto_subs(case: dict[str, Any], limits: Limits) -> None:
    """Real cases: from the post-auto-sub picks FPL returns, recover the sheet; re-running the
    rules on it must make exactly FPL's substitutions."""
    players = {int(k): v for k, v in case["players"].items()}
    picks = EntryPicks(
        picks=[
            PublicPick(**p, multiplier=1, element_type=players[p["element"]]["element_type"])
            for p in case["picks"]
        ],
        active_chip=case["active_chip"],
        automatic_subs=[AutomaticSub(**s) for s in case["automatic_subs"]],
    )
    sheet = team_sheet(picks, limits)
    played = {pid: p["minutes"] > 0 for pid, p in players.items()}
    pos = {pid: CODE[p["element_type"]] for pid, p in players.items()}
    _, subs = auto_subs(sheet, played, pos, limits)
    assert sorted(subs) == sorted(
        (s["element_out"], s["element_in"]) for s in case["automatic_subs"]
    )


# --- captain -------------------------------------------------------------------------------------


def test_captain_spread_mixes_habit_and_expected_points() -> None:
    starters = [1, 2, 3]
    xp = {1: 8.0, 2: 6.0, 3: 2.0}
    fresh = captain_probabilities(starters, xp, history=[])
    assert sum(fresh.values()) == pytest.approx(1)
    assert fresh[1] > fresh[2] > fresh[3]  # no history: expected points only
    loyal = captain_probabilities(starters, xp, history=[2, 2, 2, 2], habit_weight=0.8)
    assert loyal[2] > loyal[1]  # habit outweighs a 2-point edge
    assert loyal[2] == pytest.approx(0.8 + 0.2 * fresh[2])
    # A past captain who is no longer a starter doesn't count as habit.
    assert captain_probabilities(starters, xp, history=[99]) == pytest.approx(fresh)


def test_injured_habit_captain_loses_his_habit_share() -> None:
    starters, xp, history = [1, 2, 3], {1: 8.0, 2: 6.0, 3: 2.0}, [2, 2, 2, 2]
    fresh = captain_probabilities(starters, xp, history=[])
    out = captain_probabilities(starters, xp, history, 0.8, availability={2: 0.0})
    assert out == pytest.approx(fresh)  # ruled out: all of it goes to the expected-points part
    doubtful = captain_probabilities(starters, xp, history, 0.8, availability={2: 0.75})
    assert doubtful[2] == pytest.approx(0.8 * 0.75 + (1 - 0.6) * fresh[2])
    assert sum(doubtful.values()) == pytest.approx(1)


def test_fit_recovers_known_parameters() -> None:
    """Draw decisions from a known (habit weight, temperature); the fit must find them."""
    rng = np.random.default_rng(1)
    true_w, true_t = 0.6, 1.0
    cases = []
    for _ in range(1500):
        starters = tuple(range(11))
        xp = dict(enumerate(rng.uniform(2, 8, size=11).tolist()))
        history = tuple(int(x) for x in rng.integers(0, 11, size=3))
        probs = captain_probabilities(starters, xp, history, true_w, true_t)
        captain = int(rng.choice(starters, p=list(probs.values())))
        cases.append(CaptainCase(starters, xp, history, captain))
    w, t, _ = fit_captain(cases)
    assert w == pytest.approx(true_w, abs=0.1)
    assert t in (0.75, 1.0, 1.5)


# --- chips ---------------------------------------------------------------------------------------

FIRST_HALF = [ChipDefinition(name=c, start_event=1, stop_event=19) for c in ("3xc", "bboost")]


def calendar(doubles: set[int]) -> Calendar:
    t = datetime(2026, 8, 1, tzinfo=UTC)
    return Calendar(
        {gw: Gameweek(gw, t, (), {1: 2 if gw in doubles else 1}) for gw in range(1, 39)}, ()
    )


def test_no_chip_threat_without_the_chip_or_outside_chip_worthy_weeks() -> None:
    cal = calendar({8, 12, 16})
    assert chip_probabilities(8, set(), FIRST_HALF, cal) == {}  # nothing held
    assert chip_probabilities(7, {"3xc"}, FIRST_HALF, cal) == {}  # an ordinary week


def test_chip_chance_rises_as_the_window_closes() -> None:
    cal = calendar({8, 12, 16})
    assert chip_probabilities(8, {"3xc"}, FIRST_HALF, cal)["3xc"] == pytest.approx(CHIP_FLOOR)
    # Doubles 12, 16 and the two forfeit weeks 18, 19 are left: 1/4 -> floored at 30%.
    assert chip_probabilities(12, {"3xc"}, FIRST_HALF, cal)["3xc"] == pytest.approx(CHIP_FLOOR)
    assert chip_probabilities(16, {"3xc"}, FIRST_HALF, cal)["3xc"] == pytest.approx(1 / 3)
    assert chip_probabilities(18, {"3xc"}, FIRST_HALF, cal)["3xc"] == pytest.approx(1 / 2)
    assert chip_probabilities(19, {"3xc"}, FIRST_HALF, cal)["3xc"] == pytest.approx(CHIP_CAP)


def test_forfeit_weeks_count_even_without_doubles() -> None:
    cal = calendar(set())
    assert chip_probabilities(17, {"bboost"}, FIRST_HALF, cal) == {}
    assert chip_probabilities(18, {"bboost"}, FIRST_HALF, cal)["bboost"] == pytest.approx(0.5)


def test_one_chip_per_week_caps_the_total() -> None:
    both = chip_probabilities(19, {"3xc", "bboost"}, FIRST_HALF, calendar(set()))
    assert sum(both.values()) == pytest.approx(CHIP_CAP)
    assert both["3xc"] == pytest.approx(both["bboost"])


# --- scoring the opponent ------------------------------------------------------------------------


def sims(n: int, seed: int = 0) -> SquadSims:
    rng = np.random.default_rng(seed)
    played = rng.random((15, n)) > 0.15
    points = np.where(played, rng.integers(1, 12, size=(15, n)), 0).astype(np.int32)
    return SquadSims(tuple(POS), points, played)


def test_a_certain_captain_scores_like_my_own_team(limits: Limits) -> None:
    s = sims(500)
    model = OpponentModel(SHEET, {p: float(p == 13) for p in SHEET.starters}, {})
    got = opponent_points(s, model, POS, limits, np.random.default_rng(0))
    vice = SHEET.starters[0]  # all others tie at 0: the first in the ranking
    expected = score_lineup(s, Lineup(SHEET.starters, SHEET.bench, 13, vice), POS, limits)
    assert (got == expected).all()


def test_captain_and_chip_are_drawn_per_simulation(limits: Limits) -> None:
    n = 20_000
    s = sims(n)
    probs = {p: 0.0 for p in SHEET.starters} | {13: 0.5, 8: 0.5}
    model = OpponentModel(SHEET, probs, {"3xc": 0.3})
    got = opponent_points(s, model, POS, limits, np.random.default_rng(0))
    options = {
        (c, chip): score_lineup(
            s, Lineup(SHEET.starters, SHEET.bench, c, 8 if c == 13 else 13), POS, limits, chip
        )
        for c in (13, 8)
        for chip in (None, "3xc")
    }
    stacked = np.stack(list(options.values()))  # rows: (13,-), (13,TC), (8,-), (8,TC)
    assert (stacked == got).any(axis=0).all()  # every simulation is one of the four outcomes
    # Where all four outcomes differ, which one was drawn is unambiguous: check the frequencies.
    distinct = np.array([len(set(col)) == 4 for col in stacked.T])
    which = (stacked[:, distinct] == got[distinct]).argmax(axis=0)
    assert np.isin(which, [0, 1]).mean() == pytest.approx(0.5, abs=0.02)  # captain 13
    assert np.isin(which, [1, 3]).mean() == pytest.approx(0.3, abs=0.02)  # Triple Captain


def test_build_opponent_model_puts_the_armband_on_the_two_most_likely(limits: Limits) -> None:
    picks = EntryPicks(
        picks=[
            PublicPick(
                element=p,
                position=i + 1,
                multiplier=1,
                is_captain=p == 13,
                is_vice_captain=p == 8,
                element_type=[k for k, v in CODE.items() if v == POS[p]][0],
            )
            for i, p in enumerate(SHEET.starters + SHEET.bench)
        ],
        active_chip=None,
    )
    xp = dict.fromkeys(POS, 2.0) | {14: 9.0}
    model = build_opponent_model(picks, [13, 13, 13], xp, {}, limits)
    assert (model.lineup.captain, model.lineup.vice_captain) == (13, 14)
    assert model.lineup.starters == SHEET.starters


# --- the "AVERAGE" opponent ----------------------------------------------------------------------


def test_average_is_scaled_ownership_weighted_points() -> None:
    samples = PointsSamples(
        player=np.array([1, 2], dtype=np.int64),
        total=np.array([[10, 0], [4, 6]], dtype=np.int32),
        played=np.ones((2, 2), dtype=np.bool_),
        breakdown={},
    )
    ownership = {1: 50.0, 2: 25.0, 3: 90.0}  # 3 has no match this gameweek
    # sim 0: 0.5*10 + 0.25*4 = 6.0 -> x0.9 = 5.4 -> 5;  sim 1: 0.25*6 = 1.5 -> 1.35 -> 1
    assert average_points(samples, ownership, scale=0.9).tolist() == [5, 1]


def test_fit_average_scale_is_least_squares_through_the_origin() -> None:
    assert fit_average_scale([50.0, 100.0], [45, 90]) == pytest.approx(0.9)
