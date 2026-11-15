"""Refresh data/cache/ for the optimizer back-test (D35, D47): one command instead of by hand.

Fetches, for every FINISHED gameweek: FPL's live data, the H2H league's results, and every league
manager's picks (all public once the deadline has passed). Also refreshes bootstrap-static and the
fixture list, and mirrors the agent's saved flag snapshots and odds from the cloud bucket into
data/cache/bucket/ (the inputs it really had before each deadline). Finished gameweeks don't change,
so files already cached are skipped; re-running only fetches the new ones. Throttled to be polite.

Usage: python scripts/fetch_league_cache.py   (FPL_H2H_LEAGUE_ID; FPL_BUCKET for the mirror)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from fpl_agent.config import load_settings
from fpl_agent.data.client import API, make_session

CACHE = Path("data/cache")
MIRROR = CACHE / "bucket"
THROTTLE_S = 0.5


def main() -> int:
    settings = load_settings()
    if settings.h2h_league_id is None:
        print("Set FPL_H2H_LEAGUE_ID first (.env).")
        return 1
    http = make_session()

    def get(path: str) -> Any:
        time.sleep(THROTTLE_S)
        resp = http.get(f"{API}{path}", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def cached(path: Path, api_path: str) -> Any:
        if path.exists():
            return json.loads(path.read_text())
        data = get(api_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        print(f"  fetched {path}")
        return data

    boot = get("/bootstrap-static/")
    (CACHE / "bootstrap.json").write_text(json.dumps(boot))
    (CACHE / "fixtures.json").write_text(json.dumps(get("/fixtures/")))
    finished = [e["id"] for e in boot["events"] if e["finished"] and e["data_checked"]]
    print(f"finished gameweeks: GW{finished[0]}-{finished[-1]}")

    league = settings.h2h_league_id
    for gw in finished:
        cached(CACHE / f"live_gw{gw}.json", f"/event/{gw}/live/")
        h2h = cached(
            CACHE / "h2h" / f"gw{gw}.json",
            f"/leagues-h2h-matches/league/{league}/?event={gw}&page=1",
        )
        if h2h["has_next"]:
            print(f"GW{gw}: the league's results span several pages; only page 1 is cached.")
            return 1
        entries = {m[f"entry_{s}_entry"] for m in h2h["results"] for s in "12"} - {None}
        for entry in sorted(entries):
            cached(CACHE / "picks" / f"{entry}_{gw}.json", f"/entry/{entry}/event/{gw}/picks/")

    bucket_name = os.environ.get("FPL_BUCKET")
    if not bucket_name:
        print("FPL_BUCKET not set: skipped mirroring flag snapshots and odds.")
        return 0
    from fpl_agent.cloud.storage import BucketOddsStore, BucketSnapshotStore, GcsBucket

    bucket = GcsBucket(bucket_name)
    for gw in finished:
        for name in (BucketSnapshotStore.name(gw), BucketOddsStore.name(gw)):
            text = bucket.read(name)
            if text is not None:
                (MIRROR / name).parent.mkdir(parents=True, exist_ok=True)
                (MIRROR / name).write_text(text)
    print(f"mirrored the bucket's snapshots and odds into {MIRROR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
