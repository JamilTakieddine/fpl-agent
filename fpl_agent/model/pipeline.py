"""Fetch what the simulation needs and simulate the next gameweek, for the command-line tools.

Read-only: public data, saved flag snapshots and saved odds. Recording snapshots and odds is
`python -m fpl_agent.data`'s job. Shared by `python -m fpl_agent.model` (player view) and
`python -m fpl_agent.optimize` (lineup recommendation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from fpl_agent.config import Settings
from fpl_agent.data.calendar import Calendar, build_calendar, next_deadline
from fpl_agent.data.client import FplClient
from fpl_agent.data.kalshi import KalshiClient
from fpl_agent.data.lineups import history_events, predict_all, window_events
from fpl_agent.data.models import Bootstrap
from fpl_agent.data.odds import load_odds
from fpl_agent.data.odds_store import FileOddsStore, usable_saved_odds
from fpl_agent.data.snapshots import FileSnapshotStore
from fpl_agent.model.gameweek import GameweekSimulation, simulate_gameweek

N_SIMS = 10_000


@dataclass(frozen=True)
class NextGameweek:
    event: int
    deadline: datetime
    bootstrap: Bootstrap
    calendar: Calendar
    sim: GameweekSimulation


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
    return NextGameweek(event, deadline, boot, cal, sim)
