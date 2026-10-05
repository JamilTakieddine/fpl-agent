"""Phase 5d: replay a past season with the agent's weekly transfer and chip loop.

(docs/decisions.md D40) For every gameweek from GW2, the simulations are rebuilt from what was
known before that deadline (pointintime, D24): that week with its closing odds, the following
weeks with the xG-ratings fallback, exactly as the live agent sees them. Four policies start from
the same realistic squad (the most-owned legal 15 at GW1 within the budget) and are scored each
week on REAL results by the rules engine (real minutes, auto-subs, the captain, any chip):
- agent: the 5b/5c planner with chips + part 5's options, the exact checks deciding (D38, D39);
- agent_no_chips: the same without chips;
- part5: part 5's one-week logic only (D32);
- hold: no transfers.
All use free transfers only (D37), at most 3 a week, and the same lineup picker (the most
expected points: there's no opponent here). Selling prices keep half of any rise (rules).

Usage: python -m fpl_agent.validation.planner_backtest [first_gw] [last_gw] [--fresh]
  writes data/cache/validation/planner_backtest.json and docs/planner_backtest.md. It saves a
  checkpoint after every gameweek and, unless --fresh, resumes from it: a stopped run (laptop
  shut, session closed) picks up where it left off.
"""

from __future__ import annotations

import io
import json
import math
import sys
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from fpl_agent.agent import Advice, decide_team_chip, transfer_chip
from fpl_agent.data.lineups import predict_all
from fpl_agent.data.models import Bootstrap, PositionCode
from fpl_agent.fileio import atomic_write
from fpl_agent.model.gameweek import simulate_gameweek
from fpl_agent.model.points import PointsSamples
from fpl_agent.optimize.lineup import recommend
from fpl_agent.optimize.planner import Plan, chip_inputs
from fpl_agent.optimize.planner import plan_transfers as optimize_plan
from fpl_agent.optimize.rules import (
    Limits,
    Lineup,
    SquadPlayer,
    next_free_transfers,
    selling_price,
    team_points,
)
from fpl_agent.optimize.scoring import POSITIONS, squad_sims
from fpl_agent.optimize.transfers import (
    WEEK_WEIGHTS,
    Move,
    Option,
    TransferPlan,
    recommend_transfers,
)
from fpl_agent.submit import TRANSFER_CHIPS
from fpl_agent.validation.backtest import BACKTEST_SIMS, VALIDATION_DIR
from fpl_agent.validation.pointintime import Season, build_case, load_season

SEASON = "2025-26"
CODE: dict[int, PositionCode] = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POLICIES = ("agent", "agent_no_chips", "part5", "hold")
COMPARISONS = (
    ("agent", "hold"),
    ("agent", "part5"),
    ("agent", "agent_no_chips"),
    ("part5", "hold"),
)
PLANNER_TIME_S = 30.0
LAST_GW = 38


# --- the starting squad ----------------------------------------------------------------------


def template_squad(
    players: Sequence[SquadPlayer], selected: Mapping[int, int], limits: Limits
) -> list[int]:
    """The most-owned legal squad within the budget: a realistic manager's GW1 team, the same
    start for every policy (no policy gets a head start)."""
    import highspy

    h = highspy.Highs()  # type: ignore[no-untyped-call]
    h.setOptionValue("output_flag", False)
    x = {q.id: h.addBinary() for q in players}
    h.addConstr(h.qsum(x.values()) == limits.squad_size)
    h.addConstr(h.qsum(q.price * x[q.id] for q in players) <= limits.budget)
    for pos in POSITIONS:
        h.addConstr(
            h.qsum(x[q.id] for q in players if q.position == pos) == limits.squad_counts[pos]
        )
    for club in {q.team for q in players}:
        h.addConstr(h.qsum(x[q.id] for q in players if q.team == club) <= limits.club_limit)
    h.maximize(h.qsum(selected.get(q.id, 0) / 1e6 * x[q.id] for q in players))
    return [q.id for q in players if h.val(x[q.id]) > 0.5]


# --- one policy's season ---------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    moves: tuple[Move, ...]
    chip: str | None = None  # wildcard / freehit played
    free_hit: tuple[int, ...] | None = None
    team_chip: str | None = None  # bboost / 3xc the plan wants this week
    chip_values: dict[str, float] = field(default_factory=dict)
    last_chance: frozenset[str] = frozenset()
    note: str = ""  # the agent's chip reasoning this week (logged)


@dataclass
class State:
    squad: list[int]
    paid: dict[int, int]  # purchase prices (tenths)
    bank: int
    free: int = 1
    chips: list[tuple[str, int, int]] = field(default_factory=list)  # still available
    points: dict[int, int] = field(default_factory=dict)  # gameweek -> real points
    transfers: dict[int, int] = field(default_factory=dict)
    chips_played: dict[int, str] = field(default_factory=dict)
    notes: dict[int, str] = field(default_factory=dict)


def max_expected_lineup(
    week0: PointsSamples,
    squad: Sequence[int],
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> tuple[Lineup, Any]:
    sims = squad_sims(week0, squad)
    rec = recommend(sims, positions, limits, np.zeros(sims.points.shape[1], dtype=np.int64))
    return rec.max_expected.lineup, sims


def decide(
    policy: str,
    state: State,
    gw: int,
    weeks: Sequence[PointsSamples],
    pool: dict[int, SquadPlayer],
    squad_players: list[SquadPlayer],
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> Decision:
    """This week's moves, a Wildcard / Free Hit played, a Free Hit squad, and a Bench Boost /
    Triple Captain the plan wants (confirmed later on the final lineup)."""
    if policy == "hold":
        return Decision(())
    pool_list = list(pool.values())
    if policy == "part5":
        plan = recommend_transfers(
            squad_players,
            state.bank,
            state.free,
            pool_list,
            weeks,
            positions,
            limits,
            allow_hits=False,
        )
        return Decision(plan.best.moves)
    xps = [{int(p): float(e) for p, e in zip(w.player, w.expected(), strict=True)} for w in weeks]
    with_chips = policy == "agent"
    chip_weeks, chip_values, expiring = (
        chip_inputs(state.chips, gw, xps, state.squad, positions, limits)
        if with_chips
        else ({}, {}, set())
    )
    last_chance = frozenset(name for name, _, last in state.chips if last == gw)
    try:
        multi = optimize_plan(
            squad_players,
            state.bank,
            state.free,
            pool_list,
            xps,
            limits,
            chips=chip_weeks,
            chip_values=chip_values,
            must_play=expiring,
            time_limit_s=PLANNER_TIME_S,
        )
    except Exception as e:  # as live: the screened options remain - but say so in the log
        multi = None
        planner_error = f"PLANNER FAILED ({type(e).__name__}: {e})"
    else:
        planner_error = ""
    chip_now = multi.chips[0] if multi is not None and multi.chips else None
    plain = multi is not None and multi.weeks[0] and chip_now not in TRANSFER_CHIPS
    extra = (multi.weeks[0],) if plain and multi is not None else ()
    plan = recommend_transfers(
        squad_players,
        state.bank,
        state.free,
        pool_list,
        weeks,
        positions,
        limits,
        allow_hits=False,
        extra=extra,
    )
    advice = Advice(plan, pool, multi, chip_values, list(weeks), squad_players, last_chance)
    quiet = io.StringIO()
    move = transfer_chip(lambda *a: print(*a, file=quiet), advice, positions, limits)
    plan_chips = "-".join(c or "." for c in multi.chips) if multi is not None else "none"
    note = " ".join(
        x
        for x in (
            f"plan chips [{plan_chips}]",
            f"expiring {sorted(expiring)}" if expiring else "",
            planner_error,
            quiet.getvalue().strip().replace("\n", " "),
        )
        if x
    )
    if move is not None and move.worth_it:
        fh = multi.free_hit[0] if move.chip == "freehit" and multi is not None else None
        return Decision(move.moves, move.chip, fh, note=note)
    team = chip_now if chip_now in ("bboost", "3xc") else None
    return Decision(
        plan.best.moves,
        team_chip=team,
        chip_values=chip_values,
        last_chance=last_chance,
        note=note,
    )


def play_week(
    policy: str,
    state: State,
    gw: int,
    weeks: Sequence[PointsSamples],
    pool: dict[int, SquadPlayer],
    prices: Mapping[int, int],
    teams: Mapping[int, int],
    positions: Mapping[int, PositionCode],
    limits: Limits,
    real: Mapping[int, tuple[int, bool]],
) -> None:
    squad_players = [
        SquadPlayer(p, positions[p], teams.get(p, -p), selling_price(state.paid[p], prices[p]))
        for p in state.squad
    ]
    d = decide(policy, state, gw, weeks, pool, squad_players, positions, limits)
    moves, chip = d.moves, d.chip
    sell = {s.id: s.price for s in squad_players}
    if chip == "freehit" and d.free_hit:
        playing = list(d.free_hit)  # one week only: the regular squad, bank and transfers stand
    else:
        for m in moves:
            state.bank += sell[m.sell] - pool[m.buy].price
            state.paid.pop(m.sell, None)
            state.paid[m.buy] = pool[m.buy].price
        sold = {m.sell: m.buy for m in moves}
        state.squad = [sold.get(p, p) for p in state.squad]
        playing = state.squad
    lineup, sims = max_expected_lineup(weeks[0], playing, positions, limits)
    if d.team_chip is not None:  # the same exact check as live (agent.decide_team_chip)
        stub = Plan((), (), 0.0, True, 0.0, 0, chips=(d.team_chip,))
        empty = TransferPlan(Option((), (), 0, 0, (), 0.0, 0.0), [], state.free, 0)
        advice = Advice(empty, {}, stub, d.chip_values, list(weeks), [], d.last_chance)
        quiet = io.StringIO()
        chip = decide_team_chip(
            lambda *a: print(*a, file=quiet), advice, lineup, sims, positions, limits
        )
        d = replace(d, note=f"{d.note} {quiet.getvalue().strip()}")
    pts = {p: v[0] for p, v in real.items()}
    played = {p: v[1] for p, v in real.items()}
    state.points[gw] = team_points(lineup, pts, played, positions, limits, chip)
    state.transfers[gw] = 0 if chip in TRANSFER_CHIPS else len(moves)
    if d.note:
        state.notes[gw] = d.note
    if chip:
        state.chips_played[gw] = chip
        state.chips = [c for c in state.chips if not (c[0] == chip and c[1] <= gw <= c[2])]
    state.free = next_free_transfers(state.free, len(moves), limits, chip)


# --- the season loop -------------------------------------------------------------------------


def real_results(season: Season, gw: int) -> dict[int, tuple[int, bool]]:
    pts: dict[int, int] = defaultdict(int)
    mins: dict[int, int] = defaultdict(int)
    for r in season.rows_by_gw.get(gw, []):
        pts[r.element] += r.total_points
        mins[r.element] += r.minutes
    return {p: (pts[p], mins[p] > 0) for p in pts}


CHECKPOINT = VALIDATION_DIR / "planner_backtest_checkpoint.json"


def save_checkpoint(
    gw: int,
    states: Mapping[str, State],
    memory: Mapping[int, int],
    teams: Mapping[int, int],
    positions: Mapping[int, PositionCode],
) -> None:
    """Everything the loop carries between gameweeks, after `gw` is done (written atomically, so
    a stop mid-write never corrupts it)."""
    text = json.dumps(
        {
            "season": SEASON,
            "done": gw,
            "states": {k: asdict(v) for k, v in states.items()},
            "memory": memory,
            "teams": teams,
            "positions": positions,
        }
    )
    atomic_write(CHECKPOINT, text)
    atomic_write(CHECKPOINT.with_name(f"planner_backtest_after_gw{gw:02d}.json"), text)


def load_checkpoint(
    path: Path | None = None,
) -> tuple[int, dict[str, State], dict[int, int], dict[int, int], dict[int, PositionCode]] | None:
    path = path or CHECKPOINT  # read at call time (a default argument would freeze it)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    if raw.get("season") != SEASON:
        return None

    def ints(d: Mapping[str, Any]) -> dict[int, Any]:
        return {int(k): v for k, v in d.items()}

    states = {
        k: State(
            squad=list(v["squad"]),
            paid=ints(v["paid"]),
            bank=v["bank"],
            free=v["free"],
            chips=[(c[0], c[1], c[2]) for c in v["chips"]],
            points=ints(v["points"]),
            transfers=ints(v["transfers"]),
            chips_played=ints(v["chips_played"]),
            notes=ints(v.get("notes", {})),
        )
        for k, v in raw["states"].items()
    }
    return raw["done"], states, ints(raw["memory"]), ints(raw["teams"]), ints(raw["positions"])


def run(
    first: int = 2,
    last: int = LAST_GW,
    resume: bool = True,
    policies: Sequence[str] = POLICIES,
    after: int | None = None,
) -> dict[str, State]:
    """`after`: start from the saved state after that gameweek (per-gameweek checkpoints)."""
    template = Bootstrap.model_validate(json.loads(Path("data/cache/bootstrap.json").read_text()))
    season = load_season(SEASON, template)
    limits = Limits.from_bootstrap(template)
    case = build_case(season, 1)
    positions0 = {
        p.id: case.bootstrap.position_code(p.element_type) for p in case.bootstrap.elements
    }
    gw1 = [SquadPlayer(p.id, positions0[p.id], p.team, p.now_cost) for p in case.bootstrap.elements]
    selected = {r.element: r.selected for r in season.rows_by_gw[1]}
    start = template_squad(gw1, selected, limits)
    price1 = {q.id: q.price for q in gw1}
    chips = [(c.name, c.start_event, c.stop_event) for c in template.chips]
    states = {
        name: State(
            squad=list(start),
            paid={p: price1[p] for p in start},
            bank=limits.budget - sum(price1[p] for p in start),
            chips=list(chips),
        )
        for name in POLICIES
    }
    memory: dict[int, int] = dict(price1)  # last known price of every player
    teams: dict[int, int] = {q.id: q.team for q in gw1}
    positions: dict[int, PositionCode] = dict(positions0)
    if after is not None:
        saved = load_checkpoint(CHECKPOINT.with_name(f"planner_backtest_after_gw{after:02d}.json"))
    else:
        saved = load_checkpoint() if resume else None
    if saved is not None and (after is not None or saved[0] >= first):
        done, states, memory, teams, positions = saved
        print(f"resuming after GW{done}", flush=True)
        first = done + 1
    states = {k: v for k, v in states.items() if k in policies}
    for gw in range(first, last + 1):
        started = time.perf_counter()
        case = build_case(season, gw)
        boot = case.bootstrap
        preds = predict_all(boot.elements, case.calendar, case.lives, gw)
        weeks = []
        for k in range(len(WEEK_WEIGHTS)):
            if gw + k > LAST_GW:
                break
            sim = simulate_gameweek(
                boot,
                case.calendar,
                case.fixtures,
                gw + k,
                preds,
                case.lives,
                case.odds if k == 0 else {},
                None,
                n_sims=BACKTEST_SIMS,
                seed=gw * 100 + k,
            )
            weeks.append(sim.points)
        for p in boot.elements:
            memory[p.id] = p.now_cost
            teams[p.id] = p.team
            positions[p.id] = boot.position_code(p.element_type)
        for pid, element_type in season.player_types.items():
            positions.setdefault(pid, CODE[element_type])
        pool = {p.id: SquadPlayer(p.id, positions[p.id], p.team, p.now_cost) for p in boot.elements}
        real = real_results(season, gw)
        for name, state in states.items():
            play_week(name, state, gw, weeks, pool, memory, teams, positions, limits, real)
        line = ", ".join(f"{n} {s.points[gw]}" for n, s in states.items())
        print(f"GW{gw}: {line} ({time.perf_counter() - started:.0f}s)", flush=True)
        if "agent" in states and gw in states["agent"].notes:
            print(f"   agent chips: {states['agent'].notes[gw]}", flush=True)
        save_checkpoint(gw, states, memory, teams, positions)
    return states


# --- report ----------------------------------------------------------------------------------


def summarize(states: Mapping[str, State]) -> dict[str, Any]:
    gws = sorted(next(iter(states.values())).points)
    out: dict[str, Any] = {"gameweeks": gws, "policies": {}}
    for name, s in states.items():
        out["policies"][name] = {
            "total": sum(s.points.values()),
            "transfers": sum(s.transfers.values()),
            "chips": {str(gw): c for gw, c in s.chips_played.items()},
        }
    for a, b in COMPARISONS:
        if a not in states or b not in states:
            continue  # a run of only some policies (diagnostics)
        d = np.array([states[a].points[g] - states[b].points[g] for g in gws], dtype=float)
        se = float(d.std(ddof=1) * math.sqrt(len(d))) if len(d) > 1 else float("nan")
        out[f"{a}_vs_{b}"] = {"total": float(d.sum()), "se_total": se}
    out["weekly"] = {name: [s.points[g] for g in gws] for name, s in states.items()}
    return out


def render_markdown(s: Mapping[str, Any]) -> str:
    g = s["gameweeks"]
    p = s["policies"]
    names = {
        "agent": "Agent (planner + chips)",
        "agent_no_chips": "Agent without chips",
        "part5": "Part 5 one-week logic",
        "hold": "Hold (no transfers)",
    }
    lines = [
        "# Planner back-test",
        "",
        f"*{SEASON}, GW{g[0]}-{g[-1]}, by `python -m fpl_agent.validation.planner_backtest`. "
        "Method: D40 in `decisions.md`.*",
        "",
        "Four policies from the same realistic GW1 squad (the most-owned legal 15 within the "
        "budget), deciding each week from what was known before its deadline and scored on real "
        "results. Free transfers only, at most 3 a week, the same lineup picker for all.",
        "",
        "| Policy | Real points | Transfers | Chips played |",
        "|---|---|---|---|",
    ]
    for key, label in names.items():
        if key not in p:
            continue
        q = p[key]
        chips = ", ".join(
            f"{c} GW{gw}" for gw, c in sorted(q["chips"].items(), key=lambda t: int(t[0]))
        )
        lines.append(f"| {label} | **{q['total']}** | {q['transfers']} | {chips or '-'} |")
    lines += ["", "| Comparison | Points over the season (± 1 s.e.) |", "|---|---|"]
    for key, label in (
        ("agent_vs_hold", "Agent vs hold"),
        ("agent_vs_part5", "Agent vs part 5"),
        ("agent_vs_agent_no_chips", "Chips' contribution (agent vs agent without chips)"),
        ("part5_vs_hold", "Part 5 vs hold"),
    ):
        if key not in s:
            continue
        c = s[key]
        lines.append(f"| {label} | {c['total']:+.0f} (± {c['se_total']:.0f}) |")
    lines += [
        "",
        "## Caveats",
        "",
        "- One season, one starting squad: a single path, so luck matters. The s.e. is from the "
        "weekly differences.",
        "- No hits, no price-change strategy, prices as the archive recorded them.",
        "- No injury flags in the archive: the rebuilt weeks see everyone as fit (as in D35).",
        "- Later weeks use the xG-ratings fallback, as live; that week uses closing odds.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    first = int(args[0]) if args else 2
    last = int(args[1]) if len(args) > 1 else LAST_GW
    opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
    policies = tuple(opts["policies"].split(",")) if "policies" in opts else POLICIES
    after = int(opts["after"]) if "after" in opts else None
    states = run(first, last, resume="--fresh" not in sys.argv, policies=policies, after=after)
    summary = summarize(states)
    VALIDATION_DIR.mkdir(parents=True, exist_ok=True)
    (VALIDATION_DIR / "planner_backtest.json").write_text(
        json.dumps({"summary": summary, "states": {k: asdict(v) for k, v in states.items()}})
    )
    Path("docs/planner_backtest.md").write_text(render_markdown(summary))
    print(render_markdown(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
