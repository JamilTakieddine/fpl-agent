# Fixture calendar (rule 7): per-team fixture counts, double/blank gameweek detection, unscheduled
# (postponed) fixtures, and deadlines taken from deadline_time; synthetic seasons + recorded data.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from fpl_agent.data.calendar import build_calendar, count_fixtures, next_deadline
from fpl_agent.data.models import Bootstrap, Fixture
from tests.conftest import T0, fx, load_fixture, make_bootstrap


@pytest.fixture
def season(bootstrap_json: Any) -> Bootstrap:
    return make_bootstrap(n_teams=4, n_gws=3, base=bootstrap_json)


def test_normal_gameweek_is_neither_double_nor_blank(season: Bootstrap) -> None:
    cal = build_calendar(season, [fx(1, 1, 1, 2), fx(2, 1, 3, 4)])
    gw = cal.get(1)
    assert gw.fixture_count == {1: 1, 2: 1, 3: 1, 4: 1}
    assert not gw.is_double and not gw.is_blank


def test_team_playing_twice_makes_a_double(season: Bootstrap) -> None:
    cal = build_calendar(season, [fx(1, 2, 1, 2), fx(2, 2, 3, 4), fx(3, 2, 1, 3, hours=50)])
    gw = cal.get(2)
    assert gw.double_teams == {1, 3}
    assert gw.is_double
    assert cal.double_gameweeks() == [2]


def test_team_without_a_match_makes_a_blank(season: Bootstrap) -> None:
    cal = build_calendar(season, [fx(1, 3, 1, 2)])  # teams 3 and 4 don't play
    gw = cal.get(3)
    assert gw.blank_teams == {3, 4}
    assert gw.fixture_count[3] == 0  # present as 0, not missing
    assert cal.blank_gameweeks() == [1, 2, 3]  # GW1/2 have no fixtures at all here


def test_same_gameweek_can_be_double_and_blank(season: Bootstrap) -> None:
    # Team 1 plays twice, team 4 not at all: happens when a postponed game is squeezed in.
    cal = build_calendar(season, [fx(1, 1, 1, 2), fx(2, 1, 1, 3)])
    gw = cal.get(1)
    assert gw.is_double and gw.is_blank
    assert gw.double_teams == {1}
    assert gw.blank_teams == {4}


def test_unscheduled_fixtures_are_kept_separately(season: Bootstrap) -> None:
    postponed = fx(9, None, 1, 2)
    cal = build_calendar(season, [fx(1, 1, 3, 4), postponed])
    assert cal.unscheduled == (postponed,)
    assert cal.get(1).fixture_count[1] == 0  # not counted in any gameweek
    assert cal.team_unscheduled(1) == 1
    assert cal.team_unscheduled(3) == 0


def test_deadline_comes_from_event_not_kickoff(season: Bootstrap) -> None:
    # First kickoff 90 minutes after the deadline (as in the real GW6); the deadline must not move.
    cal = build_calendar(season, [fx(1, 1, 1, 2, hours=1.5)])
    assert cal.get(1).deadline == T0
    assert cal.get(1).fixtures[0].kickoff_time == T0 + timedelta(hours=1.5)


def test_fixtures_within_a_gameweek_are_sorted_by_kickoff(season: Bootstrap) -> None:
    cal = build_calendar(season, [fx(2, 1, 3, 4, hours=5), fx(1, 1, 1, 2, hours=2)])
    assert [f.id for f in cal.get(1).fixtures] == [1, 2]


def test_window_is_clipped_at_season_end(season: Bootstrap) -> None:
    cal = build_calendar(season, [])
    assert [g.id for g in cal.window(2, 6)] == [2, 3]


def test_count_fixtures_includes_every_team() -> None:
    assert count_fixtures([fx(1, 1, 1, 2)], [1, 2, 3]) == {1: 1, 2: 1, 3: 0}


def test_next_deadline_uses_the_clock(season: Bootstrap) -> None:
    assert next_deadline(season, T0 - timedelta(minutes=15)) == (1, T0)
    # One second after GW1's deadline, the next deadline is GW2's, whatever is_next says.
    assert next_deadline(season, T0 + timedelta(seconds=1)) == (2, T0 + timedelta(weeks=1))
    assert next_deadline(season, T0 + timedelta(weeks=10)) is None


def test_next_deadline_rejects_naive_datetimes(season: Bootstrap) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        next_deadline(season, datetime(2026, 10, 10, 9, 0))


def test_real_recorded_season_builds(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    fixtures = [Fixture.model_validate(f) for f in load_fixture("fixtures")]
    cal = build_calendar(boot, fixtures)
    assert len(cal.gameweeks) == 38
    # The recording covers GW1-8: a full 10-match round each, so no doubles or blanks.
    for gw in range(1, 9):
        assert len(cal.get(gw).fixtures) == 10
        assert not cal.get(gw).is_double and not cal.get(gw).is_blank
