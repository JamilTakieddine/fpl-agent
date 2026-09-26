"""Trim real FPL responses into small, committable test fixtures.

Input:  data/cache/{bootstrap,fixtures,my_team}.json   (raw API responses, gitignored)
        data/cache/{h2h_matches_gw6,opp_history,opp_picks_gw5,live_gw5}.json
        data/cache/kalshi_open_KXEPLGAME.json, kalshi_settled_KXEPLTOTAL.json (public market data)
Output: tests/fixtures/*.json                           (trimmed + anonymized, committed)

Public repo: other managers' names and entry ids (and mine) are replaced with fake ones.
FPL_ENTRY_ID becomes 1000001; everyone else 1000002, 1000003, ... in order of appearance.

Trimming keeps the real shape (every field of the players it keeps, so extra-field handling is
exercised) but only: all events/teams/positions, the scoring config, the squad's players + a
few others, and GW1-8 fixtures without their per-player stats. The full bootstrap is ~1.9 MB,
too big for the repo.

Usage: python scripts/record_fixtures.py   (reads FPL_ENTRY_ID from .env)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

CACHE = Path("data/cache")
OUT = Path("tests/fixtures")
EXTRA_PLAYERS = 20
MAX_EVENT = 8
FAKE_ME = 1000001
FAKE_LEAGUE = 2000001


class Anonymizer:
    """Consistent fake entry ids and names, so matches/histories still line up with each other."""

    def __init__(self, my_entry: int) -> None:
        self.ids = {my_entry: FAKE_ME}

    def entry(self, real: int | None) -> int | None:
        if real is None:
            return None
        return self.ids.setdefault(real, FAKE_ME + len(self.ids))

    def match(self, m: dict[str, Any]) -> dict[str, Any]:
        m = dict(m)
        for side in ("entry_1", "entry_2"):
            fake = self.entry(m[f"{side}_entry"])
            m[f"{side}_entry"] = fake
            m[f"{side}_name"] = f"Team {fake}" if fake else "AVERAGE"
            m[f"{side}_player_name"] = f"Manager {fake}" if fake else None
        m["winner"] = self.entry(m["winner"])
        m["league"] = FAKE_LEAGUE
        return m


def load(name: str) -> Any:
    return json.loads((CACHE / f"{name}.json").read_text())


def main() -> None:
    load_dotenv()
    boot, fixtures, team = load("bootstrap"), load("fixtures"), load("my_team")
    squad = {p["element"] for p in team["picks"]}
    others = [p["id"] for p in boot["elements"] if p["id"] not in squad][:EXTRA_PLAYERS]
    keep = squad | set(others)

    trimmed_boot = {
        "events": boot["events"],
        "teams": boot["teams"],
        "element_types": boot["element_types"],
        "chips": boot["chips"],
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

    anon = Anonymizer(int(os.environ["FPL_ENTRY_ID"]))
    matches = load("h2h_matches_gw6")
    h2h = {"has_next": False, "page": 1, "results": [anon.match(m) for m in matches["results"]]}
    history = load("opp_history")
    opp_history = {"current": history["current"], "chips": history["chips"]}
    picks = load("opp_picks_gw5")
    opp_picks = {"picks": picks["picks"], "active_chip": picks["active_chip"]}

    live = load("live_gw5")
    live_gw5 = {"elements": [e for e in live["elements"] if e["id"] in keep]}

    kalshi = {"markets": load("kalshi_open_KXEPLGAME")["markets"], "cursor": ""}
    kalshi_totals = {"markets": load("kalshi_settled_KXEPLTOTAL"), "cursor": ""}

    OUT.mkdir(parents=True, exist_ok=True)
    for name, obj in (
        ("bootstrap", trimmed_boot),
        ("fixtures", trimmed_fixtures),
        ("my_team", my_team),
        ("h2h_matches", h2h),
        ("opp_history", opp_history),
        ("opp_picks", opp_picks),
        ("live_gw5", live_gw5),
        ("kalshi_markets", kalshi),
        ("kalshi_totals_settled", kalshi_totals),
    ):
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(obj, indent=1, ensure_ascii=False) + "\n")
        print(f"{path}: {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
