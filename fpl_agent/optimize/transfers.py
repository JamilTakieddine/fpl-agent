"""Phase 3 part 5: transfer recommendations (RECOMMENDATION ONLY until the Phase 5 planner, D28).

(docs/decisions.md D32) A transfer lasts for weeks, so options are valued over the next five
gameweeks, weighted 1.0 / 0.9 / 0.8 / 0.7 / 0.6 (later weeks are less certain). A squad's value
in a week is the expected points of its best lineup (XI, bench, captain) in that week.

The search, in stages:
1. screen: for every player I could sell, the best affordable replacements at his position by
   horizon expected points, each scored with a quick lineup value (best XI + captain by expected
   points; exact for that simplified problem, but blind to bench auto-subs);
2. combine: pairs and triples from the best singles (a sale can fund a different, dearer buy);
3. legality: budget (selling prices + bank), 3 per club, squad shape (rules.squad_violations);
4. score the shortlist exactly: the part 4 lineup search in every horizon week.
Then the agreed thresholds (D32): a free transfer must gain 1.5 points (0 for one that would be
lost to the 5-transfer cap anyway); a hit must gain 4 + 2. The option with the largest gain
above its threshold wins; "roll" (no transfer) is the baseline at zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations

from fpl_agent.data.models import PositionCode
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.lineup import best_expected
from fpl_agent.optimize.rules import (
    HIT_COST,
    Limits,
    SquadPlayer,
    squad_violations,
    transfer_cost,
)
from fpl_agent.optimize.scoring import POSITIONS, squad_sims

WEEK_WEIGHTS = (1.0, 0.9, 0.8, 0.7, 0.6)  # the horizon: five gameweeks, later ones count less
FT_THRESHOLD = 1.5  # horizon points a free transfer must gain (a saved one has value)
HIT_MARGIN = 2.0  # on top of the 4-point hit: longer-range estimates are noisier
MAX_TRANSFERS = 3  # bigger rebuilds are wildcard territory (Phase 5)
BUYS_PER_SALE = 10  # replacements screened for each player I could sell
COMBO_POOL = 20  # best singles combined into pairs and triples
SHORTLIST = 5  # options scored exactly with the lineup search


@dataclass(frozen=True)
class Move:
    sell: int
    buy: int


@dataclass(frozen=True)
class Option:
    moves: tuple[Move, ...]  # empty = roll the transfer
    squad: tuple[int, ...]
    bank: int  # after the moves, tenths of a million
    hit: int  # points deducted
    by_week: tuple[float, ...]  # expected-points gain vs keeping the squad, per horizon week
    gain: float  # weighted over the horizon, before the hit
    required: float  # the gain this many transfers must reach (D32 thresholds)

    @property
    def surplus(self) -> float:
        return self.gain - self.required

    @property
    def net(self) -> float:
        return self.gain - self.hit


@dataclass(frozen=True)
class TransferPlan:
    best: Option
    alternatives: list[Option]  # the next best actual transfers, exactly scored
    free: int
    considered: int  # legal options screened
    scored: list[Option] = field(default_factory=list)  # every option scored exactly


def required_gain(moves: int, free: int, limits: Limits) -> float:
    """Horizon points `moves` transfers must gain together. Free transfers that would be lost to
    the cap if rolled cost nothing to use; other free ones must earn FT_THRESHOLD each; hits
    must earn the hit plus HIT_MARGIN each."""
    lost = max(0, free + 1 - limits.max_free_transfers)
    need = 0.0
    for i in range(moves):
        if i < lost:
            continue
        need += FT_THRESHOLD if i < free else HIT_COST + HIT_MARGIN
    return need


def quick_value(
    xp: Mapping[int, float],
    squad: Sequence[int],
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> float:
    """Expected points of the best XI plus a captain, from per-player expected points. Taking
    each position's minimum from its best players, then the best of the rest up to each maximum,
    is exact for this problem. Blind to bench auto-subs: a screen, not the final score."""
    ranked = {
        pos: sorted((xp.get(p, 0.0) for p in squad if positions[p] == pos), reverse=True)
        for pos in POSITIONS
    }
    xi = [v for pos in POSITIONS for v in ranked[pos][: limits.min_play[pos]]]
    extra = sorted(
        (v for pos in POSITIONS for v in ranked[pos][limits.min_play[pos] : limits.max_play[pos]]),
        reverse=True,
    )
    xi += extra[: limits.starters - len(xi)]
    return sum(xi) + max(xi, default=0.0)


def recommend_transfers(
    squad: Sequence[SquadPlayer],
    bank: int,
    free: int,
    pool: Sequence[SquadPlayer],
    weeks: Sequence[PointsSamples],
    positions: Mapping[int, PositionCode],
    limits: Limits,
    max_transfers: int = MAX_TRANSFERS,
    allow_hits: bool = True,
    extra: Sequence[tuple[Move, ...]] = (),
) -> TransferPlan:
    """Best transfers for `squad` (prices = SELLING prices) over the horizon `weeks` (the next
    gameweek first). `pool`: every player I could buy, at buying price. `extra`: candidate move
    sets from elsewhere (the 5b planner's week-1 transfers), always scored exactly if legal."""
    weights = WEEK_WEIGHTS[: len(weeks)]
    xps = [{int(p): float(e) for p, e in zip(w.player, w.expected(), strict=True)} for w in weeks]
    horizon = {
        q.id: sum(w * xp.get(q.id, 0.0) for w, xp in zip(weights, xps, strict=True)) for q in pool
    }
    horizon |= {
        s.id: sum(w * xp.get(s.id, 0.0) for w, xp in zip(weights, xps, strict=True)) for s in squad
    }
    owned = {s.id for s in squad}
    ids = [s.id for s in squad]

    def quick(members: Sequence[int]) -> float:
        return sum(
            w * quick_value(xp, members, positions, limits)
            for w, xp in zip(weights, xps, strict=True)
        )

    base_quick = quick(ids)
    buyable = [q for q in pool if q.id not in owned]
    cheapest = {
        pos: min((q.price for q in buyable if q.position == pos), default=0) for pos in POSITIONS
    }
    # The most one other sale could free up: lets pairs fund a dearer buy than a single could.
    slack = max(s.price - cheapest[s.position] for s in squad)

    singles: list[tuple[float, Move]] = []
    for s in squad:
        options = sorted(
            (
                q
                for q in buyable
                if q.position == s.position
                and q.price <= bank + s.price + slack
                and horizon[q.id] > horizon[s.id]
            ),
            key=lambda q: -horizon[q.id],
        )[:BUYS_PER_SALE]
        for q in options:
            members = [q.id if p == s.id else p for p in ids]
            singles.append((quick(members) - base_quick, Move(s.id, q.id)))
    singles.sort(key=lambda t: -t[0])

    by_id = {p.id: p for p in pool} | {s.id: s for s in squad}  # owned: SELLING price
    kmax = min(max_transfers, free + 1 if allow_hits else free)  # hits off: free transfers only
    move_sets: list[tuple[Move, ...]] = [(m,) for _, m in singles]
    top = [m for _, m in singles[:COMBO_POOL]]
    for k in range(2, kmax + 1):
        for combo in combinations(top, k):
            sells, buys = {m.sell for m in combo}, {m.buy for m in combo}
            if len(sells) == k and len(buys) == k:
                move_sets.append(combo)

    screened: list[tuple[float, tuple[Move, ...], tuple[int, ...], int]] = []
    seen: set[frozenset[int]] = set()
    forced = {frozenset({m.buy for m in e} | (set(ids) - {m.sell for m in e})) for e in extra}
    for moves in [*extra, *move_sets]:
        if len(moves) > kmax:
            continue
        sold = {m.sell: m.buy for m in moves}
        new_squad = tuple(sold.get(p, p) for p in ids)
        # Same final squad = same option (e.g. two defenders bought for two sold, either way round).
        if frozenset(new_squad) in seen:
            continue
        seen.add(frozenset(new_squad))
        left = bank + sum(by_id[m.sell].price - by_id[m.buy].price for m in moves)
        if squad_violations([by_id[p] for p in new_squad], limits, left):
            continue
        need = required_gain(len(moves), free, limits)
        screened.append((quick(new_squad) - base_quick - need, moves, new_squad, left))
    screened.sort(key=lambda t: -t[0])

    # Exact: the lineup search in every horizon week (auto-subs, bench and captain included).
    def exact(members: tuple[int, ...]) -> tuple[float, ...]:
        return tuple(best_expected(squad_sims(w, members), positions, limits) for w in weeks)

    base = exact(tuple(ids))

    def option(moves: tuple[Move, ...], members: tuple[int, ...], left: int) -> Option:
        per_week = tuple(v - b for v, b in zip(exact(members), base, strict=True))
        return Option(
            moves,
            members,
            left,
            transfer_cost(len(moves), free),
            per_week,
            sum(w * g for w, g in zip(weights, per_week, strict=True)),
            required_gain(len(moves), free, limits),
        )

    shortlist = screened[:SHORTLIST] + [
        s for s in screened[SHORTLIST:] if frozenset(s[2]) in forced
    ]
    scored = [option(moves, members, left) for _, moves, members, left in shortlist]
    scored.sort(key=lambda o: -o.surplus)
    roll = Option((), tuple(ids), bank, 0, tuple(0.0 for _ in weeks), 0.0, 0.0)
    best = scored[0] if scored and scored[0].surplus > 0 else roll
    return TransferPlan(best, [o for o in scored if o is not best][:3], free, len(screened), scored)
