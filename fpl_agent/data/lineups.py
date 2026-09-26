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

- Excused matches (D14): if a flag snapshot shows the player was flagged OUT before a gameweek's
  deadline and he didn't play, that gameweek's matches are dropped from his history instead of
  counting as "unused". Snapshots exist only from when recording started, so older matches still
  count against a player who was injured then.
- Top-up (D19): per team and position, expected starters are topped back up to what that team
  actually fielded over the window. Without it, surprise non-starts and absences removed starts
  that nobody inherited (~9.2 expected starters per team instead of 11), underrating replacements
  like a backup goalkeeper.
Turning these probabilities into minute distributions is Phase 2's job.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass

from fpl_agent.data.calendar import Calendar
from fpl_agent.data.client import FplClient
from fpl_agent.data.models import EventLive, LiveElement, Player
from fpl_agent.data.snapshots import FlagSnapshot, PlayerFlag, SnapshotStore

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

    team_matches: int  # matches that count (excused ones already removed)
    starts: int
    sub_appearances: int  # came on, didn't start
    excused: int = 0  # matches skipped because he was flagged out before the deadline

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
    topped_up: float = 0.0  # start probability added by top_up_starters (replacement share)

    @property
    def p_no_minutes(self) -> float:
        return 1.0 - self.p_start - self.p_cameo


def availability_of(status: str, chance_of_playing_next_round: int | None) -> float:
    """Probability of being available, from FPL's flag fields (today's or a snapshot's)."""
    if chance_of_playing_next_round is not None:
        return chance_of_playing_next_round / 100
    if status in UNAVAILABLE_STATUSES:
        return 0.0
    if status == "d":
        return DOUBTFUL_NO_PERCENT
    return 1.0


def availability(player: Player) -> float:
    return availability_of(player.status, player.chance_of_playing_next_round)


def flagged_out(flag: PlayerFlag) -> bool:
    return availability_of(flag.status, flag.chance_of_playing_next_round) == 0.0


def team_matches_in(calendar: Calendar, team: int, events: list[int]) -> set[int]:
    """Fixture ids of the team's FINISHED matches in these gameweeks."""
    return {
        f.id
        for gw in events
        if gw in calendar.gameweeks
        for f in calendar.get(gw).fixtures
        if f.finished and team in (f.team_h, f.team_a)
    }


def role_history(team_matches: set[int], lives: list[LiveElement], excused: int = 0) -> RoleHistory:
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
        excused=excused,
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


def excused_matches(
    calendar: Calendar,
    team: int,
    player_id: int,
    lives: list[LiveElement],
    snapshots: dict[int, FlagSnapshot],
) -> set[int]:
    """Team matches to skip: the player was flagged out before that gameweek's deadline AND
    didn't play (if he played anyway, the flag was wrong and the match counts)."""
    played = {x.fixture for live in lives for x in live.explain if x.minutes() > 0}
    excused: set[int] = set()
    for gw, snap in snapshots.items():
        flag = snap.flags.get(player_id)
        if flag is not None and flagged_out(flag):
            excused |= team_matches_in(calendar, team, [gw]) - played
    return excused


def predict_all(
    players: list[Player],
    calendar: Calendar,
    lives_by_event: dict[int, EventLive],
    event: int,
    snapshots: dict[int, FlagSnapshot] | None = None,
) -> dict[int, LineupPrediction]:
    """Predictions for every player for `event`, from already-fetched live data and any flag
    snapshots for the window's gameweeks."""
    window = [gw for gw in window_events(event) if gw in lives_by_event]
    window_snaps = {gw: s for gw, s in (snapshots or {}).items() if gw in window}
    by_player: dict[int, list[LiveElement]] = {}
    for gw in window:
        for el in lives_by_event[gw].elements:
            by_player.setdefault(el.id, []).append(el)

    all_past = [gw for gw in calendar.gameweeks if gw < event]
    predictions = {}
    for p in players:
        lives = by_player.get(p.id, [])
        excused = excused_matches(calendar, p.team, p.id, lives, window_snaps)
        matches = team_matches_in(calendar, p.team, window) - excused
        history = role_history(matches, lives, excused=len(excused))
        prior = season_start_rate(p, len(team_matches_in(calendar, p.team, all_past)))
        predictions[p.id] = predict(p, history, prior)

    team_matches = {t: len(team_matches_in(calendar, t, window)) for t in {p.team for p in players}}
    return top_up_starters(predictions, players, team_matches)


def starter_targets(
    predictions: dict[int, LineupPrediction],
    players: list[Player],
    team_matches: dict[int, int],
) -> dict[tuple[int, int], float]:
    """(team, element_type) -> starters per match that team actually fielded in the window."""
    starts: dict[tuple[int, int], int] = {}
    for p in players:
        key = (p.team, p.element_type)
        starts[key] = starts.get(key, 0) + predictions[p.id].history.starts
    return {k: v / team_matches[k[0]] for k, v in starts.items() if team_matches.get(k[0])}


def _water_fill(amount: float, weights: list[float], caps: list[float]) -> list[float]:
    """Share `amount` in proportion to `weights`, never giving anyone more than their cap; what a
    capped player can't take is re-shared among the others."""
    given = [0.0] * len(weights)
    active = [i for i, (w, c) in enumerate(zip(weights, caps, strict=True)) if w > 0 and c > 0]
    while amount > 1e-12 and active:
        total_w = sum(weights[i] for i in active)
        still = []
        spent = 0.0
        for i in active:
            share = amount * weights[i] / total_w
            room = caps[i] - given[i]
            take = min(share, room)
            given[i] += take
            spent += take
            if room - take > 1e-12:
                still.append(i)
        amount -= spent
        if len(still) == len(active):
            break  # nobody hit a cap: everything was handed out
        active = still
    return given


def top_up_starters(
    predictions: dict[int, LineupPrediction],
    players: list[Player],
    team_matches: dict[int, int],
) -> dict[int, LineupPrediction]:
    """Restore each team's expected starters per position to what it actually fielded.

    The shortfall goes to that team's players at that position in proportion to headroom
    (p_available - p_start) times involvement ((starts + sub apps + 1) / (matches + 1)), capped at
    p_available. Cameo chances shrink so start + cameo never exceeds availability. Surpluses are
    left alone: this only restores starts that nobody inherited, it never removes any.
    """
    targets = starter_targets(predictions, players, team_matches)
    groups: dict[tuple[int, int], list[Player]] = {}
    for p in players:
        groups.setdefault((p.team, p.element_type), []).append(p)

    result = dict(predictions)
    for key, members in groups.items():
        target = targets.get(key)
        if target is None:
            continue
        shortfall = target - sum(predictions[p.id].p_start for p in members)
        if shortfall <= 1e-9:
            continue
        members = sorted(members, key=lambda p: p.id)
        preds = [predictions[p.id] for p in members]
        caps = [max(pr.p_available - pr.p_start, 0.0) for pr in preds]
        weights = [
            cap
            * (pr.history.starts + pr.history.sub_appearances + 1)
            / (pr.history.team_matches + 1)
            for cap, pr in zip(caps, preds, strict=True)
        ]
        for pr, extra in zip(preds, _water_fill(shortfall, weights, caps), strict=True):
            if extra <= 0:
                continue
            p_start = pr.p_start + extra
            result[pr.player_id] = dataclasses.replace(
                pr,
                p_start=p_start,
                p_cameo=min(pr.p_cameo, pr.p_available - p_start),
                topped_up=extra,
            )
    return result


def load_predictions(
    client: FplClient,
    calendar: Calendar,
    event: int,
    snapshots: SnapshotStore | None = None,
) -> dict[int, LineupPrediction]:
    """Fetch the window's live data (one request per gameweek) and predict every player.

    Reads snapshots only; recording this run's snapshot is the caller's job (record_snapshot).
    """
    window = window_events(event)
    lives = {gw: client.event_live(gw) for gw in window}
    snaps: dict[int, FlagSnapshot] = {}
    if snapshots is not None:
        for gw in window:
            snap = snapshots.load(gw)
            if snap is not None:
                snaps[gw] = snap
    return predict_all(client.bootstrap().elements, calendar, lives, event, snaps)
