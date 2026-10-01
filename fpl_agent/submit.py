"""Phase 3 part 6: turn a recommended lineup into FPL's save payload, and send it only when told.

(docs/decisions.md D34) DRY RUN is the default: the exact payload is printed and nothing is sent.
A real save needs `--live` on the command line or FPL_LIVE=1 in the environment (the cloud job),
and then still has to pass every check, against a FRESH read of the team just before sending:
- the gameweek's deadline hasn't passed (minus a minute's margin). After the deadline the same
  request would quietly set the NEXT gameweek's team;
- the payload holds exactly the current squad (you may have made a transfer on the site since
  the recommendation was computed);
- the rules engine passes the lineup (formation, bench keeper first, captain among starters);
- no chip: chips are the Phase 5 planner's decision (D28).
The payload is logged before sending, the POST is never retried, and the team is read back and
compared afterwards. Transfers are recommendation-only until Phase 5; their payload waits on the
transfers-endpoint capture (spikes/phase3_transfers.md).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fpl_agent.data.client import FplClient
from fpl_agent.data.models import MyTeam, PositionCode
from fpl_agent.optimize.rules import Limits, Lineup, lineup_violations

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
    if payload["chip"] is not None:
        problems.append("chips are decided by the Phase 5 planner, not sent from here")
    return problems


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
) -> SubmitResult:
    """Build the payload and, only if `live` and every check passes, save it and read it back."""
    payload = lineup_payload(lineup)
    team = client.my_team(entry_id)  # fresh: the squad may have changed since the recommendation
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
