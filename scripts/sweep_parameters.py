"""Out-of-sample sweeps of the model's untested settings on the 2025/26 back-test (D27).

Each setting is varied on its own, holding the others at their current values; every run uses
the same per-gameweek seeds, so differences come from the setting, not luck. Scored with the
metric that setting affects, plus points error and ranking. Settings are module constants, so
the sweep sets them for the duration of a run and always restores them.

Usage: python scripts/sweep_parameters.py             (~8 minutes; 1,000 simulations per gameweek)
       python scripts/sweep_parameters.py --followup  (window x prior grid, longer saves shrinkage)
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import numpy as np

from fpl_agent.data import lineups
from fpl_agent.data.models import Bootstrap
from fpl_agent.model import attack, defence
from fpl_agent.validation.backtest import VALIDATION_DIR, GameweekRecords, run_season
from fpl_agent.validation.metrics import brier, log_loss, points_summary
from fpl_agent.validation.pointintime import load_season

SWEEP_SIMS = 1000
SWEEPS: dict[str, tuple[Any, str, list[float]]] = {
    "attack shrinkage (minutes)": (attack, "PRIOR_MINUTES", [90, 180, 270, 540, 1080]),
    "saves shrinkage (minutes)": (defence, "SAVES_PRIOR_MINUTES", [90, 270, 540, 1080]),
    "lineup window (gameweeks)": (lineups, "WINDOW_GWS", [3, 4, 6, 8, 10]),
    "lineup prior (matches)": (lineups, "PRIOR_MATCHES", [0.5, 1.0, 2.0, 4.0]),
}


@contextmanager
def setting(module: Any, name: str, value: float) -> Iterator[None]:
    original = getattr(module, name)
    setattr(module, name, type(original)(value))
    try:
        yield
    finally:
        setattr(module, name, original)


# Follow-up (D27): the first pass showed shorter lineup windows are better down to 3, and a shorter
# window leans harder on the season-long prior, so the two are swept jointly.
FOLLOWUP_GRID: list[dict[tuple[Any, str], float]] = [
    {(lineups, "WINDOW_GWS"): w, (lineups, "PRIOR_MATCHES"): pr}
    for w in (1, 2, 3)
    for pr in (0.5, 1.0, 2.0, 4.0)
] + [{(defence, "SAVES_PRIOR_MINUTES"): v} for v in (2160.0, 5000.0)]


@contextmanager
def settings(values: dict[tuple[Any, str], float]) -> Iterator[None]:
    originals = {key: getattr(key[0], key[1]) for key in values}
    for (module, name), v in values.items():
        setattr(module, name, type(originals[(module, name)])(v))
    try:
        yield
    finally:
        for (module, name), v in originals.items():
            setattr(module, name, v)


def score(rec: GameweekRecords) -> dict[str, float]:
    rows = rec.rows
    keepers = [r for r in rows if r.position == "GKP" and r.minutes >= 60]
    pts = points_summary(
        [p.gw for p in rec.points], [p.e_points for p in rec.points], [p.points for p in rec.points]
    )
    return {
        "goal_logloss": log_loss([r.p_goal for r in rows], [int(r.goals >= 1) for r in rows]),
        "assist_logloss": log_loss([r.p_assist for r in rows], [int(r.assists >= 1) for r in rows]),
        "saves_mae": float(np.mean([abs(r.e_saves - r.saves) for r in keepers])),
        "start_brier": brier([r.p_start for r in rows], [r.started for r in rows]),
        "points_mae": pts.mae,
        "points_rank": pts.spearman_within_gw,
    }


def main() -> int:
    template = Bootstrap.model_validate(json.loads(Path("data/cache/bootstrap.json").read_text()))
    season = load_season("2025-26", template)
    gws = list(range(2, 39))
    if "--followup" in sys.argv:
        rows = []
        for combo in FOLLOWUP_GRID:
            label = ", ".join(f"{name}={v:g}" for (_, name), v in combo.items())
            t0 = time.perf_counter()
            with settings(combo):
                s = score(run_season(season, gws, n_sims=SWEEP_SIMS))
            rows.append({"setting": label, **s})
            print(
                f"  {label:<34} goal LL {s['goal_logloss']:.5f}  saves MAE {s['saves_mae']:.4f}  "
                f"start Brier {s['start_brier']:.5f}  points MAE {s['points_mae']:.4f}  "
                f"rank {s['points_rank']:.4f}  ({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )
        (VALIDATION_DIR / "sweeps_followup.json").write_text(json.dumps(rows, indent=1))
        return 0
    results: dict[str, list[dict[str, float]]] = {}
    for label, (module, name, values) in SWEEPS.items():
        print(f"\n== {label} (current {getattr(module, name)})")
        results[label] = []
        for v in values:
            t0 = time.perf_counter()
            with setting(module, name, v):
                s = score(run_season(season, gws, n_sims=SWEEP_SIMS))
            s["value"] = v
            results[label].append(s)
            print(
                f"  {v:>6}: goal LL {s['goal_logloss']:.5f}  assist LL {s['assist_logloss']:.5f}  "
                f"saves MAE {s['saves_mae']:.4f}  start Brier {s['start_brier']:.5f}  "
                f"points MAE {s['points_mae']:.4f}  rank {s['points_rank']:.4f}  "
                f"({time.perf_counter() - t0:.0f}s)",
                flush=True,
            )
    (VALIDATION_DIR / "sweeps.json").write_text(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
