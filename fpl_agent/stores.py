"""Where tokens and records live: local files, or Google Cloud after the move (D36).

FPL_TOKEN_STORE=secret-manager switches EVERY command (local ones too) to the single token in
Secret Manager, guarded by the run lock in the bucket. The refresh token rotates on every use, so
two copies (a local file and the cloud's) would end in a revoked login (D12). Cloud settings:
FPL_GCP_PROJECT and FPL_BUCKET (the deploy script sets them in the job; put them in .env locally).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from fpl_agent.auth import FileTokenStore, TokenStore
from fpl_agent.config import ConfigError, Settings

TOKEN_SECRET = "fpl-tokens"
LOCK_NAME = "fpl-token"


@dataclass(frozen=True)
class CloudConfig:
    project: str
    bucket: str


def cloud_config(env: Mapping[str, str] = os.environ) -> CloudConfig | None:
    """The cloud settings when the token lives in Secret Manager, else None (local files)."""
    if env.get("FPL_TOKEN_STORE") != "secret-manager":
        return None
    project, bucket = env.get("FPL_GCP_PROJECT"), env.get("FPL_BUCKET")
    if not project or not bucket:
        raise ConfigError("FPL_TOKEN_STORE=secret-manager needs FPL_GCP_PROJECT and FPL_BUCKET")
    return CloudConfig(project, bucket)


def token_store(settings: Settings, env: Mapping[str, str] = os.environ) -> TokenStore:
    cloud = cloud_config(env)
    if cloud is None:
        return FileTokenStore(settings.token_file, import_from=settings.phase0_state_file)
    from fpl_agent.cloud.tokens import GoogleSecretVersions, SecretManagerTokenStore

    return SecretManagerTokenStore(GoogleSecretVersions(cloud.project, TOKEN_SECRET))


@contextlib.contextmanager
def token_guard(env: Mapping[str, str] = os.environ) -> Iterator[None]:
    """Hold the run lock while a command may refresh the token (cloud mode only)."""
    cloud = cloud_config(env)
    if cloud is None:
        yield
        return
    from fpl_agent.cloud.storage import GcsBucket, run_lock

    with run_lock(GcsBucket(cloud.bucket), LOCK_NAME):
        yield
