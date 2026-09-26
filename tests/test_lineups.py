# Predicted-lineup baseline: availability from FPL flags, per-match role history (doubles, blanks,
# transfers), shrinkage, surprise non-starts, the starter top-up, and request count; no network.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.calendar import build_calendar
from fpl_agent.data.client import API, FplClient
from fpl_agent.data.lineups import (
    SURPRISE_NON_START,
    LineupPrediction,
    RoleHistory,
    availability,
    load_predictions,
    predict,
    predict_all,
    role_history,
    team_matches_in,
    top_up_starters,
    window_events,
)
from fpl_agent.data.models import (
    Bootstrap,
    EventLive,
    ExplainFixture,
    ExplainStat,
    Fixture,
    LiveElement,
    LiveStats,
    Player,
)
from tests.conftest import FakeResponse, FakeSession, fx, load_fixture, make_bootstrap


def player(bootstrap_json: Any, **update: Any) -> Player:
    base = Bootstrap.model_validate(bootstrap_json).elements[0]
    defaults = {"status": "a", "chance_of_playing_next_round": None, "team": 1}
    return base.model_copy(update={**defaults, **update})


def live(pid: int, starts: int, minutes_by_fixture: dict[int, int]) -> LiveElement:
    return LiveElement(
        id=pid,
        stats=LiveStats(minutes=sum(minutes_by_fixture.values()), starts=starts),
        explain=[
            ExplainFixture(fixture=f, stats=[ExplainStat(identifier="minutes", value=m)])
            for f, m in minutes_by_fixture.items()
        ],
    )


# --- availability ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "chance", "expected"),
    [
        ("a", None, 1.0),  # no flag at all
        ("d", 75, 0.75),  # explicit percentage wins
        ("a", 100, 1.0),  # flag cleared
        ("i", 0, 0.0),
        ("i", None, 0.0),  # injured without a percentage
        ("s", None, 0.0),  # suspended
        ("u", None, 0.0),  # left the league / on loan
        ("d", None, 0.5),  # doubtful without a percentage
    ],
)
def test_availability(
    bootstrap_json: Any, status: str, chance: int | None, expected: float
) -> None:
    assert availability(
        player(bootstrap_json, status=status, chance_of_playing_next_round=chance)
    ) == pytest.approx(expected)


# --- role history ---------------------------------------------------------------------------


def test_role_history_counts_starts_subs_and_unused() -> None:
    lives = [live(9, 1, {1: 90}), live(9, 0, {2: 25}), live(9, 0, {3: 0})]
    h = role_history({1, 2, 3}, lives)
    assert (h.team_matches, h.starts, h.sub_appearances, h.unused) == (3, 1, 1, 1)


def test_double_gameweek_counts_as_two_matches() -> None:
    # Started one match of the double, came off the bench in the other.
    h = role_history({1, 2}, [live(9, 1, {1: 90, 2: 20})])
    assert (h.team_matches, h.starts, h.sub_appearances) == (2, 1, 1)


def test_blank_gameweek_is_not_counted_as_benched(bootstrap_json: Any) -> None:
    season = make_bootstrap(n_teams=4, n_gws=3, base=bootstrap_json)
    # Team 1 plays GW1 and GW3; blank in GW2.
    cal = build_calendar(
        season,
        [
            fx(1, 1, 1, 2, finished=True),
            fx(2, 2, 3, 4, finished=True),
            fx(3, 3, 1, 3, finished=True),
        ],
    )
    matches = team_matches_in(cal, 1, [1, 2, 3])
    assert matches == {1, 3}
    h = role_history(matches, [live(9, 1, {1: 90}), live(9, 1, {3: 90})])
    assert (h.team_matches, h.starts, h.unused) == (2, 2, 0)


def test_unfinished_matches_are_ignored(bootstrap_json: Any) -> None:
    season = make_bootstrap(n_teams=2, n_gws=1, base=bootstrap_json)
    cal = build_calendar(season, [fx(1, 1, 1, 2, finished=False)])
    assert team_matches_in(cal, 1, [1]) == set()


def test_matches_for_a_previous_club_are_ignored() -> None:
    # Transferred: GW1 was for another club (fixture 7), GW2 for the current one (fixture 2).
    h = role_history({2}, [live(9, 1, {7: 90}), live(9, 1, {2: 90})])
    assert (h.team_matches, h.starts, h.sub_appearances) == (1, 1, 0)


# --- predict ---------------------------------------------------------------------------------


def test_ever_present_starter_still_has_surprise_risk(bootstrap_json: Any) -> None:
    p = predict(player(bootstrap_json), RoleHistory(6, 6, 0), prior_start_rate=1.0)
    assert p.p_start == pytest.approx(1 - SURPRISE_NON_START)
    assert p.p_no_minutes == pytest.approx(SURPRISE_NON_START)


def test_one_match_is_shrunk_towards_season_rate(bootstrap_json: Any) -> None:
    # One start, season rate 0: (1 + 0) / (1 + 1) = 0.5 before the surprise factor.
    p = predict(player(bootstrap_json), RoleHistory(1, 1, 0), prior_start_rate=0.0)
    assert p.p_start == pytest.approx(0.5 * (1 - SURPRISE_NON_START))


def test_availability_scales_start_and_cameo(bootstrap_json: Any) -> None:
    doubtful = player(bootstrap_json, status="d", chance_of_playing_next_round=50)
    full = predict(player(bootstrap_json), RoleHistory(4, 2, 2), prior_start_rate=0.5)
    half = predict(doubtful, RoleHistory(4, 2, 2), prior_start_rate=0.5)
    assert half.p_start == pytest.approx(full.p_start / 2)
    assert half.p_cameo == pytest.approx(full.p_cameo / 2)


def test_unavailable_player_never_plays(bootstrap_json: Any) -> None:
    p = predict(player(bootstrap_json, status="i"), RoleHistory(5, 5, 0), prior_start_rate=1.0)
    assert (p.p_start, p.p_cameo, p.p_no_minutes) == (0.0, 0.0, 1.0)


def test_window_events_clips_at_season_start() -> None:
    assert window_events(8, 6) == [2, 3, 4, 5, 6, 7]
    assert window_events(1, 6) == []


# --- real recorded data ----------------------------------------------------------------------


def test_predictions_on_recorded_gw5_are_valid(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    cal = build_calendar(boot, [Fixture.model_validate(f) for f in load_fixture("fixtures")])
    lives = {5: EventLive.model_validate(load_fixture("live_gw5"))}
    preds = predict_all(boot.elements, cal, lives, 6)

    assert set(preds) == {p.id for p in boot.elements}
    for p in preds.values():
        assert 0.0 <= p.p_start <= 1.0 and 0.0 <= p.p_cameo <= 1.0
        assert p.p_no_minutes >= -1e-9
    for pl in boot.elements:
        if availability(pl) == 0.0:
            assert preds[pl.id].p_start == 0.0


def test_load_predictions_fetches_one_live_per_window_gameweek(bootstrap_json: Any) -> None:
    empty = FakeResponse(body={"elements": []})
    routes: dict[str, FakeResponse | list[FakeResponse]] = {
        f"{API}/bootstrap-static/": FakeResponse(body=bootstrap_json),
        **{f"{API}/event/{gw}/live/": empty for gw in range(1, 6)},
    }
    http = FakeSession(routes=routes)
    boot = Bootstrap.model_validate(bootstrap_json)
    load_predictions(FplClient(http), build_calendar(boot, []), 6)
    live_calls = [c for c in http.calls if "/live/" in c[1]]
    assert len(live_calls) == 5  # GW1-5: not one per player


# --- starter top-up (D19) --------------------------------------------------------------------


def lp(
    pid: int, avail: float, start: float, cameo: float, starts: int, subs: int
) -> LineupPrediction:
    return LineupPrediction(pid, avail, start, cameo, RoleHistory(5, starts, subs))


def squad(bootstrap_json: Any, specs: list[tuple[int, int]]) -> list[Player]:
    """Players (id, element_type), all on team 1."""
    return [player(bootstrap_json, id=pid, element_type=et) for pid, et in specs]


def test_backup_keeper_inherits_the_regulars_absence(bootstrap_json: Any) -> None:
    players = squad(bootstrap_json, [(1, 1), (2, 1)])
    preds = {1: lp(1, 0.0, 0.0, 0.0, 5, 0), 2: lp(2, 1.0, 0.0, 0.0, 0, 0)}  # regular injured
    out = top_up_starters(preds, players, {1: 5})
    assert out[2].p_start == pytest.approx(
        1.0
    )  # 5 starts in 5 matches: the team always had a keeper
    assert out[2].topped_up == pytest.approx(1.0)
    assert out[1].p_start == 0.0  # can't exceed his own availability (0)


def test_expected_starters_restored_to_what_the_team_fielded(bootstrap_json: Any) -> None:
    players = squad(bootstrap_json, [(1, 2), (2, 2), (3, 2)])
    preds = {
        1: lp(1, 1.0, 0.9, 0.0, 5, 0),
        2: lp(2, 0.0, 0.0, 0.0, 5, 0),  # injured regular
        3: lp(3, 1.0, 0.1, 0.4, 0, 3),  # bench player who's been coming on
    }
    out = top_up_starters(preds, players, {1: 5})
    assert sum(o.p_start for o in out.values()) == pytest.approx(2.0)  # 10 starts / 5 matches
    assert out[3].p_start > 0.9  # the replacement
    assert out[3].p_start + out[3].p_cameo <= out[3].p_available + 1e-9


def test_involvement_decides_who_inherits(bootstrap_json: Any) -> None:
    players = squad(bootstrap_json, [(1, 3), (2, 3), (3, 3)])
    preds = {
        1: lp(1, 0.0, 0.0, 0.0, 5, 0),  # injured regular
        2: lp(2, 1.0, 0.0, 0.3, 0, 4),  # has been coming on
        3: lp(3, 1.0, 0.0, 0.0, 0, 0),  # never used
    }
    out = top_up_starters(preds, players, {1: 5})
    assert out[2].p_start > 2 * out[3].p_start


def test_caps_are_respected_and_excess_reshared(bootstrap_json: Any) -> None:
    players = squad(bootstrap_json, [(1, 2), (2, 2), (3, 2)])
    preds = {
        1: lp(1, 0.0, 0.0, 0.0, 10, 0),  # 2 starts per match came from these two
        2: lp(2, 0.3, 0.0, 0.0, 0, 5),  # doubtful: can take at most 0.3
        3: lp(3, 1.0, 0.0, 0.0, 0, 1),
    }
    out = top_up_starters(preds, players, {1: 5})
    assert out[2].p_start == pytest.approx(0.3)
    assert out[3].p_start == pytest.approx(1.0)  # capped too: only 1.3 of the 2.0 can be restored


def test_surplus_is_left_alone(bootstrap_json: Any) -> None:
    players = squad(bootstrap_json, [(1, 4), (2, 4)])
    preds = {1: lp(1, 1.0, 0.9, 0.0, 5, 0), 2: lp(2, 1.0, 0.8, 0.0, 0, 0)}  # sums to 1.7 > 1.0
    out = top_up_starters(preds, players, {1: 5})
    assert out == preds


def test_recorded_data_stays_consistent_after_top_up(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    cal = build_calendar(boot, [Fixture.model_validate(f) for f in load_fixture("fixtures")])
    preds = predict_all(
        boot.elements, cal, {5: EventLive.model_validate(load_fixture("live_gw5"))}, 6
    )
    for p in preds.values():
        assert p.p_start + p.p_cameo <= p.p_available + 1e-9
