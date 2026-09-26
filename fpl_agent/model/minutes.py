"""Phase 2 step 2: simulated minutes for every player in every match of the gameweek.

Per simulated gameweek (docs/decisions.md D18):
1. Available or not: drawn ONCE per player per gameweek (an injured player misses both matches
   of a double gameweek), with probability LineupPrediction.p_available.
2. Role per match, given available: start / cameo / unused, from p_start and p_cameo divided by
   p_available (the Phase 1 probabilities already include availability).
3. Minutes: a starter's minutes are resampled from real starter appearances for his POSITION
   (goalkeepers ~always 90; midfielders subbed about half the time, around minute 72). A
   cameo's minutes are resampled from real substitute appearances; he comes on at 90 - minutes.

Distributions come from the same /event/{gw}/live/ data the lineup model already fetched
(double-gameweek appearances excluded: FPL doesn't say which match a start belongs to). Thin
samples fall back to pooled outfield data, then to a coarse default approximating GW1-5 2026/27.

Each player is drawn independently, so a simulated team won't always field exactly 11: it
barely changes a player's own points, and forcing 11 needs a per-team selection model (D18).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.lineups import LineupPrediction
from fpl_agent.data.models import EventLive, PositionCode

FULL_MATCH = 90
MIN_SAMPLE = 30  # fewer observations than this: fall back to a broader distribution
OUTFIELD: tuple[PositionCode, ...] = ("DEF", "MID", "FWD")

# Coarse defaults for the very start of a season, approximating the GW1-5 2026/27 buckets (D18):
# starters 62% full / 31% subbed 60-89 / 7% off before 60 (measured outfield ~57/36/6.5);
# cameos 50% up to 15 min / 40% 16-30 / 10% 31-59 (measured 49/40/10).
_DEFAULT_STARTER = np.array([90] * 60 + list(range(60, 90)) + list(range(20, 60, 6)))
_DEFAULT_CAMEO = np.array(list(range(1, 16)) * 5 + list(range(16, 31)) * 4 + list(range(31, 60, 2)))


@dataclass(frozen=True)
class MinutesDistributions:
    """Empirical minutes to resample from: starters per position, and substitutes (pooled)."""

    starter: dict[PositionCode, NDArray[np.int64]]
    cameo: NDArray[np.int64]


def minutes_distributions(
    lives: Mapping[int, EventLive], positions: Mapping[int, PositionCode]
) -> MinutesDistributions:
    """Real starter minutes per position and real cameo minutes, from single-match gameweeks."""
    starter: dict[PositionCode, list[int]] = {"GKP": [], "DEF": [], "MID": [], "FWD": []}
    cameo: list[int] = []
    for live in lives.values():
        for el in live.elements:
            pos = positions.get(el.id)
            if pos is None or len(el.explain) != 1:
                continue
            m = el.explain[0].minutes()
            if el.stats.starts == 1:
                starter[pos].append(m)
            elif m > 0:
                cameo.append(m)

    pooled = [m for p in OUTFIELD for m in starter[p]]
    starter_arrays: dict[PositionCode, NDArray[np.int64]] = {}
    for pos, obs in starter.items():
        if len(obs) >= MIN_SAMPLE:
            starter_arrays[pos] = np.array(obs, dtype=np.int64)
        elif pos == "GKP":
            starter_arrays[pos] = np.array([FULL_MATCH], dtype=np.int64)  # keepers play 90
        elif len(pooled) >= MIN_SAMPLE:
            starter_arrays[pos] = np.array(pooled, dtype=np.int64)
        else:
            starter_arrays[pos] = _DEFAULT_STARTER.astype(np.int64)
    cameo_array = (
        np.array(cameo, dtype=np.int64)
        if len(cameo) >= MIN_SAMPLE
        else _DEFAULT_CAMEO.astype(np.int64)
    )
    return MinutesDistributions(starter=starter_arrays, cameo=cameo_array)


@dataclass(frozen=True)
class MinutesSamples:
    """Row r is one player in one match. Arrays are (rows, n_sims).

    A player is on the pitch from minute on_from[r, i] for minutes[r, i] minutes. Unused players
    have minutes 0 (on_from = 90, so the interval is empty).
    """

    player: NDArray[np.int64]  # (rows,) player id
    fixture: NDArray[np.int64]  # (rows,) fixture id
    minutes: NDArray[np.int64]
    on_from: NDArray[np.int64]
    started: NDArray[np.bool_]

    def rows_for(self, player_id: int) -> NDArray[np.int64]:
        return np.flatnonzero(self.player == player_id)


def simulate_minutes(
    predictions: Mapping[int, LineupPrediction],
    positions: Mapping[int, PositionCode],
    teams: Mapping[int, int],
    team_fixtures: Mapping[int, list[int]],
    dists: MinutesDistributions,
    n_sims: int,
    rng: np.random.Generator,
) -> MinutesSamples:
    """Minutes for every predicted player in each of his team's fixtures this gameweek.

    `team_fixtures` maps team id -> fixture ids this gameweek (0 in a blank, 2 in a double).
    Draws happen in player-id order, then fixture order, so a seed gives identical results.
    """
    rows_player: list[int] = []
    rows_fixture: list[int] = []
    minutes: list[NDArray[np.int64]] = []
    on_from: list[NDArray[np.int64]] = []
    started: list[NDArray[np.bool_]] = []

    for pid in sorted(predictions):
        fixtures = sorted(team_fixtures.get(teams[pid], []))
        if not fixtures:
            continue  # blank gameweek for his team
        pred = predictions[pid]
        available = rng.random(n_sims) < pred.p_available  # once per gameweek
        p_avail = pred.p_available
        p_start = pred.p_start / p_avail if p_avail > 0 else 0.0
        p_cameo = pred.p_cameo / p_avail if p_avail > 0 else 0.0

        for fid in fixtures:
            u = rng.random(n_sims)
            is_start = available & (u < p_start)
            is_cameo = available & ~is_start & (u < p_start + p_cameo)
            start_mins = rng.choice(dists.starter[positions[pid]], n_sims)
            cameo_mins = rng.choice(dists.cameo, n_sims)

            mins = np.where(is_start, start_mins, np.where(is_cameo, cameo_mins, 0))
            entered = np.where(is_start, 0, np.where(is_cameo, FULL_MATCH - cameo_mins, FULL_MATCH))
            rows_player.append(pid)
            rows_fixture.append(fid)
            minutes.append(mins.astype(np.int64))
            on_from.append(entered.astype(np.int64))
            started.append(is_start)

    if not minutes:
        empty = np.zeros((0, n_sims), dtype=np.int64)
        return MinutesSamples(
            np.array([], dtype=np.int64),
            np.array([], dtype=np.int64),
            empty,
            empty.copy(),
            np.zeros((0, n_sims), dtype=np.bool_),
        )
    return MinutesSamples(
        player=np.array(rows_player, dtype=np.int64),
        fixture=np.array(rows_fixture, dtype=np.int64),
        minutes=np.vstack(minutes),
        on_from=np.vstack(on_from),
        started=np.vstack(started),
    )
