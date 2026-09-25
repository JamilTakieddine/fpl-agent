"""Trim real FPL responses into small, committable test fixtures.

Input:  data/cache/{bootstrap,fixtures,my_team}.json  (raw API responses, gitignored)
Output: tests/fixtures/*.json                          (trimmed, committed)

Trimming keeps the real shape (every field of the players it keeps, so extra-field handling is
exercised) but only: all events/teams/positions, the scoring config, the squad's players + a
few others, and GW1-8 fixtures without their per-player stats. The full bootstrap is ~1.9 MB,
too big for the repo.

Usage: python scripts/record_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CACHE = Path("data/cache")
OUT = Path("tests/fixtures")
EXTRA_PLAYERS = 20
MAX_EVENT = 8


def load(name: str) -> Any:
    return json.loads((CACHE / f"{name}.json").read_text())


def main() -> None:
    boot, fixtures, team = load("bootstrap"), load("fixtures"), load("my_team")
    squad = {p["element"] for p in team["picks"]}
    others = [p["id"] for p in boot["elements"] if p["id"] not in squad][:EXTRA_PLAYERS]
    keep = squad | set(others)

    trimmed_boot = {
        "events": boot["events"],
        "teams": boot["teams"],
        "element_types": boot["element_types"],
        "elements": [p for p in boot["elements"] if p["id"] in keep],
        "game_config": {"scoring": boot["game_config"]["scoring"]},
    }
    # "stats" (per-player match events) is most of the size and unused by the models.
    trimmed_fixtures = [
        {k: v for k, v in f.items() if k != "stats"}
        for f in fixtures
        if f["event"] is not None and f["event"] <= MAX_EVENT
    ]
    my_team = {k: team[k] for k in ("picks", "chips", "transfers")}

    OUT.mkdir(parents=True, exist_ok=True)
    for name, obj in (
        ("bootstrap", trimmed_boot),
        ("fixtures", trimmed_fixtures),
        ("my_team", my_team),
    ):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
        print(f"{path}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
