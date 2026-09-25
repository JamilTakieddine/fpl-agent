"""H2H opponent inputs: who I play, their last visible squad, captaincy history, chips left.

The opponent's picks for the upcoming gameweek are hidden until the deadline (404), so the model
works from their last finished gameweek plus how they've captained recently. This module only
gathers and shapes that data; the captain probability spread and chip-usage guesses are modeling
(Phase 2) and decisions (Phase 3).

Pure functions do the logic (find_opponent, chips_remaining, captain_history); load_opponent is
the only function that touches the network. See docs/decisions.md (D11).
"""

from __future__ import annotations

from dataclasses import dataclass

from fpl_agent.data.client import FplClient
from fpl_agent.data.models import ChipDefinition, ChipPlay, EntryPicks, H2HMatch

CAPTAIN_HISTORY_GWS = 5  # recent form matters more than early-season picks; ~7 requests per run


@dataclass(frozen=True)
class Opponent:
    entry_id: int
    team_name: str
    manager_name: str | None


@dataclass(frozen=True)
class CaptainChoice:
    event: int
    captain: int  # element id
    vice_captain: int
    chip: str | None  # e.g. "3xc" means the captain scored triple


@dataclass(frozen=True)
class OpponentSnapshot:
    opponent: Opponent
    event: int  # the gameweek we're playing them in
    last_picks: EntryPicks | None  # their last FINISHED gameweek; None before GW1 ends
    captain_history: tuple[CaptainChoice, ...]
    chips_remaining: frozenset[str]  # chips still usable in `event`'s window


def find_opponent(matches: list[H2HMatch], my_entry: int) -> Opponent | None:
    """My opponent in these matches, or None if I play the league AVERAGE.

    Detected by the other side's entry id being null. The API's is_bye flag is NOT reliable:
    a real match against "AVERAGE" came back with is_bye=false.
    """
    for m in matches:
        if m.entry_1_entry == my_entry:
            other = (m.entry_2_entry, m.entry_2_name, m.entry_2_player_name)
        elif m.entry_2_entry == my_entry:
            other = (m.entry_1_entry, m.entry_1_name, m.entry_1_player_name)
        else:
            continue
        entry_id, team_name, manager_name = other
        if entry_id is None:
            return None
        return Opponent(entry_id=entry_id, team_name=team_name, manager_name=manager_name)
    raise LookupError(f"Entry {my_entry} has no match in the given gameweek")


def chips_remaining(
    definitions: list[ChipDefinition], played: list[ChipPlay], event: int
) -> frozenset[str]:
    """Chips valid in `event` that haven't been played inside the same window (rule 3).

    Each chip exists once per half (GW1-19, GW20-38); playing a wildcard in GW5 uses up the
    first-half wildcard only.
    """
    remaining = set()
    for chip in definitions:
        if not chip.start_event <= event <= chip.stop_event:
            continue
        used = any(
            p.name == chip.name and chip.start_event <= p.event <= chip.stop_event for p in played
        )
        if not used:
            remaining.add(chip.name)
    return frozenset(remaining)


def captain_history(picks_by_event: dict[int, EntryPicks]) -> tuple[CaptainChoice, ...]:
    """Captain/vice per finished gameweek, oldest first."""
    choices = []
    for event in sorted(picks_by_event):
        picks = picks_by_event[event]
        captain = next(p.element for p in picks.picks if p.is_captain)
        vice = next(p.element for p in picks.picks if p.is_vice_captain)
        choices.append(CaptainChoice(event, captain, vice, picks.active_chip))
    return tuple(choices)


def history_events(event: int, n: int = CAPTAIN_HISTORY_GWS) -> list[int]:
    """The last n finished gameweeks before `event` (fewer early in the season)."""
    return list(range(max(1, event - n), event))


def load_opponent(
    client: FplClient, league_id: int, my_entry: int, event: int
) -> OpponentSnapshot | None:
    """Everything the opponent model needs for `event`, or None if I play the league average."""
    opponent = find_opponent(client.h2h_matches(league_id, event), my_entry)
    if opponent is None:
        return None
    history = client.entry_history(opponent.entry_id)
    picks_by_event = {}
    for gw in history_events(event):
        picks = client.entry_picks(opponent.entry_id, gw)
        if picks is not None:  # e.g. they joined late and have no team for that gameweek
            picks_by_event[gw] = picks
    last = max(picks_by_event) if picks_by_event else None
    return OpponentSnapshot(
        opponent=opponent,
        event=event,
        last_picks=picks_by_event[last] if last is not None else None,
        captain_history=captain_history(picks_by_event),
        chips_remaining=chips_remaining(client.bootstrap().chips, history.chips, event),
    )
