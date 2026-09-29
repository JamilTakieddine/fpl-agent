"""Rebuild a past gameweek's inputs using ONLY information from before it (docs/decisions.md D24).

A back-test that leaks the future looks great and means nothing. For target gameweek k:
- Player totals (minutes, xG, DEFCON, saves, cards, ...) are re-summed from per-match rows
  BEFORE k, not taken from end-of-season totals.
- Fixtures before k keep their results; fixtures from k on have no scores and aren't finished.
- Live data (minutes and BPS per match) is rebuilt for the lineup window before k.
- Odds: market-average CLOSING odds for k's matches, converted with the same Poisson pipeline
  as live Kalshi odds (1X2 sets the split, over/under 2.5 sets the total). Caveat: closing odds
  are set slightly AFTER FPL's deadline, so they're a little better informed than ours will be.
- No injury flags exist historically: every player is 'available' (status a, no percentage).
  The live system has flags, so the back-test UNDERSTATES it on availability.
- Deadlines: first kickoff minus 90 minutes (only for building the calendar; the back-test never
  decides anything by deadline time).
Point values and positions come from a template bootstrap (the current season's), and 2025/26
had the same scoring rules, including DEFCON; D24 verifies this by reproducing FPL's own
total_points for every 2025/26 player-match.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from fpl_agent.data.calendar import Calendar, build_calendar
from fpl_agent.data.lineups import WINDOW_GWS, window_events
from fpl_agent.data.models import (
    Bootstrap,
    Event,
    EventLive,
    ExplainFixture,
    ExplainStat,
    Fixture,
    LiveElement,
    LiveStats,
    Player,
    Team,
)
from fpl_agent.data.odds import MatchOdds, outcome_probs, split_total, total_from_line
from fpl_agent.validation.history import (
    ClosingOdds,
    HistoricalRow,
    load_closing_odds,
    load_fixtures,
    load_player_types,
    load_rows,
    load_teams,
)

DEADLINE_BEFORE_FIRST_KICKOFF = timedelta(minutes=90)
ODDS_DATE_TOLERANCE = timedelta(days=1)  # football-data dates are UK-local; FPL kickoffs are UTC
# football-data.co.uk name -> FPL name, where they differ.
FOOTBALL_DATA_TO_FPL = {"Man United": "Man Utd", "Tottenham": "Spurs"}
_STAT_FIELDS = (
    "goals_scored",
    "assists",
    "clean_sheets",
    "goals_conceded",
    "saves",
    "penalties_saved",
    "yellow_cards",
    "red_cards",
    "defensive_contribution",
    "bps",
    "bonus",
)


# (target gameweek, its fixtures, the rebuilt bootstrap, its deadline) -> (odds, unpriced ids)
OddsSource = Callable[
    [int, list[Fixture], Bootstrap, datetime], tuple[dict[int, MatchOdds], list[int]]
]


@dataclass(frozen=True)
class Season:
    name: str
    rows_by_gw: dict[int, list[HistoricalRow]]
    teams: list[Team]
    fixtures: list[Fixture]
    player_types: dict[int, int]
    odds_source: OddsSource  # closing odds (2025/26) or rebuilt Kalshi prices (2026/27)
    template: Bootstrap  # element_types, chips and game_config (scoring) to reuse
    deadlines: dict[int, datetime] | None = None  # real deadlines if known, else approximated


def closing_odds_source(season_teams: list[Team], closing: list[ClosingOdds]) -> OddsSource:
    team_ids = {t.name: t.id for t in season_teams}

    def fpl_id(name: str) -> int:
        return team_ids[FOOTBALL_DATA_TO_FPL.get(name, name)]

    def source(
        target: int, fixtures: list[Fixture], bootstrap: Bootstrap, deadline: datetime
    ) -> tuple[dict[int, MatchOdds], list[int]]:
        odds, unpriced = {}, []
        for f in fixtures:
            match = next(
                (
                    o
                    for o in closing
                    if fpl_id(o.home) == f.team_h
                    and fpl_id(o.away) == f.team_a
                    and f.kickoff_time is not None
                    and abs(o.day.date() - f.kickoff_time.date()) <= ODDS_DATE_TOLERANCE
                ),
                None,
            )
            if match is None:
                unpriced.append(f.id)
            else:
                odds[f.id] = closing_to_match_odds(f, match)
        return odds, unpriced

    return source


def load_season(name: str, template: Bootstrap) -> Season:
    by_gw: dict[int, list[HistoricalRow]] = defaultdict(list)
    for r in load_rows(name):
        by_gw[r.GW].append(r)
    teams = load_teams(name)
    return Season(
        name=name,
        rows_by_gw=dict(by_gw),
        teams=teams,
        fixtures=load_fixtures(name),
        player_types=load_player_types(name),
        odds_source=closing_odds_source(teams, load_closing_odds(name)),
        template=template,
    )


@dataclass(frozen=True)
class GameweekCase:
    """Everything simulate_gameweek needs for one past gameweek, plus what actually happened."""

    event: int
    bootstrap: Bootstrap
    fixtures: list[Fixture]
    calendar: Calendar
    lives: dict[int, EventLive]
    odds: dict[int, MatchOdds]
    actual: list[HistoricalRow]
    unpriced: list[int] = field(default_factory=list)  # fixtures with no matching odds


def _events(season: Season, target: int) -> list[Event]:
    first_kickoff: dict[int, datetime] = {}
    for f in season.fixtures:
        if f.event is not None and f.kickoff_time is not None:
            k = first_kickoff.get(f.event)
            if k is None or f.kickoff_time < k:
                first_kickoff[f.event] = f.kickoff_time
    known = season.deadlines or {}
    return [
        Event(
            id=gw,
            name=f"Gameweek {gw}",
            deadline_time=known.get(gw, kickoff - DEADLINE_BEFORE_FIRST_KICKOFF),
            finished=gw < target,
            is_previous=gw == target - 1,
            is_current=gw == target - 1,
            is_next=gw == target,
        )
        for gw, kickoff in sorted(first_kickoff.items())
    ]


def _players(season: Season, target: int) -> list[Player]:
    """Players registered at `target` (they have a row then), with totals from before it."""
    team_ids = {t.name: t.id for t in season.teams}
    current: dict[int, list[HistoricalRow]] = defaultdict(list)
    for r in season.rows_by_gw.get(target, []):
        current[r.element].append(r)
    totals: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for gw in range(1, target):
        for r in season.rows_by_gw.get(gw, []):
            if r.element not in current:
                continue
            t = totals[r.element]
            for name in (
                "minutes",
                "starts",
                "expected_goals",
                "expected_assists",
                "expected_goals_conceded",
                "goals_scored",
                "assists",
                "own_goals",
                "defensive_contribution",
                "saves",
                "yellow_cards",
                "red_cards",
                "penalties_saved",
            ):
                t[name] += getattr(r, name)

    players = []
    for pid, rows in sorted(current.items()):
        first = rows[0]
        t = totals[pid]
        players.append(
            Player(
                id=pid,
                web_name=first.name,
                team=team_ids[first.team],
                element_type=season.player_types.get(pid, _TYPE_FROM_CODE[first.position]),
                now_cost=first.value,
                status="a",  # no historical flags (see module docstring)
                chance_of_playing_next_round=None,
                news="",
                news_added=None,
                minutes=int(t["minutes"]),
                starts=int(t["starts"]),
                expected_goals=t["expected_goals"],
                expected_assists=t["expected_assists"],
                expected_goals_conceded=t["expected_goals_conceded"],
                goals_scored=int(t["goals_scored"]),
                assists=int(t["assists"]),
                own_goals=int(t["own_goals"]),
                defensive_contribution=int(t["defensive_contribution"]),
                saves=int(t["saves"]),
                yellow_cards=int(t["yellow_cards"]),
                red_cards=int(t["red_cards"]),
                penalties_saved=int(t["penalties_saved"]),
                # FPL's own pre-gameweek estimate (a benchmark); unknown for the live season
                ep_next=None if any(r.xP is None for r in rows) else sum(r.xP or 0.0 for r in rows),
            )
        )
    return players


_TYPE_FROM_CODE = {"GK": 1, "DEF": 2, "MID": 3, "FWD": 4}


def _fixtures_as_of(season: Season, target: int) -> list[Fixture]:
    """Results only for gameweeks before `target`; later fixtures look unplayed."""
    out = []
    for f in season.fixtures:
        if f.event is not None and f.event >= target:
            f = f.model_copy(update={"finished": False, "team_h_score": None, "team_a_score": None})
        out.append(f)
    return out


def live_for(rows: list[HistoricalRow]) -> EventLive:
    """Rebuild /event/{gw}/live/ from one gameweek's rows (doubles: stats summed,
    minutes kept per match)."""
    by_player: dict[int, list[HistoricalRow]] = defaultdict(list)
    for r in rows:
        by_player[r.element].append(r)
    elements = []
    for pid, rs in sorted(by_player.items()):
        stats = {name: sum(getattr(r, name) for r in rs) for name in _STAT_FIELDS}
        elements.append(
            LiveElement(
                id=pid,
                stats=LiveStats(
                    minutes=sum(r.minutes for r in rs), starts=sum(r.starts for r in rs), **stats
                ),
                explain=[
                    ExplainFixture(
                        fixture=r.fixture,
                        stats=[ExplainStat(identifier="minutes", value=r.minutes)],
                    )
                    for r in sorted(rs, key=lambda r: r.fixture)
                ],
            )
        )
    return EventLive(elements=elements)


def closing_to_match_odds(fixture: Fixture, o: ClosingOdds) -> MatchOdds:
    """Closing 1X2 + over/under 2.5 -> probabilities and expected goals, like the Kalshi path."""
    inv = [1 / o.odds_home, 1 / o.odds_draw, 1 / o.odds_away]
    total = sum(inv)
    p_home, p_draw, p_away = (x / total for x in inv)
    over, under = 1 / o.odds_over_2_5, 1 / o.odds_under_2_5
    goal_total = total_from_line(2.5, over / (over + under))
    lam_h, lam_a = split_total(goal_total, p_home, p_away)
    _, model_draw, _ = outcome_probs(lam_h, lam_a)
    return MatchOdds(
        fixture_id=fixture.id,
        p_home=p_home,
        p_draw=p_draw,
        p_away=p_away,
        lambda_home=lam_h,
        lambda_away=lam_a,
        overround=total - 1.0,
        volume=0.0,
        max_spread=0.0,
        total_source="totals",
        totals_lines=1,
        draw_gap=model_draw - p_draw,
    )


def build_case(season: Season, target: int, window: int = WINDOW_GWS) -> GameweekCase:
    fixtures = _fixtures_as_of(season, target)
    bootstrap = Bootstrap(
        events=_events(season, target),
        teams=season.teams,
        elements=_players(season, target),
        element_types=season.template.element_types,
        chips=season.template.chips,
        game_config=season.template.game_config,
    )
    calendar = build_calendar(bootstrap, fixtures)
    lives = {
        gw: live_for(season.rows_by_gw[gw])
        for gw in window_events(target, window)
        if gw in season.rows_by_gw
    }
    gw = calendar.get(target)
    odds, unpriced = season.odds_source(target, list(gw.fixtures), bootstrap, gw.deadline)
    return GameweekCase(
        event=target,
        bootstrap=bootstrap,
        fixtures=fixtures,
        calendar=calendar,
        lives=lives,
        odds=odds,
        actual=season.rows_by_gw.get(target, []),
        unpriced=unpriced,
    )
