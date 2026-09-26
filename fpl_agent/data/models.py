"""Typed models for FPL API responses.

Only the fields the agent uses are declared. Unknown fields are ignored (extra="ignore") so FPL
adding a field never breaks a run, but a missing or renamed field we rely on fails validation
loudly at the boundary instead of flowing into the optimizer. Models are frozen: API data is
read-only input. See docs/decisions.md (D8).

Money is kept as int tenths of a million (60 == 6.0m), exactly as the API sends it, so price
arithmetic (e.g. the 50% sell-on rounding) never touches floats.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

PositionCode = Literal["GKP", "DEF", "MID", "FWD"]


class FplModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# --- bootstrap-static -----------------------------------------------------------------------


class Event(FplModel):
    """A gameweek."""

    id: int
    name: str
    deadline_time: datetime  # the ONLY source of truth for deadlines (never kickoff times)
    finished: bool
    is_previous: bool
    is_current: bool
    is_next: bool


class Team(FplModel):
    id: int
    name: str
    short_name: str


class ElementType(FplModel):
    """A position, with its squad and formation limits."""

    id: int
    singular_name_short: PositionCode
    squad_select: int  # players of this position in a 15-man squad
    squad_min_play: int  # minimum in the starting XI
    squad_max_play: int  # maximum in the starting XI


class Player(FplModel):
    """An FPL 'element'."""

    id: int
    web_name: str
    team: int
    element_type: int
    now_cost: int  # tenths of a million
    status: str  # a=available, d=doubtful, i=injured, s=suspended, u=unavailable, n=not in squad
    chance_of_playing_next_round: int | None  # None means no flag, not 0%
    news: str
    minutes: int  # season total
    starts: int  # season total


class ChipDefinition(FplModel):
    """A chip and the gameweek window it's valid in. Two sets exist: GW1-19 and GW20-38 (rule 3)."""

    name: str  # "wildcard", "freehit", "bboost", "3xc"
    start_event: int
    stop_event: int


class GameConfig(FplModel):
    # Each value is either a flat number or a per-position table, e.g.
    # goals_scored: {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}. Read, never hardcode (rule 8).
    scoring: dict[str, int | dict[str, int]]


class Bootstrap(FplModel):
    events: list[Event]
    teams: list[Team]
    elements: list[Player]
    element_types: list[ElementType]
    chips: list[ChipDefinition]
    game_config: GameConfig

    def next_event(self) -> Event | None:
        """The upcoming gameweek (whose deadline the agent is working towards)."""
        return next((e for e in self.events if e.is_next), None)

    def players_by_id(self) -> dict[int, Player]:
        return {p.id: p for p in self.elements}

    def position_code(self, element_type: int) -> PositionCode:
        return next(t.singular_name_short for t in self.element_types if t.id == element_type)

    def points_for(self, action: str, position: PositionCode) -> int:
        """Scoring value for an action and position, whether stored flat or per position."""
        value = self.game_config.scoring[action]
        return value if isinstance(value, int) else value[position]


# --- fixtures -------------------------------------------------------------------------------


class Fixture(FplModel):
    id: int
    event: int | None  # None = not yet scheduled (postponed); these belong to no gameweek yet
    team_h: int
    team_a: int
    kickoff_time: datetime | None
    finished: bool
    team_h_difficulty: int
    team_a_difficulty: int


# --- my-team (authenticated) ----------------------------------------------------------------


class PublicPick(FplModel):
    """A pick as anyone can see it for a finished gameweek (no prices)."""

    element: int
    position: int  # 1-11 starting XI, 12-15 bench in auto-sub order
    multiplier: int
    is_captain: bool
    is_vice_captain: bool
    element_type: int


class Pick(PublicPick):
    """A pick from my own /my-team/, which adds prices."""

    selling_price: int  # what we'd actually get: use this, not now_cost (rule 5)
    purchase_price: int


class Chip(FplModel):
    id: int
    name: str  # "wildcard", "freehit", "bboost", "3xc"
    status_for_entry: str  # "available", "played", "unavailable", ...
    start_event: int
    stop_event: int
    is_pending: bool


class Transfers(FplModel):
    limit: int | None  # free transfers available; None when unlimited (e.g. wildcard active)
    made: int
    cost: int  # points hit per extra transfer
    bank: int  # tenths of a million
    value: int  # squad value, tenths of a million


class MyTeam(FplModel):
    picks: list[Pick]
    chips: list[Chip]
    transfers: Transfers


# --- H2H league and other managers (public) -------------------------------------------------


class H2HMatch(FplModel):
    id: int
    event: int
    entry_1_entry: int | None  # None on a bye
    entry_1_name: str
    entry_1_player_name: str | None
    entry_1_points: int
    entry_2_entry: int | None
    entry_2_name: str
    entry_2_player_name: str | None
    entry_2_points: int
    is_bye: bool  # odd-sized league: the entry plays the league AVERAGE score that week
    winner: int | None


class H2HMatchesPage(FplModel):
    has_next: bool
    page: int
    results: list[H2HMatch]


class ChipPlay(FplModel):
    name: str
    event: int


class EventHistory(FplModel):
    event: int
    points: int
    total_points: int
    event_transfers: int
    event_transfers_cost: int
    points_on_bench: int
    bank: int
    value: int


class EntryHistory(FplModel):
    current: list[EventHistory]
    chips: list[ChipPlay]


class EntryPicks(FplModel):
    """Another manager's picks for a FINISHED gameweek. The upcoming one is hidden (404)."""

    picks: list[PublicPick]
    active_chip: str | None


# --- per-gameweek live stats (public) -------------------------------------------------------


class ExplainStat(FplModel):
    identifier: str  # e.g. "minutes", "goals_scored"
    value: int


class ExplainFixture(FplModel):
    """One match's scoring breakdown. Every match the player's team played is listed,
    including minutes=0 for an unused or absent player, so it gives per-match minutes."""

    fixture: int
    stats: list[ExplainStat]

    def minutes(self) -> int:
        return next((s.value for s in self.stats if s.identifier == "minutes"), 0)


class LiveStats(FplModel):
    minutes: int  # summed over the gameweek (can exceed 90 in a double gameweek)
    starts: int  # likewise, 0-2


class LiveElement(FplModel):
    id: int
    stats: LiveStats
    explain: list[ExplainFixture]


class EventLive(FplModel):
    """/event/{gw}/live/: every player's stats for one gameweek in ONE request."""

    elements: list[LiveElement]
