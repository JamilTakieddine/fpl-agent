# Token handling: when to refresh, saving rotated tokens before use, revoked-token errors, 429
# retries, endpoint caching, the file store and Phase 0 import; fake HTTP and clock, no network.

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from fpl_agent.auth import (
    AuthError,
    FileTokenStore,
    RateLimitedError,
    TokenManager,
    TokenSet,
    needs_refresh,
    refresh,
    tokens_from_storage_state,
)
from tests.conftest import FakeResponse, FakeSession

ISSUER = "https://issuer.example/as"
TOKEN_URL = f"{ISSUER}/token"
DISCO_URL = f"{ISSUER}/.well-known/openid-configuration"
NOW = 1_800_000_000.0


def tokens(expires_at: float = NOW + 3600, refresh: str = "r-old") -> TokenSet:
    return TokenSet(
        issuer=ISSUER,
        client_id="client-1",
        access_token="a-old",
        refresh_token=refresh,
        expires_at=int(expires_at),
    )


class MemoryStore:
    def __init__(self, t: TokenSet, events: list[str]) -> None:
        self.t = t
        self.events = events

    def load(self) -> TokenSet:
        return self.t

    def save(self, t: TokenSet) -> None:
        self.events.append(f"save:{t.refresh_token}")
        self.t = t


def session_with_refresh(status: int = 200, rotate: bool = True) -> FakeSession:
    body = {"access_token": "a-new", "expires_in": 3600}
    if rotate:
        body["refresh_token"] = "r-new"
    return FakeSession(
        routes={
            DISCO_URL: FakeResponse(body={"token_endpoint": TOKEN_URL}),
            TOKEN_URL: FakeResponse(status_code=status, body=body),
        }
    )


def test_needs_refresh_uses_margin() -> None:
    assert not needs_refresh(tokens(expires_at=NOW + 600), NOW, margin_s=300)
    assert needs_refresh(tokens(expires_at=NOW + 200), NOW, margin_s=300)
    assert needs_refresh(tokens(expires_at=NOW - 1), NOW)


def test_valid_token_is_used_without_any_http() -> None:
    http = FakeSession()
    mgr = TokenManager(MemoryStore(tokens(), []), http, clock=lambda: NOW)
    assert mgr.access_token() == "a-old"
    assert http.calls == []


def test_expired_token_refreshes_and_saves_rotated_token() -> None:
    events: list[str] = []
    store = MemoryStore(tokens(expires_at=NOW - 10), events)
    http = session_with_refresh()
    mgr = TokenManager(store, http, clock=lambda: NOW)

    assert mgr.access_token() == "a-new"
    assert events == ["save:r-new"]
    assert store.t.expires_at == int(NOW) + 3600
    post = [c for c in http.calls if c[0] == "POST"][0]
    assert post[2]["data"]["grant_type"] == "refresh_token"
    assert post[2]["data"]["refresh_token"] == "r-old"


def test_token_is_saved_before_it_is_returned() -> None:
    """If saving fails, the new token must not be handed out (the old refresh token may be dead)."""

    class FailingStore(MemoryStore):
        def save(self, t: TokenSet) -> None:
            raise OSError("disk full")

    mgr = TokenManager(
        FailingStore(tokens(expires_at=NOW - 10), []),
        session_with_refresh(),
        clock=lambda: NOW,
    )
    with pytest.raises(OSError):
        mgr.access_token()


def test_non_rotating_server_keeps_old_refresh_token() -> None:
    events: list[str] = []
    mgr = TokenManager(
        MemoryStore(tokens(expires_at=NOW - 10), events),
        session_with_refresh(rotate=False),
        clock=lambda: NOW,
    )
    mgr.access_token()
    assert events == ["save:r-old"]


def test_revoked_refresh_token_raises_auth_error_with_relogin_hint() -> None:
    mgr = TokenManager(
        MemoryStore(tokens(expires_at=NOW - 10), []),
        session_with_refresh(status=400),
        clock=lambda: NOW,
    )
    with pytest.raises(AuthError, match="Log in again"):
        mgr.access_token()


def test_file_store_round_trip_is_private(tmp_path: Path) -> None:
    store = FileTokenStore(tmp_path / "secrets" / "tokens.json")
    store.save(tokens())
    assert store.load() == tokens()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert not store.path.with_suffix(".json.tmp").exists()


def test_file_store_without_tokens_raises(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="Log in again"):
        FileTokenStore(tmp_path / "missing.json").load()


def fake_jwt(claims: dict[str, str]) -> str:
    def b64(obj: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def storage_state(access: str, refresh: str) -> dict[str, object]:
    user = {"access_token": access, "refresh_token": refresh, "expires_at": 123}
    return {
        "origins": [
            {
                "origin": "https://fantasy.premierleague.com",
                "localStorage": [
                    {"name": f"oidc.user:{ISSUER}:client-1", "value": json.dumps(user)}
                ],
            }
        ]
    }


def test_tokens_from_storage_state_reads_issuer_and_client_from_jwt() -> None:
    access = fake_jwt({"iss": ISSUER, "client_id": "client-1"})
    t = tokens_from_storage_state(storage_state(access, "r-1"))
    assert (t.issuer, t.client_id, t.refresh_token, t.expires_at) == (
        ISSUER,
        "client-1",
        "r-1",
        123,
    )


def test_file_store_imports_login_state_when_newer(tmp_path: Path) -> None:
    token_file, state_file = tmp_path / "tokens.json", tmp_path / "state.json"
    store = FileTokenStore(token_file, import_from=state_file)
    store.save(tokens(refresh="r-stale"))

    access = fake_jwt({"iss": ISSUER, "client_id": "client-1"})
    state_file.write_text(json.dumps(storage_state(access, "r-fresh-login")))
    # Make the login state clearly newer than the token file (a --fresh re-login).
    os.utime(token_file, (1, 1))

    assert store.load().refresh_token == "r-fresh-login"
    assert FileTokenStore(token_file).load().refresh_token == "r-fresh-login"  # persisted


def test_token_endpoint_is_discovered_once_then_cached() -> None:
    events: list[str] = []
    store = MemoryStore(tokens(expires_at=NOW - 10), events)
    http = session_with_refresh()
    TokenManager(store, http, clock=lambda: NOW).access_token()
    assert store.t.token_endpoint == TOKEN_URL
    assert [c[0] for c in http.calls] == ["GET", "POST"]  # discovery + token

    # Next refresh (an hour later) skips discovery: one request to the rate-limited host.
    http.calls.clear()
    TokenManager(store, http, clock=lambda: NOW + 7200).access_token()
    assert [c[0] for c in http.calls] == ["POST"]


def test_rate_limited_refresh_is_retried_after_retry_after() -> None:
    waits: list[float] = []
    http = session_with_refresh()
    ok = http.routes[TOKEN_URL]
    assert isinstance(ok, FakeResponse)
    http.routes[TOKEN_URL] = [FakeResponse(status_code=429, headers={"Retry-After": "7"}), ok]
    new = refresh(tokens(expires_at=NOW - 10), http, NOW, sleep=waits.append)
    assert new.access_token == "a-new"
    assert waits == [7.0]


def test_persistent_rate_limit_is_not_reported_as_relogin() -> None:
    http = session_with_refresh()
    http.routes[TOKEN_URL] = FakeResponse(status_code=429, headers={"Retry-After": "999"})
    waits: list[float] = []
    with pytest.raises(RateLimitedError):
        refresh(tokens(expires_at=NOW - 10), http, NOW, sleep=waits.append)
    assert waits == [30.0, 30.0]  # capped, and no sleep after the last attempt
    assert len([c for c in http.calls if c[0] == "POST"]) == 3
