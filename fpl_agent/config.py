"""Settings from the environment: .env locally, real env vars (from Secret Manager) in the cloud."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    entry_id: int
    h2h_league_id: int | None
    secrets_dir: Path
    data_dir: Path
    timezone: str = "UTC"  # for times in emails (IANA name, e.g. America/New_York; D46)

    @property
    def token_file(self) -> Path:
        return self.secrets_dir / "fpl_tokens.json"

    @property
    def snapshot_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def odds_dir(self) -> Path:
        return self.data_dir / "odds"

    @property
    def phase0_state_file(self) -> Path:
        return self.secrets_dir / "fpl_state.json"


def _optional_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit():
        raise ConfigError(f"{name} must be a number, got {raw!r}")
    return int(raw)


def load_settings() -> Settings:
    load_dotenv()  # no-op if there's no .env; real env vars always win
    entry_id = _optional_int("FPL_ENTRY_ID")
    if entry_id is None:
        raise ConfigError("FPL_ENTRY_ID is not set. Copy .env.example to .env and fill it in.")
    timezone = os.environ.get("FPL_TIMEZONE", "").strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f"FPL_TIMEZONE must be an IANA name like America/New_York: {e}") from e
    return Settings(
        entry_id=entry_id,
        h2h_league_id=_optional_int("FPL_H2H_LEAGUE_ID"),
        secrets_dir=Path(os.environ.get("FPL_SECRETS_DIR", ".secrets")),
        data_dir=Path(os.environ.get("FPL_DATA_DIR", "data")),
        timezone=timezone,
    )
