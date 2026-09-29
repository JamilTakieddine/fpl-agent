"""Verify a downloaded historical season is faithful before back-testing on it.

Usage: python scripts/fetch_history.py && python scripts/check_history.py [season]
Needs a template bootstrap in data/cache/bootstrap.json (any current bootstrap-static).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fpl_agent.data.models import Bootstrap
from fpl_agent.validation.checks import leaks, score_mismatches, scoring_mismatches
from fpl_agent.validation.history import load_rows
from fpl_agent.validation.pointintime import build_case, load_season


def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else "2025-26"
    template = Bootstrap.model_validate(json.loads(Path("data/cache/bootstrap.json").read_text()))
    season = load_season(name, template)
    rows = load_rows(name)

    bad_points = scoring_mismatches(rows, template)
    print(f"scoring reproduced: {len(rows) - len(bad_points)}/{len(rows)} player-matches")
    bad_scores = score_mismatches(rows, season.fixtures, season.teams)
    n_fix = len(season.fixtures)
    print(f"goals + own goals = score: {n_fix - len(bad_scores)}/{n_fix} fixtures")

    last = max(season.rows_by_gw)
    unpriced, problems = 0, []
    for gw in range(2, last + 1):
        case = build_case(season, gw)
        unpriced += len(case.unpriced)
        problems += leaks(case)
    print(
        f"fixtures without odds (GW2-{last}): {unpriced}; future-information leaks: {len(problems)}"
    )
    ok = not bad_points and not bad_scores and not unpriced and not problems
    print("OK" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
