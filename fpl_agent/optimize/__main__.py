"""Lineup recommendation for the next gameweek: python -m fpl_agent.optimize [--live]

DRY RUN by default (D31, D34): simulates the gameweek, models this week's H2H opponent (or FPL's
average), and prints the recommended XI, bench order, captain and vice-captain next to your
current lineup, the exact save payload, then transfer advice over the next five gameweeks
(D32). Only `--live` (or FPL_LIVE=1) saves the recommended lineup, after the checks in
fpl_agent/submit.py. The run itself is fpl_agent.agent.run_gameweek, shared with the cloud job.
"""

from __future__ import annotations

import os
import sys

from fpl_agent.agent import run_gameweek
from fpl_agent.auth import AuthError, RateLimitedError, TokenManager
from fpl_agent.cloud.storage import LockHeld
from fpl_agent.config import ConfigError, load_settings
from fpl_agent.data.client import FplClient, make_session
from fpl_agent.stores import token_guard, token_store
from fpl_agent.submit import live_mode


def main() -> int:
    try:
        settings = load_settings()
        http = make_session()
        client = FplClient(http, TokenManager(token_store(settings), http))
        with token_guard():
            result = run_gameweek(
                client, settings, live=live_mode(sys.argv[1:], os.environ), out=sys.stdout
            )
    except ConfigError as e:
        print(f"Config error: {e}")
        return 1
    except (AuthError, RateLimitedError) as e:
        print(f"Couldn't use the FPL login ({type(e).__name__}): {e}")
        return 1
    except LockHeld as e:
        print(f"Another run is using the FPL login right now ({e}). Try again in a few minutes.")
        return 1
    return 1 if result is not None and result.save_error else 0


if __name__ == "__main__":
    sys.exit(main())
