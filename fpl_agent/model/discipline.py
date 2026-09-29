"""Phase 2 step 5a: yellow and red cards, and goalkeeper penalty saves, per player per match.

Small but not negligible (docs/decisions.md D22): yellows cost outfield players ~0.2 points a
match on average; penalty saves are worth ~0.1 a match to a goalkeeper. Each is a per-player
rate per 90 (card-prone players are a real trait), shrunk towards the position mean by
PRIOR_MINUTES (not yet tested out of sample), scaled by minutes played.

- Yellow / red: yes/no per match with probability 1 - exp(-rate * minutes / 90). A red card
  clears the yellow: FPL scores a second-yellow dismissal as -3 only.
- Penalties saved: Poisson(rate * minutes / 90), goalkeepers only.
Penalty misses (~-0.04 a match even for forwards) and own goals (~-0.02) are left out.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import Player
from fpl_agent.model.attack import PRIOR_MINUTES
from fpl_agent.model.minutes import FULL_MATCH, MinutesSamples

GOALKEEPER = 1  # element_type


@dataclass(frozen=True)
class DisciplineRates:
    yellow90: dict[int, float]
    red90: dict[int, float]
    pens_saved90: dict[int, float]  # goalkeepers only


def discipline_rates(players: list[Player]) -> DisciplineRates:
    by_pos: dict[int, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.element_type, []).append(p)

    def shrunk(field: str) -> dict[int, float]:
        out = {}
        for ps in by_pos.values():
            minutes = sum(p.minutes for p in ps)
            mean = sum(getattr(p, field) for p in ps) / (minutes / 90) if minutes else 0.0
            for p in ps:
                total = getattr(p, field)
                out[p.id] = (total + mean * PRIOR_MINUTES / 90) / ((p.minutes + PRIOR_MINUTES) / 90)
        return out

    pens = shrunk("penalties_saved")
    return DisciplineRates(
        yellow90=shrunk("yellow_cards"),
        red90=shrunk("red_cards"),
        pens_saved90={p.id: pens[p.id] for p in players if p.element_type == GOALKEEPER},
    )


@dataclass(frozen=True)
class DisciplineSamples:
    """Aligned with MinutesSamples rows; arrays are (rows, n_sims)."""

    yellow: NDArray[np.bool_]
    red: NDArray[np.bool_]
    pens_saved: NDArray[np.int64]


def simulate_discipline(
    minutes: MinutesSamples, rates: DisciplineRates, rng: np.random.Generator
) -> DisciplineSamples:
    share = minutes.minutes / FULL_MATCH
    players = [int(p) for p in minutes.player]

    def column(values: dict[int, float]) -> NDArray[np.float64]:
        return np.array([values.get(p, 0.0) for p in players], dtype=np.float64)[:, None]

    shape = minutes.minutes.shape
    red = rng.random(shape) < 1 - np.exp(-column(rates.red90) * share)
    yellow = (rng.random(shape) < 1 - np.exp(-column(rates.yellow90) * share)) & ~red
    pens_saved = rng.poisson(column(rates.pens_saved90) * share).astype(np.int64)
    return DisciplineSamples(yellow=yellow, red=red, pens_saved=pens_saved)
