"""
Phase 0 spike: can we log in to FPL and save a lineup from code?

What it does
  1. If a saved session exists in .secrets/, tries it first with a browserless HTTP
     client (the same shape the cloud job will use). If /api/me/ still says we're
     logged in, no browser opens at all.
  2. Otherwise opens a real Chromium window on the FPL site. You log in by hand.
     The script watches the site's own API calls and captures whatever auth it uses
     (bearer token header and/or cookies), then saves it for next time.
  3. Reads /api/me/ and /api/my-team/{entry}/ with that auth and prints a summary.
  4. With --write: POSTs your CURRENT lineup back unchanged. This is the real test.

Default is read-only. Nothing is saved to FPL unless you pass --write.

Usage
  python spikes/phase0_auth.py            # read-only checks (login only if needed)
  python spikes/phase0_auth.py --write    # also save the unchanged lineup
  python spikes/phase0_auth.py --fresh    # ignore the saved session, log in again

Outputs (gitignored)
  .secrets/fpl_state.json  browser storage state (cookies + localStorage)
  .secrets/fpl_auth.json   captured auth headers
  The file mtime is the login time, so "saved Xh ago" tells us how long a session lasts.

See docs/decisions.md for why it works this way.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import APIRequestContext, Browser, Playwright, sync_playwright
from playwright.sync_api import Error as PlaywrightError

BASE = "https://fantasy.premierleague.com"
API = f"{BASE}/api"
SECRETS = Path(".secrets")
STATE_FILE = SECRETS / "fpl_state.json"
AUTH_FILE = SECRETS / "fpl_auth.json"
AUTH_HEADER_KEYS = ("authorization", "x-api-authorization")
LOGIN_TIMEOUT_S = 300
POLL_INTERVAL_S = 3


class BrowserClosed(Exception):
    """The user closed the browser (or every tab) before we finished."""


def first_line(e: Exception) -> str:
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def api_get(req: APIRequestContext, path: str, auth: dict) -> tuple[int, dict | None]:
    resp = req.get(f"{API}{path}", headers=auth)
    try:
        return resp.status, resp.json()
    except Exception:
        return resp.status, None


def logged_in_entry(req: APIRequestContext, auth: dict) -> int | None:
    """Entry id if /api/me/ shows a logged-in player, else None."""
    status, me = api_get(req, "/me/", auth)
    player = (me or {}).get("player") if status == 200 else None
    if player and player.get("entry"):
        return int(player["entry"])
    return None


def try_saved_session(pw: Playwright) -> tuple[APIRequestContext, dict, int] | None:
    """Reuse .secrets/ from a previous login without opening a browser.

    Uses pw.request (a plain HTTP client that accepts the saved cookies) rather than a
    browser, because that's what the cloud job will have: stored credentials, no UI.
    Deliberately does NOT re-save the state, so the file mtime stays = login time.
    """
    if not STATE_FILE.exists():
        return None
    auth = json.loads(AUTH_FILE.read_text()) if AUTH_FILE.exists() else {}
    age_h = (time.time() - STATE_FILE.stat().st_mtime) / 3600

    req = pw.request.new_context(storage_state=str(STATE_FILE))
    entry_id = logged_in_entry(req, auth)
    if entry_id:
        print(f"OK  saved session still valid ({age_h:.1f}h since login). No browser needed.")
        return req, auth, entry_id

    print(f"Saved session from {age_h:.1f}h ago is no longer valid. Falling back to browser login.")
    req.dispose()
    return None


def capture_auth(ctx, captured: dict) -> None:
    """Record auth headers from any request the FPL site makes to its own API.

    Listens on the whole context, not one page, so popups, new tabs and the
    redirect through account.premierleague.com are all covered.
    """

    def on_request(req):
        if not req.url.startswith(API):
            return
        try:
            headers = req.all_headers()
        except Exception:
            headers = req.headers
        for key in AUTH_HEADER_KEYS:
            if headers.get(key):
                captured[key] = headers[key]

    ctx.on("request", on_request)


def wait_for_login(ctx, captured: dict, closed: dict) -> int | None:
    """Poll /api/me/ until it shows a logged-in entry. Returns the entry id or None on timeout.

    Uses time.sleep instead of page.wait_for_timeout so no single page handle has to
    stay alive. In sync Playwright, event handlers (our request listener) only run while
    a Playwright call is in progress, so the ctx.request.get in each poll is what gives
    them a chance to fire.
    """
    deadline = time.time() + LOGIN_TIMEOUT_S
    empty_polls = 0
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_S)
        # Zero tabs for two polls in a row = user closed the window. One poll could just
        # be the login flow swapping tabs.
        empty_polls = 0 if ctx.pages else empty_polls + 1
        if closed["flag"] or empty_polls >= 2:
            raise BrowserClosed
        entry_id = logged_in_entry(ctx.request, dict(captured))
        if entry_id:
            return entry_id
    return None


def browser_login(pw: Playwright) -> tuple[Browser, APIRequestContext, dict, int] | None:
    """Open a clean Chromium window, let the user log in, save the session. None on failure."""
    captured: dict[str, str] = {}
    browser = pw.chromium.launch(headless=False)
    closed = {"flag": False}
    browser.on("disconnected", lambda _: closed.update(flag=True))
    ctx = browser.new_context()
    capture_auth(ctx, captured)

    print("A browser window is open. Log in to FPL there (accept cookies, etc).")
    print(f"Waiting up to {LOGIN_TIMEOUT_S // 60} minutes...")

    try:
        ctx.new_page().goto(f"{BASE}/my-team")
        entry_id = wait_for_login(ctx, captured, closed)
    except BrowserClosed:
        print("\nBrowser was closed before login finished. Nothing was saved. Exiting.")
        return None
    except PlaywrightError as e:
        if closed["flag"]:
            print("\nBrowser was closed before login finished. Nothing was saved. Exiting.")
        else:
            print(f"\nPlaywright error while waiting for login: {first_line(e)}")
        with contextlib.suppress(PlaywrightError):
            browser.close()
        return None

    if not entry_id:
        print("\nFAIL: never saw a logged-in /api/me/ response.")
        print(f"Auth headers captured: {list(captured) or 'none'}")
        browser.close()
        return None

    auth = dict(captured)
    print(f"\nOK  logged in. entry={entry_id}")
    print(f"    auth mechanism: {', '.join(auth) if auth else 'cookies only'}")
    ctx.storage_state(path=str(STATE_FILE))
    AUTH_FILE.write_text(json.dumps(auth, indent=2))
    print(f"    saved session to {SECRETS}/ (gitignored); next run will reuse it")
    return browser, ctx.request, auth, entry_id


def summarise_team(team: dict, names: dict[int, str]) -> None:
    picks = team.get("picks", [])
    print("\n  Lineup (position: player)")
    for p in sorted(picks, key=lambda x: x["position"]):
        tag = " (C)" if p.get("is_captain") else " (VC)" if p.get("is_vice_captain") else ""
        bench = "  bench" if p["position"] > 11 else ""
        sell = p.get("selling_price")
        sell_s = f"  sell {sell / 10:.1f}" if sell is not None else ""
        print(f"   {p['position']:>2}: {names.get(p['element'], p['element'])}{tag}{sell_s}{bench}")

    t = team.get("transfers", {})
    if t:
        print(f"\n  Transfers: limit={t.get('limit')} made={t.get('made')} "
              f"bank={t.get('bank', 0) / 10:.1f} value={t.get('value', 0) / 10:.1f}")
    chips = team.get("chips", [])
    if chips:
        print("  Chips:")
        for c in chips:
            print(f"   {c.get('name'):<9} status={c.get('status_for_entry')} "
                  f"window=GW{c.get('start_event')}-{c.get('stop_event')}")


def run_checks(req: APIRequestContext, auth: dict, entry_id: int, write: bool) -> int:
    """Read /my-team/, and with write=True POST the unchanged lineup back."""
    # Player names for readable output (public endpoint).
    _, boot = api_get(req, "/bootstrap-static/", {})
    names = {e["id"]: e["web_name"] for e in (boot or {}).get("elements", [])}

    status, team = api_get(req, f"/my-team/{entry_id}/", auth)
    if status != 200 or not team:
        print(f"\nFAIL: GET /my-team/{entry_id}/ returned {status}")
        return 1
    print(f"\nOK  GET /my-team/{entry_id}/")
    summarise_team(team, names)

    if not write:
        print("\nRead-only run finished. Re-run with --write to test saving.")
        return 0

    payload = {
        "chip": None,
        "picks": [
            {
                "element": p["element"],
                "position": p["position"],
                "is_captain": p["is_captain"],
                "is_vice_captain": p["is_vice_captain"],
            }
            for p in sorted(team["picks"], key=lambda x: x["position"])
        ],
    }
    headers = {
        **auth,
        "Content-Type": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/my-team",
    }
    print(f"\nPOST payload: {json.dumps(payload)}")
    resp = req.post(f"{API}/my-team/{entry_id}/", data=json.dumps(payload), headers=headers)
    if resp.ok:
        print(f"\nPASS  POST /my-team/{entry_id}/ -> {resp.status}")
        print("      Write access works. Full autonomy is on the table.")
        return 0
    print(f"\nFAIL  POST /my-team/{entry_id}/ -> {resp.status}")
    print(f"      body: {resp.text()[:500]}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="POST the current lineup back unchanged (the real test)")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the saved session and log in again in a browser")
    args = ap.parse_args()

    SECRETS.mkdir(exist_ok=True)

    with sync_playwright() as pw:
        browser = None
        session = None if args.fresh else try_saved_session(pw)
        if session:
            req, auth, entry_id = session
        else:
            login = browser_login(pw)
            if login is None:
                return 1
            browser, req, auth, entry_id = login

        try:
            return run_checks(req, auth, entry_id, args.write)
        finally:
            if browser:
                with contextlib.suppress(PlaywrightError):
                    browser.close()
            else:
                req.dispose()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted. Exiting.")
        sys.exit(130)
    except PlaywrightError as e:
        # Browser closed after login (mid read/write). Short message, no traceback.
        print(f"\nBrowser closed or crashed mid-run: {first_line(e)}")
        sys.exit(1)
