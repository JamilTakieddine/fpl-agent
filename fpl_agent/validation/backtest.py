"""Back-test runner: simulate past gameweeks and record every prediction next to what happened.

(docs/decisions.md D25) For each gameweek k of a Season: rebuild its inputs without peeking
(pointintime.build_case), predict lineups, simulate with keep_samples=True, and write three kinds
of record:
- MatchRecord (per fixture): expected goals and their source, model win/draw/loss, actual score.
- RowRecord (per player-match): P(start), P(60+), P(goal), P(assist), P(clean sheet),
  P(DEFCON), expected saves and bonus, against the actual outcome.
- PointsRecord (per player-gameweek): expected points, P(6+), P(10+) and a PIT value (where the
  actual score fell in the predicted distribution), plus benchmark predictions made with the
  same no-peeking rule: FPL's own xP (2025/26 only), points per appearance so far, and the
  average of the last three appearances.
Records are saved as JSONL (data/cache/validation/, gitignored) so metrics, the report and the
parameter sweeps reuse them without re-simulating.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from fpl_agent.data.lineups import predict_all
from fpl_agent.data.odds import outcome_probs
from fpl_agent.model.gameweek import simulate_gameweek
from fpl_agent.model.scoring_rules import DEFCON_THRESHOLD
from fpl_agent.validation.history import HistoricalRow
from fpl_agent.validation.pointintime import Season, build_case

VALIDATION_DIR = Path("data/cache/validation")
BACKTEST_SIMS = 2000  # plenty for evaluation; ~1s per gameweek
FORM_MATCHES = 3


@dataclass(frozen=True)
class MatchRecord:
    season: str
    gw: int
    fixture: int
    source: str
    lambda_home: float
    lambda_away: float
    p_home: float
    p_draw: float
    p_away: float
    home_goals: int
    away_goals: int


@dataclass(frozen=True)
class RowRecord:
    season: str
    gw: int
    player: int
    fixture: int
    position: str
    p_start: float
    p_play: float
    p_60: float
    p_goal: float
    p_assist: float
    p_clean_sheet: float
    p_defcon: float
    e_saves: float
    e_bonus: float
    started: int
    minutes: int
    goals: int
    assists: int
    clean_sheet: int
    defcon: int
    saves: int
    bonus: int


@dataclass(frozen=True)
class PointsRecord:
    season: str
    gw: int
    player: int
    position: str
    e_points: float
    p_6plus: float
    p_10plus: float
    pit: float  # randomized PIT: P(sim < actual) + U * P(sim == actual); uniform if honest
    points: int
    fpl_xp: float | None  # benchmark: FPL's own pre-gameweek expected points
    ppg: float | None  # benchmark: points per appearance so far
    form3: float | None  # benchmark: average of the last three appearances


@dataclass(frozen=True)
class GameweekRecords:
    matches: list[MatchRecord]
    rows: list[RowRecord]
    points: list[PointsRecord]


def _benchmarks(season: Season, target: int) -> dict[int, tuple[float | None, float | None]]:
    """player -> (points per appearance so far, average of the last three appearances)."""
    history: dict[int, list[int]] = defaultdict(list)  # points per appearance, in order
    for gw in range(1, target):
        per_gw: dict[int, list[HistoricalRow]] = defaultdict(list)
        for r in season.rows_by_gw.get(gw, []):
            per_gw[r.element].append(r)
        for pid, rs in per_gw.items():
            if sum(r.minutes for r in rs) > 0:
                history[pid].append(sum(r.total_points for r in rs))
    return {
        pid: (float(np.mean(v)), float(np.mean(v[-FORM_MATCHES:])))
        for pid, v in history.items()
        if v
    }


def run_gameweek(season: Season, target: int, n_sims: int = BACKTEST_SIMS) -> GameweekRecords:
    case = build_case(season, target)
    preds = predict_all(case.bootstrap.elements, case.calendar, case.lives, target)
    sim = simulate_gameweek(
        case.bootstrap,
        case.calendar,
        case.fixtures,
        target,
        preds,
        case.lives,
        case.odds,
        None,
        n_sims=n_sims,
        seed=target,
        keep_samples=True,
    )
    assert sim.samples is not None
    smp = sim.samples
    positions = {
        p.id: case.bootstrap.position_code(p.element_type) for p in case.bootstrap.elements
    }
    fixtures = {f.id: f for f in case.fixtures}

    actual_by_fixture = {}
    for fid in {r.fixture for r in case.actual}:
        f = next(x for x in season.fixtures if x.id == fid)
        actual_by_fixture[fid] = (f.team_h_score or 0, f.team_a_score or 0)
    matches = []
    for fid, rate in sorted(sim.rates.items()):
        h, d, a = outcome_probs(rate.lambda_home, rate.lambda_away)
        hg, ag = actual_by_fixture.get(fid, (0, 0))
        matches.append(
            MatchRecord(
                season.name,
                target,
                fid,
                rate.source,
                rate.lambda_home,
                rate.lambda_away,
                h,
                d,
                a,
                hg,
                ag,
            )
        )

    row_index = {
        (int(p), int(f)): i
        for i, (p, f) in enumerate(zip(smp.minutes.player, smp.minutes.fixture, strict=True))
    }
    rows = []
    for r in case.actual:
        i = row_index.get((r.element, r.fixture))
        if i is None or r.fixture not in fixtures:
            continue
        pos = positions[r.element]
        threshold = DEFCON_THRESHOLD[pos]
        rows.append(
            RowRecord(
                season=season.name,
                gw=target,
                player=r.element,
                fixture=r.fixture,
                position=pos,
                p_start=float(smp.minutes.started[i].mean()),
                p_play=float((smp.minutes.minutes[i] > 0).mean()),
                p_60=float((smp.minutes.minutes[i] >= 60).mean()),
                p_goal=float((smp.attack.goals[i] >= 1).mean()),
                p_assist=float((smp.attack.assists[i] >= 1).mean()),
                p_clean_sheet=float(smp.defence.clean_sheet[i].mean()),
                p_defcon=float(smp.defence.defcon_award[i].mean()),
                e_saves=float(smp.defence.saves[i].mean()),
                e_bonus=float(smp.bonus.bonus[i].mean()),
                started=r.starts,
                minutes=r.minutes,
                goals=r.goals_scored,
                assists=r.assists,
                clean_sheet=r.clean_sheets,
                defcon=int(threshold is not None and r.defensive_contribution >= threshold),
                saves=r.saves,
                bonus=r.bonus,
            )
        )

    actual_points: dict[int, int] = defaultdict(int)
    fpl_xp: dict[int, float | None] = {}
    for r in case.actual:
        actual_points[r.element] += r.total_points
        fpl_xp[r.element] = None if r.xP is None else (fpl_xp.get(r.element) or 0.0) + r.xP
    bench = _benchmarks(season, target)
    # Randomized PIT for discrete scores: a fixed midpoint would put every certain 0 at 0.5.
    pit_rng = np.random.default_rng(10_000 + target)
    points = []
    for pid, actual in sorted(actual_points.items()):
        try:
            i = sim.points.index(pid)
        except KeyError:
            continue
        s = sim.points.total[i]
        ppg, form = bench.get(pid, (None, None))
        points.append(
            PointsRecord(
                season=season.name,
                gw=target,
                player=pid,
                position=positions[pid],
                e_points=float(s.mean()),
                p_6plus=float((s >= 6).mean()),
                p_10plus=float((s >= 10).mean()),
                pit=float((s < actual).mean() + pit_rng.random() * (s == actual).mean()),
                points=actual,
                fpl_xp=fpl_xp.get(pid),
                ppg=ppg,
                form3=form,
            )
        )
    return GameweekRecords(matches, rows, points)


def run_season(
    season: Season, gameweeks: list[int], n_sims: int = BACKTEST_SIMS
) -> GameweekRecords:
    out = GameweekRecords([], [], [])
    for gw in gameweeks:
        recs = run_gameweek(season, gw, n_sims)
        out.matches.extend(recs.matches)
        out.rows.extend(recs.rows)
        out.points.extend(recs.points)
    return out


def save(records: GameweekRecords, name: str, directory: Path = VALIDATION_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for kind, items in (
        ("matches", records.matches),
        ("rows", records.rows),
        ("points", records.points),
    ):
        with (directory / f"{name}_{kind}.jsonl").open("w") as f:
            for item in items:
                f.write(json.dumps(asdict(item)) + "\n")


def load(name: str, directory: Path = VALIDATION_DIR) -> GameweekRecords:
    def read(kind: str) -> list[dict[str, Any]]:
        path = directory / f"{name}_{kind}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    return GameweekRecords(
        matches=[MatchRecord(**r) for r in read("matches")],
        rows=[RowRecord(**r) for r in read("rows")],
        points=[PointsRecord(**r) for r in read("points")],
    )
