"""Live smoke check of the data layer: python -m fpl_agent.data

Read-only. Refreshes the access token if needed (and saves the rotated refresh token).
"""

from __future__ import annotations

import sys

from fpl_agent.auth import AuthError, FileTokenStore, TokenManager
from fpl_agent.config import ConfigError, load_settings
from fpl_agent.data.client import FplClient, make_session


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
        print(f"next deadline: {nxt.name} at {nxt.deadline_time.isoformat()}")
    fixtures = client.fixtures()
    unscheduled = sum(1 for f in fixtures if f.event is None)
    print(f"fixtures: {len(fixtures)} ({unscheduled} unscheduled)")

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
