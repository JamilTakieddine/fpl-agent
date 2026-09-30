"""Phase 3 part 3: the H2H opponent, scored in the same simulations as my team.

(docs/decisions.md D30) Their picks for the coming gameweek are hidden until the deadline, so:
- their team sheet is last gameweek's, with FPL's auto-subs undone (a Free Hit week is skipped
  upstream, in data/opponent.py, because that squad has reverted);
- their captain is uncertain: a probability spread mixing habit (who they captained recently) and
  this week's expected points, fitted on real H2H-league picks;
- a Triple Captain or Bench Boost they still hold may be played in a double gameweek, or in the
  last gameweeks before the chip is forfeited;
- in each simulated gameweek the captain and chip are DRAWN from those spreads (not averaged:
  averaging would hide their big weeks, and win probability lives in the tails).
An odd-sized league pairs someone with "AVERAGE" each week: that is FPL's overall average score
for the gameweek (checked against the league's real results), modelled from ownership.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.calendar import Calendar
from fpl_agent.data.models import ChipDefinition, EntryPicks, Player, PositionCode
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.rules import Limits, Lineup, chip_window
from fpl_agent.optimize.scoring import SquadSims, score_lineup

# Fitted on the H2H league's real GW2-5 captain picks (44 decisions; scripts/fit_opponent.py).
# The likelihood is flat for habit weights 0.7-0.9: habit dominates, the exact value matters little.
HABIT_WEIGHT = 0.85  # share of the probability that follows recent captaincy
CAPTAIN_TEMPERATURE = 1.0  # points: how sharply the rest favours the top expected scorer
MIN_CAPTAIN_PROB = 0.01  # captains below this are dropped (then renormalised): keeps it cheap

# Chip chances (D30, agreed as starting points, to be checked against what opponents really do).
SCORING_CHIPS = ("3xc", "bboost")  # the chips that change this gameweek's score from their squad
CHIP_FLOOR = 0.30  # chance in a chip-worthy gameweek, at least
CHIP_CAP = 0.80  # never a certainty: people forget, or save it
FORFEIT_WEEKS = 2  # last gameweeks of a chip's window: a held chip may be played in ANY week

# FPL's overall average ~= this x sum(ownership x points): fitted on GW2-5 (0.906, within ~3 pts).
AVERAGE_SCALE = 0.9


# --- their team sheet ----------------------------------------------------------------------------


def team_sheet(picks: EntryPicks, limits: Limits) -> Lineup:
    """The team sheet they actually set: FPL returns the lineup AFTER its auto-subs (D28), so
    swap each substitution back, latest first."""
    order = [p.element for p in sorted(picks.picks, key=lambda p: p.position)]
    for sub in reversed(picks.automatic_subs):
        i, j = order.index(sub.element_in), order.index(sub.element_out)
        order[i], order[j] = order[j], order[i]
    captain = next(p.element for p in picks.picks if p.is_captain)
    vice = next(p.element for p in picks.picks if p.is_vice_captain)
    return Lineup(tuple(order[: limits.starters]), tuple(order[limits.starters :]), captain, vice)


# --- captain -------------------------------------------------------------------------------------


def captain_probabilities(
    starters: Sequence[int],
    expected_points: Mapping[int, float],
    history: Sequence[int],
    habit_weight: float = HABIT_WEIGHT,
    temperature: float = CAPTAIN_TEMPERATURE,
    *,
    availability: Mapping[int, float] | None = None,
) -> dict[int, float]:
    """P(each starter wears the armband). `history`: their recent captains, any order.

    habit_weight x (share of recent captaincies) + the rest x softmax(expected points /
    temperature). With no recent captain among the starters, only the second part is left.

    `availability` (chance of playing, 0-1, from FPL's flags; missing = 1) scales habit: nobody
    captains a player ruled out, and a doubtful one keeps only part of his habit share. The
    share he loses goes to the expected-points part (not fitted: GW2-5 had almost no doubtful
    habitual captains; D30).
    """
    x = np.array([expected_points.get(p, 0.0) for p in starters])
    z = np.exp((x - x.max()) / temperature)
    model = z / z.sum()
    habit = np.array([sum(1 for c in history if c == p) for p in starters], dtype=float)
    if habit.sum() == 0:
        return {p: float(q) for p, q in zip(starters, model, strict=True)}
    avail = np.array([(availability or {}).get(p, 1.0) for p in starters])
    from_habit = habit_weight * habit / habit.sum() * avail
    probs = from_habit + (1 - from_habit.sum()) * model
    return {p: float(q) for p, q in zip(starters, probs, strict=True)}


def availability(players: Iterable[Player]) -> dict[int, float]:
    """Chance of playing from FPL's flags, 0-1 (no flag = 1): what a manager sees when picking."""
    return {
        p.id: 1.0
        if p.chance_of_playing_next_round is None
        else p.chance_of_playing_next_round / 100
        for p in players
    }


@dataclass(frozen=True)
class CaptainCase:
    """One real captaincy decision, for fitting: what they could see, and who they chose."""

    starters: tuple[int, ...]
    expected_points: Mapping[int, float]
    history: tuple[int, ...]
    captain: int


def captain_log_likelihood(cases: Iterable[CaptainCase], w: float, temperature: float) -> float:
    """Average log-probability the model gave the captains they actually chose (higher = better)."""
    logs = [
        math.log(
            max(
                captain_probabilities(c.starters, c.expected_points, c.history, w, temperature)[
                    c.captain
                ],
                1e-12,
            )
        )
        for c in cases
    ]
    return sum(logs) / len(logs)


WEIGHT_GRID = tuple(i / 20 for i in range(21))
TEMPERATURE_GRID = (0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0)


def fit_captain(
    cases: Sequence[CaptainCase],
    weights: Sequence[float] = WEIGHT_GRID,
    temperatures: Sequence[float] = TEMPERATURE_GRID,
) -> tuple[float, float, float]:
    """Maximum likelihood over a grid: (habit weight, temperature, average log-likelihood). A grid
    rather than an optimiser: two parameters, and the whole surface can be printed and eyeballed."""
    return max(
        ((w, t, captain_log_likelihood(cases, w, t)) for w in weights for t in temperatures),
        key=lambda r: r[2],
    )


# --- chips ---------------------------------------------------------------------------------------


def chip_probabilities(
    event: int,
    chips_remaining: Iterable[str],
    definitions: Sequence[ChipDefinition],
    calendar: Calendar,
) -> dict[str, float]:
    """P(they play each scoring chip this gameweek).

    A held Triple Captain / Bench Boost is only a threat in a "chip-worthy" gameweek: a double
    gameweek, or one of the last FORFEIT_WEEKS of the chip's window (use it or lose it). Someone
    holding a chip spreads it over the chip-worthy weeks left, so the chance is 1 / (those weeks,
    this one included), kept between CHIP_FLOOR and CHIP_CAP: it rises as the window closes. One
    chip per gameweek, so the chances together are capped too.
    """
    out: dict[str, float] = {}
    for name in SCORING_CHIPS:
        window = chip_window(name, event, definitions)
        if name not in chips_remaining or window is None:
            continue
        worthy = [
            gw
            for gw in range(event, window.stop_event + 1)
            if gw > window.stop_event - FORFEIT_WEEKS
            or (gw in calendar.gameweeks and calendar.get(gw).is_double)
        ]
        if event in worthy:
            out[name] = min(CHIP_CAP, max(CHIP_FLOOR, 1 / len(worthy)))
    total = sum(out.values())
    if total > CHIP_CAP:
        out = {k: v * CHIP_CAP / total for k, v in out.items()}
    return out


# --- scoring the opponent ------------------------------------------------------------------------


@dataclass(frozen=True)
class OpponentModel:
    lineup: Lineup  # their team sheet; captain/vice = the two most likely captains
    captain_probs: dict[int, float]  # over their starters, sums to 1
    chip_probs: dict[str, float]  # "3xc" / "bboost" -> chance this gameweek (may be empty)


def build_opponent_model(
    picks: EntryPicks,
    captain_history: Sequence[int],
    expected_points: Mapping[int, float],
    chip_probs: dict[str, float],
    limits: Limits,
    chance_of_playing: Mapping[int, float] | None = None,
) -> OpponentModel:
    sheet = team_sheet(picks, limits)
    probs = captain_probabilities(
        sheet.starters, expected_points, captain_history, availability=chance_of_playing
    )
    ranked = sorted(probs, key=lambda p: -probs[p])
    return OpponentModel(
        replace(sheet, captain=ranked[0], vice_captain=ranked[1]), probs, chip_probs
    )


def opponent_points(
    sims: SquadSims,
    model: OpponentModel,
    positions: Mapping[int, PositionCode],
    limits: Limits,
    rng: np.random.Generator,
) -> NDArray[np.int64]:
    """Their points in each simulation, with the captain and chip drawn per simulation.

    Each (captain, chip) combination is scored with score_lineup (the same exact rules as my
    team) and each simulation takes the one it drew. The vice-captain is the most likely other
    captain. Compute this ONCE per run and compare every candidate lineup of mine against it.
    """
    ranked = sorted(model.captain_probs, key=lambda p: -model.captain_probs[p])
    captains = [p for p in ranked if model.captain_probs[p] >= MIN_CAPTAIN_PROB] or ranked[:1]
    cap_p = np.array([model.captain_probs[p] for p in captains])
    chips: list[str | None] = [None, *model.chip_probs]
    chip_p = np.array([1 - sum(model.chip_probs.values()), *model.chip_probs.values()])
    n = sims.points.shape[1]
    cap_draw = rng.choice(np.arange(len(captains)), size=n, p=cap_p / cap_p.sum())
    chip_draw = rng.choice(np.arange(len(chips)), size=n, p=chip_p)

    out = np.zeros(n, dtype=np.int64)
    for ci, captain in enumerate(captains):
        vice = next(p for p in ranked if p != captain)
        lineup = replace(model.lineup, captain=captain, vice_captain=vice)
        for k, chip in enumerate(chips):
            drawn = (cap_draw == ci) & (chip_draw == k)
            if drawn.any():
                out[drawn] = score_lineup(sims, lineup, positions, limits, chip)[drawn]
    return out


# --- the league "AVERAGE" opponent ---------------------------------------------------------------


def template_points(samples: PointsSamples, ownership: Mapping[int, float]) -> NDArray[np.float64]:
    """Sum over every player of (share of FPL squads owning him x his points), per simulation."""
    own = np.array([ownership.get(int(p), 0.0) for p in samples.player]) / 100
    result: NDArray[np.float64] = own @ samples.total
    return result


def average_points(
    samples: PointsSamples, ownership: Mapping[int, float], scale: float = AVERAGE_SCALE
) -> NDArray[np.int64]:
    """FPL's overall average score in each simulation (a whole number, as FPL reports it).

    The average manager's team is the ownership-weighted template; `scale` absorbs what ownership
    alone misses (benched players score nothing, captains score double, hits), fitted on real
    gameweeks.
    """
    return np.rint(scale * template_points(samples, ownership)).astype(np.int64)


def fit_average_scale(template: Sequence[float], actual: Sequence[int]) -> float:
    """Least squares through the origin: the scale that best maps template points (computed from
    each gameweek's REAL points and its pre-deadline ownership) to FPL's reported average."""
    t, y = np.asarray(template, dtype=float), np.asarray(actual, dtype=float)
    return float(t @ y / (t @ t))
