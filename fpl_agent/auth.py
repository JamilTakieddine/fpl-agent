"""FPL authentication: keep a valid access token using the OIDC refresh token.

FPL access tokens last 1 hour; the refresh token lasts 180 days (sliding) and ROTATES on every use.
So the one rule that matters: after a refresh, save the new tokens before doing anything else,
or a crash can leave us holding only a dead refresh token. See docs/decisions.md (D5, D8).

Where tokens live is behind the TokenStore protocol: a local file now, Secret Manager in Phase 4.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from fpl_agent.fileio import atomic_write
from fpl_agent.http import HttpResponse, HttpSession

FPL_ORIGIN = "https://fantasy.premierleague.com"
OIDC_KEY_PREFIX = "oidc.user:"
REFRESH_MARGIN_S = 300  # refresh if the access token has less than 5 minutes left
REFRESH_ATTEMPTS = 3  # on 429 only; see refresh()
MAX_RETRY_AFTER_S = 30  # cap on a single Retry-After wait
TIMEOUT_S = 20
RELOGIN_HINT = "Log in again: python spikes/phase0_auth.py --fresh"


class AuthError(Exception):
    """Auth can't be recovered automatically; a human needs to log in again."""


class RateLimitedError(Exception):
    """The login server kept answering 429 (Cloudflare). Temporary: retry later, don't re-login."""


class TokenSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    issuer: str
    client_id: str
    access_token: str
    refresh_token: str
    expires_at: int  # unix seconds when access_token expires
    # Cached from OIDC discovery so each refresh is one request, not two, to the rate-limited
    # login host (D12). None in token files written before this existed: discovered once.
    token_endpoint: str | None = None


class TokenStore(Protocol):
    def load(self) -> TokenSet: ...
    def save(self, tokens: TokenSet) -> None: ...


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload WITHOUT verifying it. Only to read iss/client_id/exp; never trust it."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
    )
    return claims


def write_private(path: Path, text: str) -> None:
    """Atomic write with 0600 perms in a 0700 dir: secrets are readable by the owner only."""
    atomic_write(path, text, mode=0o600, dir_mode=0o700)


def tokens_from_storage_state(state: dict[str, Any]) -> TokenSet:
    """Pull the OIDC tokens out of a Playwright storage_state (what phase0_auth.py saves)."""
    for origin in state.get("origins", []):
        if origin.get("origin") != FPL_ORIGIN:
            continue
        for item in origin.get("localStorage", []):
            if item["name"].startswith(OIDC_KEY_PREFIX):
                user = json.loads(item["value"])
                claims = jwt_claims(user["access_token"])
                return TokenSet(
                    issuer=claims["iss"],
                    client_id=claims["client_id"],
                    access_token=user["access_token"],
                    refresh_token=user["refresh_token"],
                    expires_at=int(user["expires_at"]),
                )
    raise AuthError(f"No OIDC tokens in the browser storage state. {RELOGIN_HINT}")


class FileTokenStore:
    """Tokens in a local 0600 JSON file.

    Imports from the browser-login state (import_from) when there's no token file yet, or when the
    login state is newer, i.e. the user just re-logged in with phase0_auth.py --fresh.
    """

    def __init__(self, path: Path, import_from: Path | None = None) -> None:
        self.path = path
        self.import_from = import_from

    def _login_is_newer(self) -> bool:
        if not (self.import_from and self.import_from.exists()):
            return False
        return not self.path.exists() or (
            self.import_from.stat().st_mtime > self.path.stat().st_mtime
        )

    def load(self) -> TokenSet:
        if self.path.exists() and not self._login_is_newer():
            return TokenSet.model_validate_json(self.path.read_text())
        if self.import_from and self.import_from.exists():
            tokens = tokens_from_storage_state(json.loads(self.import_from.read_text()))
            self.save(tokens)
            return tokens
        raise AuthError(f"No saved FPL tokens at {self.path}. {RELOGIN_HINT}")

    def save(self, tokens: TokenSet) -> None:
        write_private(self.path, tokens.model_dump_json(indent=2))


def needs_refresh(tokens: TokenSet, now: float, margin_s: int = REFRESH_MARGIN_S) -> bool:
    return tokens.expires_at - now < margin_s


def _retry_after_s(resp: HttpResponse) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "5")), MAX_RETRY_AFTER_S)
    except ValueError:  # an HTTP-date instead of seconds
        return 5.0


def discover_token_endpoint(issuer: str, http: HttpSession) -> str:
    disco = http.get(f"{issuer}/.well-known/openid-configuration", timeout=TIMEOUT_S)
    if disco.status_code == 429:
        raise RateLimitedError("Login server rate-limited OIDC discovery (429)")
    disco.raise_for_status()
    endpoint: str = disco.json()["token_endpoint"]
    return endpoint


def refresh(
    tokens: TokenSet,
    http: HttpSession,
    now: float,
    sleep: Callable[[float], None] = time.sleep,
) -> TokenSet:
    """Exchange the refresh token for new tokens. Doesn't save them: the caller must, at once.

    A 429 is retried (up to REFRESH_ATTEMPTS, honoring Retry-After). That's normally unsafe for a
    token call, since a processed request would have consumed the refresh token, but Cloudflare's
    429 is returned BEFORE the request reaches the login server, so the token is untouched.
    400/401 means revoked/expired: AuthError, a human must log in. Still 429: RateLimitedError.
    """
    endpoint = tokens.token_endpoint or discover_token_endpoint(tokens.issuer, http)
    for attempt in range(1, REFRESH_ATTEMPTS + 1):
        resp = http.post(
            endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": tokens.client_id,
            },
            headers={"Origin": FPL_ORIGIN, "Accept": "application/json"},
            timeout=TIMEOUT_S,
        )
        if resp.status_code != 429:
            break
        if attempt < REFRESH_ATTEMPTS:
            sleep(_retry_after_s(resp))
    else:
        raise RateLimitedError(f"Token refresh rate-limited (429) {REFRESH_ATTEMPTS} times")

    if resp.status_code in (400, 401):
        # invalid_grant etc.: the refresh token was revoked or expired. Retrying won't help.
        raise AuthError(f"Token refresh rejected ({resp.status_code}). {RELOGIN_HINT}")
    resp.raise_for_status()
    body = resp.json()
    return TokenSet(
        issuer=tokens.issuer,
        client_id=tokens.client_id,
        access_token=body["access_token"],
        refresh_token=body.get("refresh_token", tokens.refresh_token),
        expires_at=int(now) + int(body.get("expires_in", 3600)),
        token_endpoint=endpoint,
    )


class TokenManager:
    """Hands out a valid access token, refreshing (and saving first) when needed."""

    def __init__(
        self,
        store: TokenStore,
        http: HttpSession,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.http = http
        self.clock = clock
        self._tokens: TokenSet | None = None

    def access_token(self) -> str:
        tokens = self._tokens or self.store.load()
        now = self.clock()
        if needs_refresh(tokens, now):
            tokens = refresh(tokens, self.http, now)
            self.store.save(tokens)  # persist BEFORE use: the old refresh token may now be dead
        self._tokens = tokens
        return tokens.access_token

    def auth_headers(self) -> dict[str, str]:
        return {"x-api-authorization": f"Bearer {self.access_token()}"}
