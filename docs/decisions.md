# Decision log

Why the project is built the way it is. One entry per decision that had real alternatives,
newest at the bottom. The format for each is context, what we chose, what we didn't choose and why,
and what it means for later work.

Standing design rules (dry run by default, LLMs only at the edges, rules as pure functions) live in
`CLAUDE.md`. This file records the specific calls made while building.

---

## D1. Phase 0 auth: capture the session from a browser login, not from scripted credentials

*Phase 0, 2026-09*

**Context.** FPL login goes through `account.premierleague.com` (a redirect/SSO flow, possibly with bot
checks). We need to know which auth the FPL API accepts before designing the cloud job.

**Decision.** Open a browser, let a human log in, and record whatever auth the site's own API calls use
(the `x-api-authorization` / `authorization` headers and cookies).

**Alternatives.**
- *Script the login form with email and password.* It breaks whenever the login page changes, may trip
  bot protection, and means storing the password. Not worth it before we know what the API wants.
- *Copy the token from DevTools by hand.* No automation needed, but it's a manual step every time the
  token expires, and it doesn't tell us which headers vs cookies matter.

**Consequences.** Phase 0 answers "what auth, and how long does it last?" The cloud job (Phase 4) still
needs a way to refresh the session with no browser. That is still an open question (see below).

## D2. Use an isolated Playwright Chromium, not the user's own Chrome profile

*Phase 0, 2026-09*

**Context.** Logging in through your normal Chrome would be convenient, since you might already be logged in.

**Decision.** Launch a clean Playwright Chromium with its own empty context.

**Alternatives.**
- *`launch_persistent_context` on the real Chrome profile.* Since Chrome 136, Chrome refuses to be
  controlled by automation tools when it's using the default profile folder (an anti-cookie-theft
  measure). Chrome must also be fully closed, because a profile can only be open in one Chrome
  process at a time. And it would give a throwaway script access to every site you're logged
  into (Gmail, banking, etc.).
- *Chrome extension / remote debugging into the running browser.* Same exposure, more moving parts.

**Consequences.** The captured session has only the FPL cookies and headers, which makes it clear what
auth is actually needed, and it matches the cloud job, which has no personal browser either. Having to log
in every run is fixed by D4.

## D3. Listen on the browser context and wait with sleep + polling (fix for `TargetClosedError`)

*Phase 0, 2026-09*

**Context.** The first version attached the request listener to one `page` and waited with
`page.wait_for_timeout`. The login flow opens or replaces tabs, which killed that page handle and
crashed the script with `TargetClosedError`.

**Decision.** `ctx.on("request", ...)` on the whole browser context, and a `time.sleep` loop that polls
`/api/me/` through `ctx.request`.

**Alternatives.**
- *Follow the popup with `page.expect_popup()`.* This only works if we know the exact flow ahead of time,
  and it breaks when the flow changes.
- *`ctx.wait_for_event(...)`.* We'd have to know which event marks "logged in". Polling `/api/me/` asks the
  API directly whether we're logged in.

**Consequences.** Sync Playwright only runs event handlers while some Playwright call is in progress.
The poll's `ctx.request.get` is that call, so captured headers can lag by up to one poll interval (3s).
That's fine for a login done by hand.

Closing the browser now gives a clear exit, and the closed-browser check has two signals:
- The browser's `disconnected` event.
- No open tabs for **two** polls in a row. One empty poll could just be the login flow swapping tabs.

Other Playwright errors (network failures, etc.) print their own message instead of being reported as
"browser closed".

## D4. Reuse the saved session with a browserless HTTP client before opening a browser

*Phase 0, 2026-09*

**Context.** A full browser login every run is slow, and it's not how the agent will run in the cloud.

**Decision.** If `.secrets/fpl_state.json` exists, load it into `pw.request.new_context(storage_state=...)`,
a plain HTTP client with no browser, and add the saved headers from `.secrets/fpl_auth.json`. If `/api/me/`
still shows a logged-in entry, skip the browser. Otherwise fall back to D1/D2. `--fresh` forces a new login.

**Alternatives.**
- *Launch a browser with the saved state.* This works too, but it opens a window for nothing and hides
  whether the session works with plain HTTP.
- *Use `requests` with the cookies copied over by hand.* This is closer to the final agent, but it means
  converting the cookie format ourselves. We can do that later once we know which cookies matter.

**Consequences.**
- This path is basically the cloud job's auth path, so each successful run confirms that plain HTTP with
  stored credentials works.
- The reuse path deliberately **doesn't re-save** the state files, so their modification time stays equal
  to the login time. "Saved Xh ago" on a failed reuse is then a direct measurement of how long a session lasts.

## D5. Refresh the access token over plain HTTP, and save the new tokens before using them

*Phase 0, 2026-09-24*

**Context.** Access tokens last 1 hour, but the saved state contains an OIDC refresh token. The cloud job
has no human and ideally no browser.

**Decision.** `spikes/phase0_refresh.py` POSTs `grant_type=refresh_token` plus `client_id` (a public SPA
client with no secret) to the token endpoint, which it finds via OIDC discovery (`{iss}/.well-known/openid-configuration`)
rather than hardcoding it. It uses `requests`, not Playwright. It writes the new tokens to disk **before**
testing them, with an atomic temp-file-plus-rename write and 0600 permissions.

**Alternatives.**
- *Keep a headless browser and let the site refresh its own token.* This works, but the cloud image would
  need Chromium (hundreds of MB), start more slowly and break more easily. One HTTP POST is enough.
- *Use the tokens first, then save them.* If the server rotates refresh tokens (it does, see Findings) and the
  script crashes between receiving and saving, the only valid token is lost and a human has to log in again.
- *Test whether the old refresh token still works after rotation.* We deliberately didn't. Many OIDC servers
  treat reuse of a rotated token as theft and revoke the whole session.

**Consequences.**
- The cloud job needs only `requests` and one stored secret, the latest refresh token. Every run must write
  the rotated token back to Secret Manager, and that write must succeed before the job does anything else.
- Only one process may refresh at a time. Two concurrent runs would each rotate the token, and one of
  them would end up holding a dead token.
- Refreshing rewrites `.secrets/fpl_state.json`, so its modification time no longer means "login time"
  (the D4 measurement). That no longer matters, because the refresh token's `exp` gives the lifetime directly.

---

## Findings

*Phase 0 first successful run, 2026-09-24*

- **Auth works with a header, not cookies.** The FPL API authenticates with an `x-api-authorization`
  bearer token. It's an OIDC JWT issued by `account.premierleague.com`, with scope `openid profile email`.
- **The access token lasts 1 hour** (`exp - iat`). A saved session therefore stops working for
  `/my-team/` about an hour after login, so the reuse path in D4 is only good for short gaps.
- **A refresh token exists.** The site's OIDC client stores `access_token`, `refresh_token`, `expires_at`
  and `id_token` in localStorage (`oidc.user:https://account.premierleague.com/as:<client_id>`), and
  `storage_state` already saves it.
- **Headless refresh works** (D5). The token endpoint returned 200 with new `access_token`, `refresh_token` and
  `id_token`, and `/api/me/` accepted the new access token over plain `requests` with no cookies.
- **The refresh token lasts 180 days, rotates on every use, and the window slides.** Each refresh issues a
  new refresh token whose `exp` is 180 days from *that* refresh. Since the agent refreshes at least weekly,
  the session never expires unless the server revokes it (for example after a password change) or a rotated
  token gets lost.
- Read-only access is confirmed: `/api/me/` and `/api/my-team/{entry}/` return picks with `selling_price`,
  the transfers info and chip status.
- **Write access is confirmed.** `POST /api/my-team/{entry}/` with the bearer header, `Origin` and `Referer`
  returned **202 Accepted** (not 200). Reading the team back matched the payload exactly, so the plain HTTP
  path can save lineups. The optimizer's output format is the `picks` list: element, position,
  is_captain and is_vice_captain, plus `chip`.
- `.secrets/` is chmod 700 and its files 600, because it holds a live refresh token.

## Open questions

- Does anything revoke refresh tokens early (logging out on the website, a password change, logging in on
  another device)? If so, the agent needs an alert to "log in again" instead of failing silently before a deadline.
- Secret Manager write-back: the rotated token must be saved before the job continues. How do we handle a
  failed write? (Phase 4.)
