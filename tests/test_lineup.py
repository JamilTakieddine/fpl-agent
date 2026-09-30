# Phase 3 part 4: the lineup optimizer - legal XIs, pruning that never loses a lineup within the
# points guard (checked against brute force over every XI, bench order and captain pair), the
# one-standard-error tie rule, and planted H2H cases; synthetic simulations, no network.

from __future__ import annotations

from itertools import permutations
from typing import Any

import numpy as np
import pytest

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.optimize.lineup import EDGE, candidates, legal_xis, recommend, select
from fpl_agent.optimize.rules import Limits, Lineup, lineup_violations
from fpl_agent.optimize.scoring import (
    SquadSims,
    armband_points,
    score_lineup,
    team_points_before_armband,
)

POS: dict[int, PositionCode] = {
    **dict.fromkeys((1, 2), "GKP"),
    **dict.fromkeys(range(3, 8), "DEF"),
    **dict.fromkeys(range(8, 13), "MID"),
    **dict.fromkeys(range(13, 16), "FWD"),
}


@pytest.fixture
def limits(bootstrap_json: Any) -> Limits:
    return Limits.from_bootstrap(Bootstrap.model_validate(bootstrap_json))


def random_sims(n: int, seed: int) -> SquadSims:
    rng = np.random.default_rng(seed)
    miss = rng.uniform(0.0, 0.5, size=15)[:, None]  # plenty of auto-subs
    played = rng.random((15, n)) > miss
    skill = rng.uniform(1, 6, size=15)[:, None]
    points = np.where(played, rng.poisson(skill, size=(15, n)) + 1, 0).astype(np.int32)
    return SquadSims(tuple(POS), points, played)


def test_every_legal_xi_is_found(limits: Limits) -> None:
    # 10 of 13 outfielders is 286 ways; 10 leave only 2 defenders, 1 leaves no forward: 275 per
    # keeper, and either keeper can start.
    xis = legal_xis(tuple(POS), POS, limits)
    assert len(xis) == 550
    assert len({x[0] for x in xis}) == 550
    starters, bench_gk, rest = xis[0]
    assert [POS[p] for p in starters][:2] == ["GKP", "DEF"]  # FPL order
    assert POS[bench_gk] == "GKP" and len(rest) == 3


@pytest.mark.parametrize(("seed", "guard"), [(0, 1.0), (1, 1.0), (2, 3.0)])
def test_pruned_search_equals_brute_force(seed: int, guard: float, limits: Limits) -> None:
    """Score every XI x bench order x captain pair; the optimizer's candidates must be exactly
    the lineups within the guard, with the right points in every simulation."""
    sims = random_sims(300, seed)
    feasible: dict[tuple[Any, ...], float] = {}
    armband: dict[tuple[int, int], float] = {}
    all_expected = []
    for starters, bench_gk, rest in legal_xis(sims.ids, POS, limits):
        for order in permutations(rest):
            sheet = Lineup(starters, (bench_gk, *order), starters[0], starters[1])
            base = team_points_before_armband(sims, sheet, POS, limits).mean()
            for c in starters:
                for v in starters:
                    if c == v:
                        continue
                    if (c, v) not in armband:
                        armband[(c, v)] = armband_points(sims, starters, c, v).mean()
                    all_expected.append((base + armband[(c, v)], (starters, sheet.bench, c, v)))
    best = max(e for e, _ in all_expected)
    feasible = {key: e for e, key in all_expected if e >= best - guard - EDGE}

    cands = candidates(sims, POS, limits, guard)
    found = {(x.starters, x.bench, x.captain, x.vice_captain) for x, _ in cands}
    assert len(found) == len(cands)
    assert found == set(feasible)
    assert cands.best_expected == pytest.approx(best)
    for lineup, pts in list(cands)[:: max(1, len(cands) // 25)]:
        assert (pts == score_lineup(sims, lineup, POS, limits)).all()


# --- choosing ------------------------------------------------------------------------------------


def flipped(hits: np.ndarray[Any, Any], up: int, down: int, seed: int) -> np.ndarray[Any, Any]:
    """A copy of `hits` with `up` misses turned into hits and `down` hits into misses."""
    rng = np.random.default_rng(seed)
    out = hits.copy()
    out[rng.choice(np.flatnonzero(~hits), up, replace=False)] = True
    out[rng.choice(np.flatnonzero(hits), down, replace=False)] = False
    return out


def test_within_one_standard_error_the_higher_expected_points_wins() -> None:
    base = np.random.default_rng(0).random(10_000) < 0.5
    noisy_edge = flipped(base, up=765, down=735, seed=1)  # +0.3%, but 1,500 simulations differ
    hits = np.stack([base, noisy_edge])
    assert select(np.array([50.0, 49.0]), hits) == 0  # a tie: take the extra expected points


def test_a_clear_edge_beats_expected_points() -> None:
    base = np.random.default_rng(0).random(10_000) < 0.5
    clear_edge = flipped(base, up=1000, down=500, seed=1)  # +5%
    hits = np.stack([base, clear_edge])
    assert select(np.array([50.0, 49.0]), hits) == 1


# --- planted H2H cases ---------------------------------------------------------------------------


def two_captains(n: int) -> SquadSims:
    """Everyone always plays. A 4-4-2 is clearly best (the bench players - 2, 7, 12, 15 - score 0),
    and forwards 13 and 14 are equally good, independent captain options; nobody else is close."""
    rng = np.random.default_rng(3)
    points = np.full((15, n), 2, dtype=np.int32)
    points[[1, 6, 11, 14]] = 0
    points[12] = rng.integers(0, 21, size=n)  # player 13
    points[13] = rng.integers(0, 21, size=n)  # player 14
    return SquadSims(tuple(POS), points, np.ones((15, n), dtype=np.bool_))


def my_points_with_captain(sims: SquadSims, captain: int, limits: Limits) -> np.ndarray[Any, Any]:
    starters = (1, 3, 4, 5, 6, 8, 9, 10, 11, 13, 14)
    vice = 14 if captain == 13 else 13
    return score_lineup(sims, Lineup(starters, (2, 7, 12, 15), captain, vice), POS, limits)


def test_an_underdog_captains_the_differential(limits: Limits) -> None:
    """The opponent owns and captains 13 and is 5 points better elsewhere. Captaining 13 too
    cancels out, so I can never win; only the differential 14 gives a chance."""
    sims = two_captains(4000)
    opponent = my_points_with_captain(sims, 13, limits) + 5
    rec = recommend(sims, POS, limits, opponent)
    assert rec.best.lineup.captain == 14
    assert rec.best.p_target > 0.05


def test_a_favourite_captains_the_shared_player(limits: Limits) -> None:
    """Now I'm 5 points better elsewhere: matching their captain locks in the win."""
    sims = two_captains(4000)
    opponent = my_points_with_captain(sims, 13, limits) - 5
    rec = recommend(sims, POS, limits, opponent)
    assert rec.best.lineup.captain == 13
    assert rec.best.p_target == 1.0


def test_players_who_will_not_play_are_benched_and_never_captain(limits: Limits) -> None:
    """Keeper 1 and star forward 13 are out. Starting / captaining them would score the same
    (auto-sub, vice-captain), but the recommendation shouldn't read that way."""
    sims = two_captains(2000)
    played = sims.played.copy()
    points = sims.points.copy()
    played[[0, 12]] = False
    points[[0, 12]] = 0
    points[1] = 3  # keeper 2 plays and scores
    sims = SquadSims(sims.ids, points, played)
    rec = recommend(sims, POS, limits, np.zeros(2000, dtype=np.int64))
    lineup = rec.best.lineup
    assert 2 in lineup.starters and 1 in lineup.bench
    assert lineup.captain != 13
    assert lineup_violations(lineup, POS, limits) == []


def test_recommendation_respects_the_guard_and_reports_each_buffer(limits: Limits) -> None:
    sims = random_sims(2000, 4)
    opponent = np.random.default_rng(5).poisson(55, size=2000).astype(np.int64)
    rec = recommend(sims, POS, limits, opponent)
    assert rec.best.expected >= rec.max_expected.expected - 1.0
    assert sorted(rec.by_buffer) == [1, 2, 3, 4, 5]
    assert rec.by_buffer[3].lineup == rec.best.lineup
    assert all(c.buffer == b for b, c in rec.by_buffer.items())
    assert lineup_violations(rec.best.lineup, POS, limits) == []
