# Phase 5a: making transfers - the website's payload, free transfers left (limit - made), every
# pre-send check (deadline, hits off, ownership, changed prices, budget, club limit, max per
# week, chips), dry run vs live, one un-retried POST and the read-back; the planner's "free
# transfers only" switch and the email subject; fake HTTP only, nothing reaches FPL.

from __future__ import annotations

import copy
import json
from datetime import timedelta
from typing import Any

import pytest
import requests

from fpl_agent.agent import RunResult
from fpl_agent.data.client import API, FplClient
from fpl_agent.data.models import Bootstrap, MyTeam
from fpl_agent.optimize.rules import Limits, Lineup, SquadPlayer
from fpl_agent.optimize.transfers import Move
from fpl_agent.run import email_subject
from fpl_agent.submit import (
    SubmitResult,
    free_transfers,
    hits_allowed,
    problems_before_transferring,
    submit_transfers,
    transfers_payload,
)
from tests.conftest import T0, FakeResponse, FakeSession, load_fixture

ENTRY = 1
MY_TEAM = f"{API}/my-team/{ENTRY}/"
TRANSFERS = f"{API}/transfers/"
BEFORE = T0 - timedelta(minutes=60)
GOOD = Move(sell=391, buy=9)  # DEF 5.6m -> DEF 5.2m at club 1 (2 there already): legal, +0.4m


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
def players(boot: Bootstrap) -> dict[int, SquadPlayer]:
    return {
        p.id: SquadPlayer(p.id, boot.position_code(p.element_type), p.team, p.now_cost)
        for p in boot.elements
    }


def team(**transfers: Any) -> MyTeam:
    raw = copy.deepcopy(load_fixture("my_team"))
    raw["transfers"].update(transfers)
    return MyTeam.model_validate(raw)


def payload_for(moves: list[Move], t: MyTeam, players: dict[int, SquadPlayer]) -> dict[str, Any]:
    sell = {p.element: p.selling_price for p in t.picks}
    return transfers_payload(moves, ENTRY, 7, {k: v.price for k, v in players.items()}, sell)


def test_payload_is_the_websites_shape(players: dict[int, SquadPlayer]) -> None:
    p = payload_for([GOOD], team(), players)
    assert p == {
        "chip": None,
        "entry": ENTRY,
        "event": 7,
        "transfers": [
            {"element_in": 9, "element_out": 391, "purchase_price": 52, "selling_price": 56}
        ],
    }


def test_free_transfers_are_the_limit_minus_those_made() -> None:
    assert free_transfers(team(limit=5, made=0)) == 5
    assert free_transfers(team(limit=2, made=1)) == 1
    assert free_transfers(team(limit=1, made=3)) == 0
    assert free_transfers(team(limit=None)) is None  # wildcard / free hit
    assert hits_allowed({}) is False and hits_allowed({"FPL_ALLOW_HITS": "1"}) is True


def check(
    moves: list[Move], t: MyTeam, players: dict[int, SquadPlayer], limits: Limits, **kw: Any
) -> list[str]:
    payload = kw.pop("payload", None) or payload_for(moves, t, players)
    now = kw.pop("now", BEFORE)
    return problems_before_transferring(
        payload, t, players, limits, T0, now, kw.pop("allow_hits", False)
    )


def test_a_legal_free_transfer_passes(players: dict[int, SquadPlayer], limits: Limits) -> None:
    assert check([GOOD], team(), players, limits) == []


def test_deadline_hits_ownership_and_limits(
    players: dict[int, SquadPlayer], limits: Limits
) -> None:
    t = team()
    late = check([GOOD], t, players, limits, now=T0 - timedelta(seconds=30))
    assert any("deadline" in p for p in late)
    no_free = check([GOOD], team(limit=1, made=1), players, limits)
    assert any("hits are off" in p for p in no_free)
    assert check([GOOD], team(limit=1, made=1), players, limits, allow_hits=True) == []
    owned = check([Move(sell=391, buy=8)], t, players, limits)
    assert any("already in the squad" in p for p in owned)
    four = [GOOD, Move(113, 5), Move(304, 11), Move(418, 4)]
    assert any("max 3" in p for p in check(four, t, players, limits))


def test_changed_prices_mean_re_plan(players: dict[int, SquadPlayer], limits: Limits) -> None:
    t = team()
    payload = payload_for([GOOD], t, players)
    payload["transfers"][0]["purchase_price"] = 51  # the price rose overnight
    assert any("price changed" in p for p in check([], t, players, limits, payload=payload))
    payload = payload_for([GOOD], t, players)
    payload["transfers"][0]["selling_price"] = 55
    assert any("selling price changed" in p for p in check([], t, players, limits, payload=payload))


def test_budget_and_club_limit(players: dict[int, SquadPlayer], limits: Limits) -> None:
    t = team()
    over = check([Move(sell=113, buy=9)], t, players, limits)  # 4.4m out, 5.2m in, bank 0
    assert any("over budget" in p for p in over)
    fourth = players | {999: SquadPlayer(999, "DEF", 16, 40)}  # the squad has 3 from club 16
    club = check([Move(sell=391, buy=999)], t, fourth, limits)
    assert any("from team 16" in p for p in club)


# --- sending -------------------------------------------------------------------------------------


def client(routes: dict[str, Any]) -> tuple[FplClient, FakeSession]:
    http = FakeSession(routes=routes)
    return FplClient(http, FakeTokens()), http


def after_transfer(raw: dict[str, Any], move: Move) -> dict[str, Any]:
    new = copy.deepcopy(raw)
    for p in new["picks"]:
        if p["element"] == move.sell:
            p["element"] = move.buy
    return new


def submit(fpl: FplClient, players: dict[int, SquadPlayer], limits: Limits, live: bool) -> Any:
    log: list[str] = []
    result = submit_transfers(
        fpl, ENTRY, 7, [GOOD], players, limits, T0, BEFORE, live, False, log.append
    )
    return result, log


def test_dry_run_sends_nothing(players: dict[int, SquadPlayer], limits: Limits) -> None:
    fpl, http = client({MY_TEAM: FakeResponse(body=load_fixture("my_team"))})
    result, log = submit(fpl, players, limits, live=False)
    assert not result.sent and result.problems == []
    assert not [c for c in http.calls if c[0] == "POST"]
    assert log[0].startswith("DRY RUN transfers payload")


def test_live_posts_once_and_reads_back(players: dict[int, SquadPlayer], limits: Limits) -> None:
    raw = load_fixture("my_team")
    fpl, http = client(
        {
            MY_TEAM: [FakeResponse(body=raw), FakeResponse(body=after_transfer(raw, GOOD))],
            TRANSFERS: FakeResponse(status_code=200),
        }
    )
    result, log = submit(fpl, players, limits, live=True)
    assert result.sent and result.verified
    (post,) = [c for c in http.calls if c[0] == "POST"]
    assert post[1] == TRANSFERS
    assert json.loads(post[2]["data"])["transfers"][0]["element_in"] == 9
    assert post[2]["headers"]["Referer"].endswith("/transfers")
    assert log[0].startswith("LIVE transfers payload")  # logged before sending


def test_a_read_back_without_the_buy_is_flagged(
    players: dict[int, SquadPlayer], limits: Limits
) -> None:
    raw = load_fixture("my_team")
    fpl, _ = client(
        {MY_TEAM: [FakeResponse(body=raw), FakeResponse(body=raw)], TRANSFERS: FakeResponse()}
    )
    result, _ = submit(fpl, players, limits, live=True)
    assert result.sent and not result.verified


def test_a_failed_post_raises_and_is_not_retried(
    players: dict[int, SquadPlayer], limits: Limits
) -> None:
    fpl, http = client(
        {
            MY_TEAM: FakeResponse(body=load_fixture("my_team")),
            TRANSFERS: FakeResponse(status_code=400),
        }
    )
    with pytest.raises(requests.HTTPError):
        submit(fpl, players, limits, live=True)
    assert len([c for c in http.calls if c[0] == "POST"]) == 1


# --- the planner's switch and the email ----------------------------------------------------------


def test_the_email_says_what_was_done() -> None:
    lineup = Lineup((1,), (2,), 1, 1)
    saved = SubmitResult(True, {}, [], True)
    made = SubmitResult(True, {"transfers": [{}, {}]}, [], True)
    ok = RunResult(7, T0, lineup, True, 0.6, saved, None, made, None)
    assert email_subject("save", ok, live=True) == (
        "FPL GW7: 2 transfers made, lineup saved (P(win by 3+) 60%)"
    )
    blocked = SubmitResult(False, {"transfers": [{}]}, ["price changed"])
    failed = RunResult(7, T0, lineup, True, 0.6, saved, None, blocked, None)
    subject = email_subject("save", failed, live=True)
    assert subject is not None and "TRANSFERS FAILED" in subject
