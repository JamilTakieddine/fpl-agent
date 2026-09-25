# FplClient over a fake HTTP session: per-run bootstrap caching, auth only where needed, and
# GET-only retry config; no live API.

from __future__ import annotations

import pytest
import requests

from fpl_agent.data.client import API, FplClient, make_session
from tests.conftest import FakeResponse, FakeSession, load_fixture


class FakeTokens:
    def auth_headers(self) -> dict[str, str]:
        return {"x-api-authorization": "Bearer test"}


def fake_api() -> FakeSession:
    return FakeSession(
        routes={
            f"{API}/bootstrap-static/": FakeResponse(body=load_fixture("bootstrap")),
            f"{API}/fixtures/": FakeResponse(body=load_fixture("fixtures")),
            f"{API}/my-team/1/": FakeResponse(body=load_fixture("my_team")),
        }
    )


def test_bootstrap_is_fetched_once_per_client() -> None:
    http = fake_api()
    client = FplClient(http)
    client.bootstrap()
    client.bootstrap()
    assert len(http.calls) == 1


def test_public_endpoints_send_no_auth() -> None:
    http = fake_api()
    FplClient(http, FakeTokens()).fixtures()
    assert http.calls[0][2]["headers"] == {}


def test_my_team_sends_auth_header() -> None:
    http = fake_api()
    team = FplClient(http, FakeTokens()).my_team(1)
    assert len(team.picks) == 15
    assert http.calls[0][2]["headers"]["x-api-authorization"] == "Bearer test"


def test_auth_endpoint_without_token_manager_fails_clearly() -> None:
    with pytest.raises(RuntimeError, match="needs auth"):
        FplClient(fake_api()).my_team(1)


def test_http_errors_raise() -> None:
    http = FakeSession(routes={f"{API}/fixtures/": FakeResponse(status_code=503)})
    with pytest.raises(requests.HTTPError):
        FplClient(http).fixtures()


def test_session_retries_gets_but_never_writes() -> None:
    adapter = make_session().get_adapter("https://fantasy.premierleague.com")
    retry = adapter.max_retries  # type: ignore[attr-defined]
    assert retry.total == 3
    assert retry.allowed_methods == frozenset({"GET"})
    assert 429 in retry.status_forcelist
