"""Live smoke check of the data layer: python -m fpl_agent.data

Read-only. Refreshes the access token if needed (and saves the rotated refresh token).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from fpl_agent.auth import AuthError, FileTokenStore, TokenManager
from fpl_agent.config import ConfigError, load_settings
from fpl_agent.data.calendar import build_calendar, next_deadline
from fpl_agent.data.client import FplClient, make_session
from fpl_agent.data.opponent import load_opponent


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1

    http = make_session()
    store = FileTokenStore(settings.token_file, import_from=settings.phase0_state_file)
    client = FplClient(http, TokenManager(store, http))

    boot = client.bootstrap()
    nxt = boot.next_event()
    print(f"bootstrap: {len(boot.elements)} players, {len(boot.teams)} teams")
    if nxt:
        print(f"FPL is_next flag: {nxt.name}, deadline {nxt.deadline_time.isoformat()}")
    fixtures = client.fixtures()
    cal = build_calendar(boot, fixtures)
    print(f"fixtures: {len(fixtures)} ({len(cal.unscheduled)} unscheduled / postponed)")
    upcoming = next_deadline(boot, datetime.now(UTC))
    if upcoming:
        gw_id, deadline = upcoming
        hours = (deadline - datetime.now(UTC)).total_seconds() / 3600
        print(f"next deadline (by clock): GW{gw_id} in {hours:.1f}h")
        teams = {t.id: t.short_name for t in boot.teams}
        for gw in cal.window(gw_id, 6):
            tags = []
            if gw.double_teams:
                tags.append("DOUBLE: " + ",".join(sorted(teams[t] for t in gw.double_teams)))
            if gw.blank_teams:
                tags.append("BLANK: " + ",".join(sorted(teams[t] for t in gw.blank_teams)))
            print(f"  GW{gw.id:<2} {len(gw.fixtures):>2} fixtures  {' | '.join(tags) or 'normal'}")

        if settings.h2h_league_id:
            snap = load_opponent(client, settings.h2h_league_id, settings.entry_id, gw_id)
            if snap is None:
                print(f"H2H GW{gw_id}: vs league AVERAGE")
            else:
                names = {p.id: p.web_name for p in boot.elements}
                caps = ", ".join(
                    f"GW{c.event} {names.get(c.captain, c.captain)}"
                    + (" (TC)" if c.chip == "3xc" else "")
                    for c in snap.captain_history
                )
                print(f"H2H GW{gw_id}: vs entry {snap.opponent.entry_id}")
                print(f"  recent captains: {caps or 'none yet'}")
                print(f"  chips left: {', '.join(sorted(snap.chips_remaining)) or 'none'}")

    try:
        team = client.my_team(settings.entry_id)
    except AuthError as e:
        print(f"Auth error: {e}")
        return 1
    players = boot.players_by_id()
    cap = next(p for p in team.picks if p.is_captain)
    print(
        f"my team: {len(team.picks)} picks, captain {players[cap.element].web_name}, "
        f"{team.transfers.limit} free transfers, bank {team.transfers.bank / 10:.1f}m"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
