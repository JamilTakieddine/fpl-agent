"""Run the back-test and print per-step metrics: python -m fpl_agent.validation [--reuse]

Seasons: 2025/26 GW2-38 (archives, closing odds) and 2026/27 GW2-5 (FPL live data, Kalshi
prices 60 minutes before each deadline), the held-out season. Needs the downloads from
scripts/fetch_history.py and scripts/fetch_kalshi_history.py plus the cached live data in
data/cache/. --reuse skips simulation and summarises saved records.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from fpl_agent.data.models import Bootstrap, Fixture
from fpl_agent.validation.backtest import (
    GameweekRecords,
    MatchRecord,
    PointsRecord,
    RowRecord,
    load,
    run_season,
    save,
)
from fpl_agent.validation.current_season import kalshi_odds_source, load_current_season
from fpl_agent.validation.metrics import (
    brier,
    brier_skill,
    calibration,
    multiclass_brier,
    pit_histogram,
    points_summary,
    poisson_score_log_likelihood,
)
from fpl_agent.validation.pointintime import Season, load_season

EARLY = range(2, 7)  # gameweeks 2-6: little history yet


def seasons() -> dict[str, tuple[Season, list[int]]]:
    boot = Bootstrap.model_validate(json.loads(Path("data/cache/bootstrap.json").read_text()))
    last = load_season("2025-26", boot)
    fixtures = [
        Fixture.model_validate(f) for f in json.loads(Path("data/cache/fixtures.json").read_text())
    ]
    lives = {gw: json.loads(Path(f"data/cache/live_gw{gw}.json").read_text()) for gw in range(1, 6)}
    current, _ = load_current_season(boot, fixtures, lives, kalshi_odds_source())
    return {"2025-26": (last, list(range(2, 39))), "2026-27": (current, list(range(2, 6)))}


def outcome_index(m: MatchRecord) -> int:
    return 0 if m.home_goals > m.away_goals else 1 if m.home_goals == m.away_goals else 2


def summarise_matches(matches: list[MatchRecord]) -> None:
    outcomes = [outcome_index(m) for m in matches]
    probs = [(m.p_home, m.p_draw, m.p_away) for m in matches]
    ll = np.mean(
        [
            poisson_score_log_likelihood(m.lambda_home, m.lambda_away, m.home_goals, m.away_goals)
            for m in matches
        ]
    )
    predicted = sum(m.lambda_home + m.lambda_away for m in matches)
    actual = sum(m.home_goals + m.away_goals for m in matches)
    print(
        f"[scorelines] 1X2 Brier {multiclass_brier(probs, outcomes):.3f} (uniform guess 0.667); "
        f"score log-lik {ll:.3f}; goals predicted {predicted:.0f} vs actual {actual}"
    )
    for source in sorted({m.source for m in matches}):
        sel = [i for i, m in enumerate(matches) if m.source == source]
        score = multiclass_brier([probs[i] for i in sel], [outcomes[i] for i in sel])
        print(f"   source {source:<16} n={len(sel):<4} 1X2 Brier {score:.3f}")


def summarise_rows(rows: list[RowRecord]) -> None:
    gk_def = [r for r in rows if r.position in ("GKP", "DEF")]
    outfield = [r for r in rows if r.position != "GKP"]
    keepers = [r for r in rows if r.position == "GKP"]
    events: list[tuple[str, list[float], list[int]]] = [
        ("start", [r.p_start for r in rows], [r.started for r in rows]),
        ("60+ minutes", [r.p_60 for r in rows], [int(r.minutes >= 60) for r in rows]),
        ("goal", [r.p_goal for r in rows], [int(r.goals >= 1) for r in rows]),
        ("assist", [r.p_assist for r in rows], [int(r.assists >= 1) for r in rows]),
        (
            "clean sheet (GK/DEF)",
            [r.p_clean_sheet for r in gk_def],
            [r.clean_sheet for r in gk_def],
        ),
        ("DEFCON (outfield)", [r.p_defcon for r in outfield], [r.defcon for r in outfield]),
    ]
    print("[events]               predicted  actual   Brier   skill")
    for label, probs, happened in events:
        print(
            f"   {label:<22} {np.mean(probs):8.3f} {np.mean(happened):7.3f} "
            f"{brier(probs, happened):7.4f} {brier_skill(probs, happened):+7.3f}"
        )
    print(
        f"   {'saves per GK match':<22} {np.mean([r.e_saves for r in keepers]):8.3f} "
        f"{np.mean([r.saves for r in keepers]):7.3f}"
    )
    print(
        f"   {'bonus per player-match':<22} {np.mean([r.e_bonus for r in rows]):8.3f} "
        f"{np.mean([r.bonus for r in rows]):7.3f}"
    )


def summarise_points(points: list[PointsRecord]) -> None:
    splits = (
        ("all", points),
        ("GW2-6", [p for p in points if p.gw in EARLY]),
        ("GW7+", [p for p in points if p.gw not in EARLY]),
    )
    benchmarks: tuple[tuple[str, Callable[[PointsRecord], float | None]], ...] = (
        ("points per game", lambda p: p.ppg),
        ("last-3 form", lambda p: p.form3),
    )
    for label, sel in splits:
        if not sel:
            continue
        ours = points_summary(
            [p.gw for p in sel], [p.e_points for p in sel], [p.points for p in sel]
        )
        print(
            f"[points {label}] n={ours.n}: MAE {ours.mae:.3f}  RMSE {ours.rmse:.3f}  "
            f"bias {ours.bias:+.3f}  rank corr within GW {ours.spearman_within_gw:.3f}"
        )
        for bname, getter in benchmarks:
            have = [p for p in sel if getter(p) is not None]
            if not have:
                continue
            gws, actual = [p.gw for p in have], [p.points for p in have]
            theirs = points_summary(gws, [getter(p) or 0.0 for p in have], actual)
            mine = points_summary(gws, [p.e_points for p in have], actual)
            print(
                f"   vs {bname:<16} (same {theirs.n} rows): ours MAE {mine.mae:.3f} "
                f"rank {mine.spearman_within_gw:.3f} | theirs MAE {theirs.mae:.3f} "
                f"rank {theirs.spearman_within_gw:.3f}"
            )
    p6 = np.mean([p.p_6plus for p in points])
    p10 = np.mean([p.p_10plus for p in points])
    print(
        f"[haul chances] P(6+) predicted {p6:.3f} vs actual "
        f"{np.mean([p.points >= 6 for p in points]):.3f};  P(10+) {p10:.4f} vs "
        f"{np.mean([p.points >= 10 for p in points]):.4f}"
    )
    hauls: tuple[tuple[str, Callable[[PointsRecord], float], int], ...] = (
        ("P(6+)", lambda p: p.p_6plus, 6),
        ("P(10+)", lambda p: p.p_10plus, 10),
    )
    for label, pick, threshold in hauls:
        bins = calibration([pick(p) for p in points], [int(p.points >= threshold) for p in points])
        shown = "  ".join(
            f"{b.mean_predicted:.2f}->{b.observed:.2f}(n{b.count})" for b in bins if b.count >= 30
        )
        print(f"   {label} calibration (predicted->observed): {shown}")
    hist = pit_histogram([p.pit for p in points])
    print("   PIT histogram (honest = 0.10 each): " + " ".join(f"{v:.2f}" for v in hist))


def summarise(name: str, rec: GameweekRecords) -> None:
    print(
        f"\n==== {name}: {len({p.gw for p in rec.points})} gameweeks, {len(rec.matches)} matches, "
        f"{len(rec.rows)} player-matches ===="
    )
    summarise_matches(rec.matches)
    summarise_rows(rec.rows)
    summarise_points(rec.points)


def main() -> int:
    reuse = "--reuse" in sys.argv
    for name, (season, gws) in seasons().items():
        if reuse:
            rec = load(name)
        else:
            t0 = time.perf_counter()
            rec = run_season(season, gws)
            save(rec, name)
            print(f"{name}: simulated {len(gws)} gameweeks in {time.perf_counter() - t0:.0f}s")
        summarise(name, rec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
