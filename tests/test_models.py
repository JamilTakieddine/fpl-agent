# Parsing recorded FPL responses into pydantic models: shapes, helpers, and loud failure on
# missing fields; uses committed fixtures only, no live API.

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from fpl_agent.data.models import Bootstrap, Fixture, MyTeam
from tests.conftest import load_fixture


def test_bootstrap_parses_and_finds_next_event(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    nxt = boot.next_event()
    assert nxt is not None
    assert nxt.deadline_time.tzinfo is not None  # deadlines must be timezone-aware
    assert len(boot.teams) == 20


def test_prices_stay_integer_tenths(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    assert all(isinstance(p.now_cost, int) for p in boot.elements)


def test_scoring_reads_flat_and_per_position_values(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    scoring = bootstrap_json["game_config"]["scoring"]
    # Per-position table (goals are worth more for defenders than forwards).
    assert boot.points_for("goals_scored", "DEF") == scoring["goals_scored"]["DEF"]
    assert boot.points_for("goals_scored", "DEF") > boot.points_for("goals_scored", "FWD")
    # Flat value: same for every position.
    assert boot.points_for("assists", "MID") == scoring["assists"]


def test_unknown_fields_are_ignored(bootstrap_json: Any) -> None:
    data = copy.deepcopy(bootstrap_json)
    data["elements"][0]["brand_new_field_fpl_added"] = 123
    Bootstrap.model_validate(data)  # must not raise


def test_missing_required_field_fails_loudly(bootstrap_json: Any) -> None:
    data = copy.deepcopy(bootstrap_json)
    del data["events"][0]["deadline_time"]
    with pytest.raises(ValidationError, match="deadline_time"):
        Bootstrap.model_validate(data)


def test_models_are_frozen(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    with pytest.raises(ValidationError):
        boot.elements[0].now_cost = 1  # type: ignore[misc]  # mypy catches it statically too


def test_fixtures_parse() -> None:
    fixtures = [Fixture.model_validate(f) for f in load_fixture("fixtures")]
    assert fixtures
    assert all(f.team_h != f.team_a for f in fixtures)


def test_fixture_allows_unscheduled_event() -> None:
    raw = dict(load_fixture("fixtures")[0], event=None, kickoff_time=None)
    assert Fixture.model_validate(raw).event is None


def test_my_team_parses_selling_prices_and_captaincy() -> None:
    team = MyTeam.model_validate(load_fixture("my_team"))
    assert len(team.picks) == 15
    assert sum(p.is_captain for p in team.picks) == 1
    assert sum(p.is_vice_captain for p in team.picks) == 1
    assert all(p.selling_price > 0 for p in team.picks)
    assert sorted(p.position for p in team.picks) == list(range(1, 16))
