"""Phase 3 part 4: pick the XI, bench order, captain and vice-captain for a fixed squad.

(docs/decisions.md D31) Objective (D28): maximize P(my points - opponent's >= BUFFER) over the
simulated gameweeks, among lineups within POINTS_GUARD expected points of the best possible.

About 550 legal XIs x 6 bench orders x 110 captain pairs is ~360,000 lineups: too many to score
one by one. Two exact shortcuts:
1. Prune by the guard. Each XI gets a cheap UPPER BOUND on its expected points (everything its
   bench could add, plus its best captain pair). Any XI whose bound is below (best achievable -
   guard) can never pass the guard, so it is skipped without losing anything.
2. The captain is an add-on: team points = (XI after auto-subs) + (captain bonus), and the
   captain never changes auto-subs. So each XI and bench order is scored once, and every captain
   pair is one extra vector on top; their expected values come from one small matrix product.
Every surviving lineup is compared against the same opponent draws (common random numbers).

Choosing among them, the simulations' own noise matters: the highest estimated win chance is
partly luck ("winner's curse"). One-standard-error rule: every lineup whose chance is within one
standard error of the best (of the paired difference) counts as tied, and among those the one
with the most expected points wins - steadier, and kinder to overall rank.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations, permutations

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import PositionCode
from fpl_agent.optimize.rules import GOALKEEPER, Limits, Lineup, is_valid_formation
from fpl_agent.optimize.scoring import POSITIONS, SquadSims, team_points_before_armband

BUFFER = 3  # D28: aim to win by 3+ points
POINTS_GUARD = 1.0  # D28: give up at most 1.0 expected point against the best possible lineup
TIE_SE = 1.0  # win chances within this many standard errors of the best count as tied
BUFFER_TABLE = range(1, 6)  # buffers reported (1 = just win), to show what one costs (Q5)
# Averages over the simulations can land exactly on the guard's edge (they're multiples of
# 1/n_sims); float rounding must not decide those, so the edge is always inside.
EDGE = 1e-9


@dataclass(frozen=True)
class Choice:
    """A lineup and how it does against the opponent."""

    lineup: Lineup
    expected: float
    p_win: float
    p_draw: float
    p_target: float  # P(margin >= buffer)
    buffer: int


def summarize(
    lineup: Lineup, points: NDArray[np.int64], opponent: NDArray[np.int64], buffer: int
) -> Choice:
    margin = points - opponent
    return Choice(
        lineup,
        float(points.mean()),
        float((margin > 0).mean()),
        float((margin == 0).mean()),
        float((margin >= buffer).mean()),
        buffer,
    )


# --- candidates ----------------------------------------------------------------------------------


def fpl_order(players: Sequence[int], positions: Mapping[int, PositionCode]) -> tuple[int, ...]:
    """Starters as FPL lists them: GK, defenders, midfielders, forwards (auto-subs scan in this
    order). Within a position the given order is kept."""
    return tuple(sorted(players, key=lambda p: POSITIONS.index(positions[p])))


def legal_xis(
    squad: Sequence[int], positions: Mapping[int, PositionCode], limits: Limits
) -> list[tuple[tuple[int, ...], int, tuple[int, ...]]]:
    """Every legal XI of a 15-man squad: (starters in FPL order, bench goalkeeper, the three bench
    outfielders in squad order)."""
    keepers = [p for p in squad if positions[p] == GOALKEEPER]
    outfield = [p for p in squad if positions[p] != GOALKEEPER]
    out = []
    for gk in keepers:
        bench_gk = next(k for k in keepers if k != gk)
        for chosen in combinations(outfield, limits.starters - 1):
            shape = [GOALKEEPER, *(positions[p] for p in chosen)]
            if is_valid_formation(shape, limits):
                rest = tuple(p for p in outfield if p not in chosen)
                out.append((fpl_order((gk, *chosen), positions), bench_gk, rest))
    return out


class Candidates:
    """Every (XI, bench order, captain, vice-captain) within `guard` of the best expected points.

    Iterating yields (lineup, points in each simulation), built on the fly: each XI + bench
    order's team points are kept compactly (16-bit) and the captain bonus is added per pair. A
    real squad has thousands of candidates (2,505 in GW6), mostly vice-captain and bench-order
    variants that differ in few simulations; storing all of them at 10,000 simulations took
    ~200 MB (D31). This keeps memory to the team points plus a few vectors.
    """

    def __init__(
        self,
        sims: SquadSims,
        positions: Mapping[int, PositionCode],
        limits: Limits,
        guard: float = POINTS_GUARD,
    ) -> None:
        self.sims = sims
        ids = sims.ids
        self.row = row = {p: i for i, p in enumerate(ids)}
        n = sims.points.shape[1]
        pts = sims.points.astype(np.float64)
        played = sims.played
        wearing = np.where(played, pts, 0.0)  # points counted when he wears the armband
        # E[captain bonus] for captain c, vice v = E[c's points if he played]
        #                                        + E[v's points if he played and c didn't]
        armband_mean = wearing.mean(axis=1)[:, None] + (~played).astype(np.float64) @ wearing.T / n
        np.fill_diagonal(armband_mean, -np.inf)  # captain and vice must differ
        mean_pts = pts.mean(axis=1)
        played_share = played.mean(axis=1)
        gain = np.maximum(wearing, 0.0)  # the most a substitute can add in a simulation
        loss = np.where(played, 0.0, np.maximum(-pts, 0.0))  # a non-player's negative points

        def best_pair(starters: Sequence[int]) -> float:
            r = [row[p] for p in starters]
            return float(armband_mean[np.ix_(r, r)].max())

        def bound(starters: tuple[int, ...], bench_gk: int, rest: tuple[int, ...]) -> float:
            """Upper bound on expected points: starters, plus every bench player's points wherever
            a starter of his kind (keeper / outfield) missed out, plus the best captain pair."""
            r = [row[p] for p in starters]
            gk_missing = ~played[r[0]]
            out_missing = ~played[r[1:]].all(axis=0)
            subs = gain[row[bench_gk]] @ gk_missing + sum(gain[row[b]] @ out_missing for b in rest)
            return float(mean_pts[r].sum() + (subs + loss[r].sum()) / n + best_pair(starters))

        def own_points(starters: tuple[int, ...]) -> float:
            return float(mean_pts[[row[p] for p in starters]].sum())

        xis = legal_xis(ids, positions, limits)
        # Candidates keep this order, and exact ties go to the earliest (select, recommend). So
        # among equal bounds, XIs whose starters themselves score more come first: starting a
        # keeper who won't play ties with starting his replacement (the auto-sub brings him on),
        # but reads badly.
        bounded = sorted(((bound(*x), x) for x in xis), key=lambda t: (-t[0], -own_points(t[1][0])))

        # Score XIs from the highest bound down; stop once no bound can reach (best - guard). The
        # best only rises, so everything skipped could never pass the guard.
        best = -np.inf
        scored: list[tuple[Lineup, NDArray[np.int16], float]] = []
        for ub, (starters, bench_gk, rest) in bounded:
            if ub < best - guard - EDGE:
                break
            pair = best_pair(starters)
            # Bench orders by expected points first, so exact ties keep the natural order.
            for order in permutations(sorted(rest, key=lambda p: -mean_pts[row[p]])):
                sheet = Lineup(starters, (bench_gk, *order), starters[0], starters[1])
                base = team_points_before_armband(sims, sheet, positions, limits)
                if base.min() < np.iinfo(np.int16).min or base.max() > np.iinfo(np.int16).max:
                    raise OverflowError("team points outside the 16-bit range")
                scored.append((sheet, base.astype(np.int16), float(base.mean())))
                best = max(best, float(base.mean()) + pair)

        # Keep each sheet that can still reach the guard, with its qualifying captain pairs.
        self.sheets: list[tuple[Lineup, NDArray[np.int16], list[tuple[int, int]]]] = []
        for sheet, compact, base_mean in scored:
            r = [row[p] for p in sheet.starters]
            pair_means = armband_mean[np.ix_(r, r)]
            # Pairs by expected bonus first. Captaining a player who won't play ties with
            # captaining his vice (the armband passes on), so ties go to the captain who plays.
            order_ij = sorted(
                ((i, j) for i in range(len(r)) for j in range(len(r))),
                key=lambda ij: (-pair_means[ij], -played_share[r[ij[0]]]),
            )
            pairs = [
                (sheet.starters[i], sheet.starters[j])
                for i, j in order_ij
                if base_mean + pair_means[i, j] >= best - guard - EDGE
            ]
            if pairs:
                self.sheets.append((sheet, compact, pairs))
        self.count = sum(len(pairs) for _, _, pairs in self.sheets)
        self.best_expected = float(best)
        self.xis_total = len(xis)
        self.xis_scored = len({s.starters for s, _, _ in scored})
        self._bonus: dict[tuple[int, int], NDArray[np.int64]] = {}

    def bonus(self, captain: int, vice: int) -> NDArray[np.int64]:
        """The captain bonus in each simulation (cached: at most 15 x 14 pairs)."""
        if (captain, vice) not in self._bonus:
            c, v = self.row[captain], self.row[vice]
            pts, played = self.sims.points, self.sims.played
            wearer = np.where(played[c], pts[c], np.where(played[v], pts[v], 0))
            self._bonus[(captain, vice)] = wearer.astype(np.int64)
        return self._bonus[(captain, vice)]

    def __len__(self) -> int:
        return self.count

    def __iter__(self) -> Iterator[tuple[Lineup, NDArray[np.int64]]]:
        for sheet, base, pairs in self.sheets:
            team = base.astype(np.int64)
            for c, v in pairs:
                yield Lineup(sheet.starters, sheet.bench, c, v), team + self.bonus(c, v)


def candidates(
    sims: SquadSims,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    guard: float = POINTS_GUARD,
) -> Candidates:
    return Candidates(sims, positions, limits, guard)


# --- choosing ------------------------------------------------------------------------------------


def tied_with(hits: NDArray[np.bool_], top: NDArray[np.bool_], tie_se: float = TIE_SE) -> bool:
    """Whether a candidate's chance of reaching the target is within tie_se standard errors of the
    best one's. The standard error is of the PAIRED difference: both are scored on the same
    simulations, so most of the noise cancels and only the simulations where they differ count."""
    diff = hits.astype(np.int8) - top.astype(np.int8)
    return bool(-diff.mean() <= tie_se * diff.std() / np.sqrt(len(diff)))


def select(expected: NDArray[np.float64], hits: NDArray[np.bool_], tie_se: float = TIE_SE) -> int:
    """Index of the chosen candidate. `hits` (candidates, n_sims): reached the target margin.
    Among the candidates tied with the best chance, the most expected points wins (then the
    higher chance, then the earliest)."""
    p = hits.mean(axis=1)
    top = hits[int(p.argmax())]
    tied = [k for k in range(len(hits)) if tied_with(hits[k], top, tie_se)]
    return max(tied, key=lambda k: (expected[k], p[k]))


@dataclass(frozen=True)
class Recommendation:
    best: Choice
    max_expected: Choice  # the highest-expected-points lineup, for comparison
    by_buffer: dict[int, Choice]  # the lineup each buffer would choose (what the buffer costs)
    candidates: int
    xis_total: int
    xis_scored: int


def recommend(
    sims: SquadSims,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    opponent: NDArray[np.int64],
    buffer: int = BUFFER,
    guard: float = POINTS_GUARD,
) -> Recommendation:
    """The lineup to play against `opponent` (their points in the same simulations).

    Same rule as `select`, streamed in two passes so no candidate matrix is ever held: pass 1
    finds each buffer's best chance (and keeps that lineup's hits), pass 2 picks, among the
    lineups tied with it, the one with the most expected points."""
    cands = candidates(sims, positions, limits, guard)
    buffers = sorted({*BUFFER_TABLE, buffer})

    top_p = dict.fromkeys(buffers, -1.0)
    top_hits: dict[int, NDArray[np.bool_]] = {}
    most: tuple[tuple[float, float], Choice] | None = None
    for lineup, pts in cands:
        margin = pts - opponent
        for b in buffers:
            hits = margin >= b
            if hits.mean() > top_p[b]:
                top_p[b], top_hits[b] = float(hits.mean()), hits
        key = (float(pts.mean()), float((margin >= buffer).mean()))
        if most is None or key > most[0]:
            most = (key, summarize(lineup, pts, opponent, buffer))

    chosen: dict[int, tuple[tuple[float, float], Choice]] = {}
    for lineup, pts in cands:
        margin = pts - opponent
        for b in buffers:
            hits = margin >= b
            if not tied_with(hits, top_hits[b]):
                continue
            key = (float(pts.mean()), float(hits.mean()))
            if b not in chosen or key > chosen[b][0]:
                chosen[b] = (key, summarize(lineup, pts, opponent, b))

    assert most is not None  # the best lineup itself is always a candidate
    return Recommendation(
        best=chosen[buffer][1],
        max_expected=most[1],
        by_buffer={b: chosen[b][1] for b in BUFFER_TABLE},
        candidates=len(cands),
        xis_total=cands.xis_total,
        xis_scored=cands.xis_scored,
    )
