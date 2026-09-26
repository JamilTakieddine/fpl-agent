# Flag snapshots: recording rules (before the deadline only, latest wins), the file store, and
# how lineup predictions excuse matches a player was flagged out for; synthetic data, no network.

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from fpl_agent.data.calendar import build_calendar
from fpl_agent.data.lineups import load_predictions, predict_all
from fpl_agent.data.models import (
    Bootstrap,
    EventLive,
    ExplainFixture,
    ExplainStat,
    LiveElement,
    LiveStats,
)
from fpl_agent.data.snapshots import (
    FileSnapshotStore,
    FlagSnapshot,
    PlayerFlag,
    record_snapshot,
    take_snapshot,
)
from tests.conftest import T0, fx, make_bootstrap

PID = 1  # the player under test (plays for team 1)


def season(bootstrap_json: Any) -> Bootstrap:
    """5 gameweeks, 2 teams; the player is set to available with a clean flag."""
    boot = make_bootstrap(n_teams=2, n_gws=5, base=bootstrap_json)
    me = boot.elements[0].model_copy(
        update={
            "id": PID,
            "team": 1,
            "status": "a",
            "chance_of_playing_next_round": None,
            "starts": 2,
        }
    )
    return boot.model_copy(update={"elements": [me]})


def flags(status: str, chance: int | None) -> dict[int, PlayerFlag]:
    return {
        PID: PlayerFlag(
            status=status, chance_of_playing_next_round=chance, news="", news_added=None
        )
    }


def snap(event: int, status: str, chance: int | None) -> FlagSnapshot:
    deadline = T0 + timedelta(weeks=event - 1)
    return FlagSnapshot(
        event=event,
        taken_at=deadline - timedelta(minutes=15),
        deadline=deadline,
        flags=flags(status, chance),
    )


def live_gw(fixture: int, minutes: int, starts: int) -> EventLive:
    return EventLive(
        elements=[
            LiveElement(
                id=PID,
                stats=LiveStats(minutes=minutes, starts=starts),
                explain=[
                    ExplainFixture(
                        fixture=fixture, stats=[ExplainStat(identifier="minutes", value=minutes)]
                    )
                ],
            )
        ]
    )


def history_setup(bootstrap_json: Any) -> tuple[Bootstrap, Any, dict[int, EventLive]]:
    """Team 1 plays one finished match in each of GW1-4. The player starts GW1 and GW4 and
    is absent (0 minutes) in GW2 and GW3."""
    boot = season(bootstrap_json)
    cal = build_calendar(boot, [fx(gw, gw, 1, 2, finished=True) for gw in range(1, 5)])
    lives = {1: live_gw(1, 90, 1), 2: live_gw(2, 0, 0), 3: live_gw(3, 0, 0), 4: live_gw(4, 90, 1)}
    return boot, cal, lives


# --- recording --------------------------------------------------------------------------------


def test_snapshot_captures_every_players_flags(bootstrap_json: Any) -> None:
    boot = Bootstrap.model_validate(bootstrap_json)
    event = boot.events[5]
    s = take_snapshot(boot, event.id, event.deadline_time - timedelta(hours=1))
    assert set(s.flags) == {p.id for p in boot.elements}
    assert s.deadline == event.deadline_time


def test_record_before_deadline_saves(tmp_path: Path, bootstrap_json: Any) -> None:
    boot, store = season(bootstrap_json), FileSnapshotStore(tmp_path)
    assert record_snapshot(store, boot, 2, T0 + timedelta(weeks=1, minutes=-15)) is not None
    assert store.path(2).name == "flags_gw02.json"
    loaded = store.load(2)
    assert loaded is not None and PID in loaded.flags


def test_record_after_deadline_saves_nothing(tmp_path: Path, bootstrap_json: Any) -> None:
    store = FileSnapshotStore(tmp_path)
    assert record_snapshot(store, season(bootstrap_json), 1, T0 + timedelta(seconds=1)) is None
    assert store.load(1) is None


def test_latest_snapshot_before_deadline_wins(tmp_path: Path, bootstrap_json: Any) -> None:
    boot, store = season(bootstrap_json), FileSnapshotStore(tmp_path)
    day_before, deadline_run = T0 - timedelta(days=1), T0 - timedelta(minutes=15)
    record_snapshot(store, boot, 1, day_before)
    record_snapshot(store, boot, 1, deadline_run)
    latest = store.load(1)
    assert latest is not None and latest.taken_at == deadline_run
    # An older run must never overwrite a newer snapshot.
    assert record_snapshot(store, boot, 1, day_before) is None
    still = store.load(1)
    assert still is not None and still.taken_at == deadline_run


def test_record_rejects_naive_datetime(tmp_path: Path, bootstrap_json: Any) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        record_snapshot(
            FileSnapshotStore(tmp_path), season(bootstrap_json), 1, datetime(2026, 1, 1)
        )


# --- effect on predictions ----------------------------------------------------------------


def test_without_snapshots_absences_count_as_unused(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)
    h = predict_all(boot.elements, cal, lives, 5)[PID].history
    assert (h.team_matches, h.starts, h.unused, h.excused) == (4, 2, 2, 0)


def test_flagged_out_absences_are_excused(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)
    injured = {2: snap(2, "i", 0), 3: snap(3, "i", None)}
    with_snaps = predict_all(boot.elements, cal, lives, 5, injured)[PID]
    without = predict_all(boot.elements, cal, lives, 5)[PID]

    h = with_snaps.history
    assert (h.team_matches, h.starts, h.unused, h.excused) == (2, 2, 0, 2)
    # Injured-then-back looks like a starter, not someone dropped for two games.
    assert with_snaps.p_start > without.p_start


def test_doubtful_flag_is_not_excused(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)
    h = predict_all(boot.elements, cal, lives, 5, {2: snap(2, "d", 75)})[PID].history
    assert h.excused == 0


def test_flagged_out_but_played_still_counts(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)
    # Flagged out before GW1, yet started: the flag was wrong, so the start counts.
    h = predict_all(boot.elements, cal, lives, 5, {1: snap(1, "i", 0)})[PID].history
    assert (h.excused, h.starts) == (0, 2)


def test_snapshots_outside_the_window_are_ignored(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)
    h = predict_all(boot.elements, cal, lives, 5, {9: snap(9, "i", 0)})[PID].history
    assert h.excused == 0


class MemoryStore:
    def __init__(self, snaps: dict[int, FlagSnapshot]) -> None:
        self.snaps = snaps
        self.loaded: list[int] = []

    def save(self, snapshot: FlagSnapshot) -> None:
        self.snaps[snapshot.event] = snapshot

    def load(self, event: int) -> FlagSnapshot | None:
        self.loaded.append(event)
        return self.snaps.get(event)


def test_load_predictions_reads_window_snapshots(bootstrap_json: Any) -> None:
    boot, cal, lives = history_setup(bootstrap_json)

    class FakeClient:
        def event_live(self, gw: int) -> EventLive:
            return lives.get(gw, EventLive(elements=[]))

        def bootstrap(self) -> Bootstrap:
            return boot

    store = MemoryStore({2: snap(2, "i", 0), 3: snap(3, "i", 0)})
    preds = load_predictions(FakeClient(), cal, 5, store)  # type: ignore[arg-type]
    assert store.loaded == [1, 2, 3, 4]
    assert preds[PID].history.excused == 2
