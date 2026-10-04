"""One gameweek run: simulate, model the opponent, pick the lineup, save it (if live), advise.

(D31, D34, D36) Shared by `python -m fpl_agent.optimize` (prints to the terminal) and the cloud
job `python -m fpl_agent.run` (captures the same report for the email). Writes go only through
fpl_agent.submit: DRY RUN unless `live`, after its checks against a fresh read of the team.
Transfers are recommendations only and chips information only: the Phase 5 planner decides.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TextIO

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
from fpl_agent.optimize.lineup import BUFFER, Choice, fpl_order, recommend, summarize
from fpl_agent.optimize.opponent import (
    availability,
    average_points,
    build_opponent_model,
    chip_probabilities,
    opponent_points,
)
from fpl_agent.optimize.rules import (
    Limits,
    Lineup,
    SquadPlayer,
    chip_playable,
    lineup_violations,
)
from fpl_agent.optimize.scoring import score_lineup, squad_sims
from fpl_agent.optimize.transfers import WEEK_WEIGHTS, Option, recommend_transfers
from fpl_agent.submit import SubmitResult, submit_lineup

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
    lineup: Lineup  # the recommendation
    changed: bool  # differs from the team sheet saved on FPL before this run
    p_target: float  # P(win by BUFFER+) with the recommendation
    saved: SubmitResult | None  # None if the save request itself failed
    save_error: str | None


def run_gameweek(
    client: FplClient,
    settings: Settings,
    *,
    live: bool,
    out: TextIO,
    snapshot_store: SnapshotStore | None = None,
    odds_store: OddsStore | None = None,
    transfers: bool = True,
) -> RunResult | None:
    """The whole gameweek run, reported to `out`. None if the season has no deadline left."""
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
    current = current_lineup(team, positions)
    mine = squad_sims(nxt.sim.points, current.starters + current.bench)
    label, opponent = the_opponent(client, nxt, settings.h2h_league_id, settings.entry_id, limits)

    searched = time.perf_counter()
    rec = recommend(mine, positions, limits, opponent)
    searched = time.perf_counter() - searched
    best = rec.best.lineup
    assert lineup_violations(best, positions, limits) == []  # the rules engine has the last word
    xp = dict(zip(mine.ids, mine.points.mean(axis=1), strict=True))

    say(f"GW{nxt.event}, deadline {nxt.deadline:%a %d %b %H:%M} UTC, vs {label}")
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
        f"({rec.xis_scored} of {rec.xis_total} XIs) in {searched:.1f}s."
    )

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
        )
    except requests.RequestException as e:  # never retried: a write could apply twice (D34)
        save_error = f"{type(e).__name__}: {e}"
        say(f"SAVE FAILED ({save_error}). Not retried; check the team on the site.")

    if transfers:
        print_transfers(out, client, nxt, team, positions, limits, opponent, rec.best.p_target)
    say(f"\n{time.perf_counter() - started:.0f}s in total.")
    return RunResult(
        event=nxt.event,
        deadline=nxt.deadline,
        lineup=best,
        changed=bool(diff),
        p_target=rec.best.p_target,
        saved=saved,
        save_error=save_error,
    )


def money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def print_transfers(
    out: TextIO,
    client: FplClient,
    nxt: NextGameweek,
    team: MyTeam,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    opponent: NDArray[np.int64],
    p_target_now: float,
) -> None:
    """Transfer advice over the next five gameweeks (D32). Recommendation only."""
    say = functools.partial(print, file=out)
    boot = nxt.bootstrap
    names = {p.id: p.web_name for p in boot.elements}
    free, bank = team.transfers.limit, team.transfers.bank
    say(
        "\nTransfers (RECOMMENDATION ONLY: nothing is sent to FPL; "
        f"{free} free, bank {money(bank)})"
    )
    if free is None:
        say("  Unlimited transfers this week (wildcard / free hit active): left to Phase 5.")
        return
    started = time.perf_counter()
    ahead = simulate_ahead(client, nxt, len(WEEK_WEIGHTS), datetime.now(UTC).date())
    weeks = [nxt.sim.points, *ahead.values()]
    team_of = {p.id: p.team for p in boot.elements}
    squad = [
        SquadPlayer(p.element, positions[p.element], team_of[p.element], p.selling_price)
        for p in team.picks
    ]
    pool = [
        SquadPlayer(p.id, positions[p.id], p.team, p.now_cost)
        for p in boot.elements
        if p.status not in GONE
    ]
    plan = recommend_transfers(squad, bank, free, pool, weeks, positions, limits)
    price = {p.id: p.now_cost for p in boot.elements} | {s.id: s.price for s in squad}
    gws = f"GW{nxt.event}-{nxt.event + len(weeks) - 1}"

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
