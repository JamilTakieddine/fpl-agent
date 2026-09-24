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

## D6. Production-shaped tooling: pyproject.toml, ruff, mypy --strict, pre-commit

*Phase 0, 2026-09-24*

**Context.** The parent `CLAUDE.md` sets a production-shaped Python baseline for every project in the series
(`pyproject.toml`, ruff, mypy, pytest, pre-commit). This project's `CLAUDE.md` said `requirements.txt`,
which was a leftover rather than a deliberate exception. The agent runs unattended, writes to a real
account, and lives in a public repo, so it is the kind of project the baseline exists for.

**Decision.**
- **`pyproject.toml` is the single source of truth.** Runtime deps are only what the code imports
  (`playwright`, `requests`). Tooling goes in a `[dev]` extra so it stays out of the cloud image. `python-dotenv`
  was dropped until code actually reads `.env` (Phase 1). `packages = []` for now, because there's no package
  yet and setuptools auto-discovery can fail on folders like `spikes/`.
- **mypy `strict = true` from the start.** Adding strictness later means fixing a backlog of errors. Turning
  it on for the Phase 0 spikes surfaced 14 errors, all of them bare `dict`s and missing annotations; they were
  fixed with `Json` and `Auth` aliases.
- **ruff runs as the official pinned pre-commit hook**, so anyone who clones the repo gets the same version.
- **mypy runs as a local hook from `.venv/`.** The official mirrors-mypy hook runs in an isolated environment
  without playwright and types-requests installed, so it would quietly treat their objects as `Any` and check
  much less. The cost is that the hook needs `.venv/` to exist, which the README setup creates.
- **A `no-jwt` pygrep hook** refuses any commit containing a JWT-shaped string (`eyJ...eyJ...`). The repo is
  public and `.secrets/` holds a live 180-day refresh token. The hook was tested against a fake token and
  blocks it.

**Alternatives.**
- *Keep `requirements.txt`.* It works, but it has no place for tool config, no separation of dev and runtime
  deps, and it breaks the series convention.
- *uv / Poetry.* Faster installs and lockfiles, but they're another tool to learn and the baseline says venv.
  Revisit if dependency drift becomes a problem.
- *gitleaks for secret scanning.* Broader coverage, but it needs a Go binary. The pygrep hook covers the one
  secret format this project actually handles.

**Consequences.** Every commit is formatted, lint-clean and passes mypy strict. Tests are not run in the hook
(too slow); run pytest manually / in CI. When `fpl_agent/` is created, switch `[tool.setuptools]` to package
discovery and add `fpl_agent tests` to the mypy hook's entry.

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

## Open questions (with the current recommendation)

**Q1. What revokes the refresh token early (logging out on the site, a password change, other devices)?**
Recommendation: design for revocation whatever the cause, because the job will see the same `invalid_grant`
error either way. Then do one cheap test to learn which actions trigger it.
- *Refresh-token canary run*, about 24h before each deadline: refresh, save, and on `invalid_grant` email
  "log in again: `python spikes/phase0_auth.py --fresh`". Finding out 15 minutes before the deadline is too late to fix.
- *A re-login path that doesn't need the cloud*: the local login saves the new refresh token to Secret Manager.
- *One test*: log out on fantasy.premierleague.com, then run `phase0_refresh.py`. If it fails, logging out
  revokes the token, so the email should say "don't log out on the website". Cost: one browser re-login.

**Q2. What if saving the rotated refresh token to Secret Manager fails?**
Recommendation: save it before doing anything else and retry with backoff, but **don't let a failed save
block the gameweek submission.** The new access token in memory is valid for 1 hour, which is enough to submit.
Only *next* week's run depends on the saved token. So: retry the save during the whole run, submit the lineup
regardless, and if the save still hasn't succeeded, send an alert email saying re-login is needed before next
deadline. Never print or log the token as a "backup". Also, since only one refresh may happen at a time, the canary
and the deadline run must never overlap. Use a single Cloud Run job with no parallelism, and schedules far apart.
