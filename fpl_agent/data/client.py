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

from fpl_agent.data.models import (
    Bootstrap,
    EntryHistory,
    EntryPicks,
    Fixture,
    H2HMatch,
    H2HMatchesPage,
    MyTeam,
)
from fpl_agent.http import HttpSession

API = "https://fantasy.premierleague.com/api"
TIMEOUT_S = 20
MAX_PAGES = 20  # guard against a pagination loop; H2H leagues are small
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

    def _get(self, path: str, *, auth: bool = False, allow_404: bool = False) -> Any:
        """GET and parse JSON. With allow_404, a 404 returns None (e.g. picks not visible yet)."""
        headers: dict[str, str] = {}
        if auth:
            if self.tokens is None:
                raise RuntimeError(f"{path} needs auth but the client has no AuthProvider")
            headers = self.tokens.auth_headers()
        resp = self.http.get(f"{self.base_url}{path}", headers=headers, timeout=TIMEOUT_S)
        if allow_404 and resp.status_code == 404:
            return None
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

    def h2h_matches(self, league_id: int, event: int) -> list[H2HMatch]:
        """All H2H matches in a league for one gameweek (follows pagination)."""
        matches: list[H2HMatch] = []
        for page in range(1, MAX_PAGES + 1):
            path = f"/leagues-h2h-matches/league/{league_id}/?event={event}&page={page}"
            result = H2HMatchesPage.model_validate(self._get(path))
            matches.extend(result.results)
            if not result.has_next:
                return matches
        raise RuntimeError(f"H2H league {league_id} has more than {MAX_PAGES} pages")

    def entry_history(self, entry_id: int) -> EntryHistory:
        """Per-gameweek history and chips played, for any manager (public)."""
        return EntryHistory.model_validate(self._get(f"/entry/{entry_id}/history/"))

    def entry_picks(self, entry_id: int, event: int) -> EntryPicks | None:
        """Any manager's picks for a gameweek, or None if not visible (upcoming gameweek: 404)."""
        data = self._get(f"/entry/{entry_id}/event/{event}/picks/", allow_404=True)
        return None if data is None else EntryPicks.model_validate(data)
