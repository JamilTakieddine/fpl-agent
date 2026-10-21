"""The gameweek email (D46): the decisions, your chances and the reasons, in plain English.

Built from a GameweekReport that run_gameweek fills in. It carries no payloads, player IDs or
solver statistics: the full technical output stays in the job's log, for debugging. A test run
(not live) says so in the subject and describes what a live run would have done, including the
squad AFTER the planned transfers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

CHIP_LABELS = {
    "3xc": "Triple Captain",
    "bboost": "Bench Boost",
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
}
TEST_PREFIX = "TEST, nothing sent to FPL · "


@dataclass(frozen=True)
class Transfer:
    out: str
    into: str
    sell: int  # tenths of a million
    buy: int


@dataclass(frozen=True)
class ChipVerdict:
    chip: str  # "3xc", "bboost", "wildcard", "freehit"
    played: bool
    gain: float | None = None  # expected points from playing it now; None = not checked
    keep: float | None = None  # what keeping it is worth
    last_chance: bool = False  # its window ends this week


@dataclass(frozen=True)
class Captaincy:
    captain: str
    vice: str
    forecast_if_plays: float  # the captain's expected points when he plays
    p_plays: float
    vice_forecast: float  # the vice-captain's expected points


@dataclass(frozen=True)
class GameweekReport:
    event: int
    deadline: datetime
    live: bool  # False: a test run, nothing was sent to FPL
    opponent: str  # their team name, or "FPL's average score"
    opponent_captains: tuple[tuple[str, float], ...]  # most likely captains, with chances
    opponent_chip_chance: float  # chance they play Triple Captain or Bench Boost
    opponent_forecast: float
    transfers: tuple[Transfer, ...]
    transfer_chip: str | None  # wildcard / freehit played
    team_chip: str | None  # bboost / 3xc played
    free_transfers: int | None  # available this week (None: unknown or a chip week)
    hit: int  # points deducted for extra transfers
    bank_after: int
    gain_horizon: float | None  # expected points the transfers add over the horizon (weighted)
    gain_now: float | None  # ...and this week
    roll_best: tuple[float, float] | None  # no transfers: best option's (gain, bar it missed)
    horizon: int  # weeks the transfer plan looks ahead
    chips: tuple[ChipVerdict, ...]  # every chip still available, checked or not
    starters: tuple[tuple[str, str], ...]  # (position, name), goalkeeper first
    bench: tuple[str, ...]  # bench order: goalkeeper, then 1, 2, 3
    captaincy: Captaincy
    forecast: float  # my expected points with the decisions
    p_win: float
    p_win_before: float  # with the team as it was before this run
    free_next: int | None  # free transfers next gameweek
    update: str | None = None  # the final run: what the latest news changed


def zone_label(tz: ZoneInfo) -> str:
    return "UTC" if tz.key in ("UTC", "Etc/UTC") else tz.key.rsplit("/", 1)[-1].replace("_", " ")


def when(at: datetime, tz: ZoneInfo) -> str:
    local = at.astimezone(tz)
    hour = local.hour % 12 or 12
    return f"{local:%a %d %b}, {hour}:{local:%M} {'am' if local.hour < 12 else 'pm'}"


def money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def subject(r: GameweekReport) -> str:
    if r.transfer_chip:
        moves = f"{CHIP_LABELS[r.transfer_chip]} ({plural(len(r.transfers), 'change')})"
    else:
        moves = plural(len(r.transfers), "transfer") if r.transfers else "no transfers"
    chip = f" · {CHIP_LABELS[r.team_chip]}" if r.team_chip else ""
    head = f"FPL GW{r.event} update" if r.update else f"FPL GW{r.event}"
    text = (
        f"{head}: {moves} · {r.captaincy.captain} (C){chip} · {r.p_win:.0%} to win vs {r.opponent}"
    )
    return text if r.live else TEST_PREFIX + text


def body(r: GameweekReport, tz: ZoneInfo) -> str:
    done = "done" if r.live else "what a live run would do; nothing was sent"
    lines = [f"GW{r.event} · deadline {when(r.deadline, tz)} ({zone_label(tz)} time)"]
    if not r.live:
        lines += ["", "THIS IS A TEST RUN: nothing was sent to FPL."]
    if r.update:
        lines += ["", f"UPDATED WITH THE LATEST NEWS: {r.update}"]

    lines += ["", f"DECISIONS ({done})"]
    if r.transfer_chip:
        lines.append(
            f"  {CHIP_LABELS[r.transfer_chip]} played: {plural(len(r.transfers), 'change')}"
        )
    elif r.transfers:
        free = f"{r.free_transfers} free" if r.free_transfers is not None else "free"
        hit = f", -{r.hit} points hit" if r.hit else ", no points hit"
        lines.append(f"  Transfers ({len(r.transfers)} of {free}{hit}):")
    else:
        lines.append("  Transfers: none this week")
    width = max((len(t.out) for t in r.transfers), default=0)
    lines += [f"    OUT {t.out:<{width}}  ->  IN {t.into}" for t in r.transfers]
    if r.transfers:
        lines.append(f"  Bank after: {money(r.bank_after)}")
    lines.append(f"  Captain: {r.captaincy.captain} · Vice-captain: {r.captaincy.vice}")
    played = [CHIP_LABELS[c.chip] for c in r.chips if c.played]
    lines.append(f"  Chips: {', '.join(played) + ' played' if played else 'none played'}")
    by_position: dict[str, list[str]] = {}
    for position, name in r.starters:
        by_position.setdefault(position, []).append(name)
    shape = "-".join(str(len(by_position.get(p, []))) for p in ("DEF", "MID", "FWD"))
    lines.append(f"  Lineup ({shape}): " + " | ".join(", ".join(v) for v in by_position.values()))
    bench = " · ".join(f"{i}. {n}" for i, n in enumerate(r.bench[1:], 1))
    lines.append(f"  Bench: {r.bench[0]} (GK) · {bench}")

    lines += [
        "",
        "YOUR CHANCES",
        f"  Chance to win: {r.p_win:.0%} ({r.p_win_before:.0%} with the team as it was)",
        f"  Forecast score: {r.forecast:.0f} (theirs: {r.opponent_forecast:.0f})",
    ]

    lines += ["", "WHY"]
    if r.transfer_chip and r.gain_horizon is not None:
        lines.append(
            f"  {CHIP_LABELS[r.transfer_chip]}: {r.gain_horizon:+.1f} points this week over "
            "the best normal transfers, more than keeping it is worth."
        )
    elif r.transfers and r.gain_horizon is not None:
        now = f" ({r.gain_now:+.1f} this week)" if r.gain_now is not None else ""
        lines.append(
            f"  Transfers: {r.gain_horizon:+.1f} points expected over the next "
            f"{r.horizon} weeks{now}."
        )
    elif r.roll_best is not None:
        gain, bar = r.roll_best
        lines.append(
            f"  No transfers: the best option added {gain:+.1f} points over {r.horizon} weeks, "
            f"short of the {bar:.1f} needed to be worth a transfer, so the free transfer rolls."
        )
    c = r.captaincy
    if c.p_plays >= 0.995:
        lines.append(f"  Captain {c.captain}: forecast {c.forecast_if_plays:.1f} points.")
    else:
        lines.append(
            f"  Captain {c.captain}: forecast {c.forecast_if_plays:.1f} when he plays "
            f"({c.p_plays:.0%} chance he does); if he doesn't, the armband passes to "
            f"{c.vice} ({c.vice_forecast:.1f})."
        )
    for v in r.chips:
        label = CHIP_LABELS[v.chip]
        if v.gain is None or v.keep is None:
            lines.append(f"  {label}: kept for a better week.")
        elif v.played:
            why = " (its last week)" if v.last_chance else ""
            lines.append(
                f"  {label}: PLAYED{why}, {v.gain:+.1f} points now vs {v.keep:.1f} to keep it."
            )
        else:
            lines.append(
                f"  {label}: kept, {v.gain:+.1f} points now vs {v.keep:.1f} "
                "saved for a better week."
            )

    lines += ["", f"YOUR OPPONENT: {r.opponent}"]
    if r.opponent_captains:
        captains = ", ".join(f"{n} ({p:.0%})" for n, p in r.opponent_captains)
        lines.append(f"  Likely captain: {captains}")
        lines.append(f"  Chance they play a chip: {r.opponent_chip_chance:.0%}")
    else:
        lines.append("  (no head-to-head opponent: playing to beat FPL's average score)")

    if r.free_next is not None:
        lines += ["", f"LOOKING AHEAD: {plural(r.free_next, 'free transfer')} next week."]
    return "\n".join(lines) + "\n"
