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
    minutes: int


class GameConfig(FplModel):
    # Each value is either a flat number or a per-position table, e.g.
    # goals_scored: {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}. Read, never hardcode (rule 8).
    scoring: dict[str, int | dict[str, int]]


class Bootstrap(FplModel):
    events: list[Event]
    teams: list[Team]
    elements: list[Player]
    element_types: list[ElementType]
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


class Pick(FplModel):
    element: int
    position: int  # 1-11 starting XI, 12-15 bench in auto-sub order
    multiplier: int
    is_captain: bool
    is_vice_captain: bool
    element_type: int
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
