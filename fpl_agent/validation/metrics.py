"""Scoring rules for the back-test (docs/decisions.md D25).

- Brier score: mean (p - outcome)^2 for yes/no events; lower is better. Compared against the
  "climatology" baseline (always predicting the overall rate), the skill score says how much
  better than knowing only the base rate we are: 1 = perfect, 0 = no better, < 0 = worse.
- Calibration: bin predictions and compare mean predicted vs observed frequency per bin.
- Points: MAE, RMSE, bias; Spearman rank correlation WITHIN each gameweek (what picking and
  captaincy depend on), averaged over gameweeks.
- PIT (probability integral transform): where each actual score falls in its predicted
  distribution. Points are whole numbers, so the RANDOMIZED PIT is used (a uniform draw within
  the jump at the actual score); if the distributions are honest, PIT values are uniform on
  [0, 1]. A U shape means too narrow (overconfident), a hump means too wide.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


def _arr(xs: Sequence[float]) -> NDArray[np.float64]:
    return np.asarray(xs, dtype=np.float64)


def brier(p: Sequence[float], y: Sequence[float]) -> float:
    return float(np.mean((_arr(p) - _arr(y)) ** 2))


def brier_skill(p: Sequence[float], y: Sequence[float]) -> float:
    """1 - Brier / Brier of always predicting the observed base rate."""
    yy = _arr(y)
    reference = float(np.mean((yy.mean() - yy) ** 2))
    return 1.0 - brier(p, y) / reference if reference > 0 else 0.0


def log_loss(p: Sequence[float], y: Sequence[float], eps: float = 1e-6) -> float:
    pp = np.clip(_arr(p), eps, 1 - eps)
    yy = _arr(y)
    return float(-np.mean(yy * np.log(pp) + (1 - yy) * np.log(1 - pp)))


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed: float


def calibration(
    p: Sequence[float],
    y: Sequence[float],
    edges: Sequence[float] = (0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
) -> list[CalibrationBin]:
    pp, yy = _arr(p), _arr(y)
    bins = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        mask = (pp >= lo) & ((pp < hi) if hi < 1.0 else (pp <= hi))
        if mask.any():
            bins.append(
                CalibrationBin(
                    lo, hi, int(mask.sum()), float(pp[mask].mean()), float(yy[mask].mean())
                )
            )
    return bins


def _ranks(x: NDArray[np.float64]) -> NDArray[np.float64]:
    """Average ranks (ties share their mean rank)."""
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(len(x), dtype=np.float64)
    ranks[order] = np.arange(len(x), dtype=np.float64)
    for value in np.unique(x):
        tied = x == value
        if tied.sum() > 1:
            ranks[tied] = ranks[tied].mean()
    return ranks


def spearman(pred: Sequence[float], actual: Sequence[float]) -> float:
    a, b = _ranks(_arr(pred)), _ranks(_arr(actual))
    if a.std() == 0 or b.std() == 0:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


@dataclass(frozen=True)
class PointsSummary:
    n: int
    mae: float
    rmse: float
    bias: float  # mean predicted - mean actual
    spearman_within_gw: float  # averaged over gameweeks


def points_summary(
    gws: Sequence[int], pred: Sequence[float], actual: Sequence[float]
) -> PointsSummary:
    p, a, g = _arr(pred), _arr(actual), np.asarray(gws)
    per_gw = [spearman(p[g == gw], a[g == gw]) for gw in np.unique(g)]
    per_gw = [x for x in per_gw if not np.isnan(x)]
    return PointsSummary(
        n=len(p),
        mae=float(np.mean(np.abs(p - a))),
        rmse=float(np.sqrt(np.mean((p - a) ** 2))),
        bias=float(p.mean() - a.mean()),
        spearman_within_gw=float(np.mean(per_gw)) if per_gw else float("nan"),
    )


def pit_histogram(pit: Sequence[float], bins: int = 10) -> list[float]:
    """Share of PIT values per equal-width bin (each should be ~1/bins if honest)."""
    counts, _ = np.histogram(_arr(pit), bins=bins, range=(0.0, 1.0))
    return [float(c) / max(len(pit), 1) for c in counts]


def poisson_score_log_likelihood(
    lam_h: float, lam_a: float, h: int, a: int, rho: float = 0.0
) -> float:
    """Log-probability of the exact score (independent Poisson, Dixon-Coles adjusted if rho)."""

    def lp(lam: float, k: int) -> float:
        return float(-lam + k * np.log(lam) - np.sum(np.log(np.arange(1, k + 1))))

    tau = {
        (0, 0): 1 - lam_h * lam_a * rho,
        (0, 1): 1 + lam_h * rho,
        (1, 0): 1 + lam_a * rho,
        (1, 1): 1 - rho,
    }.get((h, a), 1.0)
    return lp(lam_h, h) + lp(lam_a, a) + float(np.log(tau))


def multiclass_brier(probs: Sequence[Sequence[float]], outcome_index: Sequence[int]) -> float:
    """Brier for home/draw/away: mean over matches of sum_k (p_k - 1[k = outcome])^2."""
    p = np.asarray(probs, dtype=np.float64)
    y = np.zeros_like(p)
    y[np.arange(len(p)), np.asarray(outcome_index)] = 1.0
    return float(np.mean(np.sum((p - y) ** 2, axis=1)))


def group_by(keys: Sequence[str], values: Sequence[float]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = defaultdict(list)
    for k, v in zip(keys, values, strict=True):
        out[k].append(v)
    return dict(out)
