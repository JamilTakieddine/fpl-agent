"""
Phase 0 spike: can we get a fresh FPL access token with no browser?

The FPL site logs in via OIDC at account.premierleague.com. The access token lasts 1 hour, but the
site's OIDC client also keeps a refresh token in localStorage, which phase0_auth.py saves in
.secrets/fpl_state.json. This spike exchanges that refresh token for new tokens with one plain HTTP
POST (no Playwright), which is the path the cloud job would use.

What it does
  1. Reads the refresh token + client id from .secrets/fpl_state.json.
  2. Finds the token endpoint via OIDC discovery and POSTs grant_type=refresh_token.
  3. Saves the new tokens BEFORE using them (a rotated refresh token invalidates the old one).
  4. Checks the new access token against /api/me/, and reports lifetimes and
     whether the refresh token rotated.

Read-only against FPL. It never prints token values.

Usage
  python spikes/phase0_refresh.py      (run phase0_auth.py first to create .secrets/)

See docs/decisions.md (D5).
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

BASE = "https://fantasy.premierleague.com"
SECRETS = Path(".secrets")
STATE_FILE = SECRETS / "fpl_state.json"
AUTH_FILE = SECRETS / "fpl_auth.json"
OIDC_KEY_PREFIX = "oidc.user:"
TIMEOUT_S = 20

Json = dict[str, Any]


def jwt_claims(token: str) -> Json:
    """Decode a JWT payload without verifying it. Only used to read exp/iat, never to trust it."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    claims: Json = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    return claims


def find_oidc_entry(state: Json) -> tuple[Json, str]:
    """Return the localStorage item holding the OIDC user, and its key name."""
    for origin in state.get("origins", []):
        if origin.get("origin") != BASE:
            continue
        for item in origin.get("localStorage", []):
            if item["name"].startswith(OIDC_KEY_PREFIX):
                return item, item["name"]
    raise SystemExit(f"No {OIDC_KEY_PREFIX}* entry in {STATE_FILE}. Run phase0_auth.py first.")


def write_private(path: Path, text: str) -> None:
    """Atomic write with 0600 perms: temp file then rename, so a crash can't truncate secrets."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)


def describe_lifetime(token: str, label: str) -> None:
    c = jwt_claims(token)
    if "exp" in c and "iat" in c:
        print(f"    {label}: JWT, lifetime {(c['exp'] - c['iat']) / 3600:.1f}h")
    elif "exp" in c:
        print(f"    {label}: JWT, expires in {(c['exp'] - time.time()) / 3600:.1f}h")
    else:
        print(f"    {label}: opaque (not a JWT), lifetime unknown from the token itself")


def main() -> int:
    if (SECRETS / "fpl_tokens.json").exists():
        # fpl_agent.auth now owns the tokens and rotates them there, so the refresh token in
        # fpl_state.json is stale. Replaying a rotated token can get the whole session revoked.
        print("Superseded: fpl_agent/auth.py manages tokens now (.secrets/fpl_tokens.json).")
        print("Use: python -m fpl_agent.data")
        return 1
    if not STATE_FILE.exists():
        print(f"No {STATE_FILE}. Run spikes/phase0_auth.py first.")
        return 1
    state = json.loads(STATE_FILE.read_text())
    item, key = find_oidc_entry(state)
    user = json.loads(item["value"])
    old_refresh = user.get("refresh_token")
    if not old_refresh:
        print("OIDC entry has no refresh_token. Can't refresh headlessly.")
        return 1

    # The key is "oidc.user:<issuer>:<client_id>"; the access token carries both too.
    claims = jwt_claims(user["access_token"])
    issuer, client_id = claims["iss"], claims["client_id"]

    disco = requests.get(f"{issuer}/.well-known/openid-configuration", timeout=TIMEOUT_S).json()
    token_endpoint = disco["token_endpoint"]
    print(f"Token endpoint: {token_endpoint}")

    # Public SPA client: no client secret, just client_id. Origin mirrors what the site sends.
    resp = requests.post(
        token_endpoint,
        data={"grant_type": "refresh_token", "refresh_token": old_refresh, "client_id": client_id},
        headers={"Origin": BASE, "Accept": "application/json"},
        timeout=TIMEOUT_S,
    )
    if not resp.ok:
        print(f"\nFAIL  refresh -> {resp.status_code}")
        # Error bodies are OAuth error codes (e.g. invalid_grant), not secrets.
        print(f"      body: {resp.text[:300]}")
        return 1
    tokens = resp.json()
    new_access = tokens["access_token"]
    new_refresh = tokens.get("refresh_token", old_refresh)
    rotated = new_refresh != old_refresh

    # Persist FIRST. If the server rotated the refresh token, the old one may now be dead.
    user.update(
        access_token=new_access,
        refresh_token=new_refresh,
        expires_at=int(time.time()) + int(tokens.get("expires_in", 3600)),
    )
    if "id_token" in tokens:
        user["id_token"] = tokens["id_token"]
    item["value"] = json.dumps(user)
    write_private(STATE_FILE, json.dumps(state))
    write_private(AUTH_FILE, json.dumps({"x-api-authorization": f"Bearer {new_access}"}, indent=2))

    print(f"\nPASS  refresh -> {resp.status_code}, new tokens saved to {SECRETS}/")
    print(f"    response fields: {sorted(tokens)}")
    describe_lifetime(new_access, "access token ")
    describe_lifetime(new_refresh, "refresh token")
    print(f"    refresh token rotated: {rotated}")
    extra = {
        k: tokens[k] for k in ("refresh_expires_in", "refresh_token_expires_in") if k in tokens
    }
    if extra:
        print(f"    server-reported refresh lifetime: {extra}")

    me = requests.get(
        f"{BASE}/api/me/",
        headers={"x-api-authorization": f"Bearer {new_access}"},
        timeout=TIMEOUT_S,
    )
    entry = ((me.json() if me.ok else {}).get("player") or {}).get("entry")
    if entry:
        print(f"\nPASS  /api/me/ with the new token (plain requests, no cookies) -> entry={entry}")
        return 0
    print(f"\nFAIL  /api/me/ with the new token -> {me.status_code}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as e:
        print(f"\nNetwork error: {type(e).__name__}: {e}")
        sys.exit(1)
