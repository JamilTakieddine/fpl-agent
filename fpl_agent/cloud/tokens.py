"""FPL tokens in Secret Manager: exactly one live copy of the refresh token (D36, Q1/Q2).

The refresh token rotates on every use, and sending a replaced one after a short grace period
revokes the whole login (reuse detection, D12). So:
- save = add a new secret version, THEN disable every older enabled version, so nothing (a
  restored backup, a local file, a second process) can ever read a stale token;
- a save that fails marks the secret `needs-relogin`: the next run stops and emails instead of
  refreshing with the old, now-poisoned token (Q2). This run still holds a valid access token
  for an hour, so it carries on with the gameweek;
- after the move to the cloud, local commands use this store too (FPL_TOKEN_STORE=secret-manager)
  and the local token file is retired (`python -m fpl_agent.cloud.upload_token`).

Talks to Secret Manager through the small SecretVersions protocol: GoogleSecretVersions in
production, a fake in tests.
"""

from __future__ import annotations

import contextlib
from typing import Any, Protocol

from fpl_agent.auth import RELOGIN_HINT, AuthError, TokenSet

NEEDS_RELOGIN = "needs-relogin"


class SecretVersions(Protocol):
    def latest(self) -> bytes: ...
    def add(self, data: bytes) -> str: ...  # returns the new version's name
    def enabled(self) -> list[str]: ...  # names of the enabled versions
    def disable(self, name: str) -> None: ...
    def labels(self) -> dict[str, str]: ...
    def set_label(self, key: str, value: str | None) -> None: ...  # None removes it


class SecretManagerTokenStore:
    def __init__(self, versions: SecretVersions) -> None:
        self.versions = versions

    def load(self) -> TokenSet:
        if self.versions.labels().get(NEEDS_RELOGIN) == "true":
            raise AuthError(f"The saved token may be stale (a save failed). {RELOGIN_HINT}")
        return TokenSet.model_validate_json(self.versions.latest())

    def save(self, tokens: TokenSet) -> None:
        try:
            new = self.versions.add(tokens.model_dump_json().encode())
        except Exception:
            self._mark_needs_relogin()
            raise
        for name in self.versions.enabled():
            if name != new:
                self.versions.disable(name)

    def clear_relogin(self) -> None:
        """After a fresh login has been uploaded."""
        self.versions.set_label(NEEDS_RELOGIN, None)

    def _mark_needs_relogin(self) -> None:
        # If Secret Manager itself is failing this can fail too; the email alert still goes out.
        with contextlib.suppress(Exception):
            self.versions.set_label(NEEDS_RELOGIN, "true")


class GoogleSecretVersions:
    """SecretVersions over google-cloud-secret-manager (imported lazily: a `cloud` extra)."""

    def __init__(self, project: str, secret_id: str, client: Any = None) -> None:
        from google.cloud import secretmanager

        self.sm = secretmanager
        # REST, not gRPC: gRPC's own DNS resolver fails on some Macs; HTTPS works everywhere.
        self.client = client or secretmanager.SecretManagerServiceClient(transport="rest")
        self.secret = f"projects/{project}/secrets/{secret_id}"

    def latest(self) -> bytes:
        resp = self.client.access_secret_version(name=f"{self.secret}/versions/latest")
        data: bytes = resp.payload.data
        return data

    def add(self, data: bytes) -> str:
        resp = self.client.add_secret_version(
            parent=self.secret, payload=self.sm.SecretPayload(data=data)
        )
        name: str = resp.name
        return name

    def enabled(self) -> list[str]:
        request = self.sm.ListSecretVersionsRequest(parent=self.secret, filter="state:ENABLED")
        versions = self.client.list_secret_versions(request=request)
        return [v.name for v in versions]

    def disable(self, name: str) -> None:
        self.client.disable_secret_version(name=name)

    def labels(self) -> dict[str, str]:
        return dict(self.client.get_secret(name=self.secret).labels)

    def set_label(self, key: str, value: str | None) -> None:
        secret = self.client.get_secret(name=self.secret)
        labels = dict(secret.labels)
        if value is None:
            labels.pop(key, None)
        else:
            labels[key] = value
        from google.protobuf import field_mask_pb2

        self.client.update_secret(
            secret=self.sm.Secret(name=self.secret, labels=labels),
            update_mask=field_mask_pb2.FieldMask(paths=["labels"]),
        )
