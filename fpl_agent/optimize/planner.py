"""Phase 5b: a multi-week transfer plan, solved exactly as a mixed-integer program (HiGHS).

(docs/decisions.md D38) Part 5 decides "this week's transfers, valued over five weeks". The
planner decides the SEQUENCE: which transfers in which week (rolling one now to make two next
week, selling a player before his hard run), over the same horizon and week weights (D32).

For every candidate player p and week t, yes/no variables say: in the squad, in the XI, captain,
bought, sold. Constraints are FPL's rules: a 2/5/5/3 squad of 15, at most 3 per club, a legal XI
with one captain, the bank (selling prices for players you own, buying prices otherwise; prices
assumed constant over the horizon), free transfers banking one a week up to the cap, no hits
unless allowed, at most MAX_TRANSFERS a week. The objective is the week-weighted expected points
of the XI plus the captain's bonus, a little for the bench (auto-subs need depth), minus 4 per
hit, plus FT_VALUE per free transfer still banked after the horizon: the D32 threshold expressed
inside the plan, so a transfer that would be lost to the cap anyway costs nothing.

Expected points are a linear proxy: the plan's week-1 transfers are only a CANDIDATE, scored
exactly with the simulations next to part 5's options before anything is made (agent.py).
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fpl_agent.data.models import PositionCode
from fpl_agent.optimize.rules import HIT_COST, Limits, SquadPlayer
from fpl_agent.optimize.scoring import POSITIONS
from fpl_agent.optimize.transfers import FT_THRESHOLD, MAX_TRANSFERS, WEEK_WEIGHTS, Move

FT_VALUE = FT_THRESHOLD  # points per free transfer banked at the end of the horizon (D32)
BENCH_WEIGHT = 0.1  # bench players' expected points count this much: depth for auto-subs
POOL_PER_POSITION = 40  # best candidates per position by horizon value (plus your squad)
CHEAP_PER_POSITION = 5  # plus the cheapest per position: budget enablers
TIME_LIMIT_S = 60.0
MIP_GAP = 0.001


@dataclass(frozen=True)
class Plan:
    weeks: tuple[tuple[Move, ...], ...]  # transfers per week, week 0 = the coming gameweek
    free_transfers: tuple[int, ...]  # free transfers available at each week (and after)
    objective: float
    optimal: bool
    seconds: float
    candidates: int  # players the model considered


def candidate_pool(
    squad: Sequence[SquadPlayer],
    pool: Sequence[SquadPlayer],
    value: Mapping[int, float],
    per_position: int = POOL_PER_POSITION,
    cheap: int = CHEAP_PER_POSITION,
) -> list[SquadPlayer]:
    """Your squad, plus the best `per_position` and the cheapest `cheap` others per position."""
    owned = {s.id for s in squad}
    keep = {s.id: s for s in squad}
    for pos in POSITIONS:
        others = [q for q in pool if q.position == pos and q.id not in owned]
        for q in sorted(others, key=lambda q: -value.get(q.id, 0.0))[:per_position]:
            keep.setdefault(q.id, q)
        for q in sorted(others, key=lambda q: q.price)[:cheap]:
            keep.setdefault(q.id, q)
    return list(keep.values())


def pair_moves(
    sells: Sequence[int], buys: Sequence[int], position: Mapping[int, PositionCode]
) -> tuple[Move, ...]:
    """FPL transfers are position for position: pair each week's sales and buys by position."""
    moves = []
    for pos in POSITIONS:
        out = sorted(p for p in sells if position[p] == pos)
        inn = sorted(p for p in buys if position[p] == pos)
        moves += [Move(o, i) for o, i in zip(out, inn, strict=True)]
    return tuple(moves)


def plan_transfers(
    squad: Sequence[SquadPlayer],
    bank: int,
    free: int,
    pool: Sequence[SquadPlayer],
    xps: Sequence[Mapping[int, float]],
    limits: Limits,
    *,
    allow_hits: bool = False,
    max_transfers: int = MAX_TRANSFERS,
    weights: Sequence[float] = WEEK_WEIGHTS,
    time_limit_s: float = TIME_LIMIT_S,
) -> Plan:
    """The best transfer plan over len(xps) weeks. `squad` at SELLING prices, `pool` at buying
    prices; xps[t][p] is player p's expected points in week t (0 = the coming gameweek)."""
    import highspy

    started = time.perf_counter()
    weeks = range(len(xps))
    w = list(weights[: len(xps)])
    value = {q.id: sum(wt * xp.get(q.id, 0.0) for wt, xp in zip(w, xps, strict=True)) for q in pool}
    players = candidate_pool(squad, pool, value)
    owned = {s.id for s in squad}
    sell_price = {s.id: s.price for s in squad} | {
        q.id: q.price for q in players if q.id not in owned
    }
    buy_price = {q.id: q.price for q in players}
    ids = [q.id for q in players]
    info = {q.id: q for q in players}

    h = highspy.Highs()  # type: ignore[no-untyped-call]  # its constructor has no annotations
    h.setOptionValue("output_flag", False)
    h.setOptionValue("time_limit", time_limit_s)
    h.setOptionValue("mip_rel_gap", MIP_GAP)
    integer = highspy.HighsVarType.kInteger

    in_squad = {(p, t): h.addBinary() for p in ids for t in weeks}
    in_xi = {(p, t): h.addBinary() for p in ids for t in weeks}
    captain = {(p, t): h.addBinary() for p in ids for t in weeks}
    buy = {(p, t): h.addBinary() for p in ids for t in weeks}
    sell = {(p, t): h.addBinary() for p in ids for t in weeks}
    ft = [
        h.addVariable(lb=0, ub=limits.max_free_transfers, type=integer) for _ in range(len(xps) + 1)
    ]
    hits = [h.addVariable(lb=0, ub=max_transfers if allow_hits else 0, type=integer) for _ in weeks]
    money = [h.addVariable(lb=0) for _ in weeks]  # bank after the week's transfers, tenths

    h.addConstr(ft[0] == free)
    clubs = {q.team for q in players}
    for t in weeks:
        h.addConstr(h.qsum(in_squad[p, t] for p in ids) == limits.squad_size)
        h.addConstr(h.qsum(in_xi[p, t] for p in ids) == limits.starters)
        h.addConstr(h.qsum(captain[p, t] for p in ids) == 1)
        for pos in POSITIONS:
            at = [p for p in ids if info[p].position == pos]
            h.addConstr(h.qsum(in_squad[p, t] for p in at) == limits.squad_counts[pos])
            h.addConstr(h.qsum(in_xi[p, t] for p in at) >= limits.min_play[pos])
            h.addConstr(h.qsum(in_xi[p, t] for p in at) <= limits.max_play[pos])
        for club in clubs:
            at = [p for p in ids if info[p].team == club]
            h.addConstr(h.qsum(in_squad[p, t] for p in at) <= limits.club_limit)
        for p in ids:
            h.addConstr(in_xi[p, t] <= in_squad[p, t])
            h.addConstr(captain[p, t] <= in_xi[p, t])
            if t == 0:
                before = 1 if p in owned else 0
                h.addConstr(in_squad[p, t] == before + buy[p, t] - sell[p, t])
                if before:
                    h.addConstr(buy[p, t] == 0)
                else:
                    h.addConstr(sell[p, t] == 0)
            else:
                h.addConstr(in_squad[p, t] == in_squad[p, t - 1] + buy[p, t] - sell[p, t])
                h.addConstr(buy[p, t] + in_squad[p, t - 1] <= 1)
                h.addConstr(sell[p, t] <= in_squad[p, t - 1])
        made = h.qsum(buy[p, t] for p in ids)
        h.addConstr(made <= max_transfers)
        h.addConstr(made <= ft[t] + hits[t])
        h.addConstr(hits[t] <= made)
        # Unused free transfers roll over (+1 a week), capped by ft's upper bound.
        h.addConstr(ft[t + 1] <= ft[t] - made + hits[t] + 1)
        spent = h.qsum(buy_price[p] * buy[p, t] - sell_price[p] * sell[p, t] for p in ids)
        h.addConstr(money[t] == (bank if t == 0 else money[t - 1]) - spent)

    points = h.qsum(
        w[t]
        * h.qsum(
            xps[t].get(p, 0.0)
            * (in_xi[p, t] + captain[p, t] + BENCH_WEIGHT * (in_squad[p, t] - in_xi[p, t]))
            for p in ids
        )
        for t in weeks
    )
    h.maximize(points - h.qsum(HIT_COST * w[t] * hits[t] for t in weeks) + FT_VALUE * ft[-1])

    status = h.getModelStatus()
    optimal = status == highspy.HighsModelStatus.kOptimal
    if not (optimal or status == highspy.HighsModelStatus.kTimeLimit):
        raise RuntimeError(f"the transfer plan couldn't be solved: {status}")
    position = {p: info[p].position for p in ids}
    plan_weeks = []
    for t in weeks:
        sells = [p for p in ids if h.val(sell[p, t]) > 0.5]
        buys = [p for p in ids if h.val(buy[p, t]) > 0.5]
        plan_weeks.append(pair_moves(sells, buys, position))
    return Plan(
        weeks=tuple(plan_weeks),
        free_transfers=tuple(round(h.val(f)) for f in ft),
        objective=float(h.getInfo().objective_function_value),
        optimal=optimal,
        seconds=time.perf_counter() - started,
        candidates=len(ids),
    )
