"""Rebuild past Kalshi pre-match prices from hourly price history (docs/decisions.md D25).

Settled markets only show their final 0.99/0.01 prices, so for each gameweek we take every
market's last hourly candle BEFORE that gameweek's deadline minus AS_OF_BEFORE_DEADLINE (when the
live agent reads prices, D12). Its closing bid/ask becomes the market's quote, and the contracts
traded up to then its volume. The result goes through the SAME build_odds pipeline as live runs,
quality gate included, so the back-test sees exactly what the agent would have seen.

Candles are cached per market in data/cache/history/kalshi_candles/ (gitignored); fetching them
is scripts/fetch_kalshi_history.py's job, so rebuilding is offline and repeatable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fpl_agent.data.kalshi import Candle, KalshiMarket
from fpl_agent.data.models import Bootstrap, Fixture
from fpl_agent.data.odds import MatchOdds, build_odds
from fpl_agent.validation.history import HISTORY_DIR

AS_OF_BEFORE_DEADLINE = timedelta(minutes=60)
CANDLE_DIR = HISTORY_DIR / "kalshi_candles"
HISTORY_WINDOW = timedelta(days=7)  # candles fetched for the week before each market's occurrence


def settled_markets_path(series: str, directory: Path = HISTORY_DIR) -> Path:
    return directory / f"kalshi_settled_{series}.json"


def load_settled(series: str, directory: Path = HISTORY_DIR) -> list[KalshiMarket]:
    raw = json.loads(settled_markets_path(series, directory).read_text())
    return [KalshiMarket.model_validate(m) for m in raw]


def load_candles(ticker: str, directory: Path = CANDLE_DIR) -> list[Candle]:
    path = directory / f"{ticker}.json"
    if not path.exists():
        return []
    return [Candle.model_validate(c) for c in json.loads(path.read_text())]


def as_of(market: KalshiMarket, candles: list[Candle], when: datetime) -> KalshiMarket:
    """The market as it stood at `when`: last quote at or before it, cumulative volume to then."""
    cutoff = int(when.timestamp())
    before = [c for c in candles if c.end_period_ts <= cutoff]
    quoted = [
        c
        for c in before
        if c.yes_bid.close_dollars is not None and c.yes_ask.close_dollars is not None
    ]
    last = quoted[-1] if quoted else None
    return market.model_copy(
        update={
            "status": "active",
            "yes_bid_dollars": last.yes_bid.close_dollars if last else None,
            "yes_ask_dollars": last.yes_ask.close_dollars if last else None,
            "last_price_dollars": None,
            "volume_fp": sum(c.volume_fp or 0.0 for c in before),
        }
    )


def odds_for_gameweek(
    match_markets: list[KalshiMarket],
    totals_markets: list[KalshiMarket],
    fixtures: list[Fixture],
    deadline: datetime,
    bootstrap: Bootstrap,
    candle_dir: Path = CANDLE_DIR,
) -> tuple[dict[int, MatchOdds], dict[str, str]]:
    """Odds for one gameweek's fixtures as the agent would have seen them before its deadline."""
    when = deadline - AS_OF_BEFORE_DEADLINE
    snap_match = [as_of(m, load_candles(m.ticker, candle_dir), when) for m in match_markets]
    snap_totals = [as_of(m, load_candles(m.ticker, candle_dir), when) for m in totals_markets]
    odds, skipped = build_odds(snap_match, fixtures, bootstrap, snap_totals)
    # Events for other gameweeks can't match these fixtures; only report this gameweek's gaps.
    return odds, {e: r for e, r in skipped.items() if r != "no matching FPL fixture"}
