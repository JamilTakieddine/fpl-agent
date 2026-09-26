"""Match odds from Kalshi: implied home/draw/away probabilities and each team's expected goals.

Pipeline (all pure functions except load_odds):
1. Group Kalshi markets by event (one match = home, away and "Tie" contracts).
2. Map the event to an FPL fixture: explicit team-name table, same two teams, kickoff within
   KICKOFF_TOLERANCE. Home/away comes from FPL's fixture, not Kalshi's title order. An unknown
   name is reported and the match skipped, never guessed.
3. Quality gate: all three outcomes quoted both sides, spread <= MAX_SPREAD, total volume
   >= MIN_VOLUME. A match that fails gets NO odds rather than bad odds.
4. Probabilities: bid/ask midpoints normalised to sum to 1 (removes the overround).
5. Expected goals, independent Poisson (lambda_home, lambda_away), found by bisection (no scipy):
   - Total T = lambda_home + lambda_away: from the total-goals market when a line passes the
     quality gate (total goals ~ Poisson(T); each "over X.5" price gives a T; volume-weighted
     mean). Otherwise from the draw price, which plain Poisson biases low because it underrates
     draws. MatchOdds.total_source says which.
   - Split of T between the teams: from P(home) - P(away) in the match-result market.
   - draw_gap = model P(draw) - market P(draw): the Poisson draw bias, measured per match for
     the Phase 2 Dixon-Coles decision.

See docs/decisions.md (D15).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fpl_agent.data.kalshi import TIE, TOTALS_SERIES, KalshiClient, KalshiMarket, match_code
from fpl_agent.data.models import Bootstrap, Fixture

# Kalshi name -> FPL team name, only where they differ. Names that match exactly need no entry.
# Kalshi isn't consistent with itself ("Nottingham" and "Nottingham Forest" both appear).
KALSHI_TO_FPL_NAME = {
    "Coventry": "Coventry City",
    "Leeds United": "Leeds",
    "Manchester City": "Man City",
    "Manchester United": "Man Utd",
    "Nottingham": "Nott'm Forest",
    "Nottingham Forest": "Nott'm Forest",
    "Tottenham": "Spurs",
}

# Kalshi's occurrence time isn't always kickoff; the same two teams meet months apart.
KICKOFF_TOLERANCE = timedelta(hours=36)
MAX_SPREAD = 0.06  # starting values; revisit with Phase 2 calibration
MIN_VOLUME = 1000.0  # per match (result market) and per line (totals market)
# Totals lines priced near 0 or 1 carry little information and are very sensitive to noise.
TOTALS_PRICE_RANGE = (0.03, 0.97)
MAX_GOALS = 10  # Poisson tail beyond this is negligible for football


@dataclass(frozen=True)
class MatchOdds:
    fixture_id: int
    p_home: float
    p_draw: float
    p_away: float
    lambda_home: float  # expected goals
    lambda_away: float
    overround: float  # sum of the three midpoints minus 1; midpoints can sum under 1, so may be < 0
    volume: float
    max_spread: float
    total_source: str  # "totals" (total-goals market) or "draw" (fallback: from the draw price)
    totals_lines: int  # totals lines that passed the gate (0 when total_source == "draw")
    draw_gap: (
        float  # model P(draw) - market P(draw); ~0 by construction when total_source == "draw"
    )


# --- Poisson model ------------------------------------------------------------------------------


def _pmf(lam: float) -> list[float]:
    return [math.exp(-lam) * lam**k / math.factorial(k) for k in range(MAX_GOALS + 1)]


def outcome_probs(lambda_home: float, lambda_away: float) -> tuple[float, float, float]:
    """P(home win), P(draw), P(away win) under independent Poisson goals."""
    h, a = _pmf(lambda_home), _pmf(lambda_away)
    n = MAX_GOALS + 1
    home = sum(h[i] * a[j] for i in range(n) for j in range(i))
    draw = sum(h[i] * a[i] for i in range(n))
    away = sum(h[i] * a[j] for j in range(n) for i in range(j))
    # Normalise over the truncated grid so the cut-off tail is shared out evenly; taking
    # away = 1 - home - draw would dump it all on the away side (a small home/away asymmetry).
    total = home + draw + away
    return home / total, draw / total, away / total


def _bisect(f: Callable[[float], float], lo: float, hi: float, iters: int = 60) -> float:
    """Root of an increasing function on [lo, hi] (clamped to the ends if out of range)."""
    for _ in range(iters):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def split_total(total: float, p_home: float, p_away: float) -> tuple[float, float]:
    """Split a total T into (lambda_home, lambda_away) so P(home) - P(away) matches the market.

    The home share of T is found by bisection: a bigger share means a bigger home-minus-away
    win probability.
    """
    target_diff = p_home - p_away

    def diff_gap(share: float) -> float:
        h, _, a = outcome_probs(total * share, total * (1 - share))
        return (h - a) - target_diff

    share = _bisect(diff_gap, 0.001, 0.999)
    return total * share, total * (1 - share)


def solve_lambdas(p_home: float, p_draw: float) -> tuple[float, float]:
    """Expected goals (home, away) whose Poisson outcome probabilities match the market exactly.

    Outer bisection on the total T (a higher total means fewer draws); inner: split_total.
    """
    p_away = 1.0 - p_home - p_draw

    def draw_gap(total: float) -> float:
        # Draw probability falls as the total rises; this difference therefore rises with T.
        _, d, _ = outcome_probs(*split_total(total, p_home, p_away))
        return p_draw - d

    total = _bisect(draw_gap, 0.2, 8.0)
    return split_total(total, p_home, p_away)


def p_over(line: float, total: float) -> float:
    """P(total goals > line) when total goals ~ Poisson(total). `line` is X.5."""
    at_most = sum(math.exp(-total) * total**k / math.factorial(k) for k in range(int(line) + 1))
    return 1.0 - at_most


def total_from_line(line: float, price: float) -> float:
    """The Poisson total T that makes P(over line) equal the market price."""
    return _bisect(lambda t: p_over(line, t) - price, 0.05, 10.0)


def total_from_totals(lines: list[KalshiMarket]) -> tuple[float, int] | None:
    """Volume-weighted mean of the totals implied by each liquid 'over X.5' line, or None.

    Each line must be quoted both sides, spread <= MAX_SPREAD, volume >= MIN_VOLUME, and priced
    inside TOTALS_PRICE_RANGE.
    """
    weighted, weight, used = 0.0, 0.0, 0
    lo, hi = TOTALS_PRICE_RANGE
    for m in lines:
        mid, spread, vol = m.mid(), m.spread(), m.volume_fp or 0.0
        if m.floor_strike is None or m.strike_type != "greater" or mid is None or spread is None:
            continue
        if spread > MAX_SPREAD or vol < MIN_VOLUME or not lo <= mid <= hi:
            continue
        weighted += vol * total_from_line(m.floor_strike, mid)
        weight += vol
        used += 1
    if used == 0:
        return None
    return weighted / weight, used


# --- Kalshi -> FPL fixture mapping ---------------------------------------------------------


def fpl_team_ids(bootstrap: Bootstrap) -> dict[str, int]:
    """Kalshi outcome name -> FPL team id (exact names plus the alias table)."""
    by_fpl_name = {t.name: t.id for t in bootstrap.teams}
    ids = dict(by_fpl_name)
    for kalshi_name, fpl_name in KALSHI_TO_FPL_NAME.items():
        if fpl_name in by_fpl_name:
            ids[kalshi_name] = by_fpl_name[fpl_name]
    return ids


def group_by_event(markets: list[KalshiMarket]) -> dict[str, list[KalshiMarket]]:
    groups: dict[str, list[KalshiMarket]] = defaultdict(list)
    for m in markets:
        groups[m.event_ticker].append(m)
    return dict(groups)


def find_fixture(
    team_a: int, team_b: int, when: datetime | None, fixtures: list[Fixture]
) -> Fixture | None:
    """The FPL fixture between these two teams kicking off within KICKOFF_TOLERANCE of `when`."""
    for f in fixtures:
        if {f.team_h, f.team_a} != {team_a, team_b} or f.kickoff_time is None:
            continue
        if when is None or abs(f.kickoff_time - when) <= KICKOFF_TOLERANCE:
            return f
    return None


def match_odds(
    fixture: Fixture,
    by_team: dict[int, KalshiMarket],
    tie: KalshiMarket,
    totals: list[KalshiMarket] | None = None,
) -> MatchOdds | str:
    """Odds for one fixture, or the reason it failed the quality gate."""
    home, away = by_team.get(fixture.team_h), by_team.get(fixture.team_a)
    if home is None or away is None:
        return "missing a team's market"
    legs = (home, tie, away)
    mids = [m.mid() for m in legs]
    spreads = [m.spread() for m in legs]
    if any(x is None for x in mids) or any(s is None for s in spreads):
        return "an outcome has no two-sided quote"
    max_spread = max(s for s in spreads if s is not None)
    if max_spread > MAX_SPREAD:
        return f"spread {max_spread:.2f} > {MAX_SPREAD}"
    volume = sum(m.volume_fp or 0.0 for m in legs)
    if volume < MIN_VOLUME:
        return f"volume {volume:.0f} < {MIN_VOLUME:.0f}"

    raw = [x for x in mids if x is not None]
    total = sum(raw)
    p_home, p_draw, p_away = (x / total for x in raw)

    from_totals = total_from_totals(totals or [])
    if from_totals is not None:
        goal_total, lines_used = from_totals
        lam_h, lam_a = split_total(goal_total, p_home, p_away)
        source = "totals"
    else:
        lam_h, lam_a = solve_lambdas(p_home, p_draw)
        lines_used, source = 0, "draw"
    _, model_draw, _ = outcome_probs(lam_h, lam_a)
    return MatchOdds(
        fixture_id=fixture.id,
        p_home=p_home,
        p_draw=p_draw,
        p_away=p_away,
        lambda_home=lam_h,
        lambda_away=lam_a,
        overround=total - 1.0,
        volume=volume,
        max_spread=max_spread,
        total_source=source,
        totals_lines=lines_used,
        draw_gap=model_draw - p_draw,
    )


def build_odds(
    markets: list[KalshiMarket],
    fixtures: list[Fixture],
    bootstrap: Bootstrap,
    totals_markets: list[KalshiMarket] | None = None,
) -> tuple[dict[int, MatchOdds], dict[str, str]]:
    """Odds per FPL fixture id, plus {event_ticker: reason} for every event that was skipped.

    Totals attach to a match through the shared match code in the event ticker, so no second
    round of team-name matching is needed.
    """
    ids = fpl_team_ids(bootstrap)
    totals_by_match: dict[str, list[KalshiMarket]] = defaultdict(list)
    for m in totals_markets or []:
        totals_by_match[match_code(m.event_ticker)].append(m)
    odds: dict[int, MatchOdds] = {}
    skipped: dict[str, str] = {}
    for event, legs in group_by_event(markets).items():
        tie = next((m for m in legs if m.yes_sub_title == TIE), None)
        teams = [m for m in legs if m.yes_sub_title != TIE]
        unknown = [m.yes_sub_title for m in teams if m.yes_sub_title not in ids]
        if unknown:
            skipped[event] = f"unknown team name(s) {unknown}: add to KALSHI_TO_FPL_NAME"
            continue
        if tie is None or len(teams) != 2:
            skipped[event] = "expected two team markets and a Tie"
            continue
        by_team = {ids[m.yes_sub_title]: m for m in teams}
        a, b = by_team
        fixture = find_fixture(a, b, teams[0].occurrence_datetime, fixtures)
        if fixture is None:
            skipped[event] = "no matching FPL fixture"
            continue
        result = match_odds(fixture, by_team, tie, totals_by_match.get(match_code(event)))
        if isinstance(result, str):
            skipped[event] = result
        else:
            odds[fixture.id] = result
    return odds, skipped


def load_odds(
    kalshi: KalshiClient, fixtures: list[Fixture], bootstrap: Bootstrap
) -> tuple[dict[int, MatchOdds], dict[str, str]]:
    """Two Kalshi requests (match result + total goals), mapped onto the given FPL fixtures."""
    return build_odds(
        kalshi.open_match_markets(),
        fixtures,
        bootstrap,
        kalshi.open_match_markets(TOTALS_SERIES),
    )
