"""Phase 3 part 2: a team's points in every simulated gameweek, fast.

(docs/decisions.md D29) The optimizer scores thousands of candidate lineups on the same 10,000
simulations, so this is vectorised over simulations: Python loops only run over the handful of
bench and starter slots, and each step processes every simulation at once.

It mirrors the rules engine (rules.team_points) step for step, and a randomised test checks the
two agree in every simulation:
1. the goalkeeper swap (the bench keeper replaces a non-playing starting keeper if he played);
2. each bench outfielder in priority order, if he played, replaces the first non-playing
   outfield starter whose replacement leaves a legal formation (judged on the XI at that point);
3. the armband: captain if he played, else the vice-captain (x3 with Triple Captain);
4. Bench Boost counts all 15 as they are, with no auto-subs.
A player with no fixture (blank gameweek) scores 0 and counts as not having played.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import PositionCode
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.rules import GOALKEEPER, Limits, Lineup

POSITIONS: tuple[PositionCode, ...] = ("GKP", "DEF", "MID", "FWD")


@dataclass(frozen=True)
class SquadSims:
    """Simulated points and 'played' for a set of players, rows aligned with `ids`."""

    ids: tuple[int, ...]
    points: NDArray[np.int32]  # (players, n_sims)
    played: NDArray[np.bool_]  # (players, n_sims)

    def rows(self, players: Sequence[int]) -> NDArray[np.int64]:
        index = {pid: i for i, pid in enumerate(self.ids)}
        return np.array([index[p] for p in players], dtype=np.int64)


def squad_sims(samples: PointsSamples, players: Sequence[int]) -> SquadSims:
    """Pull these players' rows out of the gameweek simulation (zeros for players with no match)."""
    n_sims = samples.total.shape[1]
    points = np.zeros((len(players), n_sims), dtype=np.int32)
    played = np.zeros((len(players), n_sims), dtype=np.bool_)
    index = {int(p): i for i, p in enumerate(samples.player)}
    for r, pid in enumerate(players):
        i = index.get(pid)
        if i is not None:
            points[r] = samples.total[i]
            played[r] = samples.played[i]
    return SquadSims(tuple(players), points, played)


def score_lineup(
    sims: SquadSims,
    lineup: Lineup,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    chip: str | None = None,
) -> NDArray[np.int64]:
    """Team points in each simulation: shape (n_sims,).

    A slot is filled at most once: a substitute only replaces a starter who did not play, and
    every substitute who comes on did play, so a filled slot never reopens. The player leaving a
    slot is therefore always its original starter, and the total is the starters' points plus,
    where a substitution happened, the substitute's points minus the starter's. Everything is a
    1-D operation over the simulations; no (11, n_sims) "who is in each slot" matrix (D29).
    """
    squad = sims.rows(lineup.starters + lineup.bench)
    pts = sims.points[squad].astype(np.int64)  # (15, n): starters first, then the bench
    played = sims.played[squad]
    n_start = len(lineup.starters)
    armband = 3 if chip == "3xc" else 2
    cap = lineup.starters.index(lineup.captain)
    vice = lineup.starters.index(lineup.vice_captain)

    total: NDArray[np.int64] = pts.sum(axis=0) if chip == "bboost" else pts[:n_start].sum(axis=0)
    if chip != "bboost":
        slot_pos = [positions[p] for p in lineup.starters]
        slot_open = [~played[i] for i in range(n_start)]  # starter didn't play, nobody came on
        counts = {pos: np.full(total.shape, slot_pos.count(pos)) for pos in POSITIONS}

        # 1. The goalkeeper swap.
        gk_slot = slot_pos.index(GOALKEEPER)
        swap = slot_open[gk_slot] & played[n_start]
        total += np.where(swap, pts[n_start] - pts[gk_slot], 0)

        # 2. Outfield substitutes in priority order, each into the first open slot that keeps
        # the formation legal. The XI is legal before every swap, so only two counts can break:
        # the outgoing position losing one and the incoming one gaining one.
        for b in range(n_start + 1, len(squad)):
            b_pos = positions[lineup.bench[b - n_start]]
            pending = played[b].copy()  # this substitute is still looking for a slot
            for i, old_pos in enumerate(slot_pos):
                if old_pos == GOALKEEPER:
                    continue
                take = pending & slot_open[i]
                if old_pos != b_pos:
                    take &= (counts[old_pos] > limits.min_play[old_pos]) & (
                        counts[b_pos] < limits.max_play[b_pos]
                    )
                    if not take.any():
                        continue
                    counts[old_pos] -= take
                    counts[b_pos] += take
                elif not take.any():
                    continue
                total += np.where(take, pts[b] - pts[i], 0)
                slot_open[i] &= ~take
                pending &= ~take
                if not pending.any():
                    break

    # 3. The armband adds (multiplier - 1) x the wearer's points: the captain if he played, else
    # the vice-captain if he did, else nobody.
    wearer = np.where(played[cap], pts[cap], np.where(played[vice], pts[vice], 0))
    total += (armband - 1) * wearer
    return total
