"""Phase 2 step 6: bonus points from a simulated BPS (Bonus Points System) ranking per match.

(docs/decisions.md D23)
Simulated BPS per player-match = event BPS + base BPS x minutes / 90 + noise:
- Event BPS: weights x the events we simulate (FEATURES). The weights are FITTED by least squares
  on real per-match BPS from the same live data the lineup model fetches (refitted every run;
  DEFAULT_WEIGHTS from GW1-5 2026/27 when there's too little data). Fitted rather than FPL's
  published table because the job is predicting BPS FROM THE EVENTS WE SIMULATE: e.g. "60+
  minutes" fits high because it also absorbs the passing BPS regular starters earn.
- Base BPS: each player's average leftover (actual - event BPS) per full match, a real trait
  (players differ by ~2.8 BPS; a deep-lying playmaker racks up passing BPS). Shrunk towards his
  position mean by EMPIRICAL BAYES: keep factor = between-player variance / (between + within
  variance / matches), computed from the data, so the strength re-calibrates itself every run.
- Noise: Normal(0, within-player sd x sqrt(minutes / 90)).
BPS is rounded to whole numbers (ties happen, as in reality) and ranked per match among players
who played. Bonus by COMPETITION RANKING (rank = 1 + players with strictly higher BPS; rank 1 -> 3,
2 -> 2, 3 -> 1), which reproduces FPL's tie rules: two tied first both get 3 and the next gets 1;
a tie for second gives both 2 and nobody 1; ties for third all get 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.models import EventLive, LiveElement
from fpl_agent.model.attack import AttackSamples
from fpl_agent.model.defence import DefenceSamples
from fpl_agent.model.discipline import DisciplineSamples
from fpl_agent.model.minutes import FULL_MATCH, MinutesSamples

FEATURES = (
    "play_1_59",
    "play_60",
    "goal_gk_def",
    "goal_mid",
    "goal_fwd",
    "assist",
    "clean_sheet_gk_def",
    "save",
    "pen_saved",
    "conceded_gk_def",
    "yellow",
    "red",
    "defcon_action",
)
# Fitted on GW1-5 2026/27 (1,538 player-matches, R^2 0.88). Used when there's too little data.
DEFAULT_WEIGHTS = {
    "play_1_59": 3.05,
    "play_60": 9.17,
    "goal_gk_def": 13.92,
    "goal_mid": 20.93,
    "goal_fwd": 24.30,
    "assist": 11.17,
    "clean_sheet_gk_def": 12.82,
    "save": 2.85,
    "pen_saved": 10.13,
    "conceded_gk_def": -3.78,
    "yellow": -4.12,
    "red": -10.78,
    "defcon_action": 0.62,
}
DEFAULT_NOISE_SD = 3.4
MIN_FIT_ROWS = 500  # fewer real player-matches than this: use DEFAULT_WEIGHTS
GK, DEF, MID, FWD = 1, 2, 3, 4  # element_type


def live_features(el: LiveElement, element_type: int) -> dict[str, float]:
    """FEATURES for one real single-match appearance."""
    s, gk_def = el.stats, element_type in (GK, DEF)
    m = el.explain[0].minutes()
    return {
        "play_1_59": float(0 < m < 60),
        "play_60": float(m >= 60),
        "goal_gk_def": s.goals_scored * gk_def,
        "goal_mid": s.goals_scored * (element_type == MID),
        "goal_fwd": s.goals_scored * (element_type == FWD),
        "assist": s.assists,
        "clean_sheet_gk_def": s.clean_sheets * gk_def,
        "save": s.saves,
        "pen_saved": s.penalties_saved,
        "conceded_gk_def": s.goals_conceded * gk_def,
        "yellow": s.yellow_cards,
        "red": s.red_cards,
        "defcon_action": s.defensive_contribution,
    }


@dataclass(frozen=True)
class BpsModel:
    weights: dict[str, float]
    base: dict[int, float]  # player -> base BPS per full match (shrunk)
    noise_sd: float
    keep_factor: dict[int, float]  # player -> share of his own average kept (for reporting)


def fit_bps_model(lives: Mapping[int, EventLive], element_types: Mapping[int, int]) -> BpsModel:
    rows: list[tuple[int, int, list[float], float]] = []  # (player, minutes, features, bps)
    for live in lives.values():
        for el in live.elements:
            et = element_types.get(el.id)
            if et is None or len(el.explain) != 1 or el.explain[0].minutes() == 0:
                continue
            f = live_features(el, et)
            rows.append(
                (el.id, el.explain[0].minutes(), [f[k] for k in FEATURES], float(el.stats.bps))
            )

    if len(rows) < MIN_FIT_ROWS:
        return BpsModel(dict(DEFAULT_WEIGHTS), {}, DEFAULT_NOISE_SD, {})

    x = np.array([r[2] for r in rows])
    y = np.array([r[3] for r in rows])
    w, *_ = np.linalg.lstsq(x, y, rcond=None)
    residual = y - x @ w
    weights = dict(zip(FEATURES, (float(v) for v in w), strict=True))

    # Base BPS from full matches only (partial matches mix in the minutes effect).
    per_player: dict[int, list[float]] = {}
    for (pid, minutes, _, _), r in zip(rows, residual, strict=True):
        if minutes >= 60:
            per_player.setdefault(pid, []).append(float(r))
    multi = [v for v in per_player.values() if len(v) >= 2]
    within_var = (
        float(np.mean([np.var(v, ddof=1) for v in multi])) if multi else DEFAULT_NOISE_SD**2
    )

    base: dict[int, float] = {}
    keep: dict[int, float] = {}
    for et in set(element_types.values()):
        players = {p: v for p, v in per_player.items() if element_types.get(p) == et}
        if not players:
            continue
        means = {p: float(np.mean(v)) for p, v in players.items()}
        pos_mean = float(np.mean(list(means.values())))
        noise_of_means = float(np.mean([within_var / len(v) for v in players.values()]))
        between_var = max(float(np.var(list(means.values()))) - noise_of_means, 0.0)
        for p, v in players.items():
            k = between_var / (between_var + within_var / len(v)) if between_var > 0 else 0.0
            base[p] = pos_mean + k * (means[p] - pos_mean)
            keep[p] = k
        for p, t in element_types.items():  # players with no full match: the position mean
            if t == et and p not in base:
                base[p] = pos_mean
                keep[p] = 0.0
    return BpsModel(weights, base, float(np.sqrt(within_var)), keep)


@dataclass(frozen=True)
class BonusSamples:
    """Aligned with MinutesSamples rows; arrays are (rows, n_sims)."""

    bps: NDArray[np.int64]
    bonus: NDArray[np.int64]


def simulated_features(
    element_types: NDArray[np.int64],
    minutes: MinutesSamples,
    attack: AttackSamples,
    defence: DefenceSamples,
    discipline: DisciplineSamples,
) -> dict[str, NDArray[np.float64]]:
    """FEATURES from the simulated events; each is (rows, n_sims)."""
    et = element_types[:, None]
    gk_def = (et == GK) | (et == DEF)
    m = minutes.minutes
    goals = attack.goals.astype(np.float64)
    return {
        "play_1_59": ((m > 0) & (m < 60)).astype(np.float64),
        "play_60": (m >= 60).astype(np.float64),
        "goal_gk_def": goals * gk_def,
        "goal_mid": goals * (et == MID),
        "goal_fwd": goals * (et == FWD),
        "assist": attack.assists.astype(np.float64),
        "clean_sheet_gk_def": defence.clean_sheet * gk_def,
        "save": defence.saves.astype(np.float64),
        "pen_saved": discipline.pens_saved.astype(np.float64),
        "conceded_gk_def": defence.goals_conceded * gk_def,
        "yellow": discipline.yellow.astype(np.float64),
        "red": discipline.red.astype(np.float64),
        "defcon_action": defence.defcon_count.astype(np.float64),
    }


def bonus_from_bps(bps: NDArray[np.int64], eligible: NDArray[np.bool_]) -> NDArray[np.int64]:
    """Competition ranking within one match: bps and eligible are (players, n_sims)."""
    masked = np.where(eligible, bps, np.iinfo(np.int64).min)
    higher = (masked[None, :, :] > masked[:, None, :]).sum(axis=1)  # players strictly above
    bonus = np.select([higher == 0, higher == 1, higher == 2], [3, 2, 1], default=0)
    return np.where(eligible, bonus, 0).astype(np.int64)


def simulate_bonus(
    minutes: MinutesSamples,
    attack: AttackSamples,
    defence: DefenceSamples,
    discipline: DisciplineSamples,
    element_types: Mapping[int, int],
    model: BpsModel,
    rng: np.random.Generator,
) -> BonusSamples:
    types = np.array([element_types[int(p)] for p in minutes.player], dtype=np.int64)
    feats = simulated_features(types, minutes, attack, defence, discipline)
    event_bps = sum(model.weights[k] * feats[k] for k in FEATURES)
    share = minutes.minutes / FULL_MATCH
    base = np.array([model.base.get(int(p), 0.0) for p in minutes.player])[:, None]
    noise = rng.normal(0.0, 1.0, minutes.minutes.shape) * model.noise_sd * np.sqrt(share)
    bps = np.rint(event_bps + base * share + noise).astype(np.int64)

    eligible = minutes.minutes > 0
    bonus = np.zeros_like(bps)
    for fid in np.unique(minutes.fixture):
        rows = np.flatnonzero(minutes.fixture == fid)
        bonus[rows] = bonus_from_bps(bps[rows], eligible[rows])
    return BonusSamples(bps=bps, bonus=bonus)
