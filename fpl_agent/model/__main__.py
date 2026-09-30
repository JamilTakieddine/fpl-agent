"""Live check of the Phase 2 simulation: python -m fpl_agent.model

Read-only: fetches public data (plus your team if the login works), reads saved flag snapshots
and saved odds, simulates the next gameweek, and prints expected points and haul chances.
Recording snapshots and odds is `python -m fpl_agent.data`'s job, so this has no side effects.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import numpy as np

from fpl_agent.auth import AuthError, FileTokenStore, RateLimitedError, TokenManager
from fpl_agent.config import ConfigError, load_settings
from fpl_agent.data.client import FplClient, make_session
from fpl_agent.model.pipeline import N_SIMS, simulate_next


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
    nxt = simulate_next(client, settings, datetime.now(UTC), N_SIMS)
    if nxt is None:
        print("No upcoming deadline.")
        return 0
    boot, event, sim = nxt.bootstrap, nxt.event, nxt.sim
    sources: dict[str, int] = {}
    for r in sim.rates.values():
        sources[r.source] = sources.get(r.source, 0) + 1
    print(f"GW{event}: {N_SIMS:,} simulated gameweeks; expected goals from {sources}")

    try:
        picks = [p.element for p in client.my_team(settings.entry_id).picks]
        title = "your squad"
    except (AuthError, RateLimitedError) as e:
        print(f"(couldn't read your team: {type(e).__name__}; showing the top 15 instead)")
        order = np.argsort(-sim.points.expected())[:15]
        picks = [int(sim.points.player[i]) for i in order]
        title = "top 15 by expected points"

    names = {p.id: p.web_name for p in boot.elements}
    positions = {p.id: boot.position_code(p.element_type) for p in boot.elements}
    print(f"{title}:  xPts   P(0-1 pts)  P(6+)  P(10+)")
    for pid in picks:
        try:
            i = sim.points.index(pid)
        except KeyError:
            print(f"  {positions[pid]} {names[pid]:<16} no match this gameweek")
            continue
        pts = sim.points.total[i]
        print(
            f"  {positions[pid]} {names[pid]:<16} {pts.mean():5.2f}   {np.mean(pts <= 1):6.0%}"
            f"     {np.mean(pts >= 6):4.0%}   {np.mean(pts >= 10):4.0%}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
