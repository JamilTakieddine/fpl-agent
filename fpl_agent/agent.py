"""One gameweek run: simulate, model the opponent, transfers, pick the lineup, save (if live).

(D31, D34, D36) Shared by `python -m fpl_agent.optimize` (prints to the terminal) and the cloud
job `python -m fpl_agent.run` (captures the same report for the email). Writes go only through
fpl_agent.submit: DRY RUN unless `live`, after its checks against a fresh read of the team.
Transfers are made only by the cloud's save run (D37); chips are information only until 5c.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TextIO

import numpy as np
import requests
from numpy.typing import NDArray

from fpl_agent.config import Settings
from fpl_agent.data.client import FplClient
from fpl_agent.data.injuries import GONE
from fpl_agent.data.models import MyTeam, PositionCode
from fpl_agent.data.odds_store import OddsStore
from fpl_agent.data.opponent import load_opponent
from fpl_agent.data.snapshots import SnapshotStore
from fpl_agent.model.pipeline import NextGameweek, simulate_ahead, simulate_next
from fpl_agent.optimize.lineup import (
    BUFFER,
    Choice,
    Recommendation,
    fpl_order,
    recommend,
    summarize,
)
from fpl_agent.optimize.opponent import (
    availability,
    average_points,
    build_opponent_model,
    chip_probabilities,
    opponent_points,
)
from fpl_agent.optimize.planner import Plan, chip_inputs, pair_moves
from fpl_agent.optimize.planner import plan_transfers as optimize_plan
from fpl_agent.optimize.rules import (
    Limits,
    Lineup,
    SquadPlayer,
    chip_playable,
    lineup_violations,
)
from fpl_agent.optimize.scoring import score_lineup, squad_sims
from fpl_agent.optimize.transfers import (
    WEEK_WEIGHTS,
    Move,
    Option,
    TransferPlan,
    recommend_transfers,
    squad_values,
)
from fpl_agent.submit import (
    TEAM_CHIPS,
    TRANSFER_CHIPS,
    SubmitResult,
    free_transfers,
    submit_lineup,
    submit_transfers,
)

CHIP_NAMES = {"3xc": "Triple Captain", "bboost": "Bench Boost"}


def current_lineup(team: MyTeam, positions: Mapping[int, PositionCode]) -> Lineup:
    picks = sorted(team.picks, key=lambda p: p.position)
    order = [p.element for p in picks]
    return Lineup(
        fpl_order(order[:11], positions),
        tuple(order[11:]),
        next(p.element for p in picks if p.is_captain),
        next(p.element for p in picks if p.is_vice_captain),
    )


def the_opponent(
    client: FplClient, nxt: NextGameweek, league: int | None, entry: int, limits: Limits
) -> tuple[str, NDArray[np.int64]]:
    """Who I'm playing and their points in each simulation (FPL's average if nobody)."""
    boot, sim = nxt.bootstrap, nxt.sim
    ownership = {p.id: p.selected_by_percent for p in boot.elements}
    snap = load_opponent(client, league, entry, nxt.event) if league is not None else None
    if snap is None or snap.last_picks is None:
        return "AVERAGE (FPL's overall average score)", average_points(sim.points, ownership)
    names = {p.id: p.web_name for p in boot.elements}
    positions = {p.id: boot.position_code(p.element_type) for p in boot.elements}
    xp = {int(p): float(e) for p, e in zip(sim.points.player, sim.points.expected(), strict=True)}
    chips = chip_probabilities(nxt.event, snap.chips_remaining, boot.chips, nxt.calendar)
    model = build_opponent_model(
        snap.last_picks,
        [c.captain for c in snap.captain_history],
        xp,
        chips,
        limits,
        availability(boot.elements),
    )
    ids = model.lineup.starters + model.lineup.bench
    points = opponent_points(
        squad_sims(sim.points, ids), model, positions, limits, np.random.default_rng(nxt.event)
    )
    top = sorted(model.captain_probs.items(), key=lambda kv: -kv[1])[:3]
    captains = ", ".join(f"{names[p]} {q:.0%}" for p, q in top)
    chip_note = "".join(f", {CHIP_NAMES[c]} {q:.0%}" for c, q in chips.items())
    return f"{snap.opponent.team_name} (captain: {captains}{chip_note})", points


def changes(new: Lineup, old: Lineup, names: Mapping[int, str]) -> list[str]:
    out = []
    ins = [p for p in new.starters if p not in old.starters]
    outs = [p for p in old.starters if p not in new.starters]
    if ins:
        out.append(
            f"start {', '.join(names[p] for p in ins)} for {', '.join(names[p] for p in outs)}"
        )
    if new.captain != old.captain:
        out.append(f"captain {names[new.captain]} (was {names[old.captain]})")
    if new.vice_captain != old.vice_captain:
        out.append(f"vice-captain {names[new.vice_captain]} (was {names[old.vice_captain]})")
    if new.bench[1:] != old.bench[1:] and not ins:
        out.append("reorder the bench")
    return out


@dataclass(frozen=True)
class RunResult:
    event: int
    deadline: datetime
    lineup: Lineup  # the recommendation (for the squad after any transfers)
    changed: bool  # differs from the team sheet saved on FPL before this run
    p_target: float  # P(win by BUFFER+) with the recommendation
    saved: SubmitResult | None  # None if the save request itself failed
    save_error: str | None
    transfers: SubmitResult | None = None  # set when this run tried to make transfers
    transfer_error: str | None = None
    chip: str | None = None  # a chip this run played (5c)


def run_gameweek(
    client: FplClient,
    settings: Settings,
    *,
    live: bool,
    out: TextIO,
    snapshot_store: SnapshotStore | None = None,
    odds_store: OddsStore | None = None,
    transfers: bool = True,
    make_transfers: bool = False,
    allow_hits: bool = False,
) -> RunResult | None:
    """The whole gameweek run, reported to `out`. None if the season has no deadline left.

    Order (D37): the opponent, then transfers (advice; MADE only when `make_transfers` and
    `live`, i.e. the cloud's save run), then the lineup for the squad as it now stands, saved
    through the D34 checks."""
    say = functools.partial(print, file=out)
    started = time.perf_counter()
    nxt = simulate_next(
        client, settings, datetime.now(UTC), snapshot_store=snapshot_store, odds_store=odds_store
    )
    if nxt is None:
        say("No upcoming deadline.")
        return None
    team = client.my_team(settings.entry_id)

    boot = nxt.bootstrap
    names = {p.id: p.web_name for p in boot.elements}
    positions = {p.id: boot.position_code(p.element_type) for p in boot.elements}
    limits = Limits.from_bootstrap(boot)
    label, opponent = the_opponent(client, nxt, settings.h2h_league_id, settings.entry_id, limits)
    say(f"GW{nxt.event}, deadline {nxt.deadline:%a %d %b %H:%M} UTC, vs {label}")

    def best_for(squad_team: MyTeam) -> tuple[Lineup, Any, Recommendation]:
        cur = current_lineup(squad_team, positions)
        sims = squad_sims(nxt.sim.points, cur.starters + cur.bench)
        return cur, sims, recommend(sims, positions, limits, opponent)

    current, mine, rec = best_for(team)
    transfer_result, transfer_error = None, None
    advice = None
    if transfers:
        advice = plan_transfers(
            out,
            client,
            nxt,
            team,
            positions,
            limits,
            opponent,
            rec.best.p_target,
            allow_hits=allow_hits,
            making=make_transfers and live,
        )
    chip_move = transfer_chip(say, advice, positions, limits) if advice else None
    to_make: tuple[Move, ...] = ()
    transfer_chip_name: str | None = None
    if chip_move is not None and chip_move.worth_it:
        to_make, transfer_chip_name = chip_move.moves, chip_move.chip
    elif advice is not None:
        to_make = advice.plan.best.moves
    if make_transfers and advice is not None and to_make:
        say()
        try:
            transfer_result = submit_transfers(
                client,
                settings.entry_id,
                nxt.event,
                to_make,
                advice.players,
                limits,
                nxt.deadline,
                datetime.now(UTC),
                live,
                allow_hits,
                log=say,
                chip=transfer_chip_name,
            )
        except requests.RequestException as e:  # never retried: it could apply twice
            transfer_error = f"{type(e).__name__}: {e}"
            say(f"TRANSFERS FAILED ({transfer_error}). Not retried; check the team on the site.")
        if transfer_result is not None and transfer_result.sent:
            team = client.my_team(settings.entry_id)  # the squad as it now stands
            current, mine, rec = best_for(team)

    best = rec.best.lineup
    assert lineup_violations(best, positions, limits) == []  # the rules engine has the last word
    xp = dict(zip(mine.ids, mine.points.mean(axis=1), strict=True))

    say("\nRecommended lineup")
    for p in best.starters:
        role = " (C)" if p == best.captain else " (V)" if p == best.vice_captain else ""
        say(f"  {positions[p]}  {names[p] + role:<22} {xp[p]:4.1f}")
    bench = ", ".join(f"{i}. {names[p]} {xp[p]:.1f}" for i, p in enumerate(best.bench[1:], 1))
    say(f"  Bench: GK {names[best.bench[0]]} {xp[best.bench[0]]:.1f} | {bench}")
    diff = changes(best, current, names)
    say(f"  Changes: {'; '.join(diff) if diff else 'none, your lineup is already the pick'}")

    def row(title: str, c: Choice) -> None:
        chances = f"{c.p_win:6.1%}   {c.p_draw:5.1%}   {c.p_target:6.1%}"
        say(f"  {title:<24} {c.expected:5.1f}   {chances}")

    say(f"\n  {'':<24}  xPts   P(win)  P(draw)  P(win by {BUFFER}+)")
    row("Recommended", rec.best)
    row(
        "Your current lineup",
        summarize(current, score_lineup(mine, current, positions, limits), opponent, BUFFER),
    )
    row("Most expected points", rec.max_expected)

    say("\nWhat each buffer would pick (P(win) / P(win by the buffer); buffer 1 = just win):")
    for b, c in rec.by_buffer.items():
        same = " (same lineup)" if c.lineup == best else ""
        say(f"  buffer {b}: {c.p_win:6.1%} / {c.p_target:6.1%}, xPts {c.expected:.1f}{same}")

    playable = [c for c in CHIP_NAMES if chip_playable(c, nxt.event, team.chips)]
    if playable:
        say("\nChips (information only: the Phase 5 planner decides when to play them)")
        for chip in playable:
            c = summarize(best, score_lineup(mine, best, positions, limits, chip), opponent, BUFFER)
            say(
                f"  {CHIP_NAMES[chip]}: {c.expected - rec.best.expected:+.1f} xPts, "
                f"P(win by {BUFFER}+) {rec.best.p_target:.1%} -> {c.p_target:.1%}"
            )
    say(
        f"\nSearched {rec.candidates:,} lineups within the points guard "
        f"({rec.xis_scored} of {rec.xis_total} XIs)."
    )

    team_chip = None
    if advice is not None and transfer_chip_name is None:
        team_chip = decide_team_chip(say, advice, best, mine, positions, limits)
    play_chip = team_chip if make_transfers else None  # chips, like transfers: the save run only
    say()
    saved, save_error = None, None
    try:
        saved = submit_lineup(
            client,
            settings.entry_id,
            best,
            positions,
            limits,
            nxt.deadline,
            datetime.now(UTC),
            live,
            log=say,
            play_chip=play_chip,
        )
    except requests.RequestException as e:  # never retried: a write could apply twice (D34)
        save_error = f"{type(e).__name__}: {e}"
        say(f"SAVE FAILED ({save_error}). Not retried; check the team on the site.")

    say(f"\n{time.perf_counter() - started:.0f}s in total.")
    return RunResult(
        event=nxt.event,
        deadline=nxt.deadline,
        lineup=best,
        changed=bool(diff),
        p_target=rec.best.p_target,
        saved=saved,
        save_error=save_error,
        transfers=transfer_result,
        transfer_error=transfer_error,
        chip=transfer_chip_name or play_chip,
    )


def money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


@dataclass(frozen=True)
class Advice:
    """The transfer step's output: what part 5 / the planner recommend, and what chips need."""

    plan: TransferPlan  # the exactly scored choice for this week's normal transfers
    players: dict[int, SquadPlayer]  # every buyable player at his current price
    multi: Plan | None  # the optimizer's week-by-week plan (None if it failed)
    chip_values: dict[str, float]  # what keeping each chip beyond the horizon is worth
    weeks: list[Any]  # the horizon's points samples, the coming week first
    squad: list[SquadPlayer]  # the current squad at selling prices
    last_chance: frozenset[str] = frozenset()  # chips whose window ends THIS gameweek


def plan_transfers(
    out: TextIO,
    client: FplClient,
    nxt: NextGameweek,
    team: MyTeam,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    opponent: NDArray[np.int64],
    p_target_now: float,
    *,
    allow_hits: bool,
    making: bool,
) -> Advice | None:
    """Transfer (and chip) plan over the next five gameweeks (D32, D38, D39), reported to `out`.
    None when transfers are unlimited this week (a chip already active)."""
    say = functools.partial(print, file=out)
    boot = nxt.bootstrap
    names = {p.id: p.web_name for p in boot.elements}
    free, bank = free_transfers(team), team.transfers.bank
    players = {
        p.id: SquadPlayer(p.id, positions[p.id], p.team, p.now_cost)
        for p in boot.elements
        if p.status not in GONE
    }
    how = "made automatically" if making else "RECOMMENDATION ONLY: nothing is sent to FPL"
    hits = "hits allowed" if allow_hits else "free transfers only"
    say(f"\nTransfers ({how}; {free} free, {hits}, bank {money(bank)})")
    if free is None:
        say("  Unlimited transfers this week (a wildcard / free hit is already active).")
        return None
    started = time.perf_counter()
    ahead = simulate_ahead(client, nxt, len(WEEK_WEIGHTS), datetime.now(UTC).date())
    weeks = [nxt.sim.points, *ahead.values()]
    team_of = {p.id: p.team for p in boot.elements}
    squad = [
        SquadPlayer(p.element, positions[p.element], team_of[p.element], p.selling_price)
        for p in team.picks
    ]
    # 5b (D38): the optimizer's multi-week plan; its week-1 transfers join the exact comparison.
    xps = [{int(p): float(e) for p, e in zip(w.player, w.expected(), strict=True)} for w in weeks]
    # 5c (D39): the chips still available, the weeks they may be played and what keeping is worth.
    available = [
        (c.name, c.start_event, c.stop_event)
        for c in team.chips
        if c.status_for_entry == "available"
    ]
    chip_weeks, chip_values, expiring = chip_inputs(
        available, nxt.event, xps, [s.id for s in squad], positions, limits
    )
    last_chance = frozenset(name for name, _, last in available if last == nxt.event)
    try:
        multi: Plan | None = optimize_plan(
            squad,
            bank,
            free,
            list(players.values()),
            xps,
            limits,
            allow_hits=allow_hits,
            chips=chip_weeks,
            chip_values=chip_values,
            must_play=expiring,
        )
    except Exception as e:  # never let the planner stop a gameweek: part 5's options remain
        multi = None
        say(f"  Plan: the optimizer failed ({type(e).__name__}: {e}); using the screened options.")
    chip_now = multi.chips[0] if multi is not None and multi.chips else None
    plain = multi is not None and multi.weeks[0] and chip_now not in TRANSFER_CHIPS
    extra = (multi.weeks[0],) if plain and multi is not None else ()
    plan = recommend_transfers(
        squad,
        bank,
        free,
        list(players.values()),
        weeks,
        positions,
        limits,
        allow_hits=allow_hits,
        extra=extra,
    )
    price = {pid: p.price for pid, p in players.items()} | {s.id: s.price for s in squad}
    gws = f"GW{nxt.event}-{nxt.event + len(weeks) - 1}"
    if multi is not None:
        report_plan(say, multi, plan, nxt.event, names, chip_values)

    def describe(o: Option) -> str:
        moves = "; ".join(
            f"sell {names[m.sell]} ({money(price[m.sell])}) -> buy {names[m.buy]} "
            f"({money(price[m.buy])})"
            for m in o.moves
        )
        hit = f", hit -{o.hit}" if o.hit else ""
        return (
            f"{moves}{hit}, bank after {money(o.bank)}\n"
            f"      {o.gain:+.1f} xPts over {gws} (weighted; needs {o.required:+.1f}), "
            f"this week {o.by_week[0]:+.1f}"
        )

    best = plan.best
    if best.moves:
        after = recommend(squad_sims(nxt.sim.points, best.squad), positions, limits, opponent)
        say(f"  Recommended: {describe(best)}")
        say(
            f"      this week's P(win by {BUFFER}+) with the best lineup: "
            f"{p_target_now:.1%} -> {after.best.p_target:.1%}"
        )
    else:
        say("  Recommended: roll the transfer (nothing clears its threshold).")
    for o in plan.alternatives:
        say(f"  Alternative: {describe(o)}")
    flagged = [
        p
        for p in boot.elements
        if p.id in {s.id for s in squad}
        and p.chance_of_playing_next_round is not None
        and p.chance_of_playing_next_round < 100
    ]
    for p in flagged:
        say(f"  Watch: {p.web_name}, {p.chance_of_playing_next_round}% ({p.news})")
    say(
        f"  Screened {plan.considered:,} legal options over {gws} in "
        f"{time.perf_counter() - started:.0f}s (including simulating {len(ahead)} more gameweeks)."
    )
    return Advice(plan, players, multi, chip_values, weeks, squad, last_chance)


CHIP_LABELS = {
    "3xc": "Triple Captain",
    "bboost": "Bench Boost",
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
}


def report_plan(
    say: Callable[..., None],
    multi: Plan,
    plan: TransferPlan,
    event: int,
    names: Mapping[int, str],
    chip_values: Mapping[str, float],
) -> None:
    """The optimizer's week-by-week plan, and how its first week fared in the exact scoring."""

    def week(t: int, moves: tuple[Move, ...]) -> str:
        chip = multi.chips[t] if t < len(multi.chips) else None
        if chip == "freehit":
            return "FREE HIT"
        label = f"{CHIP_LABELS[chip].upper()}: " if chip else ""
        return label + ("; ".join(f"{names[m.sell]} -> {names[m.buy]}" for m in moves) or "roll")

    steps = " | ".join(f"GW{event + t} {week(t, m)}" for t, m in enumerate(multi.weeks))
    fts = " -> ".join(str(f) for f in multi.free_transfers)
    proof = "optimal" if multi.optimal else "best found in the time limit"
    say(f"  Plan ({proof}, {multi.candidates} candidates, {multi.seconds:.1f}s): {steps}")
    say(f"      free transfers by week: {fts}")
    if chip_values:
        kept = ", ".join(f"{CHIP_LABELS[c]} {v:.1f}" for c, v in chip_values.items())
        last = event + len(multi.weeks) - 1
        say(f"      chips left, and what keeping each beyond GW{last} is worth: {kept}")
    first = set(multi.weeks[0])
    if multi.chips and multi.chips[0] in TRANSFER_CHIPS:
        say(f"      Its first week is a {CHIP_LABELS[multi.chips[0]]}: checked exactly below.")
        return
    if not first:
        say("      The plan rolls this week's transfers.")
        return
    scored = next((o for o in plan.scored if set(o.moves) == first), None)
    if scored is None:
        say("      Its first-week transfers aren't legal now (checked by the rules engine).")
    elif set(plan.best.moves) == first:
        say(f"      Its first-week transfers won the exact scoring ({scored.gain:+.1f} xPts).")
    else:
        say(
            f"      Its first-week transfers scored {scored.gain:+.1f} xPts exactly "
            f"(needs {scored.required:+.1f}); another option scored higher."
        )


# --- chips (5c, D39) -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ChipMove:
    chip: str  # wildcard / freehit
    moves: tuple[Move, ...]
    value: float  # exact gain from playing it now
    needed: float  # what keeping it is worth
    worth_it: bool


def transfer_chip(
    say: Callable[..., None],
    advice: Advice,
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> ChipMove | None:
    """If the plan plays a Wildcard or Free Hit THIS week, check it exactly with the simulations:
    it must beat the best normal transfers by more than keeping the chip is worth."""
    multi = advice.multi
    chip = multi.chips[0] if multi is not None and multi.chips else None
    if multi is None or chip not in TRANSFER_CHIPS:
        return None
    current = [s.id for s in advice.squad]
    if chip == "freehit":
        fh = multi.free_hit[0] or ()
        position = {p: advice.players[p].position for p in (*current, *fh) if p in advice.players}
        position |= {s.id: s.position for s in advice.squad}
        moves = pair_moves(
            [p for p in current if p not in fh], [p for p in fh if p not in current], position
        )
        weeks = advice.weeks[:1]  # the squad reverts after one week
    else:
        moves = multi.weeks[0]
        weeks = advice.weeks
    sold = {m.sell: m.buy for m in moves}
    new = [sold.get(p, p) for p in current]
    weights = WEEK_WEIGHTS[: len(weeks)]
    before = squad_values(current, weeks, positions, limits)
    after = squad_values(new, weeks, positions, limits)
    gain = sum(w * (a - b) for w, a, b in zip(weights, after, before, strict=True))
    normal = 0.0 if chip == "freehit" else max(advice.plan.best.gain - advice.plan.best.hit, 0.0)
    value, needed = gain - normal, advice.chip_values.get(chip, 0.0)
    # On a chip's last possible week keeping it is worthless: play it unless it actively loses.
    worth = value >= needed or (chip in advice.last_chance and value >= 0)
    verdict = "PLAY IT" if worth else "keep it"
    say(
        f"  {CHIP_LABELS[chip]} this week ({len(moves)} transfers): {value:+.1f} xPts exactly "
        f"over the best normal transfers; keeping it is worth {needed:.1f}: {verdict}."
    )
    return ChipMove(chip, moves, value, needed, worth)


def decide_team_chip(
    say: Callable[..., None],
    advice: Advice,
    lineup: Lineup,
    sims: Any,
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> str | None:
    """If the plan plays Bench Boost or Triple Captain THIS week, check it exactly on the lineup
    being saved: it must add more than keeping the chip is worth."""
    multi = advice.multi
    chip = multi.chips[0] if multi is not None and multi.chips else None
    if chip not in TEAM_CHIPS or chip is None:
        return None
    plain = float(score_lineup(sims, lineup, positions, limits).mean())
    boosted = float(score_lineup(sims, lineup, positions, limits, chip).mean())
    needed = advice.chip_values.get(chip, 0.0)
    gain = boosted - plain
    worth = gain >= needed or (chip in advice.last_chance and gain >= 0)
    verdict = "PLAY IT" if worth else "keep it"
    say(
        f"\n{CHIP_LABELS[chip]} this week: {boosted - plain:+.1f} xPts exactly; "
        f"keeping it is worth {needed:.1f}: {verdict}."
    )
    return chip if worth else None
