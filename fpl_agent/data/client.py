"""Read-only FPL API client.

Politeness and resilience (docs/decisions.md D8):
- bootstrap-static is fetched at most once per client (i.e. per run).
- GETs retry 3x with backoff on timeouts, 429 and 5xx, since FPL is flaky around deadlines.
  Retries are GET-only: never auto-retry a write, it could apply twice.
- Every response is validated into a pydantic model at this boundary.
Writes (saving a lineup) arrive in Phase 3, behind DRY RUN by default.
"""

from __future__ import annotations

from typing import Any, Protocol

import requests
from pydantic import TypeAdapter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fpl_agent.data.models import Bootstrap, Fixture, MyTeam
from fpl_agent.http import HttpSession

API = "https://fantasy.premierleague.com/api"
TIMEOUT_S = 20
USER_AGENT = "fpl-agent (+https://github.com/JamilTakieddine/fpl-agent)"

_fixtures_adapter = TypeAdapter(list[Fixture])


class AuthProvider(Protocol):
    """Anything that can produce auth headers (fpl_agent.auth.TokenManager in practice)."""

    def auth_headers(self) -> dict[str, str]: ...


def make_session() -> requests.Session:
    """A requests.Session with polite headers and GET-only retry/backoff."""
    retry = Retry(
        total=3,
        backoff_factor=1.0,  # waits 1s, 2s, 4s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class FplClient:
    def __init__(
        self,
        http: HttpSession,
        tokens: AuthProvider | None = None,
        base_url: str = API,
    ) -> None:
        self.http = http
        self.tokens = tokens
        self.base_url = base_url
        self._bootstrap: Bootstrap | None = None

    def _get(self, path: str, *, auth: bool = False) -> Any:
        headers: dict[str, str] = {}
        if auth:
            if self.tokens is None:
                raise RuntimeError(f"{path} needs auth but the client has no AuthProvider")
            headers = self.tokens.auth_headers()
        resp = self.http.get(f"{self.base_url}{path}", headers=headers, timeout=TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()

    def bootstrap(self) -> Bootstrap:
        """Players, teams, gameweeks/deadlines and scoring config. Cached for the client's life."""
        if self._bootstrap is None:
            self._bootstrap = Bootstrap.model_validate(self._get("/bootstrap-static/"))
        return self._bootstrap

    def fixtures(self) -> list[Fixture]:
        return _fixtures_adapter.validate_python(self._get("/fixtures/"))

    def my_team(self, entry_id: int) -> MyTeam:
        """Current picks with selling prices, transfers and chip status. Needs auth."""
        return MyTeam.model_validate(self._get(f"/my-team/{entry_id}/", auth=True))
