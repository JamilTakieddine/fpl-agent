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
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fpl_agent.data.models import PositionCode
from fpl_agent.optimize.rules import HIT_COST, Limits, SquadPlayer, next_free_transfers
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
    chips: tuple[str | None, ...] = ()  # the chip played each week, if any (5c)
    free_hit: tuple[tuple[int, ...] | None, ...] = ()  # the one-week squad in a Free Hit week


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
    chips: Mapping[str, Sequence[int]] | None = None,
    chip_values: Mapping[str, float] | None = None,
    must_play: Collection[str] = (),
) -> Plan:
    """The best transfer (and chip) plan over len(xps) weeks. `squad` at SELLING prices, `pool`
    at buying prices; xps[t][p] is player p's expected points in week t (0 = the coming week).
    `chips`: chip name -> the weeks it may be played (available and inside its window);
    `chip_values`: what keeping it beyond the horizon is worth (0 once its window ends inside).
    `must_play`: chips whose window ends inside the horizon. They're played (as many as there are
    weeks for: one chip a week), never lost to an indifferent or timed-out solve."""
    import highspy

    chips = chips or {}
    chip_values = chip_values or {}
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
    big = limits.squad_size  # "unlimited" transfers in a wildcard week

    h = highspy.Highs()  # type: ignore[no-untyped-call]  # its constructor has no annotations
    h.setOptionValue("output_flag", False)
    h.setOptionValue("time_limit", time_limit_s)
    h.setOptionValue("mip_rel_gap", MIP_GAP)
    integer = highspy.HighsVarType.kInteger

    in_squad = {(p, t): h.addBinary() for p in ids for t in weeks}
    in_xi = {(p, t): h.addBinary() for p in ids for t in weeks}
    captain = {(p, t): h.addBinary() for p in ids for t in weeks}
    bench = {(p, t): h.addBinary() for p in ids for t in weeks}  # regular bench, counted x0.1
    buy = {(p, t): h.addBinary() for p in ids for t in weeks}
    sell = {(p, t): h.addBinary() for p in ids for t in weeks}
    ft = [
        h.addVariable(lb=0, ub=limits.max_free_transfers, type=integer) for _ in range(len(xps) + 1)
    ]
    hits = [h.addVariable(lb=0, ub=max_transfers if allow_hits else 0, type=integer) for _ in weeks]
    used = [h.addVariable(lb=0) for _ in weeks]  # free transfers used up
    money = [h.addVariable(lb=0) for _ in weeks]  # bank after the week's transfers, tenths

    # Chips (5c): play[c][t] = 1 if chip c is played in week t.
    play = {c: {t: h.addBinary() for t in ts if t in weeks} for c, ts in chips.items()}

    def chip(c: str, t: int) -> Any:
        return play.get(c, {}).get(t, 0)

    tc = {(p, t): h.addBinary() for p in ids for t in play.get("3xc", {})}
    bb = {(p, t): h.addBinary() for p in ids for t in play.get("bboost", {})}
    fh_weeks = list(play.get("freehit", {}))
    fh_squad = {(p, t): h.addBinary() for p in ids for t in fh_weeks}
    fh_xi = {(p, t): h.addBinary() for p in ids for t in fh_weeks}
    fh_cap = {(p, t): h.addBinary() for p in ids for t in fh_weeks}
    for by_week in play.values():
        h.addConstr(h.qsum(by_week.values()) <= 1)  # each chip once
    expiring = [c for c in must_play if play.get(c)]
    if expiring:  # use it or lose it, as a rule rather than a zero value (D39)
        open_weeks = {t for c in expiring for t in play[c]}
        h.addConstr(
            h.qsum(v for c in expiring for v in play[c].values())
            >= min(len(expiring), len(open_weeks))
        )
    for t in weeks:
        on = [play[c][t] for c in play if t in play[c]]
        if on:
            h.addConstr(h.qsum(on) <= 1)  # one chip per gameweek

    h.addConstr(ft[0] == free)
    clubs = {q.team for q in players}
    for t in weeks:
        fh, wc = chip("freehit", t), chip("wildcard", t)
        regular = 1 - fh  # in a Free Hit week the regular squad sits out (and is frozen)
        h.addConstr(h.qsum(in_squad[p, t] for p in ids) == limits.squad_size)
        h.addConstr(h.qsum(in_xi[p, t] for p in ids) == limits.starters * regular)
        h.addConstr(h.qsum(captain[p, t] for p in ids) == regular)
        for pos in POSITIONS:
            at = [p for p in ids if info[p].position == pos]
            h.addConstr(h.qsum(in_squad[p, t] for p in at) == limits.squad_counts[pos])
            h.addConstr(h.qsum(in_xi[p, t] for p in at) >= limits.min_play[pos] * regular)
            h.addConstr(h.qsum(in_xi[p, t] for p in at) <= limits.max_play[pos] * regular)
        for club in clubs:
            at = [p for p in ids if info[p].team == club]
            h.addConstr(h.qsum(in_squad[p, t] for p in at) <= limits.club_limit)
        for p in ids:
            h.addConstr(in_xi[p, t] <= in_squad[p, t])
            h.addConstr(captain[p, t] <= in_xi[p, t])
            h.addConstr(bench[p, t] <= in_squad[p, t] - in_xi[p, t])
            h.addConstr(bench[p, t] <= regular)
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
            if t in fh_weeks:
                h.addConstr(buy[p, t] <= regular)
                h.addConstr(sell[p, t] <= regular)
            if (p, t) in tc:
                h.addConstr(tc[p, t] <= captain[p, t])
                h.addConstr(tc[p, t] <= chip("3xc", t))
            if (p, t) in bb:
                h.addConstr(bb[p, t] <= in_squad[p, t] - in_xi[p, t])
                h.addConstr(bb[p, t] <= chip("bboost", t))
        made = h.qsum(buy[p, t] for p in ids)
        h.addConstr(made <= max_transfers + big * wc)
        h.addConstr(made <= ft[t] + hits[t] + big * wc)
        h.addConstr(hits[t] <= made)
        h.addConstr(hits[t] <= max_transfers * (1 - wc))  # no hits in a wildcard week
        # Free transfers used up: none in a wildcard (or free hit) week.
        h.addConstr(used[t] >= made - hits[t] - big * (wc + fh))
        # Unused free transfers roll over (+1 a week, capped by ft's upper bound); in a wildcard or
        # free hit week they're kept as they are (FPL's rule, rules.next_free_transfers).
        h.addConstr(ft[t + 1] <= ft[t] - used[t] + 1 - wc - fh)
        spent = h.qsum(buy_price[p] * buy[p, t] - sell_price[p] * sell[p, t] for p in ids)
        h.addConstr(money[t] == (bank if t == 0 else money[t - 1]) - spent)
        if t in fh_weeks:  # the one-week Free Hit squad, within the squad's value plus the bank
            h.addConstr(h.qsum(fh_squad[p, t] for p in ids) == limits.squad_size * fh)
            h.addConstr(h.qsum(fh_xi[p, t] for p in ids) == limits.starters * fh)
            h.addConstr(h.qsum(fh_cap[p, t] for p in ids) == fh)
            for pos in POSITIONS:
                at = [p for p in ids if info[p].position == pos]
                h.addConstr(h.qsum(fh_squad[p, t] for p in at) == limits.squad_counts[pos] * fh)
                h.addConstr(h.qsum(fh_xi[p, t] for p in at) >= limits.min_play[pos] * fh)
                h.addConstr(h.qsum(fh_xi[p, t] for p in at) <= limits.max_play[pos] * fh)
            for club in clubs:
                at = [p for p in ids if info[p].team == club]
                h.addConstr(h.qsum(fh_squad[p, t] for p in at) <= limits.club_limit)
            for p in ids:
                h.addConstr(fh_xi[p, t] <= fh_squad[p, t])
                h.addConstr(fh_cap[p, t] <= fh_xi[p, t])
            worth = h.qsum(sell_price[p] * in_squad[p, t] for p in ids) + money[t]
            h.addConstr(h.qsum(buy_price[p] * fh_squad[p, t] for p in ids) <= worth)

    def xp(p: int, t: int) -> float:
        return xps[t].get(p, 0.0)

    points = h.qsum(
        w[t]
        * h.qsum(xp(p, t) * (in_xi[p, t] + captain[p, t] + BENCH_WEIGHT * bench[p, t]) for p in ids)
        for t in weeks
    )
    extras = [w[t] * xp(p, t) * v for (p, t), v in tc.items()]
    extras += [w[t] * (1 - BENCH_WEIGHT) * xp(p, t) * v for (p, t), v in bb.items()]
    extras += [
        w[t]
        * xp(p, t)
        * (fh_xi[p, t] + fh_cap[p, t] + BENCH_WEIGHT * (fh_squad[p, t] - fh_xi[p, t]))
        for p, t in fh_squad
    ]
    kept = [chip_values.get(c, 0.0) * v for c, by_week in play.items() for v in by_week.values()]
    h.maximize(
        points
        + (h.qsum(extras) if extras else 0)
        - h.qsum(HIT_COST * w[t] * hits[t] for t in weeks)
        + FT_VALUE * ft[-1]
        - (h.qsum(kept) if kept else 0)  # a chip played gives up what keeping it was worth
    )

    status = h.getModelStatus()
    optimal = status == highspy.HighsModelStatus.kOptimal
    found = h.getInfo().primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible
    if not (optimal or (status == highspy.HighsModelStatus.kTimeLimit and found)):
        # A timed-out solve WITHOUT a feasible plan has meaningless values: never use them.
        raise RuntimeError(f"no transfer plan found: {status}")
    position = {p: info[p].position for p in ids}
    plan_weeks: list[tuple[Move, ...]] = []
    played: list[str | None] = []
    free_hit: list[tuple[int, ...] | None] = []
    for t in weeks:
        sells = [p for p in ids if h.val(sell[p, t]) > 0.5]
        buys = [p for p in ids if h.val(buy[p, t]) > 0.5]
        plan_weeks.append(pair_moves(sells, buys, position))
        on = [c for c in play if t in play[c] and h.val(play[c][t]) > 0.5]
        played.append(on[0] if on else None)
        if on == ["freehit"]:
            free_hit.append(tuple(p for p in ids if h.val(fh_squad[p, t]) > 0.5))
        else:
            free_hit.append(None)
    # The free-transfer path, recomputed from the plan's decisions with FPL's own rule: exact even
    # when the solver stops at its time limit (its own ft values only bound the path).
    path = [free]
    for moves, played_chip in zip(plan_weeks, played, strict=True):
        path.append(next_free_transfers(path[-1], len(moves), limits, played_chip))
    return Plan(
        weeks=tuple(plan_weeks),
        free_transfers=tuple(path),
        objective=float(h.getInfo().objective_function_value),
        optimal=optimal,
        seconds=time.perf_counter() - started,
        candidates=len(ids),
        chips=tuple(played),
        free_hit=tuple(free_hit),
    )


# --- chip inputs (5c) ----------------------------------------------------------------------------

WILDCARD_VALUE = 15.0  # what keeping a wildcard is worth (a starting point, D39)
FREE_HIT_VALUE = 12.0  # likewise for a free hit
GOOD_WEEK = 1.25  # BB / TC only beat keeping them in a week 25% better than a typical one


def current_chips(
    available: Sequence[tuple[str, int, int]], event: int
) -> list[tuple[str, int, int]]:
    """One chip per name: the two sets share names ("bboost" GW1-19 and GW20-38), so keyed by name
    the later set would overwrite the current one's weeks and the current chip would be lost at
    its deadline (found by the 5d back-test). Keep the copy whose window covers `event`, else the
    next one to open."""
    chosen: dict[str, tuple[str, int, int]] = {}
    for chip in sorted(available, key=lambda c: c[1]):
        name, first, last = chip
        if last < event:
            continue  # its window has passed
        if name not in chosen:
            chosen[name] = chip
    return list(chosen.values())


def chip_inputs(
    available: Sequence[tuple[str, int, int]],  # (chip, first gameweek, last gameweek) still usable
    event: int,
    xps: Sequence[Mapping[int, float]],
    squad: Sequence[int],
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> tuple[dict[str, list[int]], dict[str, float], set[str]]:
    """For the planner: the horizon weeks each chip may be played in, what keeping it beyond the
    horizon is worth, and the chips whose window ends inside the horizon.

    An expiring chip must be played (use it or lose it: first-set chips die at GW19), and keeping
    it is worth nothing. Otherwise Triple Captain and Bench Boost are worth a GOOD_WEEK multiple
    of a typical week for this squad (its best captain; its bench), so they're played in clearly
    better weeks (doubles); Wildcard and Free Hit start from fixed values."""
    from statistics import median

    from fpl_agent.optimize.transfers import quick_value

    horizon_end = event + len(xps) - 1
    weeks: dict[str, list[int]] = {}
    values: dict[str, float] = {}
    expiring: set[str] = set()
    for name, first, last in current_chips(available, event):
        usable = [t for t in range(len(xps)) if first <= event + t <= last]
        if not usable:
            continue
        weeks[name] = usable
        if last <= horizon_end:
            values[name] = 0.0
            expiring.add(name)
        elif name == "3xc":
            values[name] = GOOD_WEEK * median(max(xp.get(p, 0.0) for p in squad) for xp in xps)
        elif name == "bboost":
            bench = []
            for xp in xps:
                total = sum(xp.get(p, 0.0) for p in squad)
                xi = quick_value(xp, squad, positions, limits) - max(xp.get(p, 0.0) for p in squad)
                bench.append(total - xi)
            values[name] = GOOD_WEEK * median(bench)
        else:
            values[name] = WILDCARD_VALUE if name == "wildcard" else FREE_HIT_VALUE
    return weeks, values, expiring
