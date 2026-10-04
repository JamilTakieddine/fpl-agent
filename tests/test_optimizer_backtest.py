# Phase 3 part 7: the optimizer back-test's pure parts - real scoring of a team sheet (the real FPL
# cases must reproduce their official points, hits included), H2H outcomes, paired statistics, the
# summary and both writers; synthetic records, no network and no simulation.

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.rules import Limits, Lineup
from fpl_agent.validation.optimizer import (
    TeamWeek,
    armband_points,
    outcome,
    paired,
    real_points,
    render_html,
    render_markdown,
    summarize_records,
    swap_analysis,
)
from tests.conftest import load_fixture

CODE: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


@pytest.mark.parametrize("case", load_fixture("autosub_cases"), ids=lambda c: "+".join(c["covers"]))
def test_real_scoring_reproduces_official_points(case: dict[str, Any], limits: Limits) -> None:
    players = {int(k): v for k, v in case["players"].items()}
    pos = {pid: CODE[p["element_type"]] for pid, p in players.items()}
    order = [p["element"] for p in sorted(case["picks"], key=lambda p: p["position"])]
    for sub in reversed(case["automatic_subs"]):
        i, j = order.index(sub["element_in"]), order.index(sub["element_out"])
        order[i], order[j] = order[j], order[i]
    flags = {p["element"]: p for p in case["picks"]}
    lineup = Lineup(
        tuple(order[:11]),
        tuple(order[11:]),
        next(e for e in order if flags[e]["is_captain"]),
        next(e for e in order if flags[e]["is_vice_captain"]),
    )
    live = {pid: (p["points"], p["minutes"] > 0) for pid, p in players.items()}
    chip = case["active_chip"]
    assert real_points(lineup, live, pos, limits, chip, hits=0) == case["official_points"]
    assert real_points(lineup, live, pos, limits, chip, hits=4) == case["official_points"] - 4
    assert armband_points(lineup, live) >= 0


def test_outcome_and_paired_statistics() -> None:
    assert [outcome(60, 50), outcome(50, 50), outcome(40, 50)] == ["W", "D", "L"]
    p = paired([1.0, 3.0, 2.0, 2.0])
    assert p["mean"] == pytest.approx(2.0)
    assert p["se"] == pytest.approx((2 / 3) ** 0.5 / 2)
    assert p["low"] < 2.0 < p["high"]


def week(gw: int, actual: int, ours: int, opponent: int, p_win: float, **kw: Any) -> TeamWeek:
    base = TeamWeek(
        gw=gw,
        entry=gw * 100 + actual,
        mine=False,
        vs_average=False,
        chip=None,
        official=actual,
        opponent=opponent,
        actual=actual,
        ours=ours,
        max_xp=ours,
        actual_xp=50.0,
        ours_xp=52.0,
        max_xp_xp=52.5,
        actual_p_win=0.5,
        ours_p_win=p_win,
        actual_captain=8,
        ours_captain=10,
        same_xi=True,
        same_captain=False,
    )
    return replace(base, **kw)


RECORDS = [
    week(2, 50, 55, 52, 0.6),  # agent turns a loss into a win
    week(2, 60, 58, 59, 0.5),  # agent turns a win into a loss
    week(3, 45, 49, 40, 0.7, mine=True),  # both win
    week(3, 70, 70, 80, 0.3, same_captain=True),  # both lose
]


def test_summary_counts_results_flips_and_calibration() -> None:
    s = summarize_records(RECORDS, "2026-10-01")
    assert s["n"] == 4 and s["official_check"] == 4
    assert s["points"]["ours_vs_actual"]["mean"] == pytest.approx((5 - 2 + 4 + 0) / 4)
    assert s["h2h"]["actual"] == {"W": 2, "D": 0, "L": 2, "league_points": 6}
    assert s["h2h"]["ours"] == {"W": 2, "D": 0, "L": 2, "league_points": 6}
    assert s["flips"] == {"better": 1, "worse": 1}
    assert s["calibration"]["predicted_win"] == pytest.approx(0.525)
    assert s["calibration"]["actual_win"] == 0.5
    assert s["captain"]["same_share"] == 0.25
    assert [g["gw"] for g in s["per_gw"]] == [2, 3]
    assert len(s["mine"]) == 1 and s["mine"][0]["ours_result"] == "W"
    # Humility: T = 0 is today's agent; a huge T keeps every manager's lineup.
    curve = {h["threshold"]: h for h in s["humility"]}
    assert curve[0.0]["vs_manager"]["mean"] == pytest.approx(s["points"]["ours_vs_actual"]["mean"])
    assert curve[3.0]["switches"] == 0 and curve[3.0]["vs_manager"]["mean"] == 0


def test_writers_share_the_same_numbers() -> None:
    s = summarize_records(RECORDS, "2026-10-01")
    md = render_markdown(s)
    assert "4 team-weeks" in md and "## Your team" in md and "## Caveats" in md
    html = render_html(s)
    embedded = json.loads(html.split("const DATA = ", 1)[1].split(";\n", 1)[0])
    assert embedded == json.loads(json.dumps(s))
    assert "entry" not in html  # managers' entry ids never reach a report


def test_swap_analysis_and_the_clean_comparison() -> None:
    lineup = ((1, 2, 3), (9,), 1, 2)
    a = week(3, 50, 54, 0, 0.5, ours_lineup=((1, 2, 4), (9,), 1, 2), actual_lineup=lineup)
    b = week(3, 60, 55, 0, 0.5, ours_lineup=((1, 2, 5), (9,), 1, 2), actual_lineup=lineup)
    minutes = {2: {3: 90, 4: 90, 5: 0}, 3: {3: 90, 4: 90, 5: 0}}  # 5 was out two weeks running
    points = {2: {}, 3: {3: 2, 4: 6, 5: 0}}
    sw = swap_analysis([a, b], minutes, points)
    assert sw["per_team_week"] == 1
    assert sw["agent_only"]["n"] == 2 and sw["agent_only"]["zero_minutes"] == 0.5
    assert sw["agent_only"]["out_two_weeks"] == 1
    assert sw["agent_only"]["points_when_played"] == 6
    assert sw["manager_only"]["points"] == 2
    assert sw["clean"]["n"] == 1  # b's swap involved a player who missed the week before
    s = summarize_records([a, b], "2026-10-01", sw)
    assert "## Why the agent differs" in render_markdown(s)
