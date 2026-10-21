# The deadline watchdog (D42): a healthy schedule is left alone; a wrong trigger time is re-pointed
# and reported once; a paused or missing trigger and a missing save run are urgent, emailed once
# and exit non-zero; nothing is judged while a run may still be in progress; no cloud.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from fpl_agent import run as run_mod
from fpl_agent.agent import RunResult
from fpl_agent.cloud.schedule import MODES, Trigger, cron, next_runs
from fpl_agent.cloud.watchdog import assess, run_watchdog, saved_record
from fpl_agent.optimize.rules import Lineup
from fpl_agent.submit import SubmitResult

GW7 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
GW8 = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
DEADLINES = [(7, GW7), (8, GW8)]
WEEK_BEFORE = GW7 - timedelta(days=3)


def healthy(now: datetime) -> dict[str, Trigger]:
    return {m: Trigger(True, True, at) for m, (_, at) in next_runs(DEADLINES, now).items()}


class FakeScheduler:
    def __init__(self, triggers: dict[str, Trigger]) -> None:
        self.triggers = dict(triggers)
        self.set_to: dict[str, str] = {}

    def get(self, mode: str) -> Trigger:
        return self.triggers[mode]

    def set(self, mode: str, cron_line: str) -> None:
        self.set_to[mode] = cron_line


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def read(self, name: str) -> str | None:
        return self.objects.get(name)

    def create(self, name: str, text: str) -> bool:
        if name in self.objects:
            return False
        self.objects[name] = text
        return True


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, subject: str, body: str) -> None:
        self.sent.append((subject, body))


def never_saved(gw: int) -> bool:
    return False


def test_a_healthy_schedule_is_left_alone() -> None:
    a = assess(DEADLINES, WEEK_BEFORE, healthy(WEEK_BEFORE), never_saved)
    assert not a.repoint and a.problems == () and "GW7" in a.note
    # After the save run: check now points at GW8, save at GW8, final still at GW7. Still healthy.
    after_save = GW7 - timedelta(minutes=30)
    a = assess(DEADLINES, after_save, healthy(after_save), lambda gw: gw == 7)
    assert a.problems == ()


def test_a_wrong_time_is_repointed_and_reported_once() -> None:
    triggers = healthy(WEEK_BEFORE)
    triggers["save"] = Trigger(
        True, True, datetime(2027, 10, 10, 9, 0, tzinfo=UTC)
    )  # the GW6 state
    sched, bucket, mail = FakeScheduler(triggers), FakeBucket(), FakeMailer()
    assert run_watchdog(DEADLINES, WEEK_BEFORE, sched, bucket, mail, log=lambda s: None) == 0
    assert sched.set_to == {"check": "0 10 16 10 *", "save": "0 9 17 10 *", "final": "45 9 17 10 *"}
    assert len(mail.sent) == 1 and "fixed the schedule" in mail.sent[0][0]
    assert (
        "fpl-agent-save was set for Sun 10 Oct 09:00 UTC, should be Sat 17 Oct 09:00 UTC"
        in (mail.sent[0][1])
    )


def test_a_paused_trigger_is_urgent_emailed_once_and_not_resumed() -> None:
    triggers = healthy(WEEK_BEFORE)
    triggers["final"] = Trigger(True, False, triggers["final"].next_time)
    sched, bucket, mail = FakeScheduler(triggers), FakeBucket(), FakeMailer()
    assert run_watchdog(DEADLINES, WEEK_BEFORE, sched, bucket, mail, log=lambda s: None) == 1
    assert (
        "ACTION NEEDED" in mail.sent[0][0]
        and "fpl-agent-final trigger is paused" in mail.sent[0][1]
    )
    assert sched.set_to == {}  # pausing may be deliberate: reported, never undone
    # Half an hour later: same problem, no second email, no second failure.
    later = WEEK_BEFORE + timedelta(minutes=30)
    assert run_watchdog(DEADLINES, later, sched, bucket, mail, log=lambda s: None) == 0
    assert len(mail.sent) == 1


def test_a_missing_trigger_is_urgent() -> None:
    triggers = healthy(WEEK_BEFORE)
    triggers["check"] = Trigger(False, False, None)
    a = assess(DEADLINES, WEEK_BEFORE, triggers, never_saved)
    assert [p.urgent for p in a.problems] == [True] and "deploy.sh" in a.problems[0].message


def test_a_missing_save_run_is_urgent_before_the_deadline() -> None:
    now = GW7 - timedelta(minutes=35)  # save due 09:00 (+10 min grace), deadline 10:00
    a = assess(DEADLINES, now, healthy(now), never_saved)
    (p,) = a.problems
    assert p.urgent and p.key == "unsaved-gw07" and "in 35 minutes" in p.message
    # With the save run's record there, nothing to report.
    assert assess(DEADLINES, now, healthy(now), lambda gw: gw == 7).problems == ()
    # Too early to judge (inside the grace period), and nothing after the deadline.
    early = GW7 - timedelta(minutes=55)
    assert assess(DEADLINES, early, healthy(early), never_saved).problems == ()


def test_nothing_is_judged_while_a_run_may_be_in_progress() -> None:
    just_after_save = GW7 - MODES["save"] + timedelta(minutes=5)
    stale = {m: Trigger(True, True, None) for m in MODES}  # mid-run, not re-pointed yet
    a = assess(DEADLINES, just_after_save, stale, never_saved)
    assert not a.repoint and a.problems == () and "in progress" in a.note


def test_season_over_and_the_saved_record_name() -> None:
    assert assess(DEADLINES, GW8 + timedelta(hours=1), {}, never_saved).note.startswith(
        "no deadline"
    )
    assert saved_record(7) == "runs/saved_gw07.json"
    assert cron(GW7 - MODES["save"]) == "0 9 17 10 *"


def test_only_a_live_save_leaves_the_saved_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """gameweek_work writes runs/saved_gwNN.json when the save run's lineup was sent, never for a
    dry run or another mode (the watchdog's proof that the save happened)."""

    class Bucket:
        def __init__(self) -> None:
            self.written: dict[str, str] = {}

        def write(self, name: str, text: str) -> None:
            self.written[name] = text

    class Client:
        def my_team(self, entry: int) -> None:
            return None

    def outcome(sent: bool) -> RunResult:
        saved = SubmitResult(sent=sent, payload={}, problems=[], verified=sent)
        return RunResult(7, GW7, Lineup((1,), (2,), 1, 1), False, 0.6, saved, None)

    client: Any = Client()
    settings: Any = SimpleNamespace(entry_id=1, timezone="UTC")
    stores: Any = None  # the snapshot/odds recording is replaced below
    monkeypatch.setattr(run_mod, "record", lambda *a: "recorded")
    for mode, sent, expect in (
        ("save", True, True),
        ("save", False, False),
        ("final", True, False),
    ):
        monkeypatch.setattr(run_mod, "run_gameweek", lambda *a, _s=sent, **k: outcome(_s))
        bucket: Any = Bucket()
        code = run_mod.gameweek_work(
            mode, client, settings, FakeMailer(), sent, stores, stores, bucket
        )
        assert code == 0
        assert (saved_record(7) in bucket.written) is expect, (mode, sent)
