# Shared test helpers: loading recorded FPL fixtures and a fake HTTP session (no network).

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, Fixture

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@dataclass
class FakeResponse:
    status_code: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}")


@dataclass
class FakeSession:
    """Stands in for requests.Session: canned responses by URL, and a log of every call."""

    routes: dict[str, FakeResponse | list[FakeResponse]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self._next(url)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self._next(url)

    def _next(self, url: str) -> FakeResponse:
        """A route can be one response, or a list played in order (e.g. 429 then 200)."""
        route = self.routes[url]
        if isinstance(route, list):
            return route.pop(0) if len(route) > 1 else route[0]
        return route


@pytest.fixture
def bootstrap_json() -> Any:
    return load_fixture("bootstrap")


# --- synthetic seasons (shared by calendar and lineup tests) ---

T0 = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)


def make_bootstrap(n_teams: int, n_gws: int, base: Any) -> Bootstrap:
    """A tiny season built on the real bootstrap's shape: n teams, n gameweeks a week apart."""
    data = dict(base)
    data["teams"] = [
        {"id": i, "name": f"Team {i}", "short_name": f"T{i}"} for i in range(1, n_teams + 1)
    ]
    data["events"] = [
        {
            "id": gw,
            "name": f"Gameweek {gw}",
            "deadline_time": (T0 + timedelta(weeks=gw - 1)).isoformat(),
            "finished": False,
            "is_previous": False,
            "is_current": False,
            "is_next": gw == 1,
        }
        for gw in range(1, n_gws + 1)
    ]
    return Bootstrap.model_validate(data)


def fx(
    fid: int, gw: int | None, home: int, away: int, hours: float = 1.5, finished: bool = False
) -> Fixture:
    """A fixture kicking off `hours` after that gameweek's deadline (unless unscheduled)."""
    kickoff = None if gw is None else T0 + timedelta(weeks=gw - 1, hours=hours)
    return Fixture(
        id=fid,
        event=gw,
        team_h=home,
        team_a=away,
        kickoff_time=kickoff,
        finished=finished,
        team_h_difficulty=3,
        team_a_difficulty=3,
    )
