"""Fetch what the simulation needs and simulate the next gameweek, for the command-line tools.

Read-only: public data, saved flag snapshots and saved odds. Recording snapshots and odds is
`python -m fpl_agent.data`'s job. Shared by `python -m fpl_agent.model` (player view) and
`python -m fpl_agent.optimize` (lineup recommendation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from fpl_agent.config import Settings
from fpl_agent.data.calendar import Calendar, build_calendar, next_deadline
from fpl_agent.data.client import FplClient
from fpl_agent.data.injuries import as_of
from fpl_agent.data.kalshi import KalshiClient
from fpl_agent.data.lineups import history_events, predict_all, window_events
from fpl_agent.data.models import Bootstrap, EventLive, Fixture
from fpl_agent.data.odds import load_odds
from fpl_agent.data.odds_store import FileOddsStore, usable_saved_odds
from fpl_agent.data.snapshots import FileSnapshotStore, FlagSnapshot
from fpl_agent.model.gameweek import GameweekSimulation, simulate_gameweek
from fpl_agent.model.points import PointsSamples

N_SIMS = 10_000


@dataclass(frozen=True)
class NextGameweek:
    event: int
    deadline: datetime
    bootstrap: Bootstrap
    calendar: Calendar
    sim: GameweekSimulation
    # What the simulation was built from, reused to look further ahead (simulate_ahead).
    fixtures: list[Fixture]
    lives: dict[int, EventLive]
    snapshots: dict[int, FlagSnapshot]


def simulate_next(
    client: FplClient, settings: Settings, now: datetime, n_sims: int = N_SIMS
) -> NextGameweek | None:
    """Simulate the next gameweek whose deadline is after `now` (None if the season is over)."""
    boot = client.bootstrap()
    fixtures = client.fixtures()
    cal = build_calendar(boot, fixtures)
    upcoming = next_deadline(boot, now)
    if upcoming is None:
        return None
    event, deadline = upcoming

    # Live data for the fits (minutes, bonus); the lineup model uses its own shorter window.
    lives = {gw: client.event_live(gw) for gw in history_events(event)}
    snap_store = FileSnapshotStore(settings.snapshot_dir)
    snaps = {gw: s for gw in window_events(event) if (s := snap_store.load(gw)) is not None}
    predictions = predict_all(boot.elements, cal, lives, event, snaps)

    gw_fixtures = list(cal.get(event).fixtures)
    odds, _ = load_odds(KalshiClient(client.http), gw_fixtures, boot)
    saved = usable_saved_odds(FileOddsStore(settings.odds_dir).load(event), gw_fixtures)
    sim = simulate_gameweek(
        boot, cal, fixtures, event, predictions, lives, odds, saved, n_sims=n_sims, seed=event
    )
    return NextGameweek(event, deadline, boot, cal, sim, fixtures, lives, snaps)


def simulate_ahead(
    client: FplClient, nxt: NextGameweek, weeks: int, today: date, n_sims: int = N_SIMS
) -> dict[int, PointsSamples]:
    """Points samples for the `weeks - 1` gameweeks after the next one (fewer at season end).

    Built like the next gameweek, with two differences (D32):
    - lineup predictions use the real recent window (the one before the NEXT gameweek): the
      window before a future gameweek hasn't been played yet;
    - flags are read as they'll stand that week (data/injuries.py): a knock is over after the
      next gameweek, a dated injury ends on its date.
    Kalshi usually only prices the next week or so; other matches use the xG-ratings fallback.
    Simulated one at a time, keeping only the points (the rest is ~2 GB per simulation).
    """
    boot, cal = nxt.bootstrap, nxt.calendar
    events = [gw for gw in range(nxt.event + 1, nxt.event + weeks) if gw in cal.gameweeks]
    fixtures = [f for gw in events for f in cal.get(gw).fixtures]
    odds, _ = load_odds(KalshiClient(client.http), fixtures, boot)
    out = {}
    for gw in events:
        later = {p.id: as_of(p, cal.get(gw).deadline, nxt.deadline, today) for p in boot.elements}
        predictions = predict_all(
            boot.elements, cal, nxt.lives, nxt.event, nxt.snapshots, available_as=later
        )
        sim = simulate_gameweek(
            boot, cal, nxt.fixtures, gw, predictions, nxt.lives, odds, None, n_sims, seed=gw
        )
        out[gw] = sim.points
    return out
