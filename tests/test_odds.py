# Kalshi match odds: price parsing, the Poisson expected-goals solver, totals-market fitting and
# its draw fallback, mapping onto FPL fixtures (aliases, home/away, dates), quality gates; offline.

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from fpl_agent.data.kalshi import KALSHI_API, KalshiClient, KalshiMarket, match_code
from fpl_agent.data.models import Bootstrap, Fixture
from fpl_agent.data.odds import (
    KALSHI_TO_FPL_NAME,
    MIN_VOLUME,
    build_odds,
    fpl_team_ids,
    outcome_probs,
    p_over,
    solve_lambdas,
    total_from_line,
    total_from_totals,
)
from tests.conftest import FakeResponse, FakeSession, load_fixture


def recorded_markets() -> list[KalshiMarket]:
    return [KalshiMarket.model_validate(m) for m in load_fixture("kalshi_markets")["markets"]]


def fixtures() -> list[Fixture]:
    return [Fixture.model_validate(f) for f in load_fixture("fixtures")]


# --- parsing ----------------------------------------------------------------------------------


def test_decimal_string_prices_parse_to_floats() -> None:
    m = KalshiMarket.model_validate(
        {
            "ticker": "T",
            "event_ticker": "E",
            "yes_sub_title": "Arsenal",
            "status": "active",
            "yes_bid_dollars": "0.7100",
            "yes_ask_dollars": "0.7300",
            "volume_fp": "5715.92",
        }
    )
    assert m.mid() == pytest.approx(0.72)
    assert m.spread() == pytest.approx(0.02)
    assert m.volume_fp == pytest.approx(5715.92)


def test_one_sided_or_zero_quotes_have_no_mid() -> None:
    base = {"ticker": "T", "event_ticker": "E", "yes_sub_title": "X", "status": "active"}
    assert KalshiMarket.model_validate({**base, "yes_bid_dollars": "0.5"}).mid() is None
    zero = {**base, "yes_bid_dollars": "0", "yes_ask_dollars": "0.02"}
    assert KalshiMarket.model_validate(zero).mid() is None


# --- Poisson solver ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("p_home", "p_draw"),
    [(0.72, 0.175), (0.33, 0.27), (0.45, 0.28), (0.15, 0.22), (0.85, 0.10)],
)
def test_solver_reproduces_market_probabilities(p_home: float, p_draw: float) -> None:
    lam_h, lam_a = solve_lambdas(p_home, p_draw)
    h, d, a = outcome_probs(lam_h, lam_a)
    assert (h, d, a) == pytest.approx((p_home, p_draw, 1 - p_home - p_draw), abs=1e-6)


def test_outcome_probabilities_sum_to_one() -> None:
    assert sum(outcome_probs(1.7, 1.1)) == pytest.approx(1.0)


def test_solver_is_symmetric() -> None:
    lam_h, lam_a = solve_lambdas(0.55, 0.25)
    rev_h, rev_a = solve_lambdas(0.20, 0.25)  # same match seen from the other side
    assert (rev_h, rev_a) == pytest.approx((lam_a, lam_h), abs=1e-6)


def test_more_draws_means_fewer_goals() -> None:
    low = sum(solve_lambdas(0.40, 0.22))
    high = sum(solve_lambdas(0.37, 0.30))
    assert high < low


def test_favourite_gets_more_expected_goals() -> None:
    lam_h, lam_a = solve_lambdas(0.72, 0.175)
    assert lam_h > 2 * lam_a


# --- mapping and quality gate ---------------------------------------------------------------


def test_every_alias_points_at_a_real_fpl_team(bootstrap_json: Any) -> None:
    fpl_names = {t["name"] for t in bootstrap_json["teams"]}
    assert set(KALSHI_TO_FPL_NAME.values()) <= fpl_names


def test_recorded_gw6_markets_map_to_fixtures(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    odds, skipped = build_odds(recorded_markets(), fixtures(), boot)
    assert odds, "at least one liquid match expected in the recording"
    by_id = {f.id: f for f in fixtures()}
    for fid, o in odds.items():
        assert by_id[fid].event == 6
        assert o.p_home + o.p_draw + o.p_away == pytest.approx(1.0)
        assert 0.3 < o.lambda_home + o.lambda_away < 6
    # Everything is accounted for: mapped or skipped with a reason.
    events = {m.event_ticker for m in recorded_markets()}
    assert len(odds) + len(skipped) == len(events)


def test_home_and_away_come_from_fpl_not_kalshi_order(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    odds, _ = build_odds(recorded_markets(), fixtures(), boot)
    ids = fpl_team_ids(boot)
    by_id = {f.id: f for f in fixtures()}
    for fid, o in odds.items():
        f = by_id[fid]
        legs = [m for m in recorded_markets() if m.yes_sub_title != "Tie"]
        home_leg = next(m for m in legs if ids.get(m.yes_sub_title) == f.team_h)
        away_leg = next(
            m
            for m in legs
            if ids.get(m.yes_sub_title) == f.team_a and m.event_ticker == home_leg.event_ticker
        )
        home_mid, away_mid = home_leg.mid(), away_leg.mid()
        assert home_mid is not None and away_mid is not None
        assert (o.p_home > o.p_away) == (home_mid > away_mid)


def market(event: str, outcome: str, bid: float, ask: float, vol: float, when: Any) -> KalshiMarket:
    return KalshiMarket(
        ticker=f"{event}-{outcome}",
        event_ticker=event,
        yes_sub_title=outcome,
        status="active",
        occurrence_datetime=when,
        yes_bid_dollars=bid,
        yes_ask_dollars=ask,
        volume_fp=vol,
    )


def synthetic_event(
    boot: Bootstrap, home: str, away: str, when: Any, **override: float
) -> list[KalshiMarket]:
    """A liquid three-way market for a fixture, with optional overrides (e.g. spread)."""
    vol = override.get("vol", 5000.0)
    spread = override.get("spread", 0.02)
    return [
        market("EV", home, 0.50, 0.50 + spread, vol, when),
        market("EV", "Tie", 0.25, 0.25 + spread, vol, when),
        market("EV", away, 0.23, 0.23 + spread, vol, when),
    ]


def first_gw6(boot: Bootstrap) -> tuple[Fixture, str, str]:
    f = next(f for f in fixtures() if f.event == 6)
    names = {t.id: t.name for t in boot.teams}
    return f, names[f.team_h], names[f.team_a]


@pytest.mark.parametrize(
    ("override", "reason"),
    [({"spread": 0.10}, "spread"), ({"vol": MIN_VOLUME / 10}, "volume")],
)
def test_quality_gate_rejects_thin_markets(
    bootstrap_json: Any, override: dict[str, float], reason: str
) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, away = first_gw6(boot)
    odds, skipped = build_odds(
        synthetic_event(boot, home, away, f.kickoff_time, **override), [f], boot
    )
    assert odds == {}
    assert reason in skipped["EV"]


def test_unknown_team_name_is_skipped_not_guessed(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, _ = first_gw6(boot)
    odds, skipped = build_odds(
        synthetic_event(boot, home, "Real Madrid", f.kickoff_time), [f], boot
    )
    assert odds == {}
    assert "unknown team name" in skipped["EV"]


def test_same_teams_months_apart_do_not_match(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, away = first_gw6(boot)
    assert f.kickoff_time is not None
    later = f.kickoff_time + timedelta(days=120)  # the reverse fixture, not this one
    odds, skipped = build_odds(synthetic_event(boot, home, away, later), [f], boot)
    assert odds == {}
    assert skipped["EV"] == "no matching FPL fixture"


def test_alias_names_are_resolved(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    ids = fpl_team_ids(boot)
    assert ids["Tottenham"] == ids["Spurs"]
    assert ids["Nottingham"] == ids["Nottingham Forest"] == ids["Nott'm Forest"]


# --- client -----------------------------------------------------------------------------------


def test_client_follows_cursor_pagination() -> None:
    page = load_fixture("kalshi_markets")
    base = f"{KALSHI_API}/markets?series_ticker=KXEPLGAME&status=open&limit=200"
    http = FakeSession(
        routes={
            base: FakeResponse(body={**page, "cursor": "abc"}),
            f"{base}&cursor=abc": FakeResponse(body={**page, "cursor": ""}),
        }
    )
    markets = KalshiClient(http).open_match_markets()
    assert len(markets) == 2 * len(page["markets"])
    assert len(http.calls) == 2


# --- total-goals market ----------------------------------------------------------------------


def over_line(
    line: float,
    price: float,
    vol: float = 5000.0,
    spread: float = 0.02,
    event: str = "KXEPLTOTAL-EV",
) -> KalshiMarket:
    return KalshiMarket(
        ticker=f"{event}-{line}",
        event_ticker=event,
        yes_sub_title=f"Over {line} goals scored",
        status="active",
        strike_type="greater",
        floor_strike=line,
        yes_bid_dollars=price - spread / 2,
        yes_ask_dollars=price + spread / 2,
        volume_fp=vol,
    )


def test_line_total_round_trip() -> None:
    t = total_from_line(2.5, 0.55)
    assert p_over(2.5, t) == pytest.approx(0.55, abs=1e-6)
    assert p_over(1.5, 3.0) > p_over(2.5, 3.0) > p_over(3.5, 3.0)


def test_consistent_lines_recover_the_total() -> None:
    lines = [over_line(x, p_over(x, 2.8)) for x in (1.5, 2.5, 3.5)]
    result = total_from_totals(lines)
    assert result is not None
    total, used = result
    assert total == pytest.approx(2.8, abs=1e-4)
    assert used == 3


def test_totals_are_volume_weighted() -> None:
    heavy = over_line(2.5, p_over(2.5, 3.0), vol=90_000)
    light = over_line(1.5, p_over(1.5, 2.0), vol=10_000)
    result = total_from_totals([heavy, light])
    assert result is not None
    assert result[0] == pytest.approx(0.9 * 3.0 + 0.1 * 2.0, abs=1e-3)


@pytest.mark.parametrize(
    "bad",
    [
        {"spread": 0.10},
        {"vol": MIN_VOLUME / 10},
        {"price": 0.99},  # settled-looking / uninformative
    ],
)
def test_totals_lines_failing_the_gate_are_ignored(bad: dict[str, float]) -> None:
    good = over_line(2.5, p_over(2.5, 2.6))
    line = over_line(
        3.5, bad.get("price", 0.3), vol=bad.get("vol", 5000.0), spread=bad.get("spread", 0.02)
    )
    result = total_from_totals([good, line])
    assert result is not None
    assert result[1] == 1


def test_recorded_settled_totals_are_all_rejected() -> None:
    settled = [
        KalshiMarket.model_validate(m) for m in load_fixture("kalshi_totals_settled")["markets"]
    ]
    assert {m.floor_strike for m in settled} >= {1.5, 2.5, 3.5}  # the real structure parses
    assert total_from_totals(settled) is None  # 0.99/0.01 prices carry no pre-match information


def test_match_uses_totals_market_when_available(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, away = first_gw6(boot)
    result = synthetic_event(boot, home, away, f.kickoff_time)  # event ticker "EV"
    totals = [over_line(x, p_over(x, 3.2), event="KXEPLTOTAL-EV") for x in (1.5, 2.5, 3.5)]
    odds, _ = build_odds(result, [f], boot, totals)
    o = odds[f.id]
    assert o.total_source == "totals" and o.totals_lines == 3
    assert o.lambda_home + o.lambda_away == pytest.approx(3.2, abs=1e-3)
    # The split still honours the match-result market's home-minus-away.
    h, _, a = outcome_probs(o.lambda_home, o.lambda_away)
    assert h - a == pytest.approx(o.p_home - o.p_away, abs=1e-6)


def test_falls_back_to_draw_price_without_totals(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, away = first_gw6(boot)
    odds, _ = build_odds(synthetic_event(boot, home, away, f.kickoff_time), [f], boot)
    o = odds[f.id]
    assert o.total_source == "draw" and o.totals_lines == 0
    assert o.draw_gap == pytest.approx(0.0, abs=1e-6)  # the draw is matched exactly


def test_totals_for_another_match_do_not_attach(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    f, home, away = first_gw6(boot)
    other = [over_line(2.5, 0.6, event="KXEPLTOTAL-OTHERMATCH")]
    odds, _ = build_odds(synthetic_event(boot, home, away, f.kickoff_time), [f], boot, other)
    assert odds[f.id].total_source == "draw"


def test_match_code_links_series() -> None:
    assert match_code("KXEPLTOTAL-26SEP20FULMUN") == match_code("KXEPLGAME-26SEP20FULMUN")
