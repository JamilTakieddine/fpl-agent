# Phase 4 scheduling and the cloud job's email rule: the three runs per gameweek come from FPL's
# deadlines and always point at the next future time; which runs email you, and how; no cloud.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fpl_agent.agent import RunResult
from fpl_agent.cloud.schedule import cron, next_runs, repoint
from fpl_agent.optimize.rules import Lineup
from fpl_agent.run import email_subject
from fpl_agent.submit import SubmitResult

GW7 = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)
GW8 = datetime(2026, 10, 23, 17, 30, tzinfo=UTC)
DEADLINES = [(8, GW8), (7, GW7)]  # any order


def test_next_runs_before_and_between_deadlines() -> None:
    week_before = next_runs(DEADLINES, GW7 - timedelta(days=3))
    assert week_before == {
        "check": (7, GW7 - timedelta(hours=24)),
        "save": (7, GW7 - timedelta(minutes=60)),
        "final": (7, GW7 - timedelta(minutes=15)),
    }
    # Just after GW7's save run: check moves on to GW8, final still to come for GW7.
    after_save = next_runs(DEADLINES, GW7 - timedelta(minutes=59))
    assert after_save["check"][0] == 8 and after_save["save"][0] == 8
    assert after_save["final"] == (7, GW7 - timedelta(minutes=15))
    assert next_runs(DEADLINES, GW8) == {}  # season over


def test_cron_line_and_repoint() -> None:
    assert cron(datetime(2026, 10, 17, 9, 45, tzinfo=UTC)) == "45 9 17 10 *"

    class Fake:
        def __init__(self) -> None:
            self.set_to: dict[str, str] = {}

        def set(self, mode: str, cron_line: str) -> None:
            self.set_to[mode] = cron_line

    fake = Fake()
    repoint(fake, DEADLINES, GW7 - timedelta(days=3))
    assert fake.set_to == {"check": "0 10 16 10 *", "save": "0 9 17 10 *", "final": "45 9 17 10 *"}


def result(changed: bool, sent: bool, problems: list[str] | None = None) -> RunResult:
    lineup = Lineup((1,), (2,), 1, 1)
    saved = SubmitResult(sent=sent, payload={}, problems=problems or [], verified=sent)
    return RunResult(7, GW7, lineup, changed, 0.61, saved, None)


def test_who_gets_an_email() -> None:
    # The save run always reports.
    subject = email_subject("save", result(changed=True, sent=True), live=True)
    assert subject == "FPL GW7: lineup saved (P(win by 3+) 61%)"
    # The final run only when it re-saved a changed lineup...
    assert email_subject("final", result(changed=False, sent=False), live=True) is None
    updated = email_subject("final", result(changed=True, sent=True), live=True)
    assert updated is not None and "updated" in updated
    # ...or when something went wrong.
    blocked = email_subject("final", result(changed=True, sent=False, problems=["x"]), live=True)
    assert blocked is not None and "FAILED" in blocked
    failed = RunResult(7, GW7, Lineup((1,), (2,), 1, 1), True, 0.5, None, "HTTPError: 500")
    subject = email_subject("final", failed, live=True)
    assert subject is not None and "FAILED" in subject
