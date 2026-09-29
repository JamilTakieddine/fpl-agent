# Phase 2 step 7a: historical loaders (CSV parsing, empty values, aliases, duplicate rows), the
# point-in-time rebuild (no future information), closing-odds conversion and the consistency
# checks; a tiny synthetic season written to tmp_path, no network.

from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from fpl_agent.data.lineups import predict_all
from fpl_agent.data.models import Bootstrap
from fpl_agent.data.odds import outcome_probs, p_over
from fpl_agent.model.gameweek import simulate_gameweek
from fpl_agent.validation import history
from fpl_agent.validation.checks import (
    leaks,
    points_from_stats,
    score_mismatches,
    scoring_mismatches,
)
from fpl_agent.validation.history import (
    ClosingOdds,
    dedupe_rows,
    load_closing_odds,
    load_rows,
    sources,
)
from fpl_agent.validation.pointintime import (
    DEADLINE_BEFORE_FIRST_KICKOFF,
    Season,
    build_case,
    closing_to_match_odds,
)
from tests.conftest import fx

SEASON = "2099-00"
KICKOFF = [datetime(2099, 8, 15 + 7 * (gw - 1), 15, 0, tzinfo=UTC) for gw in (1, 2, 3)]


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def gw_row(
    element: int, name: str, team: str, fixture: int, gw: int, home: bool, **stats: Any
) -> dict[str, Any]:
    base = {
        "element": element,
        "name": name,
        "position": stats.pop("position", "MID"),
        "team": team,
        "fixture": fixture,
        "GW": gw,
        "kickoff_time": KICKOFF[gw - 1].isoformat(),
        "was_home": home,
        "opponent_team": 2 if home else 1,
        "minutes": 90,
        "starts": 1,
        "goals_scored": 0,
        "assists": 0,
        "expected_goals": 0.0,
        "expected_assists": 0.0,
        "expected_goals_conceded": 0.0,
        "clean_sheets": 0,
        "goals_conceded": 0,
        "saves": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "own_goals": 0,
        "defensive_contribution": 0,
        "bps": 10,
        "bonus": 0,
        "total_points": 2,
        "xP": 2.5,
        "value": 55,
    }
    base.update(stats)
    return base


@pytest.fixture
def season(tmp_path: Path, bootstrap_json: Any) -> Season:
    """2 teams, 3 gameweeks (one match each: Arsenal home in GW1 and GW3); 2 players."""
    write(
        tmp_path / f"teams_{SEASON}.csv",
        [
            {"id": 1, "name": "Arsenal", "short_name": "ARS"},
            {"id": 2, "name": "Man Utd", "short_name": "MUN"},
        ],
    )
    write(
        tmp_path / f"fixtures_{SEASON}.csv",
        [
            {
                "id": gw,
                "event": gw,
                "team_h": 1 if gw != 2 else 2,
                "team_a": 2 if gw != 2 else 1,
                "kickoff_time": KICKOFF[gw - 1].isoformat(),
                "finished": "True",
                "team_h_score": 1,
                "team_a_score": 0,
                "team_h_difficulty": 3,
                "team_a_difficulty": 3,
            }
            for gw in (1, 2, 3)
        ],
    )
    write(
        tmp_path / f"players_raw_{SEASON}.csv",
        [
            {"id": 1, "element_type": 3, "team": 1, "web_name": "Saka"},
            {"id": 2, "element_type": 1, "team": 2, "web_name": "Onana"},
        ],
    )
    rows = []
    for gw in (1, 2, 3):
        home = gw != 2
        rows.append(
            gw_row(
                1,
                "Bukayo Saka",
                "Arsenal",
                gw,
                gw,
                home,
                goals_scored=1 if home else 0,
                expected_goals=0.5 * gw,
                minutes=90 if gw != 2 else 70,
            )
        )
        rows.append(
            gw_row(
                2,
                "André Onana",
                "Man Utd",
                gw,
                gw,
                not home,
                position="GK",
                goals_conceded=1 if home else 0,
            )
        )
    rows.append(rows[0])  # a duplicated archive row
    write(tmp_path / f"merged_gw_{SEASON}.csv", rows)
    write(
        tmp_path / "E0_9900.csv",
        [
            {
                "Date": KICKOFF[gw - 1].strftime("%d/%m/%Y"),
                "HomeTeam": "Arsenal" if gw != 2 else "Man United",
                "AwayTeam": "Man United" if gw != 2 else "Arsenal",
                "FTHG": 1,
                "FTAG": 0,
                "AvgCH": 1.8,
                "AvgCD": 3.8,
                "AvgCA": 4.5,
                "AvgC>2.5": 1.9,
                "AvgC<2.5": 1.95,
            }
            for gw in (1, 2, 3)
        ],
    )
    template = Bootstrap.model_validate(bootstrap_json)
    by_gw: dict[int, list[Any]] = {}
    for r in history.load_rows(SEASON, tmp_path):
        by_gw.setdefault(r.GW, []).append(r)
    return Season(
        name=SEASON,
        rows_by_gw=by_gw,
        teams=history.load_teams(SEASON, tmp_path),
        fixtures=history.load_fixtures(SEASON, tmp_path),
        player_types=history.load_player_types(SEASON, tmp_path),
        closing_odds=history.load_closing_odds(SEASON, tmp_path),
        template=template,
    )


def test_sources_map_season_codes() -> None:
    urls = sources("2025-26")
    assert urls["E0_2526.csv"].endswith("/2526/E0.csv")
    assert urls["merged_gw_2025-26.csv"].endswith("/2025-26/gws/merged_gw.csv")


def test_loaders_parse_types_aliases_and_dedupe(season: Season, tmp_path: Path) -> None:
    raw = load_rows(SEASON, tmp_path)
    assert len(raw) == 6  # the duplicated row is gone
    keeper = next(r for r in raw if r.element == 2)
    assert keeper.position_code == "GKP" and isinstance(keeper.was_home, bool)
    odds = load_closing_odds(SEASON, tmp_path)
    assert odds[0].odds_over_2_5 == pytest.approx(1.9)  # the 'AvgC>2.5' column


def test_dedupe_reports_count(season: Season) -> None:
    rows = season.rows_by_gw[1]
    kept, removed = dedupe_rows(rows + rows)
    assert (len(kept), removed) == (len(rows), len(rows))


def test_point_in_time_totals_use_only_earlier_gameweeks(season: Season) -> None:
    case = build_case(season, 3)
    saka = next(p for p in case.bootstrap.elements if p.id == 1)
    assert saka.minutes == 90 + 70  # GW1 + GW2, not GW3
    assert saka.expected_goals == pytest.approx(0.5 + 1.0)
    assert saka.goals_scored == 1
    assert saka.ep_next == pytest.approx(2.5)  # FPL's xP for GW3
    assert saka.status == "a" and saka.chance_of_playing_next_round is None  # no flags


def test_no_future_information(season: Season) -> None:
    case = build_case(season, 2)
    later = [f for f in case.fixtures if f.event is not None and f.event >= 2]
    assert all(not f.finished and f.team_h_score is None for f in later)
    assert set(case.lives) == {1}
    assert leaks(case) == []
    assert [r.GW for r in case.actual] == [2, 2]


def test_deadline_is_first_kickoff_minus_90(season: Season) -> None:
    case = build_case(season, 2)
    event = next(e for e in case.bootstrap.events if e.id == 2)
    assert event.deadline_time == KICKOFF[1] - DEADLINE_BEFORE_FIRST_KICKOFF
    assert event.is_next


def test_odds_are_mapped_including_team_aliases(season: Season) -> None:
    case = build_case(season, 2)  # Man United at home in GW2: needs the alias
    assert case.unpriced == [] and set(case.odds) == {2}


def test_closing_odds_conversion() -> None:
    o = ClosingOdds.model_validate(
        {
            "Date": "15/08/2099",
            "HomeTeam": "A",
            "AwayTeam": "B",
            "FTHG": 0,
            "FTAG": 0,
            "AvgCH": 1.8,
            "AvgCD": 3.8,
            "AvgCA": 4.5,
            "AvgC>2.5": 1.9,
            "AvgC<2.5": 1.95,
        }
    )
    m = closing_to_match_odds(fx(1, 1, 1, 2), o)
    assert m.p_home + m.p_draw + m.p_away == pytest.approx(1.0)
    over = (1 / 1.9) / (1 / 1.9 + 1 / 1.95)
    assert p_over(2.5, m.lambda_home + m.lambda_away) == pytest.approx(over, abs=1e-6)
    h, _, a = outcome_probs(m.lambda_home, m.lambda_away)
    assert h - a == pytest.approx(m.p_home - m.p_away, abs=1e-6)


def test_points_from_stats_and_mismatch_detection(season: Season) -> None:
    rows = [r for rs in season.rows_by_gw.values() for r in rs]
    saka_gw1 = next(r for r in rows if r.element == 1 and r.GW == 1)
    assert points_from_stats(saka_gw1, season.template) == 2 + 5  # 90 minutes + a MID goal
    assert saka_gw1 in scoring_mismatches(rows, season.template)  # the CSV says 2 points


def test_score_mismatch_detection(season: Season) -> None:
    rows = [r for rs in season.rows_by_gw.values() for r in rs]
    # Every fixture ended 1-0 to the home side; Saka scored at home in GW1 and GW3 only.
    assert score_mismatches(rows, season.fixtures, season.teams) == [2]


def test_simulation_runs_on_a_rebuilt_gameweek(season: Season) -> None:
    case = build_case(season, 3)
    preds = predict_all(case.bootstrap.elements, case.calendar, case.lives, 3)
    sim = simulate_gameweek(
        case.bootstrap,
        case.calendar,
        case.fixtures,
        3,
        preds,
        case.lives,
        case.odds,
        None,
        200,
        seed=1,
    )
    assert {r.source for r in sim.rates.values()} == {"kalshi-totals"}
    assert set(sim.points.player) == {1, 2}
