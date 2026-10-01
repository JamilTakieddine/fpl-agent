"""Lineup recommendation for the next gameweek: python -m fpl_agent.optimize

DRY RUN, read-only (D31): simulates the gameweek, models this week's H2H opponent (or FPL's
average), and prints the recommended XI, bench order, captain and vice-captain next to your
current lineup, then transfer advice over the next five gameweeks (D32). Nothing is sent to
FPL; saving a lineup is Phase 3 part 6, behind --live. Transfers are recommendations only, and
chips are shown as information: both are decided by the Phase 5 planner.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime

import numpy as np
from numpy.typing import NDArray

from fpl_agent.auth import AuthError, FileTokenStore, RateLimitedError, TokenManager
from fpl_agent.config import ConfigError, load_settings
from fpl_agent.data.client import FplClient, make_session
from fpl_agent.data.injuries import GONE
from fpl_agent.data.models import MyTeam, PositionCode
from fpl_agent.data.opponent import load_opponent
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


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1
    http = make_session()
    client = FplClient(
        http,
        TokenManager(
            FileTokenStore(settings.token_file, import_from=settings.phase0_state_file), http
        ),
    )
    started = time.perf_counter()
    nxt = simulate_next(client, settings, datetime.now(UTC))
    if nxt is None:
        print("No upcoming deadline.")
        return 0
    try:
        team = client.my_team(settings.entry_id)
    except (AuthError, RateLimitedError) as e:
        print(f"Couldn't read your team ({type(e).__name__}): log in again, see README.")
        return 1

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

    print(f"GW{nxt.event}, deadline {nxt.deadline:%a %d %b %H:%M} UTC, vs {label}")
    print("\nRecommended lineup (DRY RUN: nothing is sent to FPL)")
    for p in best.starters:
        role = " (C)" if p == best.captain else " (V)" if p == best.vice_captain else ""
        print(f"  {positions[p]}  {names[p] + role:<22} {xp[p]:4.1f}")
    bench = ", ".join(f"{i}. {names[p]} {xp[p]:.1f}" for i, p in enumerate(best.bench[1:], 1))
    print(f"  Bench: GK {names[best.bench[0]]} {xp[best.bench[0]]:.1f} | {bench}")
    diff = changes(best, current, names)
    print(f"  Changes: {'; '.join(diff) if diff else 'none, your lineup is already the pick'}")

    def row(title: str, c: Choice) -> None:
        chances = f"{c.p_win:6.1%}   {c.p_draw:5.1%}   {c.p_target:6.1%}"
        print(f"  {title:<24} {c.expected:5.1f}   {chances}")

    print(f"\n  {'':<24}  xPts   P(win)  P(draw)  P(win by {BUFFER}+)")
    row("Recommended", rec.best)
    row(
        "Your current lineup",
        summarize(current, score_lineup(mine, current, positions, limits), opponent, BUFFER),
    )
    row("Most expected points", rec.max_expected)

    print("\nWhat each buffer would pick (P(win) / P(win by the buffer); buffer 1 = just win):")
    for b, c in rec.by_buffer.items():
        same = " (same lineup)" if c.lineup == best else ""
        print(f"  buffer {b}: {c.p_win:6.1%} / {c.p_target:6.1%}, xPts {c.expected:.1f}{same}")

    playable = [c for c in CHIP_NAMES if chip_playable(c, nxt.event, team.chips)]
    if playable:
        print("\nChips (information only: the Phase 5 planner decides when to play them)")
        for chip in playable:
            c = summarize(best, score_lineup(mine, best, positions, limits, chip), opponent, BUFFER)
            print(
                f"  {CHIP_NAMES[chip]}: {c.expected - rec.best.expected:+.1f} xPts, "
                f"P(win by {BUFFER}+) {rec.best.p_target:.1%} -> {c.p_target:.1%}"
            )
    print(
        f"\nSearched {rec.candidates:,} lineups within the points guard "
        f"({rec.xis_scored} of {rec.xis_total} XIs) in {searched:.1f}s."
    )

    print_transfers(client, nxt, team, positions, limits, opponent, rec.best.p_target)
    print(f"\n{time.perf_counter() - started:.0f}s in total.")
    return 0


def money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def print_transfers(
    client: FplClient,
    nxt: NextGameweek,
    team: MyTeam,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    opponent: NDArray[np.int64],
    p_target_now: float,
) -> None:
    """Transfer advice over the next five gameweeks (D32). Recommendation only."""
    boot = nxt.bootstrap
    names = {p.id: p.web_name for p in boot.elements}
    free, bank = team.transfers.limit, team.transfers.bank
    print(
        "\nTransfers (RECOMMENDATION ONLY: nothing is sent to FPL; "
        f"{free} free, bank {money(bank)})"
    )
    if free is None:
        print("  Unlimited transfers this week (wildcard / free hit active): left to Phase 5.")
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
        print(f"  Recommended: {describe(best)}")
        print(
            f"      this week's P(win by {BUFFER}+) with the best lineup: "
            f"{p_target_now:.1%} -> {after.best.p_target:.1%}"
        )
    else:
        print("  Recommended: roll the transfer (nothing clears its threshold).")
    for o in plan.alternatives:
        print(f"  Alternative: {describe(o)}")
    flagged = [
        p
        for p in boot.elements
        if p.id in {s.id for s in squad}
        and p.chance_of_playing_next_round is not None
        and p.chance_of_playing_next_round < 100
    ]
    for p in flagged:
        print(f"  Watch: {p.web_name}, {p.chance_of_playing_next_round}% ({p.news})")
    print(
        f"  Screened {plan.considered:,} legal options over {gws} in "
        f"{time.perf_counter() - started:.0f}s (including simulating {len(ahead)} more gameweeks)."
    )


if __name__ == "__main__":
    sys.exit(main())
