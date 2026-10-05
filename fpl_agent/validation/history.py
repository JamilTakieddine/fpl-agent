"""Historical data for back-testing: sources and typed loaders (docs/decisions.md D24).

The FPL API keeps only season TOTALS for past seasons (element-summary history_past), so a
gameweek-by-gameweek back-test of 2025/26 uses two community archives:

- vaastav/Fantasy-Premier-League (MIT licence): per player per match stats for every gameweek,
  including FPL's own pre-gameweek expected points (xP), plus teams, fixtures and players.
- football-data.co.uk: closing odds for every Premier League match. Offered as free downloads;
  no explicit licence is stated, so it's used for personal back-testing only: downloaded at run
  time into the gitignored data/cache/history/, never committed or redistributed, and credited.

Market-AVERAGE closing odds are used for every match: Pinnacle's closing odds are missing for
170 of the 380 matches, and mixing sources would make matches inconsistent with each other.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fpl_agent.data.models import Fixture, PositionCode, Team

HISTORY_DIR = Path("data/cache/history")
VAASTAV = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
FOOTBALL_DATA = "https://www.football-data.co.uk/mmz4281"


def sources(season: str) -> dict[str, str]:
    """Local file name -> download URL for a season such as '2025-26'."""
    fd_code = season[2:4] + season[5:7]  # '2025-26' -> '2526'
    return {
        f"merged_gw_{season}.csv": f"{VAASTAV}/{season}/gws/merged_gw.csv",
        f"teams_{season}.csv": f"{VAASTAV}/{season}/teams.csv",
        f"fixtures_{season}.csv": f"{VAASTAV}/{season}/fixtures.csv",
        f"players_raw_{season}.csv": f"{VAASTAV}/{season}/players_raw.csv",
        f"E0_{fd_code}.csv": f"{FOOTBALL_DATA}/{fd_code}/E0.csv",
    }


def _read(path: Path) -> list[dict[str, Any]]:
    """CSV rows with empty strings turned into None (so optional fields validate)."""
    with path.open(newline="", encoding="utf-8-sig") as f:
        return [{k: (None if v == "" else v) for k, v in row.items()} for row in csv.DictReader(f)]


_POSITION_CODES: dict[str, PositionCode] = {"GK": "GKP", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}


class HistoricalRow(BaseModel):
    """One player in one match (vaastav merged_gw.csv). CSV strings are parsed by pydantic."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    element: int
    name: str
    position: str  # "GK", "DEF", "MID", "FWD"
    team: str  # team NAME at the time (handles mid-season transfers)
    fixture: int
    GW: int
    kickoff_time: datetime
    was_home: bool
    opponent_team: int
    minutes: int
    starts: int
    goals_scored: int
    assists: int
    expected_goals: float
    expected_assists: float
    expected_goals_conceded: float
    clean_sheets: int
    goals_conceded: int
    saves: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int
    defensive_contribution: int
    bps: int
    bonus: int
    total_points: int
    xP: float | None = None  # FPL's own pre-gameweek expected points (archive only)
    value: int  # price in tenths
    selected: int = 0  # managers owning him at the time (the planner back-test's template squad)

    @property
    def position_code(self) -> PositionCode:
        return _POSITION_CODES[self.position]


class ClosingOdds(BaseModel):
    """One match from football-data.co.uk, market-average closing odds (decimal)."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    date: str = Field(alias="Date")  # dd/mm/yyyy
    home: str = Field(alias="HomeTeam")
    away: str = Field(alias="AwayTeam")
    home_goals: int = Field(alias="FTHG")
    away_goals: int = Field(alias="FTAG")
    odds_home: float = Field(alias="AvgCH")
    odds_draw: float = Field(alias="AvgCD")
    odds_away: float = Field(alias="AvgCA")
    odds_over_2_5: float = Field(alias="AvgC>2.5")
    odds_under_2_5: float = Field(alias="AvgC<2.5")

    @property
    def day(self) -> datetime:
        return datetime.strptime(self.date, "%d/%m/%Y")


def dedupe_rows(rows: list[HistoricalRow]) -> tuple[list[HistoricalRow], int]:
    """Drop repeated (player, fixture) rows, keeping the first.

    The 2025/26 archive has 10 such duplicates (one player twice in 9 fixtures). Left in, they
    double that player's minutes, goals and xG in the rebuilt totals; a duplicate player-match
    can't be real, so dropping it is safe.
    """
    seen: set[tuple[int, int]] = set()
    kept = []
    for r in rows:
        key = (r.element, r.fixture)
        if key not in seen:
            seen.add(key)
            kept.append(r)
    return kept, len(rows) - len(kept)


def load_rows(season: str, directory: Path = HISTORY_DIR) -> list[HistoricalRow]:
    rows = [HistoricalRow.model_validate(r) for r in _read(directory / f"merged_gw_{season}.csv")]
    return dedupe_rows(rows)[0]


def load_teams(season: str, directory: Path = HISTORY_DIR) -> list[Team]:
    return [Team.model_validate(r) for r in _read(directory / f"teams_{season}.csv")]


def load_fixtures(season: str, directory: Path = HISTORY_DIR) -> list[Fixture]:
    return [Fixture.model_validate(r) for r in _read(directory / f"fixtures_{season}.csv")]


def load_player_types(season: str, directory: Path = HISTORY_DIR) -> dict[int, int]:
    """Player id -> element_type (position) from players_raw.csv."""
    return {
        int(r["id"]): int(r["element_type"]) for r in _read(directory / f"players_raw_{season}.csv")
    }


def load_closing_odds(season: str, directory: Path = HISTORY_DIR) -> list[ClosingOdds]:
    fd_code = season[2:4] + season[5:7]
    return [ClosingOdds.model_validate(r) for r in _read(directory / f"E0_{fd_code}.csv")]
