"""Read-only client for Kalshi's public market data (no login needed).

Two Premier League series are used: match result (KXEPLGAME: home / tie / away) and total goals
(KXEPLTOTAL: "over X.5 goals" lines). Correct score exists but is ~50x thinner and prices
incoherently (see docs/decisions.md D15). Both series share a per-match code in the event ticker
(KXEPLGAME-26SEP20FULMUN / KXEPLTOTAL-26SEP20FULMUN), which links totals to a mapped match.
Kalshi reports prices and sizes as decimal strings (e.g. yes_bid_dollars="0.7100"); pydantic
parses them into floats. Prices in dollars on a $1 contract read directly as probabilities.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from fpl_agent.http import HttpSession

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
MATCH_SERIES = "KXEPLGAME"
TOTALS_SERIES = "KXEPLTOTAL"
TIMEOUT_S = 20
MAX_PAGES = 10
TIE = "Tie"


class KalshiMarket(BaseModel):
    """One yes/no contract, e.g. 'Arsenal wins' within the Arsenal vs Leeds event."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ticker: str
    event_ticker: str
    yes_sub_title: str  # the outcome: a team name, or "Tie"
    status: str
    occurrence_datetime: datetime | None = None
    yes_bid_dollars: float | None = None
    yes_ask_dollars: float | None = None
    last_price_dollars: float | None = None
    volume_fp: float | None = None  # contracts traded
    strike_type: str | None = None  # totals: "greater" (yes = over the line)
    floor_strike: float | None = None  # totals: the line, e.g. 2.5

    def mid(self) -> float | None:
        if self.yes_bid_dollars is None or self.yes_ask_dollars is None:
            return None
        if self.yes_bid_dollars <= 0 or self.yes_ask_dollars <= 0:
            return None
        return (self.yes_bid_dollars + self.yes_ask_dollars) / 2

    def spread(self) -> float | None:
        if self.yes_bid_dollars is None or self.yes_ask_dollars is None:
            return None
        return self.yes_ask_dollars - self.yes_bid_dollars


def match_code(event_ticker: str) -> str:
    """The per-match part of an event ticker, shared across series: KXEPLTOTAL-26SEP20FULMUN ->
    26SEP20FULMUN."""
    return event_ticker.split("-", 1)[1] if "-" in event_ticker else event_ticker


class _MarketsPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    markets: list[KalshiMarket]
    cursor: str | None = None


class KalshiClient:
    def __init__(self, http: HttpSession, base_url: str = KALSHI_API) -> None:
        self.http = http
        self.base_url = base_url

    def _get(self, path: str) -> Any:
        resp = self.http.get(f"{self.base_url}{path}", timeout=TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()

    def open_match_markets(self, series: str = MATCH_SERIES) -> list[KalshiMarket]:
        """Every open market in the series in one paginated query (not one call per match)."""
        markets: list[KalshiMarket] = []
        cursor = ""
        for _ in range(MAX_PAGES):
            path = f"/markets?series_ticker={series}&status=open&limit=200"
            if cursor:
                path += f"&cursor={cursor}"
            page = _MarketsPage.model_validate(self._get(path))
            markets.extend(page.markets)
            if not page.cursor:
                return markets
            cursor = page.cursor
        raise RuntimeError(f"Kalshi series {series} has more than {MAX_PAGES} pages")
