"""Phase 2 step 5b: FPL points from simulated events. Pure arithmetic, no randomness.

Point VALUES come from game_config.scoring via Bootstrap.points_for (rule 8); only thresholds
come from scoring_rules.py. Per player-match:
  appearance (short_play 1-59', long_play 60'+), goals, assists, clean sheet, goals conceded
  (per 2, rounded down), saves (per 3, rounded down), DEFCON award, yellow, red, penalty saves.
plus bonus (step 6). A double gameweek sums both matches; a blank gameweek is 0.

Output (docs/decisions.md D22): a (players x sims) points matrix, which Phase 3 needs in full
(captaincy doubles a whole distribution, H2H compares totals); whether the player got any
minutes (bench auto-subs trigger on 0 minutes, rule 2); and each player's MEAN points per
component, to explain a projection. Keeping every component for every simulation would be
~400 MB; the total alone is ~27 MB.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.bonus import BonusSamples
from fpl_agent.model.defence import DefenceSamples
from fpl_agent.model.discipline import DisciplineSamples
from fpl_agent.model.minutes import MinutesSamples
from fpl_agent.model.scoring_rules import (
    GOALS_CONCEDED_PER_POINT,
    LONG_PLAY_MINUTES,
    SAVES_PER_POINT,
)

COMPONENTS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "goals_conceded",
    "saves",
    "defcon",
    "cards",
    "pens_saved",
    "bonus",
)


@dataclass(frozen=True)
class PointsSamples:
    player: NDArray[np.int64]  # (players,) sorted ids; only players with at least one match
    total: NDArray[np.int32]  # (players, n_sims)
    played: NDArray[np.bool_]  # (players, n_sims): any minutes in the gameweek
    breakdown: dict[str, NDArray[np.float64]]  # component -> (players,) mean points

    def index(self, player_id: int) -> int:
        idx = np.flatnonzero(self.player == player_id)
        if idx.size == 0:
            raise KeyError(f"player {player_id} has no match this gameweek")
        return int(idx[0])

    def expected(self) -> NDArray[np.float64]:
        return self.total.mean(axis=1)


def row_components(
    bootstrap: Bootstrap,
    positions: Mapping[int, PositionCode],
    minutes: MinutesSamples,
    attack: AttackSamples,
    defence: DefenceSamples,
    discipline: DisciplineSamples,
    bonus: BonusSamples,
) -> dict[str, NDArray[np.int64]]:
    """Points per component for every player-match row: each array is (rows, n_sims)."""
    pos = [positions[int(p)] for p in minutes.player]

    def value(action: str) -> NDArray[np.int64]:
        return np.array([bootstrap.points_for(action, p) for p in pos], dtype=np.int64)[:, None]

    m = minutes.minutes
    appearance = np.where(
        m >= LONG_PLAY_MINUTES, value("long_play"), np.where(m > 0, value("short_play"), 0)
    )
    return {
        "appearance": appearance.astype(np.int64),
        "goals": attack.goals * value("goals_scored"),
        "assists": attack.assists * value("assists"),
        "clean_sheet": defence.clean_sheet * value("clean_sheets"),
        "goals_conceded": (defence.goals_conceded // GOALS_CONCEDED_PER_POINT)
        * value("goals_conceded"),
        "saves": (defence.saves // SAVES_PER_POINT) * value("saves"),
        "defcon": defence.defcon_award * value("defensive_contribution"),
        "cards": discipline.yellow * value("yellow_cards") + discipline.red * value("red_cards"),
        "pens_saved": discipline.pens_saved * value("penalties_saved"),
        "bonus": bonus.bonus * value("bonus"),
    }


def compute_points(
    bootstrap: Bootstrap,
    positions: Mapping[int, PositionCode],
    minutes: MinutesSamples,
    attack: AttackSamples,
    defence: DefenceSamples,
    discipline: DisciplineSamples,
    bonus: BonusSamples,
) -> PointsSamples:
    """Sum components over each player's matches (doubles add up; blanks have no rows)."""
    comps = row_components(bootstrap, positions, minutes, attack, defence, discipline, bonus)
    players, row_to_player = np.unique(minutes.player, return_inverse=True)
    n_players, n_sims = len(players), minutes.minutes.shape[1]

    def per_player(rows: NDArray[np.int64]) -> NDArray[np.int64]:
        out = np.zeros((n_players, n_sims), dtype=np.int64)
        np.add.at(out, row_to_player, rows)
        return out

    total = np.zeros((n_players, n_sims), dtype=np.int64)
    breakdown = {}
    for name in COMPONENTS:
        summed = per_player(comps[name])
        total += summed
        breakdown[name] = summed.mean(axis=1)
    played = per_player(minutes.minutes) > 0
    return PointsSamples(
        player=players.astype(np.int64),
        total=total.astype(np.int32),
        played=played,
        breakdown=breakdown,
    )
