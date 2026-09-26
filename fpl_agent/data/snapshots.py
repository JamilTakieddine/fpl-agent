"""Per-gameweek snapshots of every player's availability flags.

FPL only exposes a player's flag as it is NOW, so there's no way to ask later "was he injured
before GW3's deadline?". Each run therefore records all flags from the bootstrap it already
fetched (no extra requests). The lineup baseline uses them to EXCUSE matches a player was
flagged out for, instead of counting them as "benched while fit". See docs/decisions.md (D14).

One snapshot per gameweek: the latest one taken BEFORE that gameweek's deadline wins (the
day-before check and the deadline run both write; the later one has the freshest news).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from fpl_agent.data.models import Bootstrap
from fpl_agent.fileio import atomic_write


class PlayerFlag(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    chance_of_playing_next_round: int | None
    news: str
    news_added: datetime | None


class FlagSnapshot(BaseModel):
    """Flags as known before `event`'s deadline. Validated on read: a file on disk is a boundary."""

    model_config = ConfigDict(frozen=True)

    event: int
    taken_at: datetime
    deadline: datetime
    flags: dict[int, PlayerFlag]


class SnapshotStore(Protocol):
    def save(self, snapshot: FlagSnapshot) -> None: ...
    def load(self, event: int) -> FlagSnapshot | None: ...


class FileSnapshotStore:
    """One JSON file per gameweek: <dir>/flags_gw06.json."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, event: int) -> Path:
        return self.directory / f"flags_gw{event:02d}.json"

    def save(self, snapshot: FlagSnapshot) -> None:
        atomic_write(self.path(snapshot.event), snapshot.model_dump_json())

    def load(self, event: int) -> FlagSnapshot | None:
        path = self.path(event)
        if not path.exists():
            return None
        return FlagSnapshot.model_validate_json(path.read_text())


def take_snapshot(bootstrap: Bootstrap, event: int, now: datetime) -> FlagSnapshot:
    deadline = next(e.deadline_time for e in bootstrap.events if e.id == event)
    return FlagSnapshot(
        event=event,
        taken_at=now,
        deadline=deadline,
        flags={
            p.id: PlayerFlag(
                status=p.status,
                chance_of_playing_next_round=p.chance_of_playing_next_round,
                news=p.news,
                news_added=p.news_added,
            )
            for p in bootstrap.elements
        },
    )


def record_snapshot(
    store: SnapshotStore, bootstrap: Bootstrap, event: int, now: datetime
) -> FlagSnapshot | None:
    """Save this run's flags for `event`. Returns None (and saves nothing) if the deadline has
    passed: flags after the deadline say nothing about who was expected to play."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    snapshot = take_snapshot(bootstrap, event, now)
    if now >= snapshot.deadline:
        return None
    existing = store.load(event)
    if existing is not None and existing.taken_at > now:
        return None  # never overwrite a later snapshot with an earlier one
    store.save(snapshot)
    return snapshot
