# Phase 4 cloud pieces over in-memory fakes (no Google Cloud): the Secret Manager token store
# (one live version, needs-relogin on a failed save), bucket stores, the run lock (held, stale,
# released only by its holder), mailer selection and the cloud settings switch.

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from fpl_agent.auth import AuthError, TokenSet
from fpl_agent.cloud.storage import (
    BucketOddsStore,
    BucketSnapshotStore,
    LockHeld,
    run_lock,
)
from fpl_agent.cloud.tokens import NEEDS_RELOGIN, SecretManagerTokenStore
from fpl_agent.config import ConfigError
from fpl_agent.data.snapshots import FlagSnapshot, PlayerFlag
from fpl_agent.notify import GmailMailer, PrintMailer, mailer_from_env
from fpl_agent.stores import cloud_config

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def tokens(refresh: str) -> TokenSet:
    return TokenSet(
        issuer="https://issuer",
        client_id="c",
        access_token="a",
        refresh_token=refresh,
        expires_at=0,
    )


class FakeVersions:
    def __init__(self, fail_add: bool = False) -> None:
        self.versions: list[tuple[str, bytes, bool]] = []  # name, data, enabled
        self.label: dict[str, str] = {}
        self.fail_add = fail_add

    def latest(self) -> bytes:
        return [d for _, d, on in self.versions if on][-1]

    def add(self, data: bytes) -> str:
        if self.fail_add:
            raise RuntimeError("Secret Manager unavailable")
        name = f"v{len(self.versions) + 1}"
        self.versions.append((name, data, True))
        return name

    def enabled(self) -> list[str]:
        return [n for n, _, on in self.versions if on]

    def disable(self, name: str) -> None:
        self.versions = [(n, d, on and n != name) for n, d, on in self.versions]

    def labels(self) -> dict[str, str]:
        return dict(self.label)

    def set_label(self, key: str, value: str | None) -> None:
        if value is None:
            self.label.pop(key, None)
        else:
            self.label[key] = value


def test_saving_keeps_exactly_one_enabled_version() -> None:
    v = FakeVersions()
    store = SecretManagerTokenStore(v)
    store.save(tokens("r1"))
    store.save(tokens("r2"))
    store.save(tokens("r3"))
    assert v.enabled() == ["v3"]  # older refresh tokens can never be read again
    assert store.load().refresh_token == "r3"


def test_a_failed_save_flags_needs_relogin_and_the_next_load_stops() -> None:
    v = FakeVersions()
    SecretManagerTokenStore(v).save(tokens("r1"))
    v.fail_add = True
    store = SecretManagerTokenStore(v)
    with pytest.raises(RuntimeError):
        store.save(tokens("r2"))
    assert v.labels()[NEEDS_RELOGIN] == "true"
    with pytest.raises(AuthError):  # never refresh with the now-stale r1
        store.load()
    store.clear_relogin()
    assert store.load().refresh_token == "r1"


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def read(self, name: str) -> str | None:
        return self.objects.get(name)

    def write(self, name: str, text: str) -> None:
        self.objects[name] = text

    def create(self, name: str, text: str) -> bool:
        if name in self.objects:
            return False
        self.objects[name] = text
        return True

    def delete(self, name: str) -> None:
        self.objects.pop(name, None)


def test_bucket_stores_round_trip_under_the_local_file_names() -> None:
    b = FakeBucket()
    snap = FlagSnapshot(
        event=7,
        taken_at=T0,
        deadline=T0 + timedelta(days=1),
        flags={
            1: PlayerFlag(status="a", chance_of_playing_next_round=None, news="", news_added=None)
        },
        ownership={1: 12.5},
    )
    snapshots = BucketSnapshotStore(b)
    snapshots.save(snap)
    assert "snapshots/flags_gw07.json" in b.objects
    assert snapshots.load(7) == snap and snapshots.load(8) is None
    odds = BucketOddsStore(b)
    assert odds.load(7) == {}
    odds.save(7, {})
    assert "odds/odds_gw07.json" in b.objects


def test_the_run_lock_is_exclusive_and_released() -> None:
    b = FakeBucket()
    with run_lock(b, "fpl-token", now=T0):
        assert "locks/fpl-token" in b.objects
        with pytest.raises(LockHeld), run_lock(b, "fpl-token", now=T0 + timedelta(minutes=5)):
            pass
    assert "locks/fpl-token" not in b.objects  # released


def test_an_abandoned_lock_is_taken_over() -> None:
    b = FakeBucket()
    b.objects["locks/fpl-token"] = json.dumps({"holder": "crashed", "taken_at": T0.isoformat()})
    with run_lock(b, "fpl-token", now=T0 + timedelta(hours=1)):
        assert "crashed" not in b.objects["locks/fpl-token"]


def test_mailer_and_cloud_settings_come_from_the_environment() -> None:
    assert isinstance(mailer_from_env({}), PrintMailer)
    mailer = mailer_from_env({"FPL_EMAIL": "me@example.com", "FPL_EMAIL_APP_PASSWORD": "abcd efgh"})
    assert isinstance(mailer, GmailMailer) and mailer.app_password == "abcdefgh"
    assert cloud_config({}) is None
    env: dict[str, Any] = {"FPL_TOKEN_STORE": "secret-manager"}
    with pytest.raises(ConfigError):
        cloud_config(env)
    cfg = cloud_config(env | {"FPL_GCP_PROJECT": "p", "FPL_BUCKET": "b"})
    assert cfg is not None and (cfg.project, cfg.bucket) == ("p", "b")
