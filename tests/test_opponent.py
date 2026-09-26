# H2H opponent inputs: finding this week's opponent (including the league-AVERAGE case), chips left
# per half-season window, captaincy history, and load_opponent over a fake API; no network.

from __future__ import annotations

from typing import Any

import pytest

from fpl_agent.data.client import API, FplClient
from fpl_agent.data.models import ChipDefinition, ChipPlay, EntryPicks, H2HMatch
from fpl_agent.data.opponent import (
    captain_history,
    chips_remaining,
    find_opponent,
    history_events,
    load_opponent,
)
from tests.conftest import FakeResponse, FakeSession, load_fixture

ME = 1000001  # scripts/record_fixtures.py maps FPL_ENTRY_ID to this
LEAGUE = 2000001


def matches() -> list[H2HMatch]:
    return [H2HMatch.model_validate(m) for m in load_fixture("h2h_matches")["results"]]


def chip_defs(bootstrap_json: Any) -> list[ChipDefinition]:
    return [ChipDefinition.model_validate(c) for c in bootstrap_json["chips"]]


def test_finds_my_opponent_from_either_side() -> None:
    opp = find_opponent(matches(), ME)
    assert opp is not None
    assert opp.entry_id != ME
    # Symmetric: the opponent's opponent is me.
    back = find_opponent(matches(), opp.entry_id)
    assert back is not None and back.entry_id == ME


def test_average_opponent_detected_by_null_entry_not_is_bye() -> None:
    # The recorded data has a real match against "AVERAGE" that says is_bye=false.
    avg = next(m for m in matches() if m.entry_2_entry is None)
    assert avg.is_bye is False
    assert avg.entry_1_entry is not None
    assert find_opponent(matches(), avg.entry_1_entry) is None


def test_entry_not_in_league_raises() -> None:
    with pytest.raises(LookupError):
        find_opponent(matches(), 999)


def test_all_chips_available_before_any_are_played(bootstrap_json: Any) -> None:
    assert chips_remaining(chip_defs(bootstrap_json), [], 6) == {
        "wildcard",
        "freehit",
        "bboost",
        "3xc",
    }


def test_chip_played_in_first_half_is_gone_only_for_first_half(bootstrap_json: Any) -> None:
    played = [ChipPlay(name="wildcard", event=5)]
    assert "wildcard" not in chips_remaining(chip_defs(bootstrap_json), played, 6)
    # Second half has its own wildcard (rule 3).
    assert "wildcard" in chips_remaining(chip_defs(bootstrap_json), played, 25)


def test_first_half_chips_are_gone_in_second_half_even_if_unused(bootstrap_json: Any) -> None:
    # Unused first-set chips are forfeited at GW19: at GW20 only the second set exists.
    played = [ChipPlay(name=n, event=30) for n in ("wildcard", "freehit", "bboost", "3xc")]
    assert chips_remaining(chip_defs(bootstrap_json), played, 31) == frozenset()


def test_freehit_is_not_available_in_gw1(bootstrap_json: Any) -> None:
    # The FPL data starts the first free hit and wildcard at GW2.
    assert chips_remaining(chip_defs(bootstrap_json), [], 1) == {"bboost", "3xc"}


def test_captain_history_is_oldest_first_and_records_chip() -> None:
    raw = load_fixture("opp_picks")
    gw4 = EntryPicks.model_validate(raw)
    gw5 = EntryPicks.model_validate(dict(raw, active_chip="3xc"))
    history = captain_history({5: gw5, 4: gw4})
    assert [c.event for c in history] == [4, 5]
    assert history[1].chip == "3xc"
    assert history[0].captain != history[0].vice_captain


def test_history_events_clips_at_season_start() -> None:
    assert history_events(8, 5) == [3, 4, 5, 6, 7]
    assert history_events(3, 5) == [1, 2]
    assert history_events(1, 5) == []


def fake_league_api(bootstrap_json: Any, opp: int) -> FakeSession:
    routes: dict[str, FakeResponse | list[FakeResponse]] = {
        f"{API}/bootstrap-static/": FakeResponse(body=bootstrap_json),
        f"{API}/leagues-h2h-matches/league/{LEAGUE}/?event=6&page=1": FakeResponse(
            body=load_fixture("h2h_matches")
        ),
        f"{API}/entry/{opp}/history/": FakeResponse(
            body=dict(load_fixture("opp_history"), chips=[{"name": "wildcard", "event": 5}])
        ),
    }
    for gw in range(1, 6):
        # GW1 missing (joined late) to check that gaps are skipped, not fatal.
        routes[f"{API}/entry/{opp}/event/{gw}/picks/"] = (
            FakeResponse(status_code=404, body={"detail": "Not found."})
            if gw == 1
            else FakeResponse(body=load_fixture("opp_picks"))
        )
    return FakeSession(routes=routes)


def test_load_opponent_assembles_snapshot(bootstrap_json: Any) -> None:
    opp = find_opponent(matches(), ME)
    assert opp is not None
    http = fake_league_api(bootstrap_json, opp.entry_id)
    snap = load_opponent(FplClient(http), LEAGUE, ME, 6)

    assert snap is not None
    assert snap.opponent == opp
    assert [c.event for c in snap.captain_history] == [2, 3, 4, 5]  # GW1 404 skipped
    assert snap.last_picks is not None
    assert "wildcard" not in snap.chips_remaining
    assert "freehit" in snap.chips_remaining
    # Politeness: 1 matches page + 1 history + 5 picks + 1 bootstrap.
    assert len(http.calls) == 8


def test_upcoming_picks_are_none_not_an_error() -> None:
    http = FakeSession(
        routes={f"{API}/entry/5/event/6/picks/": FakeResponse(status_code=404, body={})}
    )
    assert FplClient(http).entry_picks(5, 6) is None


def test_h2h_matches_follows_pagination() -> None:
    page = load_fixture("h2h_matches")
    base = f"{API}/leagues-h2h-matches/league/{LEAGUE}/?event=6"
    http = FakeSession(
        routes={
            f"{base}&page=1": FakeResponse(body=dict(page, has_next=True)),
            f"{base}&page=2": FakeResponse(body=dict(page, page=2, has_next=False)),
        }
    )
    result = FplClient(http).h2h_matches(LEAGUE, 6)
    assert len(result) == 2 * len(page["results"])
