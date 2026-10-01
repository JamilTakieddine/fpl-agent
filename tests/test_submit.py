# Phase 3 part 6: the lineup save - payload shape, DRY RUN unless --live / FPL_LIVE=1, every
# pre-save check (deadline, squad changed, illegal lineup, chip), one un-retried POST and the
# read-back; over a fake HTTP session, so nothing can reach FPL.

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any

import pytest
import requests

from fpl_agent.data.client import API, FplClient
from fpl_agent.data.models import Bootstrap, MyTeam, PositionCode
from fpl_agent.optimize.rules import Limits, Lineup
from fpl_agent.submit import (
    lineup_payload,
    live_mode,
    problems_before_saving,
    submit_lineup,
)
from tests.conftest import T0, FakeResponse, FakeSession, load_fixture

ENTRY = 1
MY_TEAM = f"{API}/my-team/{ENTRY}/"
DEADLINE = T0
BEFORE = T0 - timedelta(minutes=15)


class FakeTokens:
    def auth_headers(self) -> dict[str, str]:
        return {"x-api-authorization": "Bearer test"}


@pytest.fixture
def boot(bootstrap_json: Any) -> Bootstrap:
    return Bootstrap.model_validate(bootstrap_json)


@pytest.fixture
def limits(boot: Bootstrap) -> Limits:
    return Limits.from_bootstrap(boot)


@pytest.fixture
def positions(boot: Bootstrap) -> dict[int, PositionCode]:
    return {p.id: boot.position_code(p.element_type) for p in boot.elements}


def current(team_json: Any) -> Lineup:
    picks = sorted(team_json["picks"], key=lambda p: p["position"])
    order = [p["element"] for p in picks]
    return Lineup(
        tuple(order[:11]),
        tuple(order[11:]),
        next(p["element"] for p in picks if p["is_captain"]),
        next(p["element"] for p in picks if p["is_vice_captain"]),
    )


def swapped_captaincy(lineup: Lineup) -> Lineup:
    return Lineup(lineup.starters, lineup.bench, lineup.vice_captain, lineup.captain)


def team_after(team_json: Any, payload: dict[str, Any]) -> Any:
    """What /my-team/ returns once FPL has applied `payload`."""
    after = copy.deepcopy(team_json)
    sent = {p["element"]: p for p in payload["picks"]}
    for p in after["picks"]:
        p.update(sent[p["element"]])
    return after


def client(routes: dict[str, Any]) -> tuple[FplClient, FakeSession]:
    http = FakeSession(routes=routes)
    return FplClient(http, FakeTokens()), http


def posts(http: FakeSession) -> list[tuple[str, str, dict[str, Any]]]:
    return [c for c in http.calls if c[0] == "POST"]


# --- payload and mode ----------------------------------------------------------------------------


def test_payload_positions_and_armband() -> None:
    lineup = Lineup(tuple(range(1, 12)), (12, 13, 14, 15), captain=9, vice_captain=10)
    payload = lineup_payload(lineup)
    assert payload["chip"] is None
    assert [p["position"] for p in payload["picks"]] == list(range(1, 16))
    assert [p["element"] for p in payload["picks"]] == list(range(1, 16))
    assert [p["element"] for p in payload["picks"] if p["is_captain"]] == [9]
    assert [p["element"] for p in payload["picks"] if p["is_vice_captain"]] == [10]


def test_live_needs_an_explicit_flag_or_env() -> None:
    assert not live_mode([], {})
    assert not live_mode(["--dry-run"], {"FPL_LIVE": "0"})
    assert live_mode(["--live"], {})
    assert live_mode([], {"FPL_LIVE": "1"})


# --- the checks ----------------------------------------------------------------------------------


def test_checks_pass_for_a_legal_lineup_of_the_current_squad(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    lineup = current(team_json)
    team = MyTeam.model_validate(team_json)
    assert (
        problems_before_saving(
            lineup_payload(lineup), lineup, team, positions, limits, DEADLINE, BEFORE
        )
        == []
    )


def test_checks_catch_deadline_squad_change_illegal_lineup_and_chip(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    lineup = current(team_json)
    team = MyTeam.model_validate(team_json)

    late = problems_before_saving(
        lineup_payload(lineup),
        lineup,
        team,
        positions,
        limits,
        DEADLINE,
        DEADLINE - timedelta(seconds=30),
    )
    assert any("deadline" in p for p in late)

    sold = lineup.bench[-1]
    changed = Lineup(
        lineup.starters, (*lineup.bench[:-1], 99999), lineup.captain, lineup.vice_captain
    )
    squad = problems_before_saving(
        lineup_payload(changed), lineup, team, positions, limits, DEADLINE, BEFORE
    )
    assert sold != 99999 and any("current squad" in p for p in squad)

    illegal = Lineup(lineup.starters, lineup.bench, lineup.captain, lineup.captain)
    assert problems_before_saving(
        lineup_payload(illegal), illegal, team, positions, limits, DEADLINE, BEFORE
    )

    chip = problems_before_saving(
        lineup_payload(lineup, chip="bboost"), lineup, team, positions, limits, DEADLINE, BEFORE
    )
    assert any("chip" in p for p in chip)


# --- sending -------------------------------------------------------------------------------------


def test_dry_run_sends_nothing_but_logs_the_payload(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    fpl, http = client({MY_TEAM: FakeResponse(body=team_json)})
    log: list[str] = []
    lineup = swapped_captaincy(current(team_json))
    result = submit_lineup(
        fpl, ENTRY, lineup, positions, limits, DEADLINE, BEFORE, False, log.append
    )
    assert not result.sent and posts(http) == []
    assert json.dumps(lineup_payload(lineup)) in log[0] and log[0].startswith("DRY RUN")


def test_live_sends_once_and_reads_back(positions: dict[int, PositionCode], limits: Limits) -> None:
    team_json = load_fixture("my_team")
    lineup = swapped_captaincy(current(team_json))
    payload = lineup_payload(lineup)
    fpl, http = client({})
    # One URL serves the fresh read, the POST (202 Accepted) and the read-back, in that order.
    http.routes[MY_TEAM] = [
        FakeResponse(body=team_json),
        FakeResponse(status_code=202),
        FakeResponse(body=team_after(team_json, payload)),
    ]
    log: list[str] = []
    result = submit_lineup(
        fpl, ENTRY, lineup, positions, limits, DEADLINE, BEFORE, True, log.append
    )
    assert result.sent and result.verified
    (post,) = posts(http)
    assert json.loads(post[2]["data"]) == payload
    headers = post[2]["headers"]
    assert headers["x-api-authorization"] == "Bearer test" and headers["Origin"].startswith("https")
    assert log[0].startswith("LIVE payload")  # logged before sending


def test_live_flags_a_read_back_that_does_not_match(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    lineup = swapped_captaincy(current(team_json))
    fpl, http = client({})
    http.routes[MY_TEAM] = [
        FakeResponse(body=team_json),
        FakeResponse(status_code=202),
        FakeResponse(body=team_json),  # FPL kept the old captaincy
    ]
    result = submit_lineup(fpl, ENTRY, lineup, positions, limits, DEADLINE, BEFORE, True, print)
    assert result.sent and not result.verified


def test_live_refuses_after_the_deadline(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    fpl, http = client({MY_TEAM: FakeResponse(body=team_json)})
    after = DEADLINE + timedelta(minutes=1)
    lineup = current(team_json)
    result = submit_lineup(fpl, ENTRY, lineup, positions, limits, DEADLINE, after, True, print)
    assert not result.sent and posts(http) == []


def test_a_failed_save_raises_and_is_not_retried(
    positions: dict[int, PositionCode], limits: Limits
) -> None:
    team_json = load_fixture("my_team")
    fpl, http = client({})
    http.routes[MY_TEAM] = [FakeResponse(body=team_json), FakeResponse(status_code=500)]
    with pytest.raises(requests.HTTPError):
        submit_lineup(
            fpl, ENTRY, current(team_json), positions, limits, DEADLINE, BEFORE, True, print
        )
    assert len(posts(http)) == 1
