# Phase 3 part 2: the vectorised team scorer must equal the rules engine's team_points in every
# simulation - randomised scenarios (minutes, points, legal lineups, every chip) and the real FPL
# team-gameweek replays - plus pulling squads out of the gameweek simulation; no network.

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.rules import Limits, Lineup, is_valid_formation, team_points
from fpl_agent.optimize.scoring import SquadSims, score_lineup, squad_sims
from tests.conftest import load_fixture

CODE: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def random_lineup(rng: np.random.Generator, limits: Limits) -> Lineup:
    """A random legal team sheet from the 15-man squad."""
    while True:
        outfield = [p for p in POS if POS[p] != "GKP"]
        rng.shuffle(outfield)
        starters_out = outfield[:10]
        shape: list[PositionCode] = ["GKP", *(POS[p] for p in starters_out)]
        if not is_valid_formation(shape, limits):
            continue
        gk_start, gk_bench = (1, 2) if rng.random() < 0.5 else (2, 1)
        starters = (gk_start, *starters_out)
        bench = (gk_bench, *outfield[10:])
        cap, vice = rng.choice(starters, size=2, replace=False)
        return Lineup(
            tuple(int(x) for x in starters), tuple(int(x) for x in bench), int(cap), int(vice)
        )


@pytest.mark.parametrize("chip", [None, "3xc", "bboost"])
def test_vectorised_scorer_equals_rules_engine(chip: str | None, limits: Limits) -> None:
    rng = np.random.default_rng(0)
    n = 400
    ids = tuple(POS)
    for _ in range(25):  # 25 lineups x 400 simulations = 10,000 scenarios per chip
        # Mix of players who often miss (to force subs, including goalkeepers) and regulars.
        miss = rng.uniform(0.0, 0.6, size=len(ids))[:, None]
        played = rng.random((len(ids), n)) > miss
        points = np.where(played, rng.integers(1, 15, size=(len(ids), n)), 0).astype(np.int32)
        sims = SquadSims(ids, points, played)
        lineup = random_lineup(rng, limits)
        fast = score_lineup(sims, lineup, POS, limits, chip)
        for s in range(n):
            pts = {pid: int(points[i, s]) for i, pid in enumerate(ids)}
            pl = {pid: bool(played[i, s]) for i, pid in enumerate(ids)}
            assert fast[s] == team_points(lineup, pts, pl, POS, limits, chip), (lineup, s)


@pytest.mark.parametrize("case", load_fixture("autosub_cases"), ids=lambda c: "+".join(c["covers"]))
def test_real_fpl_cases_through_the_fast_scorer(case: dict[str, Any], limits: Limits) -> None:
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
    ids = tuple(players)
    sims = SquadSims(
        ids,
        np.array([[players[p]["points"]] for p in ids], dtype=np.int32),
        np.array([[players[p]["minutes"] > 0] for p in ids], dtype=np.bool_),
    )
    assert (
        score_lineup(sims, lineup, pos, limits, case["active_chip"])[0] == case["official_points"]
    )


def test_squad_sims_pulls_rows_and_zeroes_blank_players() -> None:
    samples = PointsSamples(
        player=np.array([5, 9], dtype=np.int64),
        total=np.array([[3, 4], [7, 8]], dtype=np.int32),
        played=np.array([[True, False], [True, True]]),
        breakdown={},
    )
    s = squad_sims(samples, [9, 42, 5])
    assert s.points.tolist() == [[7, 8], [0, 0], [3, 4]]  # 42 has no fixture this gameweek
    assert s.played.tolist() == [[True, True], [False, False], [True, False]]
