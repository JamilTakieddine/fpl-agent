"""The cloud job: python -m fpl_agent.run --mode check|save|final|watchdog   (D36, D42)

Every run holds the run lock (one token refresher at a time), records this run's flag and odds
snapshots, does its mode's work, and ALWAYS finishes by pointing the three schedules at their
next times, so one failed run can't break the chain:
- check (24h before): proves the login still works and records snapshots; email only if
  something needs you, while there's still time to fix it, or a gameweek reminder of a review
  due (fpl_agent/reminders.py, subject "⚠️ ACTION NEEDED");
- save (60 min before): makes the planned transfers (free ones only unless FPL_ALLOW_HITS=1,
  D37), then picks and saves the lineup for the new squad (live when FPL_LIVE=1); emails a
  plain-English summary of the decisions and chances (fpl_agent/report.py, D46; the technical
  output stays in the log). A test run (FPL_LIVE=0) says TEST in the subject;
- final (15 min before): runs again with the latest news and re-saves the lineup (the same one
  if nothing changed); emails only if the pick changed, or on a failure.
Any error emails you before the job exits non-zero. A save run that saved the lineup leaves a
record in the bucket for the watchdog (D42): every 30 minutes, on its own fixed trigger, it checks
the three triggers will fire and that the save run happened (fpl_agent/cloud/watchdog.py). It
never logs in to FPL, so it doesn't take the run lock.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fpl_agent.agent import CHIP_LABELS, RunResult, run_gameweek
from fpl_agent.auth import AuthError, RateLimitedError, TokenManager
from fpl_agent.cloud.schedule import MODES, CloudScheduler, repoint
from fpl_agent.cloud.storage import BucketOddsStore, BucketSnapshotStore, GcsBucket, LockHeld
from fpl_agent.cloud.watchdog import run_watchdog, saved_record
from fpl_agent.config import Settings, load_settings
from fpl_agent.data.calendar import build_calendar, next_deadline
from fpl_agent.data.client import FplClient, make_session
from fpl_agent.data.kalshi import KalshiClient
from fpl_agent.data.odds import load_odds
from fpl_agent.data.odds_store import record_odds
from fpl_agent.data.snapshots import record_snapshot
from fpl_agent.notify import Mailer, mailer_from_env
from fpl_agent.reminders import ACTION_PREFIX, reminder_for
from fpl_agent.report import TEST_PREFIX
from fpl_agent.report import body as report_body
from fpl_agent.report import subject as report_subject
from fpl_agent.stores import cloud_config, token_guard, token_store
from fpl_agent.submit import hits_allowed, live_mode

REGION_ENV = "FPL_REGION"
LOCK_ATTEMPTS = 6  # about 3 minutes: a local command finishing, then this run goes ahead
LOCK_WAIT_S = 30


def record(client: FplClient, snapshots: BucketSnapshotStore, odds: BucketOddsStore) -> str:
    """Save this run's flags (and ownership) and the market odds for the next gameweek."""
    boot = client.bootstrap()
    now = datetime.now(UTC)
    upcoming = next_deadline(boot, now)
    if upcoming is None:
        return "no upcoming deadline"
    event = upcoming[0]
    record_snapshot(snapshots, boot, event, now)
    gw_fixtures = list(build_calendar(boot, client.fixtures()).get(event).fixtures)
    priced, _ = load_odds(KalshiClient(client.http), gw_fixtures, boot)
    updated = record_odds(odds, event, priced, gw_fixtures, now)
    return f"GW{event}: snapshot saved, odds for {updated}/{len(gw_fixtures)} fixtures"


def email_subject(mode: str, result: RunResult, live: bool) -> str | None:
    """The summary email's subject, or None when this run shouldn't email: the save run always
    reports; the final run only when it re-saved a changed lineup; any failure reports."""
    t = result.transfers
    transfers_failed = bool(
        result.transfer_error or (t and t.problems) or (t and t.sent and not t.verified)
    )
    failed = bool(result.save_error or (result.saved and result.saved.problems)) or transfers_failed
    resaved = mode == "final" and result.changed and bool(result.saved and result.saved.sent)
    if not (mode == "save" or failed or resaved):
        return None
    if not (transfers_failed or failed) and result.report is not None:
        return report_subject(result.report)  # D46: decisions and chances, plain English
    test = "" if live else TEST_PREFIX
    if transfers_failed:
        state = "TRANSFERS FAILED"
    elif failed:
        state = "lineup SAVE FAILED"
    else:
        made = len(t.payload["transfers"]) if t and t.sent else 0
        moves = f"{made} transfer{'s' if made != 1 else ''} made, " if made else ""
        chip = f"{CHIP_LABELS[result.chip]} played, " if result.chip and live else ""
        state = f"{chip}{moves}lineup {'saved' if live else 'dry run'}"
    extra = " - updated with the latest news" if resaved else ""
    return f"{test}FPL GW{result.event}: {state} (P(win by 3+) {result.p_target:.0%}){extra}"


def email_body(result: RunResult, log: str, tz: ZoneInfo, failed: bool) -> str:
    """The plain-English report (D46); the full technical log only when something failed (or
    there's no report), because that's when the details are needed."""
    parts = [report_body(result.report, tz)] if result.report is not None else []
    if failed or result.report is None:
        parts.append(f"TECHNICAL DETAILS (for troubleshooting)\n{log}")
    return "\n".join(parts)


def gameweek_work(
    mode: str,
    client: FplClient,
    settings: Settings,
    mailer: Mailer,
    live: bool,
    snapshots: BucketSnapshotStore,
    odds: BucketOddsStore,
    bucket: GcsBucket,
) -> int:
    client.my_team(settings.entry_id)  # refreshes the token if needed, proving the login
    note = record(client, snapshots, odds)
    if mode == "check":
        print(f"check: login OK; {note}")
        upcoming = next_deadline(client.bootstrap(), datetime.now(UTC))
        reminder = reminder_for(upcoming[0]) if upcoming else None
        if reminder is not None:  # the few times the agent needs you (D40)
            mailer.send(reminder.subject, reminder.body)
            print(f"reminder sent: {reminder.subject}")
        return 0
    out = io.StringIO()
    result = run_gameweek(
        client,
        settings,
        live=live,
        out=out,
        snapshot_store=snapshots,
        odds_store=odds,
        transfers=mode == "save",
        make_transfers=mode == "save",  # transfers can't be undone: once, in the save run (D37)
        allow_hits=hits_allowed(os.environ),
    )
    print(out.getvalue())
    if result is None:
        return 0
    subject = email_subject(mode, result, live)
    if subject:
        text = email_body(result, out.getvalue(), ZoneInfo(settings.timezone), "FAILED" in subject)
        print(f"--- email ---\n{subject}\n\n{text}")
        mailer.send(subject, text)
    if mode == "save" and result.saved is not None and result.saved.sent:
        # The watchdog's proof that this gameweek's save run happened (D42).
        proof = {"event": result.event, "at": datetime.now(UTC).isoformat()}
        proof["verified"] = result.saved.verified
        bucket.write(saved_record(result.event), json.dumps(proof))
    return 1 if subject and "FAILED" in subject else 0


def run(mode: str, settings: Settings, mailer: Mailer, live: bool) -> int:
    cloud = cloud_config()
    if cloud is None:
        raise SystemExit("python -m fpl_agent.run is the cloud job: set FPL_TOKEN_STORE etc.")
    bucket = GcsBucket(cloud.bucket)
    snapshots, odds = BucketSnapshotStore(bucket), BucketOddsStore(bucket)
    http = make_session()
    client = FplClient(http, TokenManager(token_store(settings), http))
    try:
        for attempt in range(1, LOCK_ATTEMPTS + 1):
            try:
                with token_guard():
                    return gameweek_work(
                        mode, client, settings, mailer, live, snapshots, odds, bucket
                    )
            except LockHeld:
                if attempt == LOCK_ATTEMPTS:
                    raise
                print(f"the FPL login is in use (attempt {attempt}); waiting {LOCK_WAIT_S}s")
                time.sleep(LOCK_WAIT_S)
        return 1
    except AuthError as e:
        mailer.send(
            "FPL agent: log in again",
            f"The FPL login stopped working ({e}).\n\nOn your Mac:\n"
            "  python spikes/phase0_auth.py --fresh\n  python -m fpl_agent.cloud.upload_token\n",
        )
        return 1
    except RateLimitedError as e:
        mailer.send("FPL agent: login server busy", f"{e}\nThe next scheduled run will retry.")
        return 1
    finally:
        try:
            boot = client.bootstrap()
            region = os.environ.get(REGION_ENV, "us-east1")
            plan = repoint(
                CloudScheduler(cloud.project, region),
                [(e.id, e.deadline_time) for e in boot.events],
                datetime.now(UTC),
            )
            print(
                "next runs: "
                + ", ".join(f"{m} GW{gw} {at:%a %d %b %H:%M}" for m, (gw, at) in plan.items())
            )
        except Exception as e:  # the email below must still go out
            mailer.send("FPL agent: couldn't re-point the schedule", f"{type(e).__name__}: {e}")


def watchdog(mailer: Mailer) -> int:
    """One watchdog pass (D42): public FPL deadlines, the triggers, the bucket. No FPL login."""
    cloud = cloud_config()
    if cloud is None:
        raise SystemExit("the watchdog is part of the cloud job: set FPL_TOKEN_STORE etc.")
    bucket = GcsBucket(cloud.bucket)
    try:
        boot = FplClient(make_session()).bootstrap()
        return run_watchdog(
            [(e.id, e.deadline_time) for e in boot.events],
            datetime.now(UTC),
            CloudScheduler(cloud.project, os.environ.get(REGION_ENV, "us-east1")),
            bucket,
            mailer,
        )
    except Exception:
        # It runs every 30 minutes: a broken watchdog emails (and fails) once a day, not 48 times.
        print(traceback.format_exc())
        key = f"watchdog/crash-{datetime.now(UTC):%Y%m%d}.json"
        try:
            first_today = bucket.create(key, json.dumps({"at": datetime.now(UTC).isoformat()}))
        except Exception:
            first_today = True
        if not first_today:
            return 0
        mailer.send(f"{ACTION_PREFIX}the watchdog itself failed", traceback.format_exc())
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=[*MODES, "watchdog"], required=True)
    args = parser.parse_args()
    mailer = mailer_from_env()
    if args.mode == "watchdog":
        return watchdog(mailer)
    try:
        return run(args.mode, load_settings(), mailer, live_mode(sys.argv[1:], os.environ))
    except Exception:
        mailer.send(f"FPL agent: {args.mode} run failed", traceback.format_exc())
        raise


if __name__ == "__main__":
    sys.exit(main())
