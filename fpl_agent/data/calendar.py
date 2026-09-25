"""Fixture calendar: per-gameweek fixture counts per team, double/blank gameweeks, deadlines.

Pure functions over already-validated models (so plain frozen dataclasses here, not pydantic).
Doubles and blanks are tracked PER TEAM: for Bench Boost / Triple Captain / Free Hit what matters
is which of *my* players play twice or not at all, which the Phase 5 planner derives from this.

Recompute every run: FPL moves fixtures mid-season. A postponed match gets event=None
("unscheduled") and later lands in a gameweek where that team already plays, creating a double.
Unscheduled fixtures are reported, not dropped: they are the early warning of future doubles.

Deadlines come ONLY from events[].deadline_time, never from kickoff times (rule; see D10).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from fpl_agent.data.models import Bootstrap, Fixture


@dataclass(frozen=True)
class Gameweek:
    id: int
    deadline: datetime
    fixtures: tuple[Fixture, ...]
    fixture_count: dict[int, int]  # team id -> matches this gameweek (0, 1, 2, ...)

    @property
    def blank_teams(self) -> frozenset[int]:
        return frozenset(t for t, n in self.fixture_count.items() if n == 0)

    @property
    def double_teams(self) -> frozenset[int]:
        return frozenset(t for t, n in self.fixture_count.items() if n >= 2)

    @property
    def is_blank(self) -> bool:
        """At least one team has no match: a Free Hit window (rule 7)."""
        return bool(self.blank_teams)

    @property
    def is_double(self) -> bool:
        """At least one team plays twice: a Bench Boost / Triple Captain window (rule 7)."""
        return bool(self.double_teams)


@dataclass(frozen=True)
class Calendar:
    gameweeks: dict[int, Gameweek]
    unscheduled: tuple[Fixture, ...]  # postponed, no gameweek yet: likely future doubles

    def get(self, gw: int) -> Gameweek:
        return self.gameweeks[gw]

    def window(self, start: int, length: int) -> list[Gameweek]:
        """Up to `length` gameweeks from `start` (fewer at season end). For the planner horizon."""
        return [self.gameweeks[g] for g in range(start, start + length) if g in self.gameweeks]

    def double_gameweeks(self) -> list[int]:
        return [g for g, gw in sorted(self.gameweeks.items()) if gw.is_double]

    def blank_gameweeks(self) -> list[int]:
        return [g for g, gw in sorted(self.gameweeks.items()) if gw.is_blank]

    def team_unscheduled(self, team: int) -> int:
        return sum(1 for f in self.unscheduled if team in (f.team_h, f.team_a))


def count_fixtures(fixtures: list[Fixture], team_ids: list[int]) -> dict[int, int]:
    """Matches per team. Every team is present, so a blank shows as 0 rather than missing."""
    counts: Counter[int] = Counter()
    for f in fixtures:
        counts[f.team_h] += 1
        counts[f.team_a] += 1
    return {t: counts[t] for t in team_ids}


def build_calendar(bootstrap: Bootstrap, fixtures: list[Fixture]) -> Calendar:
    team_ids = [t.id for t in bootstrap.teams]
    by_gw: dict[int, list[Fixture]] = {e.id: [] for e in bootstrap.events}
    unscheduled: list[Fixture] = []
    for f in fixtures:
        if f.event is None:
            unscheduled.append(f)
        else:
            by_gw.setdefault(f.event, []).append(f)

    deadlines = {e.id: e.deadline_time for e in bootstrap.events}
    gameweeks = {
        gw: Gameweek(
            id=gw,
            deadline=deadlines[gw],
            fixtures=tuple(
                sorted(fs, key=lambda f: (f.kickoff_time is None, f.kickoff_time, f.id))
            ),
            fixture_count=count_fixtures(fs, team_ids),
        )
        for gw, fs in by_gw.items()
    }
    return Calendar(gameweeks=gameweeks, unscheduled=tuple(unscheduled))


def next_deadline(bootstrap: Bootstrap, now: datetime) -> tuple[int, datetime] | None:
    """The first gameweek whose deadline is still in the future.

    Uses deadline_time and the clock rather than the is_next flag: the flag flips on FPL's schedule,
    while the agent needs "which deadline is ahead of me right now". `now` must be timezone-aware.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (deadlines are UTC)")
    upcoming = sorted((e.deadline_time, e.id) for e in bootstrap.events if e.deadline_time > now)
    if not upcoming:
        return None
    deadline, gw = upcoming[0]
    return gw, deadline
