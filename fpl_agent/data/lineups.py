"""Predicted lineups, baseline: per-player chances of starting / a cameo / no minutes.

The agent runs ~15 min before the deadline, before confirmed lineups exist, so minutes decisions
rest on predictions. This baseline uses only FPL's own data (see docs/decisions.md D13):

- Availability: an explicit chance_of_playing_next_round wins (75 -> 0.75); with no flag, the
  status decides (available 1.0; injured/suspended/unavailable/not in squad 0.0).
- Role: start and substitute-appearance rates over the team's matches in the last WINDOW_GWS
  finished gameweeks, counted PER MATCH (a blank gameweek isn't "benched"; a double is two
  matches), from /event/{gw}/live/ (one request per gameweek for all players).
- Shrinkage: one pseudo-match of season-long behaviour (a Dirichlet prior worth PRIOR_MATCHES)
  so thin recent data can't produce a 0% or 100% role.
- Surprise non-starts: even an unflagged ever-present starter misses some starts (late knocks,
  illness, rotation). Every start probability is scaled by (1 - SURPRISE_NON_START); the removed
  share goes to "no minutes" (slightly conservative).

Known limitation: a player just back from injury looks like a non-starter (his recent matches
were missed). News-text flags (LLM, at the edge) or external lineups can correct that later.
Turning these probabilities into minute distributions is Phase 2's job.
"""

from __future__ import annotations

from dataclasses import dataclass

from fpl_agent.data.calendar import Calendar
from fpl_agent.data.client import FplClient
from fpl_agent.data.models import EventLive, LiveElement, Player

WINDOW_GWS = 6
PRIOR_MATCHES = 1.0
UNAVAILABLE_STATUSES = frozenset({"i", "s", "u", "n"})
DOUBTFUL_NO_PERCENT = 0.5  # status "d" without a percentage (rare)
# Measured 2026-09-26: of 130 currently-unflagged players who started all their team's GW1-4
# matches, 13 (10.0%) didn't start GW5. One gameweek, so roughly +/-3 points: re-measure as the
# season goes on (Phase 2 calibration).
SURPRISE_NON_START = 0.10


@dataclass(frozen=True)
class RoleHistory:
    """What a player did in his team's finished matches over the window."""

    team_matches: int
    starts: int
    sub_appearances: int  # came on, didn't start

    @property
    def unused(self) -> int:
        return self.team_matches - self.starts - self.sub_appearances


@dataclass(frozen=True)
class LineupPrediction:
    player_id: int
    p_available: float
    p_start: float
    p_cameo: float  # on as a substitute
    history: RoleHistory

    @property
    def p_no_minutes(self) -> float:
        return 1.0 - self.p_start - self.p_cameo


def availability(player: Player) -> float:
    """Probability the player is available for the next round, from FPL's flags."""
    if player.chance_of_playing_next_round is not None:
        return player.chance_of_playing_next_round / 100
    if player.status in UNAVAILABLE_STATUSES:
        return 0.0
    if player.status == "d":
        return DOUBTFUL_NO_PERCENT
    return 1.0


def team_matches_in(calendar: Calendar, team: int, events: list[int]) -> set[int]:
    """Fixture ids of the team's FINISHED matches in these gameweeks."""
    return {
        f.id
        for gw in events
        if gw in calendar.gameweeks
        for f in calendar.get(gw).fixtures
        if f.finished and team in (f.team_h, f.team_a)
    }


def role_history(team_matches: set[int], lives: list[LiveElement]) -> RoleHistory:
    """Starts and substitute appearances in the given team matches.

    `lives` are this player's entries from each gameweek's live data. Only matches of his
    CURRENT team count, so games for a previous club (a mid-season transfer) are ignored.
    """
    appearances = 0
    starts = 0
    for live in lives:
        played_here = [x for x in live.explain if x.fixture in team_matches]
        appearances += sum(1 for x in played_here if x.minutes() > 0)
        if played_here:
            starts += min(live.stats.starts, len(played_here))
    starts = min(starts, appearances)
    return RoleHistory(
        team_matches=len(team_matches),
        starts=starts,
        sub_appearances=appearances - starts,
    )


def season_start_rate(player: Player, team_finished_matches: int) -> float:
    if team_finished_matches == 0:
        return 0.0
    return min(player.starts / team_finished_matches, 1.0)


def predict(player: Player, history: RoleHistory, prior_start_rate: float) -> LineupPrediction:
    """Blend recent role with one pseudo-match of season-long behaviour, times availability."""
    n = history.team_matches + PRIOR_MATCHES
    start_rate = (history.starts + PRIOR_MATCHES * prior_start_rate) / n
    start_rate *= 1 - SURPRISE_NON_START
    cameo_rate = history.sub_appearances / n
    p_avail = availability(player)
    return LineupPrediction(
        player_id=player.id,
        p_available=p_avail,
        p_start=p_avail * start_rate,
        p_cameo=p_avail * cameo_rate,
        history=history,
    )


def window_events(event: int, n: int = WINDOW_GWS) -> list[int]:
    """The last n gameweeks before `event`."""
    return list(range(max(1, event - n), event))


def predict_all(
    players: list[Player],
    calendar: Calendar,
    lives_by_event: dict[int, EventLive],
    event: int,
) -> dict[int, LineupPrediction]:
    """Predictions for every player for `event`, from already-fetched live data."""
    window = [gw for gw in window_events(event) if gw in lives_by_event]
    by_player: dict[int, list[LiveElement]] = {}
    for gw in window:
        for el in lives_by_event[gw].elements:
            by_player.setdefault(el.id, []).append(el)

    all_past = [gw for gw in calendar.gameweeks if gw < event]
    predictions = {}
    for p in players:
        history = role_history(team_matches_in(calendar, p.team, window), by_player.get(p.id, []))
        prior = season_start_rate(p, len(team_matches_in(calendar, p.team, all_past)))
        predictions[p.id] = predict(p, history, prior)
    return predictions


def load_predictions(
    client: FplClient, calendar: Calendar, event: int
) -> dict[int, LineupPrediction]:
    """Fetch the window's live data (one request per gameweek) and predict every player."""
    lives = {gw: client.event_live(gw) for gw in window_events(event)}
    return predict_all(client.bootstrap().elements, calendar, lives, event)
