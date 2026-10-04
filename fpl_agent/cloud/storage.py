"""Cloud Storage for the job's state: flag snapshots, saved odds, and the run lock (D36).

The local stores write files under data/; the cloud job has no lasting disk, so the same
records go to a bucket, with the same names and JSON (snapshots/flags_gw06.json,
odds/odds_gw06.json): the SnapshotStore and OddsStore protocols don't change.

The lock makes sure only ONE process refreshes the FPL token at a time: the cloud job and a
local command could otherwise both send the same refresh token, and the second would revoke the
login (D12). Taking it = creating locks/<name> only if it doesn't exist (a storage precondition,
atomic on the server); a lock older than its time-to-live is treated as abandoned.
"""

from __future__ import annotations

import contextlib
import json
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from fpl_agent.data.odds_store import GameweekOdds, SavedOdds
from fpl_agent.data.snapshots import FlagSnapshot

LOCK_TTL = timedelta(minutes=20)  # longer than any run (the job's own timeout is 15 minutes)


class Bucket(Protocol):
    def read(self, name: str) -> str | None: ...  # None if absent
    def write(self, name: str, text: str) -> None: ...
    def create(self, name: str, text: str) -> bool: ...  # only if absent; False if it exists
    def delete(self, name: str) -> None: ...


class BucketSnapshotStore:
    def __init__(self, bucket: Bucket) -> None:
        self.bucket = bucket

    @staticmethod
    def name(event: int) -> str:
        return f"snapshots/flags_gw{event:02d}.json"

    def load(self, event: int) -> FlagSnapshot | None:
        text = self.bucket.read(self.name(event))
        return None if text is None else FlagSnapshot.model_validate_json(text)

    def save(self, snapshot: FlagSnapshot) -> None:
        self.bucket.write(self.name(snapshot.event), snapshot.model_dump_json())


class BucketOddsStore:
    def __init__(self, bucket: Bucket) -> None:
        self.bucket = bucket

    @staticmethod
    def name(event: int) -> str:
        return f"odds/odds_gw{event:02d}.json"

    def load(self, event: int) -> dict[int, SavedOdds]:
        text = self.bucket.read(self.name(event))
        return {} if text is None else dict(GameweekOdds.model_validate_json(text).fixtures)

    def save(self, event: int, odds: dict[int, SavedOdds]) -> None:
        self.bucket.write(
            self.name(event), GameweekOdds(event=event, fixtures=odds).model_dump_json()
        )


class LockHeld(Exception):
    """Another process holds the lock (a run is in progress)."""


@contextmanager
def run_lock(
    bucket: Bucket, name: str, now: datetime | None = None, ttl: timedelta = LOCK_TTL
) -> Iterator[None]:
    """Hold locks/<name> for the duration of the block, or raise LockHeld."""
    now = now or datetime.now(UTC)
    path = f"locks/{name}"
    me = {"holder": f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}", "taken_at": now.isoformat()}
    if not bucket.create(path, json.dumps(me)):
        current = bucket.read(path)
        taken = datetime.fromisoformat(json.loads(current)["taken_at"]) if current else now
        if now - taken < ttl:
            raise LockHeld(f"{path} is held since {taken:%d %b %H:%M} UTC")
        bucket.delete(path)  # abandoned (a crashed run): take it over
        if not bucket.create(path, json.dumps(me)):
            raise LockHeld(f"{path} was taken by another process just now")
    try:
        yield
    finally:
        current = bucket.read(path)
        if current and json.loads(current).get("holder") == me["holder"]:
            bucket.delete(path)


class GcsBucket:
    """Bucket over google-cloud-storage (imported lazily: a `cloud` extra)."""

    def __init__(self, name: str, client: Any = None) -> None:
        from google.cloud import storage  # type: ignore[attr-defined]  # the library ships no types

        self.bucket = (client or storage.Client()).bucket(name)

    def read(self, name: str) -> str | None:
        blob = self.bucket.blob(name)
        return blob.download_as_text() if blob.exists() else None

    def write(self, name: str, text: str) -> None:
        self.bucket.blob(name).upload_from_string(text, content_type="application/json")

    def create(self, name: str, text: str) -> bool:
        from google.api_core.exceptions import PreconditionFailed

        try:
            # if_generation_match=0: succeed only if no object of that name exists (atomic).
            self.bucket.blob(name).upload_from_string(
                text, content_type="application/json", if_generation_match=0
            )
        except PreconditionFailed:
            return False
        return True

    def delete(self, name: str) -> None:
        from google.api_core.exceptions import NotFound

        with contextlib.suppress(NotFound):
            self.bucket.blob(name).delete()
