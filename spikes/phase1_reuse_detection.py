"""
Spike: does FPL's login server punish reuse of an already-rotated refresh token?

WARNING: with --delay beyond the grace period (e.g. 180) this REVOKES your session by design;
you'll need `python spikes/phase0_auth.py --fresh`. Result (2026-09-25): yes, it does. See the
Findings in docs/decisions.md. Only re-run to re-check, e.g. at the start of a new season.

OAuth security guidance recommends that servers with refresh-token rotation treat reuse of an
OLD refresh token as theft and revoke the whole token family. FPL does (after a short grace
period), so any stale copy of a refresh token (a second process, a restored backup) logs the
agent out. See docs/decisions.md Q1.

Steps
  1. Force a refresh with the current tokens; save the result immediately (old -> stale).
  2. Wait --delay seconds (to get past any reuse grace period), then replay the stale refresh
     token; record status + OAuth error code.
  3. Refresh with the current refresh token: fails => whole session revoked.
  4. Call /api/me/ with whatever survived.

Worst case: the session is revoked and you log in again: python spikes/phase0_auth.py --fresh
Never prints token values.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import requests

from fpl_agent.auth import AuthError, FileTokenStore, TokenSet, refresh
from fpl_agent.data.client import make_session

SECRETS = Path(".secrets")
API = "https://fantasy.premierleague.com/api"


def oauth_error(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return f"{body.get('error')}: {body.get('error_description', '')}".strip()
    except ValueError:
        return resp.text[:200]


def replay(tokens: TokenSet, http: requests.Session) -> requests.Response:
    disco = http.get(f"{tokens.issuer}/.well-known/openid-configuration", timeout=20).json()
    return http.post(
        disco["token_endpoint"],
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
        },
        headers={"Origin": "https://fantasy.premierleague.com", "Accept": "application/json"},
        timeout=20,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=int, default=0, help="seconds to wait before the replay")
    args = ap.parse_args()

    http = make_session()
    store = FileTokenStore(SECRETS / "fpl_tokens.json", import_from=SECRETS / "fpl_state.json")
    old = store.load()

    # 1. Rotate once and persist straight away.
    new = refresh(old, http, time.time())
    store.save(new)
    print(f"1. forced refresh: OK, rotated={new.refresh_token != old.refresh_token}")
    if new.refresh_token == old.refresh_token:
        print("   Server does not rotate this time; nothing to test.")
        return 0

    # 2. Replay the stale refresh token, optionally after a delay.
    if args.delay:
        print(f"   waiting {args.delay}s before replaying...")
        time.sleep(args.delay)
    r = replay(old, http)
    print(f"2. replay OLD refresh token -> {r.status_code}  {oauth_error(r) if not r.ok else ''}")
    if r.ok:
        # Old token still accepted: no single-use enforcement. Keep the newest tokens.
        body = r.json()
        newest = TokenSet(
            issuer=old.issuer,
            client_id=old.client_id,
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token", old.refresh_token),
            expires_at=int(time.time()) + int(body.get("expires_in", 3600)),
        )
        store.save(newest)
        print("   Old token still worked: rotation is NOT enforced. Saved the newest tokens.")

    # 3. Does the NEW refresh token still work?
    current = store.load()
    try:
        after = refresh(current, http, time.time())
        store.save(after)
        print("3. refresh with current token -> OK: session NOT revoked")
    except AuthError as e:
        print(f"3. refresh with current token -> REJECTED: session revoked. {e}")
        return 2

    # 4. The API still accepts us.
    me = http.get(
        f"{API}/me/", headers={"x-api-authorization": f"Bearer {after.access_token}"}, timeout=20
    )
    entry = ((me.json() if me.ok else {}).get("player") or {}).get("entry")
    print(f"4. /api/me/ -> {me.status_code}, logged in: {bool(entry)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
