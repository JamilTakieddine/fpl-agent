# Shared test helpers: loading recorded FPL fixtures and a fake HTTP session (no network).

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

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
