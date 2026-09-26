# Saved Kalshi odds: merging runs (newer wins, unpriced fixtures keep their last price), no in-play
# saves, ignoring prices for a rescheduled kickoff, and live > saved > xG precedence; no network.

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from fpl_agent.data.models import Fixture
from fpl_agent.data.odds import MatchOdds
from fpl_agent.data.odds_store import FileOddsStore, record_odds, usable_saved_odds
from fpl_agent.model.scoreline import RatingsModel, fixture_rates
from tests.conftest import T0, fx

KICKOFF = T0 + timedelta(hours=1.5)  # fx() kicks GW1 off 1.5h after T0


def odds(fid: int, lam_h: float = 1.8) -> MatchOdds:
    return MatchOdds(fid, 0.5, 0.25, 0.25, lam_h, 1.0, 0.0, 5000, 0.02, "totals", 3, -0.01)


def fixtures() -> list[Fixture]:
    return [fx(1, 1, 1, 2), fx(2, 1, 3, 4)]


def test_round_trip(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    assert record_odds(store, 1, {1: odds(1)}, fixtures(), T0 - timedelta(days=3)) == 1
    saved = store.load(1)
    assert store.path(1).name == "odds_gw01.json"
    assert saved[1].to_match_odds() == odds(1)
    assert saved[1].kickoff == KICKOFF


def test_unpriced_fixture_keeps_its_last_price(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    record_odds(store, 1, {1: odds(1), 2: odds(2)}, fixtures(), T0 - timedelta(days=5))
    # Next run: fixture 2's market is gone; fixture 1 is re-priced.
    record_odds(store, 1, {1: odds(1, lam_h=2.1)}, fixtures(), T0 - timedelta(days=1))
    saved = store.load(1)
    assert saved[1].lambda_home == 2.1
    assert saved[2].lambda_home == 1.8
    assert saved[2].taken_at == T0 - timedelta(days=5)


def test_older_run_never_overwrites_newer_price(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    record_odds(store, 1, {1: odds(1, lam_h=2.1)}, fixtures(), T0 - timedelta(days=1))
    assert record_odds(store, 1, {1: odds(1, lam_h=1.2)}, fixtures(), T0 - timedelta(days=4)) == 0
    assert store.load(1)[1].lambda_home == 2.1


def test_no_in_play_prices(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    assert record_odds(store, 1, {1: odds(1)}, fixtures(), KICKOFF + timedelta(minutes=5)) == 0
    assert store.load(1) == {}


def test_naive_datetime_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        record_odds(FileOddsStore(tmp_path), 1, {}, fixtures(), T0.replace(tzinfo=None))


def test_price_for_a_rescheduled_kickoff_is_ignored(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    record_odds(store, 1, {1: odds(1), 2: odds(2)}, fixtures(), T0 - timedelta(days=3))
    moved = fixtures()[0].model_copy(update={"kickoff_time": KICKOFF + timedelta(days=10)})
    usable = usable_saved_odds(store.load(1), [moved, fixtures()[1]])
    assert set(usable) == {2}


def test_precedence_live_then_saved_then_xg(tmp_path: Path) -> None:
    store = FileOddsStore(tmp_path)
    taken = T0 - timedelta(days=2)
    record_odds(store, 1, {2: odds(2, lam_h=1.4)}, fixtures(), taken)
    three = [*fixtures(), fx(3, 1, 1, 3)]
    model = RatingsModel(1.5, 1.2, dict.fromkeys(range(1, 5), 1.0), dict.fromkeys(range(1, 5), 1.0))

    rates = fixture_rates(
        three, {1: odds(1, lam_h=2.3)}, model, usable_saved_odds(store.load(1), three)
    )
    assert (rates[1].lambda_home, rates[1].source, rates[1].odds_as_of) == (
        2.3,
        "kalshi-totals",
        None,
    )
    assert (rates[2].lambda_home, rates[2].source, rates[2].odds_as_of) == (
        1.4,
        "kalshi-saved-totals",
        taken,
    )
    assert (rates[3].lambda_home, rates[3].source) == (1.5, "xg-ratings")
