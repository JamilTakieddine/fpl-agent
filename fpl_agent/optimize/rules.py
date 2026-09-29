"""FPL rules as small pure functions: the reference specification the optimizer is built on.

(docs/decisions.md D28) Each function handles one gameweek outcome at a time and is written for
clarity, not speed; Phase 3 part 2 vectorises team scoring over the simulations and is tested
against these functions. Limits come from the API (element_types, game_config.rules), not from
constants. Validation returns readable violations, so logs and the email can say WHY.

The CLAUDE.md rules covered here:
  1  transfer hits beyond the free ones; at most 5 banked free transfers
  2  auto-subs in bench order; the goalkeeper swap is separate; formations stay legal
  3  chips: one per gameweek, only inside their window
  4  captain and vice-captain as a pair (vice takes over only if the captain plays 0 minutes)
  5  selling price: purchase + half the rise, rounded down
  6  max 3 players per club
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from fpl_agent.data.models import Bootstrap, Chip, ChipDefinition, PositionCode

GOALKEEPER: PositionCode = "GKP"
HIT_COST = 4  # points per transfer beyond the free ones (my-team reports it as transfers.cost)


# --- limits --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Limits:
    """Squad and formation limits, read from bootstrap-static."""

    squad_counts: Mapping[PositionCode, int]  # players per position in a 15-man squad
    min_play: Mapping[PositionCode, int]  # minimum starters per position
    max_play: Mapping[PositionCode, int]  # maximum starters per position
    starters: int  # 11
    squad_size: int  # 15
    club_limit: int  # 3
    budget: int  # tenths
    max_free_transfers: int  # 1 + max_extra_free_transfers = 5

    @classmethod
    def from_bootstrap(cls, bootstrap: Bootstrap) -> Limits:
        types = bootstrap.element_types
        rules = bootstrap.game_config.rules
        return cls(
            squad_counts={t.singular_name_short: t.squad_select for t in types},
            min_play={t.singular_name_short: t.squad_min_play for t in types},
            max_play={t.singular_name_short: t.squad_max_play for t in types},
            starters=rules.squad_squadplay,
            squad_size=rules.squad_squadsize,
            club_limit=rules.squad_team_limit,
            budget=rules.squad_total_spend,
            max_free_transfers=1 + rules.max_extra_free_transfers,
        )


@dataclass(frozen=True)
class SquadPlayer:
    id: int
    position: PositionCode
    team: int
    price: int  # tenths: the selling price for players we own, the buying price for targets


# --- squads (rule 6) and formations --------------------------------------------------------------


def squad_violations(players: Sequence[SquadPlayer], limits: Limits, bank: int = 0) -> list[str]:
    """Why a 15-man squad is illegal (empty list = legal). `bank` is money left, in tenths."""
    problems = []
    if len(players) != limits.squad_size:
        problems.append(f"{len(players)} players (need {limits.squad_size})")
    if len({p.id for p in players}) != len(players):
        problems.append("a player appears twice")
    counts = Counter(p.position for p in players)
    for pos, need in limits.squad_counts.items():
        if counts[pos] != need:
            problems.append(f"{counts[pos]} {pos} (need {need})")
    for team, n in Counter(p.team for p in players).items():
        if n > limits.club_limit:
            problems.append(f"{n} players from team {team} (max {limits.club_limit})")
    if bank < 0:
        problems.append(f"over budget by {-bank / 10:.1f}m")
    return problems


def formation_violations(positions: Iterable[PositionCode], limits: Limits) -> list[str]:
    """Why a starting XI's shape is illegal: 1 GK, at least 3 DEF / 2 MID / 1 FWD (from the API)."""
    counts = Counter(positions)
    problems = []
    if sum(counts.values()) != limits.starters:
        problems.append(f"{sum(counts.values())} starters (need {limits.starters})")
    for pos in limits.min_play:
        if not limits.min_play[pos] <= counts[pos] <= limits.max_play[pos]:
            allowed = f"{limits.min_play[pos]}-{limits.max_play[pos]}"
            problems.append(f"{counts[pos]} {pos} starting (allowed {allowed})")
    return problems


def is_valid_formation(positions: Iterable[PositionCode], limits: Limits) -> bool:
    return not formation_violations(positions, limits)


# --- lineups -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lineup:
    """A gameweek team sheet. bench[0] is the substitute goalkeeper; bench[1:] are the outfield
    substitutes in auto-sub priority order (FPL positions 12, 13, 14, 15)."""

    starters: tuple[int, ...]
    bench: tuple[int, ...]
    captain: int
    vice_captain: int


def lineup_violations(
    lineup: Lineup, positions: Mapping[int, PositionCode], limits: Limits
) -> list[str]:
    problems = formation_violations((positions[p] for p in lineup.starters), limits)
    squad = lineup.starters + lineup.bench
    if len(set(squad)) != limits.squad_size or len(squad) != limits.squad_size:
        problems.append("starters + bench must be the 15 squad players, each once")
    if lineup.bench and positions[lineup.bench[0]] != GOALKEEPER:
        problems.append("the first bench slot must be the substitute goalkeeper")
    if lineup.captain == lineup.vice_captain:
        problems.append("captain and vice-captain must differ")
    for role, pid in (("captain", lineup.captain), ("vice-captain", lineup.vice_captain)):
        if pid not in lineup.starters:
            problems.append(f"the {role} must be a starter")
    return problems


# --- auto-subs (rule 2) --------------------------------------------------------------------------


def auto_subs(
    lineup: Lineup,
    played: Mapping[int, bool],
    positions: Mapping[int, PositionCode],
    limits: Limits,
) -> tuple[tuple[int, ...], list[tuple[int, int]]]:
    """The XI that actually scores, and the (out, in) substitutions made.

    FPL's order: the goalkeeper swap is separate (the bench goalkeeper replaces a non-playing
    starting goalkeeper, if he played). Outfield: bench players are tried in priority order; each
    one who played replaces the first non-playing outfield starter whose removal keeps the
    formation legal. Each substitute is used at most once; a starter who played is never removed.
    """
    xi = list(lineup.starters)
    subs: list[tuple[int, int]] = []
    bench_gk, *bench_outfield = lineup.bench

    for i, pid in enumerate(xi):
        if positions[pid] == GOALKEEPER and not played.get(pid, False):
            if played.get(bench_gk, False):
                xi[i] = bench_gk
                subs.append((pid, bench_gk))
            break

    for sub in bench_outfield:
        if not played.get(sub, False):
            continue
        for i, pid in enumerate(xi):
            # A starter who played, or a substitute who already came on, is never replaced.
            if positions[pid] == GOALKEEPER or played.get(pid, False):
                continue
            trial = [positions[x] for x in xi]
            trial[i] = positions[sub]
            if is_valid_formation(trial, limits):
                xi[i] = sub
                subs.append((pid, sub))
                break
    return tuple(xi), subs


# --- captaincy (rule 4) and team points -----------------------------------------------------------


def multipliers(
    lineup: Lineup, played: Mapping[int, bool], chip: str | None = None
) -> dict[int, int]:
    """Points multiplier per starter: captain x2 (x3 with Triple Captain); the vice-captain gets
    it only if the captain played 0 minutes; if neither played, nobody does."""
    armband = 3 if chip == "3xc" else 2
    out = dict.fromkeys(lineup.starters, 1)
    if played.get(lineup.captain, False):
        out[lineup.captain] = armband
    elif played.get(lineup.vice_captain, False):
        out[lineup.vice_captain] = armband
    return out


def team_points(
    lineup: Lineup,
    points: Mapping[int, int],
    played: Mapping[int, bool],
    positions: Mapping[int, PositionCode],
    limits: Limits,
    chip: str | None = None,
) -> int:
    """A team's gameweek score: auto-subs, then captaincy. Bench Boost counts all 15 as they are
    (no auto-subs); Triple Captain triples the armband. The reference the vectorised scorer is
    tested against."""
    mult = multipliers(lineup, played, chip)
    if chip == "bboost":
        scorers: Sequence[int] = lineup.starters + lineup.bench
    else:
        scorers, _ = auto_subs(lineup, played, positions, limits)
    # The armband stays with the captain/vice even if a substitute took another starter's place.
    return sum(points.get(p, 0) * mult.get(p, 1) for p in scorers)


# --- transfers (rules 1 and 5) --------------------------------------------------------------------


def transfer_cost(transfers: int, free: int, chip: str | None = None, hit: int = HIT_COST) -> int:
    """Points deducted: each transfer beyond the free ones costs `hit`; none with a wildcard or
    free hit active."""
    if chip in ("wildcard", "freehit"):
        return 0
    return max(0, transfers - free) * hit


def next_free_transfers(free: int, used: int, limits: Limits, chip: str | None = None) -> int:
    """Free transfers next gameweek: unused ones roll over plus one new, capped at the maximum
    (5). A wildcard or free hit week keeps the current number."""
    if chip in ("wildcard", "freehit"):
        return free
    return min(limits.max_free_transfers, max(free - used, 0) + 1)


def selling_price(purchase: int, current: int, sell_on_fee: float = 0.5) -> int:
    """What selling returns, in tenths: the purchase price plus the share of any rise FPL lets you
    keep, rounded down (a 0.3m rise returns 0.1m); a fall is taken in full."""
    if current <= purchase:
        return current
    return purchase + int((current - purchase) * (1 - sell_on_fee))


# --- chips (rule 3) -------------------------------------------------------------------------------


def chip_playable(name: str, event: int, chips: Sequence[Chip]) -> bool:
    """Whether this entry can play chip `name` in `event`: an available copy whose window covers
    it (the first set runs GW1-19, the second GW20-38; wildcard and free hit from GW2)."""
    return any(
        c.name == name
        and c.status_for_entry == "available"
        and c.start_event <= event <= c.stop_event
        for c in chips
    )


def chips_expiring(event: int, chips: Sequence[Chip], within: int = 0) -> list[str]:
    """Available chips whose window ends within `within` gameweeks of `event`: unused first-set
    chips are forfeited at the GW19 deadline, so the planner must schedule them, not just flag."""
    return sorted(
        c.name
        for c in chips
        if c.status_for_entry == "available"
        and c.start_event <= event <= c.stop_event
        and c.stop_event - event <= within
    )


def chip_window(
    name: str, event: int, definitions: Sequence[ChipDefinition]
) -> ChipDefinition | None:
    """The definition of chip `name` whose window covers `event` (None between windows)."""
    return next(
        (d for d in definitions if d.name == name and d.start_event <= event <= d.stop_event), None
    )
