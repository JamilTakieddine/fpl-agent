"""Download Kalshi price history for this season's settled Premier League markets (D25).

Lists settled match-result and total-goals markets, then fetches hourly candles for the week
before each market's occurrence into data/cache/history/kalshi_candles/ (gitignored). Already
cached markets are skipped, so re-running only fetches new ones. Throttled to be polite.

Usage: python scripts/fetch_kalshi_history.py
"""

from __future__ import annotations

import json
import sys
import time

from fpl_agent.data.client import make_session
from fpl_agent.data.kalshi import MATCH_SERIES, TOTALS_SERIES, KalshiClient
from fpl_agent.validation.history import HISTORY_DIR
from fpl_agent.validation.kalshi_history import CANDLE_DIR, HISTORY_WINDOW, settled_markets_path

THROTTLE_S = 0.35


def main() -> int:
    client = KalshiClient(make_session())
    CANDLE_DIR.mkdir(parents=True, exist_ok=True)
    fetched = skipped = 0
    for series in (MATCH_SERIES, TOTALS_SERIES):
        markets = client.markets(series, "settled")
        settled_markets_path(series).write_text(
            json.dumps([m.model_dump(mode="json") for m in markets])
        )
        print(f"{series}: {len(markets)} settled markets")
        for m in markets:
            path = CANDLE_DIR / f"{m.ticker}.json"
            if path.exists() or m.occurrence_datetime is None:
                skipped += 1
                continue
            end = int(m.occurrence_datetime.timestamp())
            start = end - int(HISTORY_WINDOW.total_seconds())
            candles = client.candlesticks(series, m.ticker, start, end)
            path.write_text(json.dumps([c.model_dump(mode="json") for c in candles]))
            fetched += 1
            time.sleep(THROTTLE_S)
    print(f"candles fetched for {fetched} markets ({skipped} already cached) -> {HISTORY_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
