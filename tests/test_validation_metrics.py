# Phase 2 step 7b: back-test scoring rules (Brier and skill, calibration bins, Spearman with ties,
# points summaries, PIT histogram, 1X2 Brier, Poisson score likelihood) and the Kalshi price
# rebuild from candles; pure functions, no network.

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from fpl_agent.data.kalshi import Candle, KalshiMarket
from fpl_agent.validation.kalshi_history import as_of, load_candles
from fpl_agent.validation.metrics import (
    brier,
    brier_skill,
    calibration,
    log_loss,
    multiclass_brier,
    pit_histogram,
    points_summary,
    poisson_score_log_likelihood,
    spearman,
)


def test_brier_and_skill() -> None:
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    # Predicting the base rate exactly has zero skill; perfect predictions have skill 1.
    assert brier_skill([0.5, 0.5, 0.5, 0.5], [1, 0, 1, 0]) == pytest.approx(0.0)
    assert brier_skill([1, 0, 1, 0], [1, 0, 1, 0]) == pytest.approx(1.0)


def test_log_loss_is_finite_at_extremes() -> None:
    assert math.isfinite(log_loss([0.0, 1.0], [1, 0]))


def test_calibration_bins() -> None:
    bins = calibration([0.02, 0.04, 0.95, 0.95], [0, 0, 1, 0], edges=(0, 0.1, 1.0))
    assert [(b.count, b.observed) for b in bins] == [(2, 0.0), (2, 0.5)]
    assert bins[1].mean_predicted == pytest.approx(0.95)


def test_spearman_handles_ties_and_constants() -> None:
    assert spearman([1, 2, 3], [10, 20, 30]) == pytest.approx(1.0)
    assert spearman([1, 2, 3], [30, 20, 10]) == pytest.approx(-1.0)
    assert spearman([1, 1, 2], [1, 1, 2]) == pytest.approx(1.0)
    assert math.isnan(spearman([1, 2, 3], [5, 5, 5]))


def test_points_summary_averages_rank_correlation_per_gameweek() -> None:
    s = points_summary([1, 1, 2, 2], [1.0, 2.0, 2.0, 1.0], [0, 5, 0, 5])
    assert s.spearman_within_gw == pytest.approx(0.0)  # +1 in GW1, -1 in GW2
    assert s.bias == pytest.approx(1.5 - 2.5)
    assert s.mae == pytest.approx((1 + 3 + 2 + 4) / 4)


def test_pit_histogram_shares() -> None:
    hist = pit_histogram([0.05, 0.15, 0.15, 0.95], bins=10)
    assert hist[0] == 0.25 and hist[1] == 0.5 and hist[9] == 0.25 and sum(hist) == pytest.approx(1)


def test_multiclass_brier() -> None:
    assert multiclass_brier([(1, 0, 0)], [0]) == 0.0
    assert multiclass_brier([(1 / 3, 1 / 3, 1 / 3)], [1]) == pytest.approx(2 / 3)


def test_poisson_score_log_likelihood() -> None:
    expected = math.log(math.exp(-1.5) * 1.5**2 / 2) + math.log(math.exp(-1.0))
    assert poisson_score_log_likelihood(1.5, 1.0, 2, 0) == pytest.approx(expected)


# --- Kalshi price rebuild ------------------------------------------------------------------------

T = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def candle(hours: int, bid: float | None, ask: float | None, vol: float) -> Candle:
    return Candle.model_validate(
        {
            "end_period_ts": int((T + timedelta(hours=hours)).timestamp()),
            "yes_bid": {"close_dollars": bid},
            "yes_ask": {"close_dollars": ask},
            "volume_fp": vol,
        }
    )


def market() -> KalshiMarket:
    return KalshiMarket(ticker="X", event_ticker="E", yes_sub_title="Arsenal", status="finalized")


def test_as_of_takes_last_quote_before_cutoff_and_cumulative_volume() -> None:
    candles = [candle(0, 0.40, 0.42, 100), candle(1, None, None, 50), candle(2, 0.60, 0.62, 900)]
    m = as_of(market(), candles, T + timedelta(hours=1, minutes=30))
    assert (m.yes_bid_dollars, m.yes_ask_dollars) == (
        0.40,
        0.42,
    )  # hour-2 candle is after the cutoff
    assert m.volume_fp == 150  # volume up to the cutoff, including the unquoted hour
    assert m.status == "active"


def test_as_of_before_any_trading_has_no_quote() -> None:
    m = as_of(market(), [candle(5, 0.5, 0.52, 10)], T)
    assert m.yes_bid_dollars is None and m.volume_fp == 0


def test_candles_round_trip(tmp_path: Path) -> None:
    (tmp_path / "X.json").write_text(json.dumps([candle(0, 0.4, 0.42, 10).model_dump(mode="json")]))
    assert load_candles("X", tmp_path)[0].yes_ask.close_dollars == 0.42
    assert load_candles("missing", tmp_path) == []
