# Phase 2 step 7c: the validation report (likely-starter slicing, top-10 and captain comparison,
# points reliability bins, scoreline summaries) and its markdown/HTML writers; synthetic records.

from __future__ import annotations

import json

import pytest

from fpl_agent.validation.backtest import GameweekRecords, MatchRecord, PointsRecord, RowRecord
from fpl_agent.validation.report import (
    build_report,
    decisions,
    reliability,
    render_html,
    render_markdown,
    scorelines,
)


def pts(
    gw: int, player: int, e: float, actual: int, ppg: float | None, pos: str = "MID"
) -> PointsRecord:
    return PointsRecord("s", gw, player, pos, e, 0.1, 0.02, 0.5, actual, None, ppg, ppg)


def row(gw: int, player: int, p_start: float) -> RowRecord:
    return RowRecord(
        "s",
        gw,
        player,
        1,
        "MID",
        p_start,
        0.9,
        0.8,
        0.1,
        0.1,
        0.0,
        0.1,
        0.0,
        0.1,
        1,
        90,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def test_decisions_compare_model_and_ppg_picks() -> None:
    pool = []
    for i in range(12):
        # The model ranks player 0 first (he scores 10); ppg ranks player 11 first (he scores 1).
        pool.append(pts(1, i, e=12 - i, actual=10 if i == 0 else 1, ppg=float(i)))
    d = decisions(pool)
    assert d["gameweeks"] == 1
    assert d["captain_model"] == 20.0 and d["captain_ppg"] == 2.0
    assert d["top10_model"] == pytest.approx((10 + 9) / 10)
    assert d["top10_model_better_gws"] == 1


def test_decisions_ignore_players_without_history() -> None:
    pool = [pts(1, i, e=5, actual=5, ppg=None) for i in range(12)]
    assert decisions(pool)["gameweeks"] == 0


def test_reliability_needs_thirty_per_bin() -> None:
    records = [pts(1, i, e=2.5, actual=3, ppg=1.0) for i in range(30)]
    records += [pts(1, 100 + i, e=5.5, actual=5, ppg=1.0) for i in range(5)]
    bins = reliability(records, (0, 3, 6))
    assert len(bins) == 1 and bins[0]["n"] == 30 and bins[0]["actual"] == 3


def test_scorelines_summary() -> None:
    m = [
        MatchRecord("s", 1, 1, "x", 1.5, 1.0, 0.5, 0.25, 0.25, 1, 1),
        MatchRecord("s", 1, 2, "x", 2.0, 0.5, 0.7, 0.2, 0.1, 2, 0),
    ]
    sc = scorelines(GameweekRecords(m, [], []))
    assert sc["draw_actual"] == 0.5 and sc["home_win_actual"] == 0.5
    assert sc["goals_predicted"] == pytest.approx(5.0) and sc["goals_actual"] == 4


def records() -> GameweekRecords:
    m = [
        MatchRecord("s", gw, gw, "kalshi-totals", 1.4, 1.1, 0.45, 0.27, 0.28, 1, 0) for gw in (2, 8)
    ]
    rows = [row(gw, i, 0.9) for gw in (2, 8) for i in range(40)]
    points = [
        pts(gw, i, e=2 + i % 5, actual=i % 7, ppg=float(i % 4)) for gw in (2, 8) for i in range(40)
    ]
    return GameweekRecords(m, rows, points)


def test_writers_embed_the_same_numbers() -> None:
    report = build_report({"2025-26": records(), "2026-27": records()}, "2026-09-29")
    html = render_html(report)
    assert "/*__DATA__*/" not in html and "<title>" in html
    embedded = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    assert embedded == json.loads(json.dumps(report))
    md = render_markdown(report)
    starters = report["2025-26"]["points"]["vs_benchmarks_likely_starters"]
    assert f"**{starters['model']['mae']:.2f}**" in md
    assert "## To tune in 7d" in md
