"""Phase 3 part 6: turn a recommended lineup into FPL's save payload, and send it only when told.

(docs/decisions.md D34) DRY RUN is the default: the exact payload is printed and nothing is sent.
A real save needs `--live` on the command line or FPL_LIVE=1 in the environment (the cloud job),
and then still has to pass every check, against a FRESH read of the team just before sending:
- the gameweek's deadline hasn't passed (minus a minute's margin). After the deadline the same
  request would quietly set the NEXT gameweek's team;
- the payload holds exactly the current squad (you may have made a transfer on the site since
  the recommendation was computed);
- the rules engine passes the lineup (formation, bench keeper first, captain among starters);
- the chip: whatever team chip is already active is sent back unchanged (saving `null` would
  cancel it, as the website's own code shows); a new one only when the planner plays it (5c).
The payload is logged before sending, the POST is never retried, and the team is read back and
compared afterwards.

Transfers (Phase 5a, D37) follow the same pattern: the payload is exactly what the FPL website
sends to POST transfers/ (read from its own code), checked against a fresh read of the team and
of prices, sent once, then read back. Only the cloud's save run makes transfers.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fpl_agent.data.client import FplClient
from fpl_agent.data.models import MyTeam, PositionCode
from fpl_agent.optimize.rules import (
    Limits,
    Lineup,
    SquadPlayer,
    lineup_violations,
    squad_violations,
)
from fpl_agent.optimize.transfers import MAX_TRANSFERS, Move

CUTOFF = timedelta(minutes=1)  # don't start a save this close to the deadline: it could land after


def live_mode(argv: Sequence[str], env: Mapping[str, str]) -> bool:
    """A real save only with an explicit --live, or FPL_LIVE=1 (the cloud job's setting)."""
    return "--live" in argv or env.get("FPL_LIVE") == "1"


def lineup_payload(lineup: Lineup, chip: str | None = None) -> dict[str, Any]:
    """FPL's /my-team/ body: positions 1-11 the starters (FPL order), 12 the bench keeper, 13-15
    the bench in auto-sub order; captain and vice flags; the chip (None = no chip)."""
    order = lineup.starters + lineup.bench
    return {
        "chip": chip,
        "picks": [
            {
                "element": p,
                "position": i,
                "is_captain": p == lineup.captain,
                "is_vice_captain": p == lineup.vice_captain,
            }
            for i, p in enumerate(order, 1)
        ],
    }


def problems_before_saving(
    payload: Mapping[str, Any],
    lineup: Lineup,
    team: MyTeam,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    deadline: datetime,
    now: datetime,
) -> list[str]:
    """Every reason not to send (empty = safe), checked against a FRESH read of the team."""
    problems = []
    if now >= deadline - CUTOFF:
        problems.append(f"too close to or past the deadline ({deadline:%a %d %b %H:%M} UTC)")
    squad = {p.element for p in team.picks}
    sent = [p["element"] for p in payload["picks"]]
    if set(sent) != squad or len(sent) != len(squad):
        problems.append(
            "the payload's players aren't exactly the current squad (a transfer since?)"
        )
    problems += lineup_violations(lineup, positions, limits)
    chip = payload["chip"]
    if chip is not None and chip != active_team_chip(team):
        playable = any(c.name == chip and c.status_for_entry == "available" for c in team.chips)
        if chip not in TEAM_CHIPS or not playable:
            problems.append(f"chip {chip} isn't available to play this gameweek")
    return problems


TEAM_CHIPS = ("bboost", "3xc")  # played through /my-team/
TRANSFER_CHIPS = ("wildcard", "freehit")  # played through /transfers/ (D37, D39)


def active_team_chip(team: MyTeam) -> str | None:
    """The Bench Boost / Triple Captain already played (or proposed) this gameweek, if any. The
    FPL website sends it back with every lineup save: saving `chip: null` would CANCEL it."""
    for c in team.chips:
        if c.name in TEAM_CHIPS and c.status_for_entry in ("active", "proposed"):
            return c.name
    return None


def saved_matches(payload: Mapping[str, Any], team: MyTeam) -> bool:
    """Whether the team read back after saving is the one sent."""

    def key(p: Mapping[str, Any]) -> tuple[int, int, bool, bool]:
        return (p["element"], p["position"], p["is_captain"], p["is_vice_captain"])

    sent = sorted(key(p) for p in payload["picks"])
    got = sorted(key(p.model_dump()) for p in team.picks)
    return sent == got


@dataclass(frozen=True)
class SubmitResult:
    sent: bool
    payload: dict[str, Any]
    problems: list[str]
    verified: bool = False  # read back after sending and matched


def submit_lineup(
    client: FplClient,
    entry_id: int,
    lineup: Lineup,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    deadline: datetime,
    now: datetime,
    live: bool,
    log: Callable[[str], None] = print,
    play_chip: str | None = None,
) -> SubmitResult:
    """Build the payload and, only if `live` and every check passes, save it and read it back.
    The chip sent is `play_chip` (the planner playing Bench Boost / Triple Captain, 5c), else the
    team chip already active this gameweek, kept as it is."""
    team = client.my_team(entry_id)  # fresh: the squad may have changed since the recommendation
    payload = lineup_payload(lineup, play_chip or active_team_chip(team))
    problems = problems_before_saving(payload, lineup, team, positions, limits, deadline, now)
    mode = "LIVE" if live else "DRY RUN"
    log(f"{mode} payload for POST /my-team/: {json.dumps(payload)}")
    if problems:
        log("NOT SAVED: " + "; ".join(problems))
        return SubmitResult(False, payload, problems)
    if not live:
        log("DRY RUN: nothing sent. Pass --live (or set FPL_LIVE=1) to save this lineup.")
        return SubmitResult(False, payload, [])
    status = client.save_lineup(entry_id, payload)
    verified = saved_matches(payload, client.my_team(entry_id))
    log(f"SAVED: FPL answered {status}; read back {'matches' if verified else 'DOES NOT match'}.")
    return SubmitResult(True, payload, [], verified)


# --- transfers (Phase 5a, D37) -------------------------------------------------------------------


def hits_allowed(env: Mapping[str, str]) -> bool:
    """-4 hits stay off until the GW10-11 review (D37); FPL_ALLOW_HITS=1 turns them on."""
    return env.get("FPL_ALLOW_HITS") == "1"


def free_transfers(team: MyTeam) -> int | None:
    """Free transfers left this gameweek: the limit minus those already made (as the FPL site
    computes it). None while a wildcard or free hit makes transfers unlimited."""
    if team.transfers.limit is None:
        return None
    return max(team.transfers.limit - team.transfers.made, 0)


def transfers_payload(
    moves: Sequence[Move],
    entry: int,
    event: int,
    buy_price: Mapping[int, int],
    sell_price: Mapping[int, int],
    chip: str | None = None,
) -> dict[str, Any]:
    """FPL's /transfers/ body, as the website builds it: the incoming player's current price
    (now_cost) and the outgoing player's selling price, both in tenths of a million."""
    return {
        "chip": chip,
        "entry": entry,
        "event": event,
        "transfers": [
            {
                "element_in": m.buy,
                "element_out": m.sell,
                "purchase_price": buy_price[m.buy],
                "selling_price": sell_price[m.sell],
            }
            for m in moves
        ],
    }


def problems_before_transferring(
    payload: Mapping[str, Any],
    team: MyTeam,
    players: Mapping[int, SquadPlayer],
    limits: Limits,
    deadline: datetime,
    now: datetime,
    allow_hits: bool,
) -> list[str]:
    """Every reason not to send (empty = safe), against a FRESH read of the team. `players`:
    every player as he'd be bought (position, club, current price)."""
    problems = []
    if now >= deadline - CUTOFF:
        problems.append(f"too close to or past the deadline ({deadline:%a %d %b %H:%M} UTC)")
    moves = payload["transfers"]
    if not moves:
        return problems + ["no transfers to make"]
    chip = payload["chip"]
    free = free_transfers(team)
    if chip is not None:  # a wildcard or free hit (5c): unlimited free transfers this week
        if chip not in TRANSFER_CHIPS:
            problems.append(f"{chip} isn't played through transfers")
        elif not any(c.name == chip and c.status_for_entry == "available" for c in team.chips):
            problems.append(f"chip {chip} isn't available to play")
    elif free is None:
        problems.append("a wildcard or free hit is already active: unlimited transfers")
    else:
        if not allow_hits and len(moves) > free:
            problems.append(f"{len(moves)} transfers with {free} free: hits are off (D37)")
        if len(moves) > MAX_TRANSFERS:
            problems.append(f"{len(moves)} transfers (max {MAX_TRANSFERS} a week)")
    owned = {p.element: p for p in team.picks}
    sells = [m["element_out"] for m in moves]
    buys = [m["element_in"] for m in moves]
    if len(set(sells)) != len(sells) or len(set(buys)) != len(buys):
        problems.append("a player is sold or bought twice")
    for m in moves:
        out, inn = m["element_out"], m["element_in"]
        if out not in owned:
            problems.append(f"player {out} isn't in the squad any more")
        elif owned[out].selling_price != m["selling_price"]:
            problems.append(f"player {out}'s selling price changed: re-plan")
        if inn in owned:
            problems.append(f"player {inn} is already in the squad")
        elif inn not in players or players[inn].price != m["purchase_price"]:
            problems.append(f"player {inn}'s price changed or is unknown: re-plan")
    if problems:
        return problems
    sold = dict(zip(sells, buys, strict=True))
    squad = [
        players[sold[p]] if p in sold else replace_price(players[p], owned[p].selling_price)
        for p in owned
    ]
    bank = team.transfers.bank + sum(m["selling_price"] - m["purchase_price"] for m in moves)
    return squad_violations(squad, limits, bank)


def replace_price(player: SquadPlayer, price: int) -> SquadPlayer:
    return SquadPlayer(player.id, player.position, player.team, price)


def transfers_applied(payload: Mapping[str, Any], team: MyTeam) -> bool:
    """Whether the squad read back after transferring has every buy and none of the sales."""
    squad = {p.element for p in team.picks}
    moves = payload["transfers"]
    return all(m["element_in"] in squad and m["element_out"] not in squad for m in moves)


def submit_transfers(
    client: FplClient,
    entry_id: int,
    event: int,
    moves: Sequence[Move],
    players: Mapping[int, SquadPlayer],
    limits: Limits,
    deadline: datetime,
    now: datetime,
    live: bool,
    allow_hits: bool,
    log: Callable[[str], None] = print,
    chip: str | None = None,
) -> SubmitResult:
    """Build the transfers payload and, only if `live` and every check passes, send it and read
    the squad back. With `chip` (wildcard / free hit, 5c) the transfers are free and unlimited."""
    team = client.my_team(entry_id)  # fresh: the squad or prices may have moved since planning
    sell_price = {p.element: p.selling_price for p in team.picks}
    buy_price = {pid: p.price for pid, p in players.items()}
    owned_moves = [m for m in moves if m.sell in sell_price]
    payload = transfers_payload(owned_moves, entry_id, event, buy_price, sell_price, chip)
    if len(owned_moves) != len(moves):
        problems = ["a player to sell isn't in the squad any more"]
    else:
        problems = problems_before_transferring(
            payload, team, players, limits, deadline, now, allow_hits
        )
    mode = "LIVE" if live else "DRY RUN"
    log(f"{mode} transfers payload for POST /transfers/: {json.dumps(payload)}")
    if problems:
        log("TRANSFERS NOT MADE: " + "; ".join(problems))
        return SubmitResult(False, payload, problems)
    if not live:
        log("DRY RUN: no transfers made. The cloud's save run makes them (FPL_LIVE=1).")
        return SubmitResult(False, payload, [])
    status = client.make_transfers(payload)
    verified = transfers_applied(payload, client.my_team(entry_id))
    readback = "matches" if verified else "DOES NOT match"
    log(f"TRANSFERS MADE: FPL answered {status}; read back {readback}.")
    return SubmitResult(True, payload, [], verified)
