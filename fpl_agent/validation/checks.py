"""Consistency checks proving a rebuilt historical season is faithful, before judging any model
on it (docs/decisions.md D24). Run on real data with scripts/check_history.py.

2025/26 results: scoring reproduced for 29,747/29,747 player-matches (so our rules, thresholds
and point values held last season, DEFCON included); goals + own goals = final score in
380/380 fixtures (after removing 10 duplicated archive rows); odds for 380/380 matches; no
future information in any of the 37 rebuilt gameweeks.
"""

from __future__ import annotations

from collections import defaultdict

from fpl_agent.data.models import Bootstrap, Fixture, Team
from fpl_agent.model.scoring_rules import (
    DEFCON_THRESHOLD,
    GOALS_CONCEDED_PER_POINT,
    LONG_PLAY_MINUTES,
    SAVES_PER_POINT,
)
from fpl_agent.validation.history import HistoricalRow
from fpl_agent.validation.pointintime import GameweekCase


def points_from_stats(row: HistoricalRow, bootstrap: Bootstrap) -> int:
    """FPL points for one real player-match, from its stats, with our rules and config values."""
    p, v = row.position_code, bootstrap.points_for
    pts = 0
    if row.minutes > 0:
        pts += v("long_play", p) if row.minutes >= LONG_PLAY_MINUTES else v("short_play", p)
    pts += row.goals_scored * v("goals_scored", p) + row.assists * v("assists", p)
    pts += row.clean_sheets * v("clean_sheets", p)
    pts += (row.goals_conceded // GOALS_CONCEDED_PER_POINT) * v("goals_conceded", p)
    pts += (row.saves // SAVES_PER_POINT) * v("saves", p)
    pts += row.penalties_saved * v("penalties_saved", p)
    pts += row.penalties_missed * v("penalties_missed", p) + row.own_goals * v("own_goals", p)
    pts += row.yellow_cards * v("yellow_cards", p) + row.red_cards * v("red_cards", p)
    pts += row.bonus * v("bonus", p)
    threshold = DEFCON_THRESHOLD[p]
    if threshold is not None and row.defensive_contribution >= threshold:
        pts += v("defensive_contribution", p)
    return pts


def scoring_mismatches(rows: list[HistoricalRow], bootstrap: Bootstrap) -> list[HistoricalRow]:
    return [r for r in rows if points_from_stats(r, bootstrap) != r.total_points]


def score_mismatches(
    rows: list[HistoricalRow], fixtures: list[Fixture], teams: list[Team]
) -> list[int]:
    """Fixtures where players' goals (plus the other side's own goals) don't make the score."""
    team_ids = {t.name: t.id for t in teams}
    by_id = {f.id: f for f in fixtures}
    goals: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        f = by_id[r.fixture]
        side = 0 if team_ids[r.team] == f.team_h else 1
        goals[r.fixture][side] += r.goals_scored
        goals[r.fixture][1 - side] += r.own_goals
    return [
        fid
        for fid, (h, a) in goals.items()
        if (h, a) != (by_id[fid].team_h_score, by_id[fid].team_a_score)
    ]


def leaks(case: GameweekCase) -> list[str]:
    """Anything in a rebuilt gameweek that couldn't have been known before it."""
    problems = []
    for f in case.fixtures:
        if (
            f.event is not None
            and f.event >= case.event
            and (f.finished or f.team_h_score is not None)
        ):
            problems.append(f"fixture {f.id} (GW{f.event}) has a result")
    problems += [f"live data for GW{gw}" for gw in case.lives if gw >= case.event]
    return problems
