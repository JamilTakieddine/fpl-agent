"""When the job runs: three runs per gameweek, re-pointed by the job itself (D36).

- check: 24 hours before the deadline (token refresh proves the login still works, snapshots
  are recorded; an email only if something needs you, while there's time to fix it);
- save: 60 minutes before (the lineup is saved; the summary email);
- final: 15 minutes before (latest news; saves again only if the pick changed).
If the final run fails, the save run's lineup stands: never miss a deadline (goal 1).
Times come from FPL's deadline_time (CLAUDE.md: never derived from kickoffs). After every run,
each Cloud Scheduler job is pointed at its next time, so the chain survives a failed run.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

MODES: dict[str, timedelta] = {
    "check": timedelta(hours=24),
    "save": timedelta(minutes=60),
    "final": timedelta(minutes=15),
}


def next_runs(
    deadlines: Sequence[tuple[int, datetime]], now: datetime
) -> dict[str, tuple[int, datetime]]:
    """For each mode, the earliest (gameweek, run time) still in the future."""
    out = {}
    for mode, before in MODES.items():
        upcoming = [(gw, d - before) for gw, d in sorted(deadlines) if d - before > now]
        if upcoming:
            out[mode] = upcoming[0]
    return out


def cron(at: datetime) -> str:
    """A cron line for one moment (UTC). It would repeat yearly, but each run re-points it."""
    return f"{at.minute} {at.hour} {at.day} {at.month} *"


class Scheduler(Protocol):
    def set(self, mode: str, cron_line: str) -> None: ...


class CloudScheduler:
    """Scheduler over google-cloud-scheduler: one job per mode, named fpl-agent-<mode>."""

    def __init__(self, project: str, region: str, client: Any = None) -> None:
        from google.cloud import scheduler_v1

        self.types = scheduler_v1
        self.client = client or scheduler_v1.CloudSchedulerClient(transport="rest")  # see tokens.py
        self.parent = f"projects/{project}/locations/{region}"

    def set(self, mode: str, cron_line: str) -> None:
        from google.protobuf import field_mask_pb2

        job = self.types.Job(
            name=f"{self.parent}/jobs/fpl-agent-{mode}", schedule=cron_line, time_zone="Etc/UTC"
        )
        self.client.update_job(
            job=job, update_mask=field_mask_pb2.FieldMask(paths=["schedule", "time_zone"])
        )


def repoint(
    scheduler: Scheduler, deadlines: Sequence[tuple[int, datetime]], now: datetime
) -> dict[str, tuple[int, datetime]]:
    """Point every mode's job at its next run; returns what was set."""
    plan = next_runs(deadlines, now)
    for mode, (_, at) in plan.items():
        scheduler.set(mode, cron(at))
    return plan
