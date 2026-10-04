"""Phase 3 part 7: back-test the lineup optimizer on the H2H league's real gameweeks.

(docs/decisions.md D35) Would the agent's lineups have beaten what the league's managers really
played? For every manager and gameweek GW2-5 (the cached league picks):
- rebuild that gameweek's simulation from what was known before its deadline (pointintime, D24);
- take the manager's ACTUAL squad and actual H2H opponent, modelled as part 3 would have before
  the deadline (their previous squad and captaincy; FPL's average in AVERAGE weeks);
- compare three lineups of the same squad: what the manager played, the agent's recommendation
  (part 4) and the highest-expected-points lineup, each scored on REAL results by the rules
  engine (real minutes, so auto-subs happen as they did), with the manager's own chip and hits.
Then: real points gained, H2H results re-scored against the opponent's real score, captain
points, and whether the predicted win chances matched how often the agent actually won.

Caveats (also in the report): 44 team-weeks only; the opponent captain model was fitted on these
same picks; no flags existed before GW6, so the rebuilt weeks see everyone as fit; AVERAGE weeks
use today's ownership. Offline: everything comes from data/cache/ (gitignored).

Usage: python -m fpl_agent.validation.optimizer [--reuse]   (--reuse: re-render saved records)
  writes docs/optimizer_backtest.md and data/cache/validation/optimizer.html (published as a page)
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from fpl_agent.data.calendar import Calendar
from fpl_agent.data.lineups import predict_all
from fpl_agent.data.models import Bootstrap, ChipPlay, EntryPicks, Fixture, PositionCode
from fpl_agent.data.opponent import captain_history, chips_remaining, current_squad
from fpl_agent.model.gameweek import simulate_gameweek
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.lineup import BUFFER, recommend, summarize
from fpl_agent.optimize.opponent import (
    average_points,
    build_opponent_model,
    chip_probabilities,
    opponent_points,
    team_sheet,
)
from fpl_agent.optimize.rules import Limits, Lineup, multipliers, team_points
from fpl_agent.optimize.scoring import score_lineup, squad_sims
from fpl_agent.validation.backtest import VALIDATION_DIR
from fpl_agent.validation.current_season import kalshi_odds_source, load_current_season
from fpl_agent.validation.pointintime import build_case

CACHE = Path("data/cache")
GAMEWEEKS = range(2, 6)
N_SIMS = 10_000  # as in production: lineup choices depend on the tails
TEMPLATE = Path(__file__).with_name("optimizer_template.html")


@dataclass(frozen=True)
class TeamWeek:
    """One manager's gameweek, three ways. Points are REAL, after the manager's hits."""

    gw: int
    entry: int  # stays in the gitignored cache; reports never show it
    mine: bool
    vs_average: bool
    chip: str | None
    official: int  # FPL's score for the manager (a check: `actual` must equal it)
    opponent: int  # the opponent's real score, or FPL's average
    actual: int
    ours: int
    max_xp: int
    actual_xp: float  # expected points in the pre-deadline simulation
    ours_xp: float
    max_xp_xp: float
    actual_p_win: float  # predicted, against the modelled opponent
    ours_p_win: float
    actual_captain: int  # real points of whoever wore the armband
    ours_captain: int
    same_xi: bool
    same_captain: bool
    # The lineups themselves (FPL order; bench keeper first), for diagnosis.
    actual_lineup: tuple[tuple[int, ...], tuple[int, ...], int, int] = ((), (), 0, 0)
    ours_lineup: tuple[tuple[int, ...], tuple[int, ...], int, int] = ((), (), 0, 0)


# --- real results --------------------------------------------------------------------------------


def real_points(
    lineup: Lineup,
    live: Mapping[int, tuple[int, bool]],
    positions: Mapping[int, PositionCode],
    limits: Limits,
    chip: str | None,
    hits: int,
) -> int:
    """The score this team sheet really got: auto-subs from real minutes, captaincy, the chip,
    minus the manager's transfer hits. `live`: player -> (real points, played)."""
    pts = {p: v[0] for p, v in live.items()}
    played = {p: v[1] for p, v in live.items()}
    return team_points(lineup, pts, played, positions, limits, chip) - hits


def armband_points(lineup: Lineup, live: Mapping[int, tuple[int, bool]]) -> int:
    """Real points of whoever wore the armband (captain, or vice if the captain didn't play)."""
    played = {p: v[1] for p, v in live.items()}
    worn = [p for p, m in multipliers(lineup, played).items() if m > 1]
    return live.get(worn[0], (0, False))[0] if worn else 0


def outcome(me: int, them: int) -> str:
    return "W" if me > them else "D" if me == them else "L"


# --- running -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class League:
    picks: dict[tuple[int, int], EntryPicks]  # (entry, gw) -> picks
    hits: dict[tuple[int, int], int]
    matches: dict[int, list[dict[str, Any]]]  # gw -> H2H results


def load_league(gws: Sequence[int]) -> League:
    picks, hits = {}, {}
    for path in (CACHE / "picks").glob("*.json"):
        entry, gw = map(int, path.stem.split("_"))
        raw = json.loads(path.read_text())
        picks[(entry, gw)] = EntryPicks.model_validate(raw)
        hits[(entry, gw)] = raw["entry_history"]["event_transfers_cost"]
    matches = {
        gw: json.loads((CACHE / "h2h" / f"gw{gw}.json").read_text())["results"] for gw in gws
    }
    return League(picks, hits, matches)


def pairings(results: list[dict[str, Any]]) -> dict[int, tuple[int | None, int]]:
    """entry -> (opponent entry or None for AVERAGE, opponent's real score)."""
    out: dict[int, tuple[int | None, int]] = {}
    for m in results:
        for a, b in (("1", "2"), ("2", "1")):
            if m[f"entry_{a}_entry"] is not None:
                out[m[f"entry_{a}_entry"]] = (m[f"entry_{b}_entry"], m[f"entry_{b}_points"])
    return out


def modelled_opponent(
    entry: int | None,
    gw: int,
    league: League,
    boot: Bootstrap,
    calendar: Calendar,
    points: PointsSamples,
    xp: Mapping[int, float],
    limits: Limits,
    positions: Mapping[int, PositionCode],
    ownership: Mapping[int, float],
    rng: np.random.Generator,
) -> NDArray[np.int64]:
    """The opponent's points in each simulation, as part 3 would have modelled them before the
    gameweek's deadline: their previous squad and captaincy (FPL's average for AVERAGE)."""
    if entry is None:
        return average_points(points, ownership)
    before = {g: p for (e, g), p in league.picks.items() if e == entry and g < gw}
    squad = current_squad(before)
    assert squad is not None
    recent = {g: p for g, p in before.items() if g >= gw - 5}
    played = [ChipPlay(name=p.active_chip, event=g) for g, p in before.items() if p.active_chip]
    chips = chip_probabilities(gw, chips_remaining(boot.chips, played, gw), boot.chips, calendar)
    model = build_opponent_model(
        squad, [c.captain for c in captain_history(recent)], xp, chips, limits
    )
    ids = model.lineup.starters + model.lineup.bench
    return opponent_points(squad_sims(points, ids), model, positions, limits, rng)


def run(my_entry: int | None = None, gws: Sequence[int] = GAMEWEEKS) -> list[TeamWeek]:
    boot = Bootstrap.model_validate(json.loads((CACHE / "bootstrap.json").read_text()))
    fixtures = [
        Fixture.model_validate(f) for f in json.loads((CACHE / "fixtures.json").read_text())
    ]
    raw_lives = {gw: json.loads((CACHE / f"live_gw{gw}.json").read_text()) for gw in range(1, 6)}
    season, _ = load_current_season(boot, fixtures, raw_lives, kalshi_odds_source())
    league = load_league(gws)
    limits = Limits.from_bootstrap(boot)
    positions = {p.id: boot.position_code(p.element_type) for p in boot.elements}
    ownership = {p.id: p.selected_by_percent for p in boot.elements}  # today's (D35 caveat)

    records = []
    for gw in gws:
        case = build_case(season, gw)
        preds = predict_all(case.bootstrap.elements, case.calendar, case.lives, gw)
        sim = simulate_gameweek(
            case.bootstrap,
            case.calendar,
            case.fixtures,
            gw,
            preds,
            case.lives,
            case.odds,
            None,
            n_sims=N_SIMS,
            seed=gw,
        )
        live = {
            el["id"]: (el["stats"]["total_points"], el["stats"]["minutes"] > 0)
            for el in raw_lives[gw]["elements"]
        }
        xp = {
            int(p): float(e) for p, e in zip(sim.points.player, sim.points.expected(), strict=True)
        }
        rng = np.random.default_rng(gw)
        opponents = {
            opp: modelled_opponent(
                opp,
                gw,
                league,
                boot,
                case.calendar,
                sim.points,
                xp,
                limits,
                positions,
                ownership,
                rng,
            )
            for opp, _ in sorted(pairings(league.matches[gw]).values(), key=lambda t: t[0] or 0)
        }

        for entry, (opp, opp_score) in sorted(pairings(league.matches[gw]).items()):
            picks = league.picks[(entry, gw)]
            chip, hits = picks.active_chip, league.hits[(entry, gw)]
            actual = team_sheet(picks, limits)
            mine = squad_sims(sim.points, actual.starters + actual.bench)
            opp_vec = opponents[opp]
            rec = recommend(mine, positions, limits, opp_vec)
            ours, top = rec.best.lineup, rec.max_expected.lineup
            theirs = summarize(
                actual, score_lineup(mine, actual, positions, limits), opp_vec, BUFFER
            )

            official = next(
                m[f"entry_{s}_points"]
                for m in league.matches[gw]
                for s in ("1", "2")
                if m[f"entry_{s}_entry"] == entry
            )
            records.append(
                TeamWeek(
                    gw=gw,
                    entry=entry,
                    mine=entry == my_entry,
                    vs_average=opp is None,
                    chip=chip,
                    official=official,
                    opponent=opp_score,
                    actual=real_points(actual, live, positions, limits, chip, hits),
                    ours=real_points(ours, live, positions, limits, chip, hits),
                    max_xp=real_points(top, live, positions, limits, chip, hits),
                    actual_xp=theirs.expected,
                    ours_xp=rec.best.expected,
                    max_xp_xp=rec.max_expected.expected,
                    actual_p_win=theirs.p_win,
                    ours_p_win=rec.best.p_win,
                    actual_captain=armband_points(actual, live),
                    ours_captain=armband_points(ours, live),
                    same_xi=set(ours.starters) == set(actual.starters),
                    same_captain=ours.captain == actual.captain,
                    actual_lineup=(
                        actual.starters,
                        actual.bench,
                        actual.captain,
                        actual.vice_captain,
                    ),
                    ours_lineup=(ours.starters, ours.bench, ours.captain, ours.vice_captain),
                )
            )
        print(f"GW{gw}: {sum(r.gw == gw for r in records)} team-weeks", flush=True)
    return records


# --- summary -------------------------------------------------------------------------------------


def paired(diffs: Sequence[float]) -> dict[str, float]:
    """Mean difference with its standard error and a 95% interval (normal approximation)."""
    d = np.asarray(diffs, dtype=float)
    se = float(d.std(ddof=1) / math.sqrt(len(d))) if len(d) > 1 else float("nan")
    mean = float(d.mean())
    return {"mean": mean, "se": se, "low": mean - 1.96 * se, "high": mean + 1.96 * se}


def record(results: Sequence[str]) -> dict[str, int]:
    w, d, losses = (sum(r == x for r in results) for x in "WDL")
    return {"W": w, "D": d, "L": losses, "league_points": 3 * w + d}


def swap_analysis(
    records: Sequence[TeamWeek],
    minutes: Mapping[int, Mapping[int, int]],
    points: Mapping[int, Mapping[int, int]],
) -> dict[str, Any]:
    """Why the agent differs: the starters only one side picked (`minutes`/`points`: real, per
    gameweek and player), and a CLEAN comparison - only team-weeks where every swapped player
    played the gameweek before, so neither side had an obvious injury to know about. Managers
    saw FPL's injury flags; the rebuilt GW2-5 weeks have none (D35)."""

    def side(players: list[tuple[int, int]]) -> dict[str, float]:
        mins = [minutes[gw].get(p, 0) for gw, p in players]
        pts = [points[gw].get(p, 0) for gw, p in players]
        prev_out = sum(
            minutes[gw].get(p, 0) == 0 and minutes.get(gw - 1, {}).get(p, 0) == 0
            for gw, p in players
        )
        played = [x for x, m in zip(pts, mins, strict=True) if m > 0]
        return {
            "n": len(players),
            "zero_minutes": sum(m == 0 for m in mins) / max(1, len(players)),
            "out_two_weeks": prev_out,
            "points": sum(pts) / max(1, len(players)),
            "points_when_played": sum(played) / max(1, len(played)),
        }

    agent, manager, clean = [], [], []
    for r in records:
        ours, theirs = set(r.ours_lineup[0]), set(r.actual_lineup[0])
        a = [(r.gw, p) for p in ours - theirs]
        m = [(r.gw, p) for p in theirs - ours]
        agent += a
        manager += m
        if all(minutes.get(gw - 1, {}).get(p, 0) > 0 for gw, p in a + m):
            clean.append(r.ours - r.actual)
    return {
        "per_team_week": len(agent) / max(1, len(records)),
        "agent_only": side(agent),
        "manager_only": side(manager),
        "clean": {"n": len(clean), **(paired(clean) if len(clean) > 1 else {})},
    }


def summarize_records(
    records: Sequence[TeamWeek], generated: str, swaps: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    n = len(records)
    res = {
        k: [outcome(getattr(r, k), r.opponent) for r in records]
        for k in ("actual", "ours", "max_xp")
    }
    won = np.array([o == "W" for o in res["ours"]], dtype=float)
    p = np.array([r.ours_p_win for r in records])
    changed = [(a, o) for a, o in zip(res["actual"], res["ours"], strict=True) if a != o]
    per_gw = []
    for gw in sorted({r.gw for r in records}):
        rs = [r for r in records if r.gw == gw]
        per_gw.append(
            {
                "gw": gw,
                "n": len(rs),
                "actual": float(np.mean([r.actual for r in rs])),
                "ours": float(np.mean([r.ours for r in rs])),
                "max_xp": float(np.mean([r.max_xp for r in rs])),
                "ours_record": record([outcome(r.ours, r.opponent) for r in rs]),
                "actual_record": record([outcome(r.actual, r.opponent) for r in rs]),
            }
        )
    return {
        "generated": generated,
        "n": n,
        "gameweeks": sorted({r.gw for r in records}),
        "official_check": sum(r.actual == r.official for r in records),
        "points": {
            "ours_vs_actual": paired([r.ours - r.actual for r in records]),
            "max_xp_vs_actual": paired([r.max_xp - r.actual for r in records]),
            "ours_vs_max_xp": paired([r.ours - r.max_xp for r in records]),
            "expected_gain": paired([r.ours_xp - r.actual_xp for r in records]),
        },
        "diffs": sorted(r.ours - r.actual for r in records),
        "h2h": {k: record(v) for k, v in res.items()},
        "flips": {
            "better": sum("WDL".index(o) < "WDL".index(a) for a, o in changed),
            "worse": sum("WDL".index(o) > "WDL".index(a) for a, o in changed),
        },
        "captain": {
            "same_share": float(np.mean([r.same_captain for r in records])),
            "same_xi_share": float(np.mean([r.same_xi for r in records])),
            "ours_vs_actual": paired([r.ours_captain - r.actual_captain for r in records]),
        },
        "calibration": {
            "predicted_win": float(p.mean()),
            "actual_win": float(won.mean()),
            "brier": float(np.mean((p - won) ** 2)),
            "brier_coin_flip": 0.25,
        },
        "per_gw": per_gw,
        "mine": [
            {k: getattr(r, k) for k in ("gw", "actual", "ours", "opponent", "chip")}
            | {
                "actual_result": outcome(r.actual, r.opponent),
                "ours_result": outcome(r.ours, r.opponent),
            }
            for r in records
            if r.mine
        ],
        "swaps": dict(swaps) if swaps else None,
        "humility": humility_curve(records),
    }


HUMILITY_THRESHOLDS = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)


def humility_curve(records: Sequence[TeamWeek]) -> list[dict[str, Any]]:
    """A humbler agent (D35): keep the manager's lineup unless the agent's EXPECTED gain is at
    least T points, for each threshold T. T = 0 is today's agent; real points and H2H results."""
    out = []
    for t in HUMILITY_THRESHOLDS:
        switch = [r.ours_xp - r.actual_xp >= t for r in records]
        picks = [r.ours if s else r.actual for r, s in zip(records, switch, strict=True)]
        out.append(
            {
                "threshold": t,
                "switches": sum(switch),
                "vs_manager": paired([p - r.actual for p, r in zip(picks, records, strict=True)]),
                "record": record(
                    [outcome(p, r.opponent) for p, r in zip(picks, records, strict=True)]
                ),
            }
        )
    return out


# --- rendering -----------------------------------------------------------------------------------


def _pm(d: Mapping[str, float]) -> str:
    return f"{d['mean']:+.2f} (95% {d['low']:+.1f} to {d['high']:+.1f})"


def render_markdown(s: Mapping[str, Any]) -> str:
    pts, h2h, cal, cap = s["points"], s["h2h"], s["calibration"], s["captain"]

    def rec(r: Mapping[str, int]) -> str:
        return f"{r['W']}-{r['D']}-{r['L']} ({r['league_points']} league pts)"

    lines = [
        "# Optimizer back-test",
        "",
        f"*Generated {s['generated']} by `python -m fpl_agent.validation.optimizer`. Method and "
        "caveats: D35 in `decisions.md`. An interactive version is published as an HTML page.*",
        "",
        f"Every manager in the H2H league, GW{s['gameweeks'][0]}-{s['gameweeks'][-1]}: **{s['n']} "
        "team-weeks**. Same squad, three lineups, each scored on real results (real minutes, the "
        "manager's own chip and hits): what the manager played, the agent's recommendation, and "
        "the highest-expected-points lineup.",
        "",
        "## Real points per team-week",
        "",
        "| Comparison | Mean difference |",
        "|---|---|",
        f"| Agent vs manager | **{_pm(pts['ours_vs_actual'])}** |",
        f"| Highest expected points vs manager | {_pm(pts['max_xp_vs_actual'])} |",
        f"| Agent vs highest expected points | {_pm(pts['ours_vs_max_xp'])} |",
        f"| What the model expected the agent to gain | {_pm(pts['expected_gain'])} |",
        "",
        "## H2H results against the real opponent score",
        "",
        "| Lineup | W-D-L |",
        "|---|---|",
        f"| Manager's own | {rec(h2h['actual'])} |",
        f"| Agent | **{rec(h2h['ours'])}** |",
        f"| Highest expected points | {rec(h2h['max_xp'])} |",
        "",
        f"The agent's lineup changed the result in {s['flips']['better'] + s['flips']['worse']} "
        f"team-weeks: {s['flips']['better']} better, {s['flips']['worse']} worse.",
        "",
        "## Captain and lineup choices",
        "",
        f"- Same captain as the manager: {cap['same_share']:.0%}; same starting XI: "
        f"{cap['same_xi_share']:.0%}.",
        f"- Armband points, agent vs manager: {_pm(cap['ours_vs_actual'])}.",
        "",
        "## Were the win chances honest?",
        "",
        f"- Predicted P(win) for the agent's lineups averaged **{cal['predicted_win']:.1%}**; they "
        f"actually won **{cal['actual_win']:.1%}**.",
        f"- Brier score {cal['brier']:.3f} (lower is better; always guessing 50% scores 0.250).",
        "",
    ]
    sw = s["swaps"]
    if sw:
        a, m, c = sw["agent_only"], sw["manager_only"], sw["clean"]
        clean = f"**{_pm(c)}** over {c['n']} team-weeks" if c["n"] > 1 else f"{c['n']} team-weeks"
        lines += [
            "## Why the agent differs",
            "",
            f"The agent changed {sw['per_team_week']:.1f} starters per team-week on average.",
            "",
            "| Starters only one side picked | n | Played 0 minutes | Out two weeks running "
            "| Real points | When they played |",
            "|---|---|---|---|---|---|",
            f"| Agent's picks | {a['n']} | {a['zero_minutes']:.0%} | {a['out_two_weeks']} "
            f"| {a['points']:.2f} | {a['points_when_played']:.2f} |",
            f"| Manager's picks | {m['n']} | {m['zero_minutes']:.0%} | {m['out_two_weeks']} "
            f"| {m['points']:.2f} | {m['points_when_played']:.2f} |",
            "",
            '"Out two weeks running": 0 minutes that gameweek and the one before, almost '
            "always an injury FPL had flagged, which managers saw and the rebuilt weeks don't.",
            "",
            f"Clean comparison (only team-weeks where every swapped player played the gameweek "
            f"before): agent vs manager {clean}.",
            "",
        ]
    lines += [
        "## A humbler agent",
        "",
        "Keep the manager's lineup unless the agent's expected gain is at least T points "
        "(T = 0 is today's agent).",
        "",
        "| T | Switches | Agent vs manager (95%) | W-D-L |",
        "|---|---|---|---|",
    ]
    for h in s["humility"]:
        r = h["record"]
        lines.append(
            f"| {h['threshold']:.1f} | {h['switches']} | {_pm(h['vs_manager'])} "
            f"| {r['W']}-{r['D']}-{r['L']} |"
        )
    lines += [
        "",
        "## By gameweek",
        "",
        "| GW | Team-weeks | Manager | Agent | Highest xP | Agent W-D-L | Manager W-D-L |",
        "|---|---|---|---|---|---|---|",
    ]
    for g in s["per_gw"]:
        a, o = g["actual_record"], g["ours_record"]
        lines.append(
            f"| {g['gw']} | {g['n']} | {g['actual']:.1f} | {g['ours']:.1f} | {g['max_xp']:.1f} | "
            f"{o['W']}-{o['D']}-{o['L']} | {a['W']}-{a['D']}-{a['L']} |"
        )
    if s["mine"]:
        lines += [
            "",
            "## Your team",
            "",
            "| GW | You | Agent | Opponent | You | Agent |",
            "|---|---|---|---|---|---|",
        ]
        for m in s["mine"]:
            chip = f" ({m['chip']})" if m["chip"] else ""
            lines.append(
                f"| {m['gw']}{chip} | {m['actual']} | {m['ours']} | {m['opponent']} | "
                f"{m['actual_result']} | {m['ours_result']} |"
            )
    lines += [
        "",
        "## Caveats",
        "",
        f"- {s['n']} team-weeks is a small sample: a difference inside the 95% interval could "
        "be luck. Rerun every ~5 gameweeks as the sample grows.",
        "- The opponent captain model was fitted on these same GW2-5 picks (D30), which slightly "
        "flatters the H2H part.",
        "- No injury flags exist before GW6, so the rebuilt weeks see everyone as fit.",
        "- AVERAGE weeks use today's ownership (the only one available for GW2-5).",
        f"- Check: the managers' own team sheets, scored by our rules, reproduce FPL's official "
        f"score in {s['official_check']} of {s['n']} team-weeks.",
        "",
    ]
    return "\n".join(lines)


def render_html(s: Mapping[str, Any]) -> str:
    return TEMPLATE.read_text().replace("/*__DATA__*/null", json.dumps(s))


def main() -> int:
    from fpl_agent.config import ConfigError, load_settings

    path = VALIDATION_DIR / "optimizer_records.jsonl"
    if "--reuse" in sys.argv:  # re-render from saved records, no simulation
        records = [TeamWeek(**json.loads(line)) for line in path.read_text().splitlines()]
    else:
        try:
            my_entry: int | None = load_settings().entry_id
        except ConfigError:
            my_entry = None
        records = run(my_entry)
        VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            for r in records:
                f.write(json.dumps(asdict(r)) + "\n")
    lives = {
        gw: {
            e["id"]: e["stats"]
            for e in json.loads((CACHE / f"live_gw{gw}.json").read_text())["elements"]
        }
        for gw in range(1, max(r.gw for r in records) + 1)
    }
    minutes = {gw: {p: st["minutes"] for p, st in live.items()} for gw, live in lives.items()}
    points = {gw: {p: st["total_points"] for p, st in live.items()} for gw, live in lives.items()}
    swaps = swap_analysis(records, minutes, points)
    summary = summarize_records(records, date.today().isoformat(), swaps)
    Path("docs/optimizer_backtest.md").write_text(render_markdown(summary))
    (VALIDATION_DIR / "optimizer.html").write_text(render_html(summary))
    print(render_markdown(summary))
    print(f"wrote docs/optimizer_backtest.md and {VALIDATION_DIR / 'optimizer.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
