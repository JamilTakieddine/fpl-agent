"""The current season as a back-test Season: FPL's own live data + rebuilt Kalshi prices (D25).

Player rows come from the cached /event/{gw}/live/ responses, converted to the archive's row
format so the same rebuild and metrics apply. Differences from the 2025/26 archive:
- No xP: FPL only publishes the CURRENT ep_next, so the FPL benchmark is 2025/26-only.
- Team at the time: live data doesn't say which side a player played for. If his current club
  is in the fixture it's that club; otherwise (transferred since) the row is ambiguous and is
  skipped and counted, never guessed.
- Real deadlines from bootstrap-static (no kickoff-based approximation).
- Odds: Kalshi prices as they stood 60 minutes before each gameweek's deadline.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fpl_agent.data.kalshi import MATCH_SERIES, TOTALS_SERIES
from fpl_agent.data.models import Bootstrap, Fixture
from fpl_agent.data.odds import MatchOdds
from fpl_agent.validation.history import HISTORY_DIR, HistoricalRow
from fpl_agent.validation.kalshi_history import CANDLE_DIR, load_settled, odds_for_gameweek
from fpl_agent.validation.pointintime import OddsSource, Season

_CODE = {1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}


def rows_from_live(
    event: int,
    live: dict[str, Any],
    bootstrap: Bootstrap,
    fixtures: list[Fixture],
) -> tuple[list[HistoricalRow], int]:
    """Archive-style rows for one gameweek of raw live JSON; returns (rows, ambiguous skipped)."""
    players = bootstrap.players_by_id()
    team_names = {t.id: t.name for t in bootstrap.teams}
    by_id = {f.id: f for f in fixtures}
    rows, ambiguous = [], 0
    for el in live["elements"]:
        p = players.get(el["id"])
        if p is None or len(el["explain"]) != 1:
            continue  # unknown player, not in a squad, or a double gameweek (none in 2026/27 yet)
        f = by_id[el["explain"][0]["fixture"]]
        if p.team not in (f.team_h, f.team_a):
            ambiguous += 1
            continue
        s = el["stats"]
        home = p.team == f.team_h
        rows.append(
            HistoricalRow(
                element=p.id,
                name=p.web_name,
                position=_CODE[p.element_type],
                team=team_names[p.team],
                fixture=f.id,
                GW=event,
                kickoff_time=f.kickoff_time,
                was_home=home,
                opponent_team=f.team_a if home else f.team_h,
                minutes=s["minutes"],
                starts=s["starts"],
                goals_scored=s["goals_scored"],
                assists=s["assists"],
                expected_goals=float(s["expected_goals"]),
                expected_assists=float(s["expected_assists"]),
                expected_goals_conceded=float(s["expected_goals_conceded"]),
                clean_sheets=s["clean_sheets"],
                goals_conceded=s["goals_conceded"],
                saves=s["saves"],
                penalties_saved=s["penalties_saved"],
                penalties_missed=s["penalties_missed"],
                yellow_cards=s["yellow_cards"],
                red_cards=s["red_cards"],
                own_goals=s["own_goals"],
                defensive_contribution=s["defensive_contribution"],
                bps=s["bps"],
                bonus=s["bonus"],
                total_points=s["total_points"],
                xP=None,
                value=p.now_cost,
            )
        )
    return rows, ambiguous


def kalshi_odds_source(directory: Path = HISTORY_DIR, candles: Path = CANDLE_DIR) -> OddsSource:
    match_markets = load_settled(MATCH_SERIES, directory)
    totals_markets = load_settled(TOTALS_SERIES, directory)

    def source(
        target: int, fixtures: list[Fixture], bootstrap: Bootstrap, deadline: datetime
    ) -> tuple[dict[int, MatchOdds], list[int]]:
        odds, _ = odds_for_gameweek(
            match_markets, totals_markets, fixtures, deadline, bootstrap, candles
        )
        return odds, [f.id for f in fixtures if f.id not in odds]

    return source


def load_current_season(
    bootstrap: Bootstrap,
    fixtures: list[Fixture],
    lives: dict[int, dict[str, Any]],
    odds_source: OddsSource,
    name: str = "2026-27",
) -> tuple[Season, int]:
    """The live season up to the last cached gameweek; returns (season, ambiguous rows skipped)."""
    rows_by_gw, ambiguous = {}, 0
    for gw, live in sorted(lives.items()):
        rows, skipped = rows_from_live(gw, live, bootstrap, fixtures)
        rows_by_gw[gw] = rows
        ambiguous += skipped
    season = Season(
        name=name,
        rows_by_gw=rows_by_gw,
        teams=bootstrap.teams,
        fixtures=fixtures,
        player_types={p.id: p.element_type for p in bootstrap.elements},
        odds_source=odds_source,
        template=bootstrap,
        deadlines={e.id: e.deadline_time for e in bootstrap.events},
    )
    return season, ambiguous
