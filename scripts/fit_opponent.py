"""Refit the opponent model on the H2H league's real data and print it (D30). Changes nothing.

1. Captain spread (habit weight, temperature): maximum likelihood over every league manager's
   real captain picks, using the model's no-peeking expected points for that gameweek (from the
   back-test records, data/cache/validation/2026-27_points.jsonl). Compared against two simple
   rules: "top expected points" and "same as last week".
2. AVERAGE scale: FPL's overall average vs ownership x real points, for finished gameweeks
   whose pre-deadline ownership was saved in a flag snapshot. Weeks without one are shown with
   TODAY's ownership, marked indicative (ownership drifts: GW1 was 14 points off that way).

Caches league picks and H2H results in data/cache/ (gitignored); only missing ones are fetched,
throttled. Other managers are never printed by name or entry id. Updating the constants in
fpl_agent/optimize/opponent.py stays a reviewed decision, recorded in docs/decisions.md.

Usage: python scripts/fit_opponent.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from fpl_agent.config import load_settings
from fpl_agent.data.client import API, make_session
from fpl_agent.data.models import Bootstrap, EntryPicks
from fpl_agent.data.opponent import CAPTAIN_HISTORY_GWS
from fpl_agent.data.snapshots import FileSnapshotStore
from fpl_agent.optimize.opponent import (
    AVERAGE_SCALE,
    CAPTAIN_TEMPERATURE,
    HABIT_WEIGHT,
    TEMPERATURE_GRID,
    CaptainCase,
    captain_log_likelihood,
    captain_probabilities,
    fit_average_scale,
    fit_captain,
    team_sheet,
)
from fpl_agent.optimize.rules import Limits
from fpl_agent.validation.backtest import VALIDATION_DIR

CACHE = Path("data/cache")
THROTTLE_S = 1.0
SEASON = "2026-27"


def cached_json(http: Any, path: Path, url: str) -> Any:
    if not path.exists():
        resp = http.get(url, timeout=20)
        resp.raise_for_status()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(resp.text)
        time.sleep(THROTTLE_S)
    return json.loads(path.read_text())


def main() -> int:
    settings = load_settings()
    http = make_session()
    boot = Bootstrap.model_validate(http.get(f"{API}/bootstrap-static/", timeout=20).json())
    limits = Limits.from_bootstrap(boot)
    finished = [e.id for e in boot.events if e.finished]

    # --- league data: H2H results (members, AVERAGE) and every member's picks ---------------
    entries: set[int] = set()
    for gw in finished:
        url = f"{API}/leagues-h2h-matches/league/{settings.h2h_league_id}/?event={gw}&page=1"
        page = cached_json(http, CACHE / "h2h" / f"gw{gw}.json", url)
        if page["has_next"]:
            raise RuntimeError("league has more than one page of matches: extend this script")
        for m in page["results"]:
            entries |= {e for e in (m["entry_1_entry"], m["entry_2_entry"]) if e is not None}
    picks: dict[tuple[int, int], EntryPicks] = {}
    for e in sorted(entries):
        for gw in finished:
            url = f"{API}/entry/{e}/event/{gw}/picks/"
            raw = cached_json(http, CACHE / "picks" / f"{e}_{gw}.json", url)
            picks[(e, gw)] = EntryPicks.model_validate(raw)
    print(f"League: {len(entries)} managers, gameweeks {finished[0]}-{finished[-1]} finished")

    # --- 1. captain spread -----------------------------------------------------------------------
    xp: dict[int, dict[int, float]] = defaultdict(dict)
    for line in (VALIDATION_DIR / f"{SEASON}_points.jsonl").read_text().splitlines():
        r = json.loads(line)
        xp[r["gw"]][r["player"]] = r["e_points"]
    cases, last_captain = [], []
    for (e, gw), p in sorted(picks.items()):
        if gw not in xp:
            continue
        sheet = team_sheet(p, limits)
        history = tuple(
            team_sheet(picks[(e, g)], limits).captain
            for g in range(max(1, gw - CAPTAIN_HISTORY_GWS), gw)
            if (e, g) in picks
        )
        cases.append(CaptainCase(sheet.starters, xp[gw], history, sheet.captain))
        last_captain.append(history[-1] if history else None)
    missing = [gw for gw in finished if gw > 1 and gw not in xp]
    print(
        f"\nCaptain: {len(cases)} decisions (gameweeks {sorted(xp)} have back-test expected points)"
    )
    if missing:
        print(f"  no expected points yet for gameweeks {missing}: extend and re-run the back-test")
    w, t, ll = fit_captain(cases)
    now = captain_log_likelihood(cases, HABIT_WEIGHT, CAPTAIN_TEMPERATURE)
    print(f"  best fit: habit weight {w:.2f}, temperature {t}: avg log-likelihood {ll:.3f}")
    print(f"  in use:   habit weight {HABIT_WEIGHT}, temperature {CAPTAIN_TEMPERATURE}: {now:.3f}")
    for weight in (0.0, 0.5, 0.6, 0.7, 0.8, 0.9):
        best_t = max(TEMPERATURE_GRID, key=lambda x: captain_log_likelihood(cases, weight, x))
        score = captain_log_likelihood(cases, weight, best_t)
        print(f"    habit weight {weight:.1f} (temperature {best_t}): {score:.3f}")

    def hit(choice: list[int | None]) -> float:
        return float(np.mean([c == case.captain for c, case in zip(choice, cases, strict=True)]))

    fitted = [
        max(
            probs := captain_probabilities(c.starters, c.expected_points, c.history, w, t),
            key=probs.__getitem__,
        )
        for c in cases
    ]
    top_xp = [max(c.starters, key=lambda p: c.expected_points.get(p, 0.0)) for c in cases]
    print(
        f"  most likely captain was right: fitted {hit(list(fitted)):.0%}, "
        f"top expected points {hit(list(top_xp)):.0%}, same as last week {hit(last_captain):.0%}"
    )

    # --- 2. AVERAGE scale ------------------------------------------------------------------------
    store = FileSnapshotStore(settings.snapshot_dir)
    today = {p.id: p.selected_by_percent for p in boot.elements}
    average = {e.id: e.average_entry_score for e in boot.events}
    point_in_time, template, actual = [], [], []
    print("\nAVERAGE: FPL's overall average vs ownership x points")
    for gw in finished:
        live = cached_json(http, CACHE / f"live_gw{gw}.json", f"{API}/event/{gw}/live/")
        points = {el["id"]: el["stats"]["total_points"] for el in live["elements"]}
        snap = store.load(gw)
        own = snap.ownership if snap is not None and snap.ownership else None
        s = sum((own or today).get(p, 0.0) / 100 * v for p, v in points.items())
        tag = "pre-deadline ownership" if own else "today's ownership (indicative)"
        print(
            f"  GW{gw}: average {average[gw]:3d}, template {s:6.1f}, "
            f"ratio {average[gw] / s:.3f}  [{tag}]"
        )
        if own:
            point_in_time.append(gw)
            template.append(s)
            actual.append(average[gw])
    if point_in_time:
        k = fit_average_scale(template, actual)
        resid = [a - round(k * s) for s, a in zip(template, actual, strict=True)]
        print(f"  fit on {point_in_time}: scale {k:.3f} (in use {AVERAGE_SCALE}); misses {resid}")
    else:
        print(
            f"  no pre-deadline ownership saved yet (snapshots from GW6 on); in use {AVERAGE_SCALE}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
