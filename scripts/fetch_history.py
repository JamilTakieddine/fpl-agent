"""Download historical back-testing data into data/cache/history/ (gitignored; never committed).

Sources and their terms: see fpl_agent/validation/history.py and docs/decisions.md (D24).

Usage: python scripts/fetch_history.py [season]      (default 2025-26)
"""

from __future__ import annotations

import sys
import time

from fpl_agent.data.client import make_session
from fpl_agent.validation.history import HISTORY_DIR, sources


def main() -> int:
    season = sys.argv[1] if len(sys.argv) > 1 else "2025-26"
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    http = make_session()
    for name, url in sources(season).items():
        resp = http.get(url, timeout=60)
        resp.raise_for_status()
        (HISTORY_DIR / name).write_bytes(resp.content)
        print(f"{name}: {len(resp.content) / 1024:.0f} KB  <- {url}")
        time.sleep(0.5)  # polite
    return 0


if __name__ == "__main__":
    sys.exit(main())
