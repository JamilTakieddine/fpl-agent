"""Re-measure the availability rules from the agent's own flag snapshots. Changes nothing.

`docs/maintenance.md` schedules these for every ~5 gameweeks. Each uses the flag as it really was
15 minutes before the deadline (the cloud bucket's snapshots, mirrored into data/cache/bucket/ by
scripts/fetch_league_cache.py) and FPL's live data for what happened.
1. SURPRISE_NON_START (D13, 0.10; keepers 0.035, D50): of players unflagged at the deadline who
   started every one of the previous 4 gameweeks, the share who didn't start; pooled and per
   position.
2. Doubtful-then-absent (D33): players doubtful at a deadline who played 0 minutes are excused
   as injured. Did they start the next gameweek, and were they still flagged?
3. Flag duration (D32), from one snapshot to the next: did a dateless doubtful knock clear
   within a gameweek; did an "Expected back" player return when the date passed; how long did an
   undated injury or suspension last?

Usage: python scripts/measure_availability.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from fpl_agent.data.injuries import DOUBTFUL, GONE, return_date
from fpl_agent.data.lineups import (
    GOALKEEPER_SURPRISE_NON_START,
    SURPRISE_NON_START,
    availability_of,
)
from fpl_agent.data.models import Bootstrap
from fpl_agent.data.snapshots import FileSnapshotStore, PlayerFlag

CACHE = Path("data/cache")
SNAPSHOTS = FileSnapshotStore(CACHE / "bucket" / "snapshots")
RUN_IN = 4  # gameweeks a player must have started, back to back (D13's definition)


def share(k: int, n: int) -> str:
    return f"{k}/{n} = {k / n:.1%}" if n else "0/0"


def main() -> int:
    boot = Bootstrap.model_validate(json.loads((CACHE / "bootstrap.json").read_text()))
    position = {p.id: boot.position_code(p.element_type) for p in boot.elements}
    finished = [e.id for e in boot.events if e.finished]
    stats: dict[int, dict[int, Any]] = {
        gw: {
            el["id"]: el["stats"]
            for el in json.loads((CACHE / f"live_gw{gw}.json").read_text())["elements"]
        }
        for gw in finished
    }
    snaps = {gw: s for gw in finished if (s := SNAPSHOTS.load(gw)) is not None}
    print(f"snapshots for GW{sorted(snaps)}; live data GW{finished[0]}-{finished[-1]}")

    def starts(pid: int, gw: int) -> int:
        return int(stats[gw].get(pid, {}).get("starts", 0))

    def minutes(pid: int, gw: int) -> int:
        return int(stats[gw].get(pid, {}).get("minutes", 0))

    def chance(flag: PlayerFlag | None) -> float:
        return (
            1.0 if flag is None else availability_of(flag.status, flag.chance_of_playing_next_round)
        )

    # --- 1. surprise non-starts ------------------------------------------------------------------
    eligible: Counter[str] = Counter()
    missed: Counter[str] = Counter()
    by_gw = []
    for gw, snap in sorted(snaps.items()):
        if gw - RUN_IN < 1:
            continue
        n = k = 0
        for pid, flag in snap.flags.items():
            if chance(flag) < 1.0 or flag.status != "a":
                continue
            if not all(starts(pid, g) >= 1 for g in range(gw - RUN_IN, gw)):
                continue
            pos, out = position[pid], starts(pid, gw) == 0
            eligible[pos] += 1
            missed[pos] += out
            n, k = n + 1, k + out
        by_gw.append(f"GW{gw} {share(k, n)}")
    total_n, total_k = sum(eligible.values()), sum(missed.values())
    print(f"\n1. Surprise non-start (in use {SURPRISE_NON_START:.0%}): {share(total_k, total_n)}")
    print(f"   by gameweek: {', '.join(by_gw)}")
    for pos in ("GKP", "DEF", "MID", "FWD"):
        print(f"   {pos}: {share(missed[pos], eligible[pos])}")
    print(f"   keepers in use: {GOALKEEPER_SURPRISE_NON_START:.1%}")

    # --- 2. doubtful at the deadline, then 0 minutes ---------------------------------------------
    print(
        "\n2. Doubtful at the deadline (chance between 0 and 100%), then 0 minutes; next gameweek:"
    )
    rows: Counter[str] = Counter()
    played_doubtful = Counter[str]()
    for gw, snap in sorted(snaps.items()):
        if gw + 1 not in snaps:
            continue
        nxt = snaps[gw + 1]
        for pid, flag in snap.flags.items():
            if not 0.0 < chance(flag) < 1.0:
                continue
            if minutes(pid, gw) > 0:
                played_doubtful["played the doubtful week"] += 1
                played_doubtful["... and started the next"] += starts(pid, gw + 1) >= 1
                continue
            still = chance(nxt.flags.get(pid)) < 1.0
            started = starts(pid, gw + 1) >= 1
            rows["cases"] += 1
            rows["started the next gameweek"] += started
            rows["still flagged at the next deadline"] += still
            rows["unflagged next and didn't start (looks like rotation)"] += (
                not still and not started
            )
    for label, v in rows.items():
        print(f"   {label}: {v}" if label == "cases" else f"   {label}: {share(v, rows['cases'])}")
    n_played = played_doubtful["played the doubtful week"]
    print(
        f"   for comparison, doubtful players who DID play: {n_played}; started the next gameweek "
        f"{share(played_doubtful['... and started the next'], n_played)}"
    )

    # --- 3. how long flags last ------------------------------------------------------------------
    print("\n3. Flag duration (D32), snapshot to snapshot:")
    knock: Counter[str] = Counter()
    dated: Counter[str] = Counter()
    undated: dict[int, list[int]] = {}
    for gw, snap in sorted(snaps.items()):
        for pid, flag in snap.flags.items():
            if flag.status == "a" and flag.chance_of_playing_next_round is None:
                continue
            if flag.status in GONE:
                continue
            back = return_date(flag.news, snap.deadline.date())
            later = [g for g in sorted(snaps) if g > gw]
            if back is None and flag.status == DOUBTFUL and gw + 1 in snaps:
                fit = chance(snaps[gw + 1].flags.get(pid)) == 1.0
                knock["cases"] += 1
                knock["fit at the next deadline"] += fit
                knock["played the next gameweek"] += minutes(pid, gw + 1) > 0
            elif back is not None:
                due = [g for g in later if snaps[g].deadline.date() >= back]
                if due:  # the first snapshotted deadline on or after the date
                    g = due[0]
                    dated["cases"] += 1
                    dated["fit at the first deadline after the date"] += (
                        chance(snaps[g].flags.get(pid)) == 1.0
                    )
                    dated["played that gameweek"] += minutes(pid, g) > 0
            elif flag.status in ("i", "s") and back is None:
                undated.setdefault(pid, []).append(gw)
    print(
        f"   dateless doubtful knock (D32: over after the next gameweek), {knock['cases']} cases:"
    )
    for label in ("fit at the next deadline", "played the next gameweek"):
        print(f"     {label}: {share(knock[label], knock['cases'])}")
    print(f"   'Expected back' / 'until' a date, {dated['cases']} cases with a later snapshot:")
    for label in ("fit at the first deadline after the date", "played that gameweek"):
        print(f"     {label}: {share(dated[label], dated['cases'])}")
    # An undated injury: first seen at gw, and was it cleared at a later snapshot?
    lasted = Counter[str]()
    for pid, gws in undated.items():
        first, last = gws[0], gws[-1]
        cleared = [g for g in sorted(snaps) if g > last and chance(snaps[g].flags.get(pid)) == 1.0]
        if cleared:
            lasted[f"cleared after {cleared[0] - first} gameweek(s)"] += 1
        else:
            lasted[f"still out at GW{max(snaps)} (seen from GW{first})"] += 1
    print(f"   undated injury/suspension (D32: out for the whole horizon), {len(undated)} players:")
    for label, v in sorted(lasted.items()):
        print(f"     {label}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
