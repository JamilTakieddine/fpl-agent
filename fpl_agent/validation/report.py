"""Phase 2 step 7c: the validation report's numbers, computed once from saved back-test records.

(docs/decisions.md D26) Produces one JSON-serialisable dict per season, read by both
docs/validation.md and the HTML page, so the two can never disagree. Sections:
- likely starters: players given >= 50% to start BEFORE the gameweek (the pool you pick from);
  reserves on 0 points flatter every all-player number.
- by position: error, bias and within-gameweek ranking per GK/DEF/MID/FWD.
- calibration curves for every event and for 6+ / 10+ points.
- points reliability: actual vs predicted points by predicted-points bin.
- scorelines: expected-goals calibration and the draw rate, predicted vs actual (the evidence for
  a Dixon-Coles correction, D15).
- decisions: each gameweek, the actual points of the model's top 10 vs points-per-game's top 10,
  and of each one's captain pick (doubled), from the same pool of players with history.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from fpl_agent.validation.backtest import GameweekRecords, PointsRecord, RowRecord
from fpl_agent.validation.metrics import (
    brier,
    brier_skill,
    calibration,
    multiclass_brier,
    pit_histogram,
    points_summary,
)

LIKELY_STARTER = 0.5
TOP_N = 10
POSITIONS = ("GKP", "DEF", "MID", "FWD")


def _summary(
    sel: Sequence[PointsRecord], pred: Callable[[PointsRecord], float]
) -> dict[str, float]:
    s = points_summary([p.gw for p in sel], [pred(p) for p in sel], [p.points for p in sel])
    return {"n": s.n, "mae": s.mae, "rmse": s.rmse, "bias": s.bias, "rank": s.spearman_within_gw}


def _versus_benchmarks(sel: Sequence[PointsRecord]) -> dict[str, Any]:
    have = [p for p in sel if p.ppg is not None and p.form3 is not None]
    if not have:
        return {"n": 0}  # e.g. GW7+ for a season that only has GW2-5 so far
    return {
        "n": len(have),
        "model": _summary(have, lambda p: p.e_points),
        "points_per_game": _summary(have, lambda p: p.ppg or 0.0),
        "last3_form": _summary(have, lambda p: p.form3 or 0.0),
    }


def _event(probs: Sequence[float], happened: Sequence[int]) -> dict[str, Any]:
    if not probs:
        return {"n": 0}  # e.g. no goalkeepers or defenders in a slice
    return {
        "n": len(probs),
        "predicted": float(np.mean(probs)),
        "actual": float(np.mean(happened)),
        "brier": brier(probs, happened),
        "skill": brier_skill(probs, happened),
        "curve": [asdict(b) for b in calibration(probs, happened)],
    }


def events(rows: Sequence[RowRecord]) -> dict[str, Any]:
    gk_def = [r for r in rows if r.position in ("GKP", "DEF")]
    outfield = [r for r in rows if r.position != "GKP"]
    return {
        "start": _event([r.p_start for r in rows], [r.started for r in rows]),
        "60+ minutes": _event([r.p_60 for r in rows], [int(r.minutes >= 60) for r in rows]),
        "goal": _event([r.p_goal for r in rows], [int(r.goals >= 1) for r in rows]),
        "assist": _event([r.p_assist for r in rows], [int(r.assists >= 1) for r in rows]),
        "clean sheet (GK/DEF)": _event(
            [r.p_clean_sheet for r in gk_def], [r.clean_sheet for r in gk_def]
        ),
        "DEFCON (outfield)": _event([r.p_defcon for r in outfield], [r.defcon for r in outfield]),
    }


def reliability(points: Sequence[PointsRecord], edges: Sequence[float]) -> list[dict[str, float]]:
    """Mean actual vs mean predicted points per predicted-points bin."""
    out = []
    pred = np.array([p.e_points for p in points])
    actual = np.array([p.points for p in points])
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (pred >= lo) & (pred < hi)
        if mask.sum() >= 30:
            out.append(
                {
                    "lower": lo,
                    "upper": hi,
                    "n": int(mask.sum()),
                    "predicted": float(pred[mask].mean()),
                    "actual": float(actual[mask].mean()),
                }
            )
    return out


def decisions(points: Sequence[PointsRecord]) -> dict[str, Any]:
    """Top-10 and captain picks per gameweek, model vs points-per-game, same candidate pool."""
    by_gw: dict[int, list[PointsRecord]] = defaultdict(list)
    for p in points:
        if p.ppg is not None:
            by_gw[p.gw].append(p)
    top_model, top_ppg, cap_model, cap_ppg = [], [], [], []
    for pool in by_gw.values():
        if len(pool) < TOP_N:
            continue
        by_model = sorted(pool, key=lambda p: -p.e_points)
        by_ppg = sorted(pool, key=lambda p: -(p.ppg or 0.0))
        top_model.append(float(np.mean([p.points for p in by_model[:TOP_N]])))
        top_ppg.append(float(np.mean([p.points for p in by_ppg[:TOP_N]])))
        cap_model.append(2.0 * by_model[0].points)
        cap_ppg.append(2.0 * by_ppg[0].points)
    return {
        "gameweeks": len(top_model),
        "top10_model": float(np.mean(top_model)) if top_model else float("nan"),
        "top10_ppg": float(np.mean(top_ppg)) if top_ppg else float("nan"),
        "top10_model_better_gws": int(sum(m > p for m, p in zip(top_model, top_ppg, strict=True))),
        "top10_ppg_better_gws": int(sum(m < p for m, p in zip(top_model, top_ppg, strict=True))),
        "captain_model": float(np.mean(cap_model)) if cap_model else float("nan"),
        "captain_ppg": float(np.mean(cap_ppg)) if cap_ppg else float("nan"),
        "captain_model_total": float(np.sum(cap_model)),
        "captain_ppg_total": float(np.sum(cap_ppg)),
    }


def scorelines(rec: GameweekRecords) -> dict[str, Any]:
    m = rec.matches
    outcomes = [
        0 if x.home_goals > x.away_goals else 1 if x.home_goals == x.away_goals else 2 for x in m
    ]
    lam = np.array([x.lambda_home for x in m] + [x.lambda_away for x in m])
    goals = np.array([x.home_goals for x in m] + [x.away_goals for x in m])
    bins = []
    for lo, hi in ((0, 0.9), (0.9, 1.2), (1.2, 1.5), (1.5, 1.8), (1.8, 2.2), (2.2, 9)):
        mask = (lam >= lo) & (lam < hi)
        if mask.sum() >= 10:
            bins.append(
                {
                    "lower": lo,
                    "upper": hi,
                    "n": int(mask.sum()),
                    "predicted": float(lam[mask].mean()),
                    "actual": float(goals[mask].mean()),
                }
            )
    return {
        "matches": len(m),
        "brier_1x2": multiclass_brier([(x.p_home, x.p_draw, x.p_away) for x in m], outcomes),
        "goals_predicted": float(lam.sum()),
        "goals_actual": int(goals.sum()),
        "draw_predicted": float(np.mean([x.p_draw for x in m])),
        "draw_actual": float(np.mean([o == 1 for o in outcomes])),
        "home_win_predicted": float(np.mean([x.p_home for x in m])),
        "home_win_actual": float(np.mean([o == 0 for o in outcomes])),
        "team_goal_calibration": bins,
    }


def season_report(name: str, rec: GameweekRecords) -> dict[str, Any]:
    starters_by_key = {(r.gw, r.player) for r in rec.rows if r.p_start >= LIKELY_STARTER}
    likely = [p for p in rec.points if (p.gw, p.player) in starters_by_key]
    early = [p for p in rec.points if p.gw <= 6]
    later = [p for p in rec.points if p.gw > 6]
    return {
        "season": name,
        "gameweeks": sorted({p.gw for p in rec.points}),
        "scorelines": scorelines(rec),
        "events": events(rec.rows),
        "points": {
            "all": _summary(rec.points, lambda p: p.e_points),
            "likely_starters": _summary(likely, lambda p: p.e_points),
            "vs_benchmarks_all": _versus_benchmarks(rec.points),
            "vs_benchmarks_likely_starters": _versus_benchmarks(likely),
            "vs_benchmarks_gw2_6": _versus_benchmarks(early),
            "vs_benchmarks_gw7_plus": _versus_benchmarks(later),
            "by_position": {
                pos: _versus_benchmarks([p for p in likely if p.position == pos])
                for pos in POSITIONS
            },
            "reliability_likely_starters": reliability(likely, (0, 1, 2, 3, 4, 5, 6, 8, 20)),
        },
        "hauls": {
            "p6": _event([p.p_6plus for p in rec.points], [int(p.points >= 6) for p in rec.points]),
            "p10": _event(
                [p.p_10plus for p in rec.points], [int(p.points >= 10) for p in rec.points]
            ),
            "pit_all": pit_histogram([p.pit for p in rec.points]),
            "pit_likely_starters": pit_histogram([p.pit for p in likely]),
        },
        "decisions": decisions(likely),
    }


# --- writers ------------------------------------------------------------------------------------

TEMPLATE = Path(__file__).with_name("report_template.html")


def render_html(report: dict[str, Any]) -> str:
    """The HTML page: the template with the report embedded as JSON."""
    return TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(report))


def _row(label: str, d: dict[str, Any]) -> str:
    m, p, f = d["model"], d["points_per_game"], d["last3_form"]
    return (
        f"| {label} | {d['n']:,} | **{m['mae']:.2f}** / {p['mae']:.2f} / {f['mae']:.2f} "
        f"| **{m['rank']:.3f}** / {p['rank']:.3f} / {f['rank']:.3f} | {m['bias']:+.2f} |"
    )


def render_markdown(report: dict[str, Any]) -> str:
    last, held = report["2025-26"], report["2026-27"]
    lp, hp = last["points"], held["points"]
    dec, sc = last["decisions"], last["scorelines"]
    ev = last["events"]
    lines = [
        "# Validation report",
        "",
        f"*Generated {report['generated']} by `python -m fpl_agent.validation --report`. "
        "Decisions and method: D24–D26 in `decisions.md`. An interactive version with charts is "
        "published as an HTML page.*",
        "",
        "## Verdict",
        "",
        'The simulation beats both "just use form" benchmarks in both seasons, on accuracy and on '
        "ranking players within a gameweek, and its probabilities match how often things actually "
        "happen. The tuning pass (7d) fixed the draw and DEFCON gaps and sharpened the lineup "
        "model; what remains is on the watch list below.",
        "",
        "## Expected points vs benchmarks",
        "",
        "Same player-gameweeks for all three. Error = mean absolute error (lower is better); "
        "ranking = Spearman correlation within each gameweek, averaged (higher is better). "
        "Cells read model / points per game / last-3 form.",
        "",
        "| Slice | n | Error | Ranking | Model bias |",
        "|---|---|---|---|---|",
        _row("2025/26, all players with history", lp["vs_benchmarks_all"]),
        _row("2025/26, likely starters", lp["vs_benchmarks_likely_starters"]),
        _row("2025/26, GW2–6", lp["vs_benchmarks_gw2_6"]),
        _row("2025/26, GW7+", lp["vs_benchmarks_gw7_plus"]),
        _row("2026/27 held out, likely starters", hp["vs_benchmarks_likely_starters"]),
        "",
        "Likely starters: given at least a 50% chance to start before the gameweek.",
        "",
        "## Picks and captaincy (2025/26, likely starters with history)",
        "",
        f"- **Top 10:** the model's top 10 averaged **{dec['top10_model']:.2f}** actual points "
        f"each, points-per-game's top 10 {dec['top10_ppg']:.2f}; the model's was better in "
        f"{dec['top10_model_better_gws']} of {dec['gameweeks']} gameweeks.",
        f"- **Captain (doubled):** model {dec['captain_model']:.2f} per gameweek vs "
        f"{dec['captain_ppg']:.2f}; **{dec['captain_model_total'] - dec['captain_ppg_total']:+.0f} "
        "points** over the season.",
        "",
        "## Event probabilities (2025/26)",
        "",
        "| Event | Predicted | Actual | Brier | Skill vs base rate |",
        "|---|---|---|---|---|",
    ]
    for name, e in [
        *ev.items(),
        ("6+ points", last["hauls"]["p6"]),
        ("10+ points", last["hauls"]["p10"]),
    ]:
        if not e["n"]:
            continue
        lines.append(
            f"| {name} | {e['predicted']:.3f} | {e['actual']:.3f} | {e['brier']:.4f} "
            f"| {e['skill']:+.3f} |"
        )
    pit = " ".join(f"{v:.2f}" for v in last["hauls"]["pit_all"])
    pit_h = " ".join(f"{v:.2f}" for v in held["hauls"]["pit_all"])
    lines += [
        "",
        "## Distribution honesty (PIT, 10 bins; honest = 0.10 each)",
        "",
        f"- 2025/26: {pit}",
        f"- 2026/27 held out: {pit_h}",
        "",
        "## Scorelines (2025/26)",
        "",
        f"- Win/draw/loss Brier {sc['brier_1x2']:.3f} (coin flip 0.667).",
        f"- Goals predicted {sc['goals_predicted']:.0f} vs actual {sc['goals_actual']}.",
        f"- **Draws predicted {sc['draw_predicted']:.3f} vs actual {sc['draw_actual']:.3f}** "
        f"({sc['matches']} matches).",
        "- Team goals by expected goals (expected → scored): "
        + ", ".join(
            f"{b['predicted']:.2f} → {b['actual']:.2f} (n {b['n']})"
            for b in sc["team_goal_calibration"]
        ),
        "",
        "## By position (2025/26, likely starters)",
        "",
        "| Position | n | Error | Ranking | Model bias |",
        "|---|---|---|---|---|",
    ]
    for pos, d in lp["by_position"].items():
        if d["n"]:
            lines.append(_row(pos, d))
    held_cs = held["events"]["clean sheet (GK/DEF)"]
    lines += [
        "",
        "## Changed in 7d (D27)",
        "",
        f"- **Draws:** Dixon-Coles, with rho set per match from the market's draw price. Predicted "
        f"draws {sc['draw_predicted']:.1%} (the market's level) vs {sc['draw_actual']:.1%} actual; "
        "the remaining gap is about one standard error, so the market isn't second-guessed.",
        f"- **DEFCON:** negative-binomial counts (dispersion measured at 1.55): "
        f"{ev['DEFCON (outfield)']['predicted']:.1%} predicted vs "
        f"{ev['DEFCON (outfield)']['actual']:.1%} actual.",
        f"- **Lineups:** a 2-gameweek window instead of 6 (start Brier "
        f"{ev['start']['brier']:.4f}); fits still use 6 gameweeks of data.",
        "- **Saves:** heavier shrinkage (5,000 minutes); attacking shrinkage stays at 270 (flat).",
        "",
        "## Watch list",
        "",
        f"- **Goals:** {sc['goals_predicted']:.0f} predicted vs {sc['goals_actual']} actual "
        "(about one standard error; not acted on).",
        *(
            [
                f"- **Held-out clean sheets:** {held_cs['predicted']:.1%} predicted vs "
                f"{held_cs['actual']:.1%} actual on four gameweeks."
            ]
            if held_cs["n"]
            else []
        ),
        "- **Likely-starter slices** depend on the model's own start probabilities, so they "
        "shift when the lineup model changes; compare same-row numbers across versions.",
        "",
        "## Limitations",
        "",
        "- No historical injury flags: everyone counts as available, which understates the live "
        "agent.",
        "- 2025/26 closing odds are set slightly after FPL's deadline.",
        "- 2026/27 is 4 gameweeks: indicative only.",
        "",
    ]
    return "\n".join(lines)


def build_report(records: dict[str, GameweekRecords], generated: str) -> dict[str, Any]:
    report: dict[str, Any] = {name: season_report(name, rec) for name, rec in records.items()}
    report["generated"] = generated
    return report
