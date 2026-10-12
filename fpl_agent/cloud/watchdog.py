"""The deadline watchdog (D42): a fourth trigger on a fixed timetable, never re-pointed, that makes
sure the three gameweek runs are going to happen and that the save run did. D41's alerts catch a
trigger that fires and fails; this catches the silent case, a trigger that never fires.

It reads public FPL data, the bucket and Cloud Scheduler only: it never logs in to FPL (so it
never touches the token or the run lock) and never writes to FPL. Every 30 minutes:
- each trigger (check, save, final) must exist, be ENABLED and be set for its next time
  (`next_runs`). A wrong time is fixed with the same `repoint` every run already does, and you're
  told. A paused or missing trigger is only reported: pausing may be deliberate.
- once the save run should have finished (its time plus SAVE_GRACE) and the deadline hasn't
  passed, its record must be in the bucket (`saved_record`, written by a successful save run).
  If not, an urgent email while you can still set the team yourself.
Each problem emails once (a create-if-absent record in the bucket). A new urgent problem also
exits non-zero, so D41's "a run failed" alert is a second email path in case Gmail is the problem.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from fpl_agent.cloud.schedule import MODES, Trigger, next_runs, repoint
from fpl_agent.notify import Mailer
from fpl_agent.reminders import ACTION_PREFIX

SAVE_GRACE = timedelta(minutes=10)  # a save run takes about 2-3 minutes
IN_PROGRESS = timedelta(minutes=20)  # a run due this recently may not have re-pointed yet
TOLERANCE = timedelta(minutes=1)


def saved_record(event: int) -> str:
    """The bucket object a successful save run leaves for its gameweek."""
    return f"runs/saved_gw{event:02d}.json"


@dataclass(frozen=True)
class Problem:
    key: str  # one email per key, ever
    message: str
    urgent: bool  # urgent: the run won't happen / didn't happen; not urgent: fixed already


@dataclass(frozen=True)
class Assessment:
    repoint: bool  # some trigger's time was wrong: re-point all three
    problems: tuple[Problem, ...]
    note: str


def when(at: datetime) -> str:
    return f"{at:%a %d %b %H:%M} UTC"


def assess(
    deadlines: Sequence[tuple[int, datetime]],
    now: datetime,
    triggers: Mapping[str, Trigger],
    saved: Callable[[int], bool],
) -> Assessment:
    """What's wrong right now, and whether to re-point. Pure: the runner does the I/O."""
    upcoming = sorted((gw, d) for gw, d in deadlines if d > now)
    if not upcoming:
        return Assessment(False, (), "no deadline left this season")
    gw, deadline = upcoming[0]
    due = [d - before for _, d in deadlines for before in MODES.values()]
    if any(now - IN_PROGRESS < t <= now for t in due):
        return Assessment(False, (), "a run is due or in progress; checking next time")

    problems: list[Problem] = []
    wrong: list[str] = []
    for mode, (run_gw, at) in sorted(next_runs(deadlines, now).items()):
        t, name = triggers[mode], f"fpl-agent-{mode}"
        if not t.exists:
            problems.append(
                Problem(
                    f"missing-{mode}-gw{run_gw:02d}",
                    f"The {name} trigger doesn't exist, so GW{run_gw}'s {mode} run "
                    f"({when(at)}) won't happen. Run ./deploy/deploy.sh to recreate it.",
                    urgent=True,
                )
            )
        elif not t.enabled:
            problems.append(
                Problem(
                    f"paused-{mode}-gw{run_gw:02d}",
                    f"The {name} trigger is paused, so GW{run_gw}'s {mode} run ({when(at)}) "
                    f"won't happen. If that's deliberate, pause fpl-agent-watchdog too. Otherwise: "
                    f"gcloud scheduler jobs resume {name} --location <region>",
                    urgent=True,
                )
            )
        elif t.next_time is None or abs(t.next_time - at) > TOLERANCE:
            wrong.append(
                f"{name} was set for {when(t.next_time) if t.next_time else 'no time'}, "
                f"should be {when(at)} (GW{run_gw} {mode})"
            )
    if wrong:
        problems.append(
            Problem(
                f"repointed-{now:%Y%m%d%H%M}",
                "The schedule was wrong and has been fixed (re-pointed):\n  " + "\n  ".join(wrong),
                urgent=False,
            )
        )

    save_done_by = deadline - MODES["save"] + SAVE_GRACE
    if save_done_by <= now < deadline and not saved(gw):
        minutes = int((deadline - now).total_seconds() // 60)
        problems.append(
            Problem(
                f"unsaved-gw{gw:02d}",
                f"URGENT: GW{gw}'s save run should have finished by {when(save_done_by)}, but "
                f"there's no record that it did. The deadline is {when(deadline)}, in {minutes} "
                "minutes: set your team on the FPL site now. Then open Claude Code in fpl-agent "
                "and paste this email.",
                urgent=True,
            )
        )
    return Assessment(bool(wrong), tuple(problems), f"checked GW{gw} (deadline {when(deadline)})")


class Bucket(Protocol):
    def read(self, name: str) -> str | None: ...
    def create(self, name: str, text: str) -> bool: ...


class Triggers(Protocol):
    def get(self, mode: str) -> Trigger: ...
    def set(self, mode: str, cron_line: str) -> None: ...


def run_watchdog(
    deadlines: Sequence[tuple[int, datetime]],
    now: datetime,
    scheduler: Triggers,
    bucket: Bucket,
    mailer: Mailer,
    log: Callable[[str], None] = print,
) -> int:
    """One watchdog pass. Returns 1 if a NEW urgent problem was found (exit code), else 0."""
    triggers = {mode: scheduler.get(mode) for mode in MODES}
    a = assess(deadlines, now, triggers, lambda gw: bucket.read(saved_record(gw)) is not None)
    if a.repoint:
        plan = repoint(scheduler, deadlines, now)
        log("re-pointed: " + ", ".join(f"{m} GW{gw} {when(at)}" for m, (gw, at) in plan.items()))
    record = json.dumps({"at": now.isoformat()})
    new = [p for p in a.problems if bucket.create(f"watchdog/{p.key}.json", record)]
    urgent = [p for p in new if p.urgent]
    if new:
        subject = (
            f"{ACTION_PREFIX}the watchdog found a problem"
            if urgent
            else "FPL agent watchdog: fixed the schedule"
        )
        mailer.send(subject, "\n\n".join(p.message for p in new) + "\n\n(D42)")
    for p in a.problems:
        log(f"{'NEW ' if p in new else ''}{'URGENT' if p.urgent else 'fixed'}: {p.message}")
    log(f"watchdog: {a.note}; {len(a.problems)} problem(s), {len(new)} new")
    return 1 if urgent else 0
