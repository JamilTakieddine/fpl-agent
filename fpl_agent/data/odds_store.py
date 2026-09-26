"""Saved Kalshi odds: the latest market price per fixture, kept across runs.

Kalshi opens match markets about two weeks early, but a fixture's market can be missing or too
thin at run time. A slightly stale market price for THIS fixture beats the xG-ratings fallback
(docs/decisions.md D16, D17), so every run merges the odds it got into one file per gameweek:

- A fixture priced now replaces its older entry; fixtures not priced now keep their last price.
- A newer saved price is never overwritten by an older run.
- Only pre-kickoff prices are saved (in-play prices aren't pre-match expectations).
- Each entry records the kickoff it was priced for; if FPL later moves the fixture by more than
  KICKOFF_TOLERANCE the entry is ignored, because that market was for a different date.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from fpl_agent.data.models import Fixture
from fpl_agent.data.odds import KICKOFF_TOLERANCE, MatchOdds
from fpl_agent.fileio import atomic_write


class SavedOdds(BaseModel):
    """MatchOdds plus when it was priced and for which kickoff. Validated when read back."""

    model_config = ConfigDict(frozen=True)

    fixture_id: int
    p_home: float
    p_draw: float
    p_away: float
    lambda_home: float
    lambda_away: float
    overround: float
    volume: float
    max_spread: float
    total_source: str
    totals_lines: int
    draw_gap: float
    taken_at: datetime
    kickoff: datetime

    def to_match_odds(self) -> MatchOdds:
        fields = {f.name for f in dataclasses.fields(MatchOdds)}
        return MatchOdds(**{k: v for k, v in self.model_dump().items() if k in fields})


class GameweekOdds(BaseModel):
    model_config = ConfigDict(frozen=True)

    event: int
    fixtures: dict[int, SavedOdds]


class OddsStore(Protocol):
    def load(self, event: int) -> dict[int, SavedOdds]: ...
    def save(self, event: int, odds: dict[int, SavedOdds]) -> None: ...


class FileOddsStore:
    """One JSON file per gameweek: <dir>/odds_gw06.json."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, event: int) -> Path:
        return self.directory / f"odds_gw{event:02d}.json"

    def load(self, event: int) -> dict[int, SavedOdds]:
        path = self.path(event)
        if not path.exists():
            return {}
        return dict(GameweekOdds.model_validate_json(path.read_text()).fixtures)

    def save(self, event: int, odds: dict[int, SavedOdds]) -> None:
        atomic_write(self.path(event), GameweekOdds(event=event, fixtures=odds).model_dump_json())


def record_odds(
    store: OddsStore,
    event: int,
    odds: dict[int, MatchOdds],
    fixtures: list[Fixture],
    now: datetime,
) -> int:
    """Merge this run's pre-kickoff odds into the gameweek's saved odds. Returns how many fixtures
    were updated."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    kickoffs = {f.id: f.kickoff_time for f in fixtures}
    saved = store.load(event)
    updated = 0
    for fid, o in odds.items():
        kickoff = kickoffs.get(fid)
        if kickoff is None or now >= kickoff:
            continue  # unscheduled, or already kicked off
        existing = saved.get(fid)
        if existing is not None and existing.taken_at > now:
            continue  # never replace a newer price with an older run's
        saved[fid] = SavedOdds(**dataclasses.asdict(o), taken_at=now, kickoff=kickoff)
        updated += 1
    if updated:
        store.save(event, saved)
    return updated


def usable_saved_odds(saved: dict[int, SavedOdds], fixtures: list[Fixture]) -> dict[int, SavedOdds]:
    """Saved prices whose fixture still kicks off within KICKOFF_TOLERANCE of the priced kickoff."""
    by_id = {f.id: f for f in fixtures}
    usable = {}
    for fid, s in saved.items():
        f = by_id.get(fid)
        if f is None or f.kickoff_time is None:
            continue
        if abs(f.kickoff_time - s.kickoff) <= KICKOFF_TOLERANCE:
            usable[fid] = s
    return usable
